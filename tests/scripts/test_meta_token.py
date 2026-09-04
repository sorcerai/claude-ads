"""Safety and exchange behavior for the Meta token CLI."""


from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import requests

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import meta_token  # noqa: E402


TOKEN = "synthetic-token-never-print"
APP_ID = "synthetic-app-id-never-print"
APP_SECRET = "synthetic-app-secret-never-print"
EXTENDED = "synthetic-extended-token-never-print"


def _keychain_values(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    services: list[str] = []
    values = {
        meta_token.TOKEN_SERVICE: TOKEN,
        meta_token.APP_ID_SERVICE: APP_ID,
        meta_token.SECRET_SERVICE: APP_SECRET,
    }

    def fake_read(service: str) -> str:
        services.append(service)
        return values[service]

    monkeypatch.setattr(meta_token, "keychain_read", fake_read)
    return services


def test_missing_keychain_guidance_uses_prompt_without_value_argument(
    monkeypatch: pytest.MonkeyPatch,
):
    def fail_run(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(meta_token.subprocess, "run", fail_run)
    with pytest.raises(SystemExit) as raised:
        meta_token.keychain_read(meta_token.TOKEN_SERVICE)
    message = str(raised.value)
    assert f"security add-generic-password -U -s {meta_token.TOKEN_SERVICE} -a $USER -w" in message
    assert "<value>" not in message
    assert "Enter the value at the Keychain password prompt" in message



@pytest.mark.parametrize("line_ending", ["\n"])
def test_keychain_read_removes_only_one_line_ending(
    monkeypatch: pytest.MonkeyPatch,
    line_ending: str,
):
    class Result:
        stdout = TOKEN + line_ending

    monkeypatch.setattr(meta_token.subprocess, "run", lambda *args, **kwargs: Result())
    assert meta_token.keychain_read(meta_token.TOKEN_SERVICE) == TOKEN


@pytest.mark.parametrize(
    "stdout",
    [
        f"\n{TOKEN}\n",
        f"{TOKEN}\r\n",
        f"{TOKEN}\nextra\n",
        f"{TOKEN}\n\n",
    ],
)
def test_keychain_read_rejects_unexpected_credential_line_endings(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
):
    class Result:
        pass

    Result.stdout = stdout
    monkeypatch.setattr(meta_token.subprocess, "run", lambda *args, **kwargs: Result())
    with pytest.raises(SystemExit) as raised:
        meta_token.keychain_read(meta_token.TOKEN_SERVICE)
    assert "unsupported control characters" in str(raised.value)
    assert TOKEN not in str(raised.value)


def test_safe_error_redacts_before_constructing_exception(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: list[str] = []

    def spy_sanitize(error: Exception) -> str:
        seen.append(str(error))
        return str(error)

    monkeypatch.setattr(meta_token, "sanitize_error", spy_sanitize)
    result = meta_token._safe_error(f"plain secret {TOKEN}", (TOKEN,))
    assert seen == ["plain secret [REDACTED]"]
    assert TOKEN not in result

@pytest.mark.parametrize("bad_character", ["\n", "\r", "\0"])
@pytest.mark.parametrize(
    "service",
    [meta_token.TOKEN_SERVICE, meta_token.APP_ID_SERVICE, meta_token.SECRET_SERVICE],
)
def test_extend_rejects_control_characters_before_network(
    monkeypatch: pytest.MonkeyPatch,
    service: str,
    bad_character: str,
):
    values = {
        meta_token.TOKEN_SERVICE: TOKEN,
        meta_token.APP_ID_SERVICE: APP_ID,
        meta_token.SECRET_SERVICE: APP_SECRET,
    }
    invalid = f"synthetic-invalid{bad_character}value"
    values[service] = invalid
    monkeypatch.setattr(meta_token, "keychain_read", lambda requested: values[requested])
    monkeypatch.setattr(
        meta_token.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("network must not run with control characters")
        ),
    )
    with pytest.raises(SystemExit) as raised:
        meta_token.cmd_extend(None)
    assert service in str(raised.value)
    assert invalid not in str(raised.value)


@pytest.mark.parametrize("bad_character", ["\n", "\r", "\0"])
def test_extend_rejects_control_characters_in_returned_token_before_storage(
    monkeypatch: pytest.MonkeyPatch,
    bad_character: str,
):
    _keychain_values(monkeypatch)
    invalid = f"synthetic-returned{bad_character}token"

    class Response:
        def json(self) -> dict:
            return {"access_token": invalid}

    monkeypatch.setattr(meta_token.requests, "post", lambda url, **kwargs: Response())
    monkeypatch.setattr(
        meta_token,
        "keychain_write",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid returned token must not be stored")
        ),
    )
    with pytest.raises(SystemExit) as raised:
        meta_token.cmd_extend(None)
    assert "access_token" in str(raised.value)
    assert invalid not in str(raised.value)


def test_debug_token_query_would_expose_token_but_inspection_surface_is_removed():
    """The old prepared GET URL demonstrates why automated inspection is forbidden."""
    prepared = requests.Request(
        "GET",
        f"{meta_token.GRAPH}/debug_token",
        params={"input_token": TOKEN},
    ).prepare()
    assert TOKEN in prepared.url
    assert not hasattr(meta_token, "debug_token")
    assert not hasattr(meta_token, "cmd_status")


def test_cli_exposes_extend_only(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "argv", ["meta_token.py", "status"])
    with pytest.raises(SystemExit):
        meta_token.main()


def test_extend_posts_credentials_in_body_and_stores_returned_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    services = _keychain_values(monkeypatch)
    calls: list[dict] = []

    class Response:
        def json(self) -> dict:
            return {"access_token": EXTENDED, "expires_in": 5_184_000}

    def fake_post(url: str, **kwargs: object) -> Response:
        calls.append({"url": url, **kwargs})
        return Response()

    stored: list[tuple[str, str]] = []
    monkeypatch.setattr(meta_token.requests, "post", fake_post)
    monkeypatch.setattr(meta_token, "keychain_write", lambda service, value: stored.append((service, value)))

    assert meta_token.cmd_extend(None) == 0

    assert services == [meta_token.TOKEN_SERVICE, meta_token.APP_ID_SERVICE, meta_token.SECRET_SERVICE]
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == f"{meta_token.GRAPH}/oauth/access_token"
    assert "?" not in call["url"]
    assert call.get("params") is None
    assert call["data"] == {
        "grant_type": "fb_exchange_token",
        "client_id": APP_ID,
        "client_secret": APP_SECRET,
        "fb_exchange_token": TOKEN,
    }
    assert stored == [(meta_token.TOKEN_SERVICE, EXTENDED)]

    output = capsys.readouterr()
    rendered = output.out + output.err
    for secret in (TOKEN, APP_ID, APP_SECRET, EXTENDED):
        assert secret not in rendered
    assert "60 days" in output.out


def test_extend_reports_missing_expiry_metadata_without_inspection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _keychain_values(monkeypatch)

    class Response:
        def json(self) -> dict:
            return {"access_token": EXTENDED}

    monkeypatch.setattr(meta_token.requests, "post", lambda url, **kwargs: Response())
    monkeypatch.setattr(meta_token, "keychain_write", lambda service, value: None)

    assert meta_token.cmd_extend(None) == 0
    assert "expiry metadata was not returned" in capsys.readouterr().out


@pytest.mark.parametrize("expires_in", [0, -1, float("nan"), float("inf"), "5184000"])
def test_nonpositive_or_nonfinite_expiry_is_reported_as_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    expires_in: object,
):
    _keychain_values(monkeypatch)

    class Response:
        def json(self) -> dict:
            return {"access_token": EXTENDED, "expires_in": expires_in}

    monkeypatch.setattr(meta_token.requests, "post", lambda url, **kwargs: Response())
    monkeypatch.setattr(meta_token, "keychain_write", lambda service, value: None)

    assert meta_token.cmd_extend(None) == 0
    assert "expiry metadata was not returned" in capsys.readouterr().out


def test_request_exception_is_sanitized(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    _keychain_values(monkeypatch)

    def fake_post(url: str, **kwargs: object) -> None:
        raise requests.RequestException(f"client_secret={APP_SECRET} token={TOKEN}")

    monkeypatch.setattr(meta_token.requests, "post", fake_post)
    with pytest.raises(SystemExit) as raised:
        meta_token.cmd_extend(None)

    output = capsys.readouterr()
    rendered = str(raised.value) + output.out + output.err
    assert "exchange request failed" in rendered
    for secret in (TOKEN, APP_ID, APP_SECRET):
        assert secret not in rendered


def test_invalid_json_uses_fixed_safe_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _keychain_values(monkeypatch)

    class Response:
        def json(self) -> dict:
            raise ValueError(f"invalid body leaked {TOKEN} {APP_SECRET}")

    monkeypatch.setattr(meta_token.requests, "post", lambda url, **kwargs: Response())
    with pytest.raises(SystemExit) as raised:
        meta_token.cmd_extend(None)

    output = capsys.readouterr()
    rendered = str(raised.value) + output.out + output.err
    assert str(raised.value) == "Error: exchange returned invalid JSON."
    for secret in (TOKEN, APP_ID, APP_SECRET):
        assert secret not in rendered


def test_api_error_is_sanitized_and_does_not_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _keychain_values(monkeypatch)
    stored: list[tuple[str, str]] = []

    class Response:
        def json(self) -> dict:
            return {
                "error": {
                    "code": 190,
                    "error_subcode": 463,
                    "message": f"access_token={TOKEN} client_secret={APP_SECRET}",
                }
            }

    monkeypatch.setattr(meta_token.requests, "post", lambda url, **kwargs: Response())
    monkeypatch.setattr(meta_token, "keychain_write", lambda service, value: stored.append((service, value)))
    with pytest.raises(SystemExit) as raised:
        meta_token.cmd_extend(None)

    output = capsys.readouterr()
    rendered = str(raised.value) + output.out + output.err
    assert "exchange refused" in rendered
    assert "190/463" in rendered
    assert stored == []
    for secret in (TOKEN, APP_ID, APP_SECRET):
        assert secret not in rendered


def test_no_prepared_request_can_contain_credentials(monkeypatch: pytest.MonkeyPatch):
    _keychain_values(monkeypatch)
    prepared_urls: list[str] = []

    class Response:
        def json(self) -> dict:
            return {"access_token": EXTENDED}

    def fake_post(url: str, **kwargs: object) -> Response:
        prepared_urls.append(
            requests.Request("POST", url, data=kwargs["data"]).prepare().url
        )
        assert kwargs.get("params") is None
        return Response()

    monkeypatch.setattr(meta_token.requests, "post", fake_post)
    monkeypatch.setattr(meta_token, "keychain_write", lambda service, value: None)
    meta_token.cmd_extend(None)



    assert prepared_urls == [f"{meta_token.GRAPH}/oauth/access_token"]
    assert all(secret not in prepared_urls[0] for secret in (TOKEN, APP_ID, APP_SECRET, EXTENDED))

def test_keychain_write_uses_stdin_and_never_renders_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(meta_token, "keychain_account", lambda service: "synthetic-account")
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fail_run(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))
        raise subprocess.CalledProcessError(
            1,
            ["security", "add-generic-password", "-w"],
            stderr=f"failed for {EXTENDED}",
        )

    monkeypatch.setattr(meta_token.subprocess, "run", fail_run)
    with pytest.raises(SystemExit) as raised:
        meta_token.keychain_write(meta_token.TOKEN_SERVICE, EXTENDED)

    output = capsys.readouterr()
    argv, kwargs = calls[0]
    command = argv[0]
    assert command[-1] == "-w"
    assert EXTENDED not in command
    assert kwargs["input"] == EXTENDED + "\n"
    assert EXTENDED not in str(raised.value)
    assert EXTENDED not in output.out + output.err


@pytest.mark.parametrize(
    ("empty_service",),
    [
        (meta_token.TOKEN_SERVICE,),
        (meta_token.APP_ID_SERVICE,),
        (meta_token.SECRET_SERVICE,),
    ],
)
def test_extend_rejects_empty_keychain_credentials_before_network(
    monkeypatch: pytest.MonkeyPatch,
    empty_service: str,
):
    values = {
        meta_token.TOKEN_SERVICE: TOKEN,
        meta_token.APP_ID_SERVICE: APP_ID,
        meta_token.SECRET_SERVICE: APP_SECRET,
    }
    values[empty_service] = ""
    monkeypatch.setattr(meta_token, "keychain_read", lambda service: values[service])

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("network must not run with an empty keychain value")

    monkeypatch.setattr(meta_token.requests, "post", no_network)
    with pytest.raises(SystemExit) as raised:
        meta_token.cmd_extend(None)
    assert empty_service in str(raised.value)
    for secret in (TOKEN, APP_ID, APP_SECRET):
        assert secret not in str(raised.value)


def test_json_payload_is_not_rendered_on_failure(monkeypatch: pytest.MonkeyPatch):
    """Keep this guard explicit if error handling is changed later."""
    _keychain_values(monkeypatch)
    payload = {"error": {"message": json.dumps({"token": TOKEN, "secret": APP_SECRET})}}

    class Response:
        def json(self) -> dict:
            return payload

    monkeypatch.setattr(meta_token.requests, "post", lambda url, **kwargs: Response())
    with pytest.raises(SystemExit) as raised:
        meta_token.cmd_extend(None)
    assert TOKEN not in str(raised.value)
    assert APP_SECRET not in str(raised.value)
