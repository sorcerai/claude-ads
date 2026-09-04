#!/usr/bin/env python3
"""Exchange the Meta Ad Library access token without exposing credentials.

A token minted in the Graph API Explorer lives about two hours. Meta can swap a
live short-lived user token for a long-lived one (about 60 days) via
``grant_type=fb_exchange_token``. The swap needs the current token to remain
valid. Once it expires, a human must re-authenticate in a browser.

The official Meta v26 token-inspection operation requires a GET request with a
required token query parameter. That conflicts with this CLI's no-secret-in-URL
policy, so automated status and token inspection are removed.
The ``extend`` command reads credentials from separate macOS keychain entries,
sends them only in the POST body, stores the returned token, and reports only
non-secret exchange metadata.

    python scripts/meta_token.py extend

``extend`` replaces the keychain entry only after a successful exchange.
"""

# Meta's ``debug_token`` operation requires GET with ``input_token`` in the
# query. Keep that inspection path out of executable code under the policy.

from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys

from url_utils import sanitize_error

try:
    import requests
except ImportError:
    print("Error: requests library required. Install with: pip install -r requirements.txt")
    sys.exit(1)

API_VERSION = "v26.0"
GRAPH = f"https://graph.facebook.com/{API_VERSION}"
TOKEN_SERVICE = "META_AD_LIBRARY_TOKEN"
APP_ID_SERVICE = "META_APP_ID"
SECRET_SERVICE = "META_APP_SECRET"
TIMEOUT = 30


def keychain_read(service: str) -> str:
    """Read a generic password, or exit with a message that names the service."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        sys.exit(
            f"Error: no keychain entry for {service}. Store it with:\n"
            f"  security add-generic-password -U -s {service} -a $USER -w\n"
            "  Enter the value at the Keychain password prompt."
        )
    raw = result.stdout
    if raw.endswith("\n"):
        raw = raw[:-1]
    return _validate_credential(service, raw)


def keychain_account(service: str) -> str:
    """The account label on the existing entry, or the current user.

    `find-generic-password -s SERVICE -w` resolves by service and returns ONE
    entry. `add-generic-password -U` updates only an entry with a matching
    service AND account, so writing under a different account silently creates a
    SECOND entry that reads never reach — the new token is stored, the old one
    keeps being used, and nothing reports a failure. Reuse the label already
    there so the update lands on the entry the reads see.
    """
    result = subprocess.run(
        ["security", "find-generic-password", "-s", service],
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith('"acct"<blob>='):
            account = line.split("=", 1)[1].strip('"')
            if account and account != "<NULL>":
                return account
    return os.environ.get("USER", "")

def keychain_write(service: str, value: str) -> None:
    account = keychain_account(service)
    try:
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w"],
            input=f"{value}\n",
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        sys.exit(f"Error: could not store a value in keychain service {service}.")
    # Prove the write is what a read returns. Without this the caller reports a
    # 60-day expiry while every later pull keeps using the old token.
    if keychain_read(service) != value:
        sys.exit(
            f"Error: wrote {service} but a read returned a different value. "
            f"There is probably a duplicate entry under another account; run "
            f"`security find-generic-password -s {service}` and remove the extras."
        )



def _validate_credential(service: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        sys.exit(f"Error: keychain value for {service} is empty.")
    if any(character in value for character in "\r\n\0"):
        sys.exit(f"Error: keychain value for {service} contains unsupported control characters.")
    return value

def _safe_error(value: object, secrets: tuple[str, ...] = ()) -> str:
    """Return an error string with known credentials removed."""
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return sanitize_error(Exception(text))


def graph_error(payload: dict, *secrets: str) -> str | None:
    err = payload.get("error")
    if not isinstance(err, dict):
        return None
    code = err.get("code", "unknown")
    subcode = err.get("error_subcode")
    location = f"{code}/{subcode}" if subcode is not None else str(code)
    location = _safe_error(location, secrets)
    message = err.get("message")
    if message:
        return f"{location}: {_safe_error(message, secrets)}"
    return location


def _format_duration(seconds: int | float) -> str:
    if seconds >= 86_400:
        days = seconds / 86_400
        if days.is_integer():
            return f"{int(days)} days"
        return f"{days:.1f} days"
    if seconds >= 3_600:
        hours = seconds / 3_600
        if hours.is_integer():
            return f"{int(hours)} hours"
        return f"{hours:.1f} hours"
    if seconds >= 60:
        minutes = seconds / 60
        if minutes.is_integer():
            return f"{int(minutes)} minutes"
        return f"{minutes:.1f} minutes"
    return f"{seconds:g} seconds"


def cmd_extend(_args: argparse.Namespace) -> int:
    token = _validate_credential(TOKEN_SERVICE, keychain_read(TOKEN_SERVICE))
    app_id = _validate_credential(APP_ID_SERVICE, keychain_read(APP_ID_SERVICE))
    app_secret = _validate_credential(SECRET_SERVICE, keychain_read(SECRET_SERVICE))
    secrets = (token, app_id, app_secret)

    try:
        response = requests.post(
            f"{GRAPH}/oauth/access_token",
            data={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": token,
            },
            timeout=TIMEOUT,
        )
    except requests.RequestException as err:
        sys.exit(f"Error: exchange request failed — {_safe_error(err, secrets)}")

    try:
        payload = response.json()
    except (TypeError, ValueError):
        sys.exit("Error: exchange returned invalid JSON.")

    if not isinstance(payload, dict):
        sys.exit("Error: exchange returned an invalid response.")

    problem = graph_error(payload, *secrets)
    if problem:
        sys.exit(f"Error: exchange refused — {problem}")

    extended = payload.get("access_token")
    if (
        not isinstance(extended, str)
        or not extended.strip()
        or any(character in extended for character in "\r\n\0")
    ):
        sys.exit("Error: exchange returned an invalid access_token.")
    keychain_write(TOKEN_SERVICE, extended)
    expires_in = payload.get("expires_in")
    if (
        isinstance(expires_in, (int, float))
        and not isinstance(expires_in, bool)
        and expires_in > 0
        and math.isfinite(expires_in)
    ):
        print(f"expires    : in {_format_duration(expires_in)}")
    else:
        print("expires    : expiry metadata was not returned")
    print(f"stored     : keychain {TOKEN_SERVICE} (replaced)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("extend", help="swap a live short-lived token for ~60 days").set_defaults(fn=cmd_extend)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
