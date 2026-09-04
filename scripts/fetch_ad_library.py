#!/usr/bin/env python3
"""
Search the Meta Ad Library (`ads_archive`) for public ad creative.

Scope is set by Meta, not by this script. Per the official reference, "Ads that
did not reach any location in the EU will only return if they are about social
issues, elections or politics." A commercial competitor search therefore returns
rows only when `--countries` includes an EU member state.

Usage:
    python fetch_ad_library.py --search-terms "project management" --countries DE,FR
    python fetch_ad_library.py --search-page-ids 12345 --countries US \
        --ad-type POLITICAL_AND_ISSUE_ADS --include-political-fields

The access token is read from the META_AD_LIBRARY_TOKEN environment variable and
sent as a Bearer header so it never enters a URL, log line, or error string.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlparse

from url_utils import (
    guarded_request,
    resolve_output_path,
    sanitize_error,
)

# The planner and this client must agree on when an empty result is a coverage
# limit rather than absence of ads, so the rule has exactly one definition.
from claude_ads_core.competitor_fanout import (
    meta_coverage_note as scope_warning,
    normalize_archived_ads,
)

try:
    import requests
except ImportError:
    print("Error: requests library required. Install with: pip install -r requirements.txt")
    sys.exit(1)

API_VERSION = "v26.0"
API_HOST = "graph.facebook.com"
ENDPOINT = f"https://{API_HOST}/{API_VERSION}/ads_archive"

# Returned for every ad the archive discloses, commercial or political.
DEFAULT_FIELDS = (
    "id",
    "ad_creation_time",
    "ad_creative_bodies",
    "ad_creative_link_captions",
    "ad_creative_link_descriptions",
    "ad_creative_link_titles",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "ad_snapshot_url",
    "languages",
    "page_id",
    "page_name",
    "publisher_platforms",
)

# Documented as populated for social issue, election, and political ads only.
POLITICAL_FIELDS = (
    "bylines",
    "currency",
    "spend",
    "impressions",
    "demographic_distribution",
    "delivery_by_region",
    "estimated_audience_size",
)

# Documented as populated for UK and EU delivery only.
EU_FIELDS = (
    "age_country_gender_reach_breakdown",
    "beneficiary_payers",
    "eu_total_reach",
    "target_ages",
    "target_gender",
    "target_locations",
    "total_reach_by_location",
)

# Documented throttle codes: 4 app, 17 user, 32 Pages, 613 Ad Library. These are
# excluded from retry even when Meta marks them transient, because retrying a rate
# limit deepens the very throttle the error is reporting.
THROTTLE_CODES = frozenset({4, 17, 32, 613})

AD_TYPES = (
    "ALL",
    "EMPLOYMENT_ADS",
    "FINANCIAL_PRODUCTS_AND_SERVICES_ADS",
    "HOUSING_ADS",
    "POLITICAL_AND_ISSUE_ADS",
)

MAX_PAGE_LIMIT = 100
MAX_PAGES_CEILING = 10
USAGE_THROTTLE_LIMIT = 80.0
DEFAULT_PACING_DELAY = 0.5


def build_fields(include_political: bool, include_eu: bool) -> list[str]:
    """Assemble the requested field list, widest-scope fields last."""
    fields = list(DEFAULT_FIELDS)
    if include_political:
        fields.extend(POLITICAL_FIELDS)
    if include_eu:
        fields.extend(EU_FIELDS)
    return fields


def _validate_next_url(url: str) -> str:
    """Confirm a paging cursor still points at the Graph API host.

    `paging.next` is server-supplied. guarded_request blocks private addresses,
    but the token travels in our Authorization header, so an off-host cursor
    would forward that credential to whatever host the response named.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != API_HOST:
        raise ValueError(f"Paging cursor left {API_HOST}; refusing to send credentials.")
    return url


def search_ad_library(
    *,
    token: str,
    countries: list[str],
    search_terms: str | None = None,
    search_page_ids: str | None = None,
    ad_type: str = "ALL",
    fields: list[str] | None = None,
    delivery_date_min: str | None = None,
    delivery_date_max: str | None = None,
    limit: int = 100,
    max_pages: int = 5,
    timeout: int = 30,
    retry_delay: float = 2.0,
    pacing_delay: float = DEFAULT_PACING_DELAY,
    sleep=time.sleep,
) -> dict:
    """Query ads_archive and page through results with hard limits and queue pacing.

    Returns a dict with query metadata, ads, pages_fetched, usage, warning, and
    error.

    The single-retry backoff shape is adapted from the MIT-licensed
    krusemediallc/arcads-claude-code meta_api.py, reduced from its four attempts
    to the one retry ads/SKILL.md permits, and narrowed to exclude throttle codes.
    """
    limit = min(max(1, limit), MAX_PAGE_LIMIT)
    max_pages = min(max(1, max_pages), MAX_PAGES_CEILING)

    result = {
        "source": "meta-ad-library-api",
        "endpoint": ENDPOINT,
        "retrieved_at": date.today().isoformat(),
        "query": {
            "search_terms": search_terms,
            "search_page_ids": search_page_ids,
            "ad_reached_countries": countries,
            "ad_type": ad_type,
        },
        "warning": scope_warning(ad_type, countries),
        "ads": [],
        "pages_fetched": 0,
        "usage": None,
        "error": None,
    }

    if not search_terms and not search_page_ids:
        result["error"] = "Provide --search-terms or --search-page-ids."
        return result

    params = {
        "ad_reached_countries": json.dumps([c.upper() for c in countries]),
        "ad_type": ad_type,
        "fields": ",".join(fields or DEFAULT_FIELDS),
        "limit": limit,
    }
    if search_terms:
        params["search_terms"] = search_terms
    if search_page_ids:
        params["search_page_ids"] = search_page_ids
    if delivery_date_min:
        params["ad_delivery_date_min"] = delivery_date_min
    if delivery_date_max:
        params["ad_delivery_date_max"] = delivery_date_max

    session = requests.Session()
    # Ambient proxy settings would see the Authorization header.
    session.trust_env = False
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def dispatch(target: str, page_params: dict | None):
        """Send one page, retrying a single transient failure.

        ads/SKILL.md allows exactly one retry of a transient tool failure and
        forbids retrying authentication, authorization, schema, policy, or
        validation errors. Throttle codes are excluded too: they are the one
        "transient" class where retrying makes the condition worse.
        """
        for attempt in (1, 2):
            try:
                response = guarded_request(
                    session, "GET", target, headers=headers,
                    params=page_params, timeout=timeout,
                )
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt == 2:
                    raise
                sleep(retry_delay)
                continue

            if response.status_code == 200 or attempt == 2:
                return response
            try:
                error = (response.json() or {}).get("error") or {}
            except ValueError:
                return response
            if error.get("is_transient") and error.get("code") not in THROTTLE_CODES:
                sleep(retry_delay)
                continue
            return response
        return response

    url: str | None = ENDPOINT
    try:
        while url and result["pages_fetched"] < max_pages:
            if result["pages_fetched"] > 0:
                sleep(pacing_delay)

            # A cursor URL already carries its query; re-sending params would
            # double-apply them.
            response = dispatch(url, params if result["pages_fetched"] == 0 else None)
            if response.status_code != 200:
                # Meta puts the actionable part in the body: an app token yields
                # "App role required" (code 10) while a bad token yields a plain
                # 401. Collapsing both into one hint sent us debugging the wrong
                # thing, so surface the API's own message.
                detail = ""
                throttled = False
                try:
                    error = (response.json() or {}).get("error") or {}
                    # 4 app limit, 17 user limit, 32 Pages limit, 613 Ad Library limit.
                    throttled = error.get("code") in (4, 17, 32, 613)
                    parts = [
                        str(error.get(key))
                        for key in ("message", "error_user_title", "error_user_msg")
                        if error.get(key)
                    ]
                    if error.get("code") is not None:
                        parts.append(
                            f"error_code {error['code']} subcode {error.get('error_subcode')}"
                        )
                    detail = " ".join(parts)
                except ValueError:
                    detail = ""
                if throttled:
                    result["error"] = (
                        f"Ad Library API returned HTTP {response.status_code}. "
                        + (f"{sanitize_error(ValueError(detail))} " if detail else "")
                        + "Rate limited. Stop calling and let the rolling one-hour window "
                        "recover; check the usage field for headroom. This is throttling, "
                        "not a penalty."
                    )
                else:
                    result["error"] = (
                        f"Ad Library API returned HTTP {response.status_code}. "
                        + (f"{sanitize_error(ValueError(detail))} " if detail else "")
                        + "The archive requires a user access token from an "
                        "identity-confirmed account; app tokens are rejected."
                    )
                return result

            # Meta's throttle signal. ads_archive reports through
            # x-business-use-case-usage rather than the X-App-Usage documented for
            # the general Graph API, so read that first. Values are percentages of
            # the limit; estimated_time_to_regain_access is minutes, 0 when clear.
            for header in ("x-business-use-case-usage", "X-App-Usage"):
                raw = response.headers.get(header)
                if not raw:
                    continue
                try:
                    result["usage"] = {"header": header, "value": json.loads(raw)}
                except ValueError:
                    result["usage"] = {"header": header, "value": raw}
                break

            payload = response.json()
            result["ads"].extend(payload.get("data", []))
            result["pages_fetched"] += 1

            # Check for usage throttling signals
            should_stop = False
            usage_val = result["usage"].get("value") if result.get("usage") else None
            if isinstance(usage_val, dict):
                for metric in ("call_count", "total_cputime", "total_time"):
                    if float(usage_val.get(metric, 0) or 0) >= USAGE_THROTTLE_LIMIT:
                        should_stop = True
                for items in usage_val.values():
                    if isinstance(items, list):
                        for entry in items:
                            if isinstance(entry, dict):
                                for metric in ("call_count", "total_cputime", "total_time"):
                                    if float(entry.get(metric, 0) or 0) >= USAGE_THROTTLE_LIMIT:
                                        should_stop = True
                                if int(entry.get("estimated_time_to_regain_access", 0) or 0) > 0:
                                    should_stop = True

            if should_stop:
                stop_msg = (
                    "Usage throttle threshold reached (>=80% utilization or backoff requested); "
                    "stopping further pagination."
                )
                result["warning"] = f"{result['warning']}; {stop_msg}" if result.get("warning") else stop_msg
                url = None
            else:
                next_url = (payload.get("paging") or {}).get("next")
                url = _validate_next_url(next_url) if next_url else None

    except ValueError as exc:
        result["error"] = sanitize_error(exc)
    except requests.exceptions.Timeout:
        result["error"] = f"Request timed out after {timeout} seconds"
    except requests.exceptions.RequestException as exc:
        result["error"] = f"Request failed: {sanitize_error(exc)}"

    return result


def build_canonical_artifact(
    result: dict[str, Any],
    *,
    run_id: str,
    client_id: str,
    purpose: str,
    privacy_class: str = "public",
) -> dict[str, Any]:
    """Fold raw public ad search results into one canonical competitor artifact.

    Raw API payloads are never persisted directly. Every artifact binds run,
    client-purpose, retrieval timestamp, source digest, warning/error/usage, and
    lifecycle, then normalizes through canonical competitor observations before storage.
    """
    raw_ads = result.get("ads", [])
    now_iso = datetime.now(timezone.utc).isoformat()
    raw_bytes = json.dumps(raw_ads, sort_keys=True).encode("utf-8")
    source_digest = f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}"
    query_bytes = json.dumps(result.get("query", {}), sort_keys=True).encode("utf-8")
    query_digest = f"sha256:{hashlib.sha256(query_bytes).hexdigest()}"

    normalized_observations = normalize_archived_ads(
        raw_ads,
        captured_at=now_iso,
        provenance="ad-library-api",
    )

    return {
        "schema_version": "1.0.0",
        "artifact_type": "competitor-observations",
        "run_id": run_id,
        "client_id": client_id,
        "purpose": purpose,
        "retrieved_at": now_iso,
        "source": "meta-ad-library-api",
        "source_digest": source_digest,
        "query_digest": query_digest,
        "query": result.get("query"),
        "warning": result.get("warning"),
        "usage": result.get("usage"),
        "error": result.get("error"),
        "pages_fetched": result.get("pages_fetched", 0),
        "observation_count": len(normalized_observations),
        "data_lifecycle": {
            "schema_version": "1.0.0",
            "lifecycle_id": f"lifecycle-{run_id}",
            "classification": privacy_class,
            "retention": {
                "minimum_seconds": 0,
                "mode": "operator-defined",
                "delete_after": None,
                "purpose": purpose,
                "exception_reason": None,
            },
            "encryption": {
                "at_rest": "verified" if privacy_class != "public" else "not-applicable",
                "in_transit": "verified" if privacy_class != "public" else "not-applicable",
                "evidence_refs": [],
            },
            "access": {
                "owner": "competitor-research-agent",
                "authorized_roles": ["research-worker", "conductor"],
                "access_log_locator": None,
            },
            "deletion": {
                "status": "scheduled",
                "method": "file-removal",
                "verification_required": False,
                "verification_artifact_locator": None,
            },
            "incident": {
                "owner": "security-owner",
                "reporting_channel": "security-incident",
                "status": "not-triggered",
                "record_locator": None,
            },
        },
        "observations": normalized_observations,
    }


def main():
    parser = argparse.ArgumentParser(description="Search the Meta Ad Library")
    parser.add_argument("--search-terms", help="Keyword query")
    parser.add_argument("--search-page-ids", help="Comma-separated Facebook Page IDs")
    parser.add_argument(
        "--countries",
        required=True,
        help="Comma-separated ISO country codes (required by the API)",
    )
    parser.add_argument("--ad-type", default="ALL", choices=AD_TYPES)
    parser.add_argument("--delivery-date-min", help="YYYY-MM-DD")
    parser.add_argument("--delivery-date-max", help="YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=100, help="Results per page (max 100)")
    parser.add_argument("--max-pages", type=int, default=5, help="Maximum pages to fetch (max 10)")
    parser.add_argument("--include-political-fields", action="store_true")
    parser.add_argument("--include-eu-fields", action="store_true")
    parser.add_argument("--run-id", default=None, help="Run ID for artifact provenance")
    parser.add_argument("--client-id", default=None, help="Client ID for artifact provenance")
    parser.add_argument("--purpose", default="competitor_analysis", help="Purpose of data retrieval")
    parser.add_argument(
        "--privacy-class",
        default="public",
        choices=("public", "internal", "confidential", "restricted"),
        help="Data lifecycle classification",
    )
    parser.add_argument("--output", "-o", help="Write JSON here instead of stdout")

    args = parser.parse_args()

    # Pre-flight validate output path before making any network calls
    output_path = None
    if args.output:
        try:
            output_path = resolve_output_path(args.output, create_parent=True)
        except ValueError as exc:
            print(f"Error: {sanitize_error(exc)}", file=sys.stderr)
            sys.exit(1)

    token = os.environ.get("META_AD_LIBRARY_TOKEN")
    if not token:
        print(
            "Error: META_AD_LIBRARY_TOKEN is not set. Store the token in the "
            "environment or an OS keychain; never in a profile or the repository.",
            file=sys.stderr,
        )
        sys.exit(1)

    result = search_ad_library(
        token=token,
        countries=[c.strip() for c in args.countries.split(",") if c.strip()],
        search_terms=args.search_terms,
        search_page_ids=args.search_page_ids,
        ad_type=args.ad_type,
        fields=build_fields(args.include_political_fields, args.include_eu_fields),
        delivery_date_min=args.delivery_date_min,
        delivery_date_max=args.delivery_date_max,
        limit=args.limit,
        max_pages=args.max_pages,
    )

    if result.get("warning"):
        print(f"Warning: {result['warning']}", file=sys.stderr)

    run_id = args.run_id or f"run-{date.today().strftime('%Y%m%d')}-ad-lib"
    client_id = args.client_id or "default-client"
    purpose = args.purpose
    privacy_class = args.privacy_class

    canonical_artifact = build_canonical_artifact(
        result,
        run_id=run_id,
        client_id=client_id,
        purpose=purpose,
        privacy_class=privacy_class,
    )

    payload = json.dumps(canonical_artifact, indent=2, ensure_ascii=False)
    if output_path:
        output_path.write_text(payload, encoding="utf-8")
        print(
            f"Saved {len(canonical_artifact['observations'])} normalized observations to {output_path}",
            file=sys.stderr,
        )
    else:
        print(payload)

    if result.get("error"):
        print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
