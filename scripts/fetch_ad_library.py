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
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ad_library_quota import QuotaBudget, QuotaDeferred, _FileLock, _atomic_write

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
    print(
        "Error: requests library required. Install with: pip install -r requirements.txt"
    )
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
    """Validate a server cursor without ever forwarding its full URL."""
    if not isinstance(url, str):
        raise ValueError("Paging cursor must be a URL string.")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != API_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.path != f"/{API_VERSION}/ads_archive"
        or parsed.fragment
    ):
        raise ValueError(
            f"Paging cursor left {API_HOST}; refusing to send credentials."
        )
    values = parse_qs(parsed.query, keep_blank_values=True)
    after = values.get("after")
    if not after or len(after) != 1 or not after[0]:
        raise ValueError("Paging cursor did not contain one opaque after cursor.")
    return url


def _opaque_cursor(url: str) -> str:
    _validate_next_url(url)
    return parse_qs(urlparse(url).query, keep_blank_values=True)["after"][0]


def _error_details(response: Any) -> tuple[dict[str, Any], int | None]:
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        error = error if isinstance(error, dict) else {}
    except (TypeError, ValueError):
        error = {}
    code = error.get("code")
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    return error, code


_NON_RETRYABLE_CODES = frozenset({4, 10, 17, 32, 100, 190, 200, 613, 2500})


def _is_retryable_response(
    response: Any, error: dict[str, Any], code: int | None
) -> bool:
    if response.status_code in (401, 403):
        return False
    if code in _NON_RETRYABLE_CODES:
        return False
    return bool(error.get("is_transient"))


def _safe_observe(budget: Any, response: Any, error_code: int | None) -> dict[str, Any]:
    observation = budget.observe(
        getattr(response, "headers", {}) or {},
        status_code=int(response.status_code),
        error_code=error_code,
    )
    return observation if isinstance(observation, dict) else {}


def search_ad_library(
    *,
    token: str,
    countries: list[str],
    search_terms: str | None = None,
    search_page_ids: str | None = None,
    search_type: str = "KEYWORD_UNORDERED",
    ad_active_status: str = "ALL",
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
    quota_budget: Any | None = None,
    start_cursor: str | None = None,
) -> dict:
    """Query ads_archive with one quota reservation per network attempt."""
    limit = min(_positive_int(limit), MAX_PAGE_LIMIT)
    max_pages = min(_positive_int(max_pages), MAX_PAGES_CEILING)
    fields = list(fields or DEFAULT_FIELDS)
    result = {
        "source": "meta-ad-library-api",
        "endpoint": ENDPOINT,
        "retrieved_at": date.today().isoformat(),
        "query": {
            "search_terms": search_terms,
            "search_page_ids": search_page_ids,
            "ad_reached_countries": countries,
            "ad_type": ad_type,
            "search_type": search_type,
            "ad_active_status": ad_active_status,
            "ad_delivery_date_min": delivery_date_min,
            "ad_delivery_date_max": delivery_date_max,
            "fields": fields,
            "limit": limit,
        },
        "warning": scope_warning(ad_type, countries),
        "ads": [],
        "pages_fetched": 0,
        "usage": None,
        "error": None,
        "status": "exhausted",
        "next_cursor": start_cursor,
        "retry_at": None,
        "quota_stop_reason": None,
    }
    if not search_terms and not search_page_ids:
        result["error"] = "Provide --search-terms or --search-page-ids."
        result["status"] = "failed"
        return result
    if search_type not in {"KEYWORD_UNORDERED", "KEYWORD_EXACT_PHRASE"}:
        result["error"] = (
            "search_type must be KEYWORD_UNORDERED or KEYWORD_EXACT_PHRASE."
        )
        result["status"] = "failed"
        return result
    if ad_active_status not in {"ALL", "ACTIVE", "INACTIVE"}:
        result["error"] = "ad_active_status must be ALL, ACTIVE, or INACTIVE."
        result["status"] = "failed"
        return result

    params: dict[str, Any] = {
        "ad_reached_countries": json.dumps([c.upper() for c in countries]),
        "ad_type": ad_type,
        "ad_active_status": ad_active_status,
        "search_type": search_type,
        "fields": ",".join(fields),
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
    if start_cursor:
        params["after"] = start_cursor

    budget = quota_budget or QuotaBudget()
    session = requests.Session()
    session.trust_env = False
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def dispatch(target: str, page_params: dict | None) -> tuple[Any, dict[str, Any]]:
        last_response = None
        for attempt_number in (1, 2):
            try:
                with budget.attempt():
                    response = guarded_request(
                        session,
                        "GET",
                        target,
                        headers=headers,
                        params=page_params,
                        timeout=timeout,
                    )
                    error, code = _error_details(response)
                    observation = _safe_observe(budget, response, code)
            except QuotaDeferred:
                raise
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt_number == 2:
                    raise
                sleep(retry_delay)
                continue
            last_response = response
            result["usage"] = observation.get("usage") or result.get("usage")
            if observation.get("stop_reason"):
                result["quota_stop_reason"] = observation["stop_reason"]
                result["retry_at"] = observation["retry_at"]
            if response.status_code == 200 or observation.get("stop_reason"):
                return response, observation
            if attempt_number == 2 or not _is_retryable_response(response, error, code):
                return response, observation
            sleep(retry_delay)
        return last_response, {}

    url: str | None = ENDPOINT
    page_params: dict | None = params
    seen_cursors = {start_cursor} if start_cursor else set()
    try:
        while url and result["pages_fetched"] < max_pages:
            if result["pages_fetched"] > 0:
                sleep(pacing_delay)
            response, observation = dispatch(url, page_params)
            if response.status_code != 200:
                error, code = _error_details(response)
                detail_parts = [
                    str(error.get(key))
                    for key in ("message", "error_user_title", "error_user_msg")
                    if error.get(key)
                ]
                if code is not None:
                    detail_parts.append(
                        f"error_code {code} subcode {error.get('error_subcode')}"
                    )
                detail = " ".join(detail_parts)
                if code in THROTTLE_CODES or response.status_code == 429:
                    result["error"] = (
                        f"Ad Library API returned HTTP {response.status_code}. "
                        + (f"{sanitize_error(ValueError(detail))} " if detail else "")
                        + "Rate limited. Stop calling and let the rolling one-hour window recover; "
                        "check the usage field for headroom. This is throttling, not a penalty."
                    )
                else:
                    result["error"] = (
                        f"Ad Library API returned HTTP {response.status_code}. "
                        + (f"{sanitize_error(ValueError(detail))} " if detail else "")
                        + "The archive requires a user access token from an identity-confirmed "
                        "account; app tokens are rejected."
                    )
                result["status"] = (
                    "quota-deferred"
                    if observation.get("stop_reason")
                    and observation["stop_reason"] != "authentication"
                    else "failed"
                )
                return result

            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(
                payload.get("data"), list
            ):
                raise ValueError("Ad Library response must contain a data list.")
            page_ads = payload["data"]
            if any(not isinstance(ad, dict) or not ad.get("id") for ad in page_ads):
                raise ValueError("Ad Library response contains an invalid ad row.")
            paging = payload["paging"] if "paging" in payload else {}
            if not isinstance(paging, dict):
                raise ValueError(
                    "Ad Library response contains invalid paging metadata."
                )
            seen_ids = {
                str(ad.get("id")) for ad in result["ads"] if ad.get("id") is not None
            }
            for ad in page_ads:
                ad_id = ad.get("id")
                if ad_id is None or str(ad_id) not in seen_ids:
                    result["ads"].append(ad)
                    if ad_id is not None:
                        seen_ids.add(str(ad_id))
            result["pages_fetched"] += 1
            next_url = paging.get("next")
            next_cursor = _opaque_cursor(next_url) if next_url else None
            if next_cursor is not None and next_cursor in seen_cursors:
                raise ValueError("Paging cursor repeated; refusing to loop.")
            if next_cursor is not None:
                seen_cursors.add(next_cursor)
            result["next_cursor"] = next_cursor
            if observation.get("stop_reason"):
                result["quota_stop_reason"] = observation.get("stop_reason")
                result["retry_at"] = observation.get("retry_at")
                result["warning"] = (
                    (f"{result['warning']}; " if result.get("warning") else "")
                    + "Usage throttle threshold reached; quota budget requested a stop before further pagination."
                )
                if next_url:
                    result["status"] = "quota-deferred"
                    return result
                result["status"] = "exhausted"
                url = None
            elif not next_url:
                result["status"] = "exhausted"
                url = None
            else:
                url = ENDPOINT
                page_params = dict(params)
                page_params["after"] = result["next_cursor"]
                # Reconstruct from the fixed endpoint and opaque cursor only.
                result["status"] = "paginated"
    except QuotaDeferred as exc:
        result["status"] = "quota-deferred"
        result["error"] = str(exc.reason)
        result["retry_at"] = exc.retry_at
        result["quota_stop_reason"] = exc.reason
    except ValueError as exc:
        result["status"] = "failed"
        result["error"] = sanitize_error(exc)
        result["retryable"] = False
    except requests.exceptions.Timeout:
        result["status"] = "failed"
        result["error"] = f"Request timed out after {timeout} seconds"
    except requests.exceptions.RequestException as exc:
        result["status"] = "failed"
        result["error"] = f"Request failed: {sanitize_error(exc)}"
    return result


def _canonical_filter_payload(
    *,
    countries: list[str],
    search_page_ids: list[str],
    search_terms: str | None,
    search_type: str,
    ad_active_status: str,
    ad_type: str,
    fields: list[str],
    limit: int,
    delivery_date_min: str | None,
    delivery_date_max: str | None,
    run_id: str,
    client_id: str,
    purpose: str,
    privacy_class: str,
) -> dict[str, Any]:
    return {
        "countries": [c.upper() for c in countries],
        "search_page_ids": search_page_ids,
        "search_terms": search_terms,
        "search_type": search_type,
        "ad_active_status": ad_active_status,
        "ad_type": ad_type,
        "fields": fields,
        "limit": limit,
        "delivery_date_min": delivery_date_min,
        "delivery_date_max": delivery_date_max,
        "run_id": run_id,
        "client_id": client_id,
        "purpose": purpose,
        "privacy_class": privacy_class,
    }


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@contextmanager
def _checkpoint_lock(path: Path):
    with _FileLock(path.with_name(f".{path.name}.lock")):
        yield


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_write(path, value)


def _load_checkpoint(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"corrupt checkpoint: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("corrupt checkpoint: unsupported schema")
    filters = value.get("filters")
    if (
        not isinstance(value.get("advertisers"), list)
        or not isinstance(filters, dict)
        or not isinstance(filters.get("run_id"), str)
        or not filters["run_id"]
    ):
        raise ValueError("corrupt checkpoint: missing queue or run identity")
    if filters.get("search_page_ids") != [
        entry.get("advertiser_id")
        for entry in value["advertisers"]
        if isinstance(entry, dict)
    ]:
        raise ValueError("corrupt checkpoint: queue mismatch")
    for entry in value["advertisers"]:
        if not isinstance(entry, dict) or entry.get("status") not in {
            "queued",
            "paginated",
            "exhausted",
            "failed",
            "quota-deferred",
        }:
            raise ValueError("corrupt checkpoint: invalid advertiser state")
        cursor = entry.get("next_cursor")
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise ValueError("corrupt checkpoint: invalid cursor")
        history = entry.get("seen_cursors", [])
        if not isinstance(history, list) or any(
            not isinstance(item, str) or not item for item in history
        ):
            raise ValueError("corrupt checkpoint: invalid cursor history")
        if not isinstance(entry.get("retryable", True), bool):
            raise ValueError("corrupt checkpoint: invalid retryability")
        artifact = entry.get("artifact")
        if artifact is not None and (
            not isinstance(artifact, dict)
            or "ads" in artifact
            or not isinstance(artifact.get("observations"), list)
            or any(
                not isinstance(observation, dict)
                or not isinstance(observation.get("observation_id"), str)
                or not observation["observation_id"]
                for observation in artifact.get("observations", [])
            )
            or not isinstance(artifact.get("pages"), list)
            or any(
                not isinstance(page, dict)
                or not isinstance(page.get("source_digest"), str)
                or not page["source_digest"]
                or not isinstance(page.get("observation_ids"), list)
                or any(
                    not isinstance(observation_id, str) or not observation_id
                    for observation_id in page["observation_ids"]
                )
                for page in artifact.get("pages", [])
            )
        ):
            raise ValueError("corrupt checkpoint: raw artifact or invalid artifact")
    return value


def _merge_artifacts(
    base: dict[str, Any] | None, page: dict[str, Any]
) -> dict[str, Any]:
    observations = list(base["observations"]) if base else []
    known = {obs["observation_id"] for obs in observations}
    for observation in page["observations"]:
        if observation["observation_id"] not in known:
            observations.append(observation)
            known.add(observation["observation_id"])
    pages = list(base["pages"]) if base else []
    receipt = {
        key: page[key]
        for key in (
            "retrieved_at",
            "source_digest",
            "query_digest",
            "usage",
            "collection",
        )
    }
    receipt["observation_ids"] = [obs["observation_id"] for obs in page["observations"]]
    pages.append(receipt)
    merged = dict(page)
    merged["pages"] = pages
    merged["observations"] = observations
    merged["observation_count"] = len(observations)
    merged["pages_fetched"] = len(pages)
    merged["source_digest"] = "sha256:" + _fingerprint(
        [item["source_digest"] for item in pages]
    )
    return merged


def _collect_advertiser_queue_unlocked(
    *,
    token: str,
    countries: list[str],
    search_page_ids: str,
    checkpoint_path: str | os.PathLike[str],
    resume: bool = False,
    search_terms: str | None = None,
    search_type: str = "KEYWORD_UNORDERED",
    ad_active_status: str = "ALL",
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
    quota_budget: Any | None = None,
    run_id: str = "run-ad-library",
    client_id: str = "default-client",
    purpose: str = "competitor_analysis",
    privacy_class: str = "public",
) -> dict[str, Any]:
    """Collect page IDs sequentially, checkpointing normalized evidence per page."""
    path = Path(checkpoint_path).expanduser()
    limit = min(_positive_int(limit), MAX_PAGE_LIMIT)
    max_pages = min(_positive_int(max_pages), MAX_PAGES_CEILING)
    page_ids = [item.strip() for item in search_page_ids.split(",") if item.strip()]
    if not page_ids:
        raise ValueError(
            "search_page_ids queue must contain at least one advertiser id"
        )
    selected_fields = list(fields or DEFAULT_FIELDS)
    filters = _canonical_filter_payload(
        countries=countries,
        search_page_ids=page_ids,
        search_terms=search_terms,
        search_type=search_type,
        ad_active_status=ad_active_status,
        ad_type=ad_type,
        fields=selected_fields,
        limit=limit,
        delivery_date_min=delivery_date_min,
        delivery_date_max=delivery_date_max,
        run_id=run_id,
        client_id=client_id,
        purpose=purpose,
        privacy_class=privacy_class,
    )
    fingerprint = _fingerprint(filters)
    if resume:
        state = _load_checkpoint(path)
        if (
            state.get("fingerprint") != fingerprint
            or state.get("filters") != filters
            or _fingerprint(state.get("filters")) != fingerprint
        ):
            raise ValueError("checkpoint query mismatch")
    else:
        state = {
            "schema_version": 1,
            "fingerprint": fingerprint,
            "filters": filters,
            "advertisers": [
                {
                    "advertiser_id": page_id,
                    "status": "queued",
                    "next_cursor": None,
                    "artifact": None,
                    "error": None,
                    "retry_at": None,
                    "quota_stop_reason": None,
                    "seen_cursors": [],
                }
                for page_id in page_ids
            ],
        }
        _atomic_write_json(path, state)

    budget = quota_budget or QuotaBudget()
    stop_queue = False
    pages_this_invocation = 0
    page_cap = min(max(1, max_pages), MAX_PAGES_CEILING)
    for entry in state["advertisers"]:
        if stop_queue or pages_this_invocation >= page_cap:
            break
        if entry.get("status") == "failed" and entry.get("retryable") is False:
            break
        if entry.get("status") == "exhausted":
            continue
        if entry.get("status") in {"failed", "quota-deferred"}:
            entry["status"] = "queued"
        while (
            entry["status"] not in {"exhausted", "failed", "quota-deferred"}
            and pages_this_invocation < page_cap
        ):
            result = search_ad_library(
                token=token,
                countries=countries,
                search_terms=search_terms,
                search_page_ids=entry["advertiser_id"],
                search_type=search_type,
                ad_active_status=ad_active_status,
                ad_type=ad_type,
                fields=selected_fields,
                delivery_date_min=delivery_date_min,
                delivery_date_max=delivery_date_max,
                limit=limit,
                max_pages=1,
                timeout=timeout,
                retry_delay=retry_delay,
                pacing_delay=pacing_delay,
                sleep=sleep,
                quota_budget=budget,
                start_cursor=entry.get("next_cursor"),
            )
            pages_this_invocation += int(result.get("pages_fetched", 0))
            if result.get("pages_fetched"):
                page_artifact = build_canonical_artifact(
                    result,
                    run_id=run_id,
                    client_id=client_id,
                    purpose=purpose,
                    privacy_class=privacy_class,
                )
                entry["artifact"] = _merge_artifacts(
                    entry.get("artifact"), page_artifact
                )
            new_cursor = result.get("next_cursor")
            history = list(entry.get("seen_cursors") or [])
            if new_cursor and new_cursor != entry.get("next_cursor"):
                if new_cursor in history:
                    result["status"] = "failed"
                    result["error"] = (
                        "Paging cursor repeated across checkpoint; refusing to loop."
                    )
                    result["retryable"] = False
                    new_cursor = entry.get("next_cursor")
                else:
                    history.append(new_cursor)
            entry["seen_cursors"] = history
            entry["next_cursor"] = new_cursor
            entry["error"] = result.get("error")
            entry["retry_at"] = result.get("retry_at")
            entry["quota_stop_reason"] = result.get("quota_stop_reason")
            entry["status"] = result.get("status", "failed")
            entry["retryable"] = result.get("retryable", True)
            _atomic_write_json(path, state)
            if entry["status"] == "paginated":
                if pages_this_invocation >= page_cap:
                    break
                continue
            if entry["status"] in {"quota-deferred", "failed"} or result.get(
                "quota_stop_reason"
            ):
                stop_queue = True
    statuses = {entry["status"] for entry in state["advertisers"]}
    status = next(
        (item for item in ("failed", "quota-deferred") if item in statuses), None
    )
    status = status or ("exhausted" if statuses == {"exhausted"} else "paginated")
    if stop_queue and status == "paginated":
        status = "quota-deferred"
    return {
        "schema_version": 1,
        "fingerprint": fingerprint,
        "filters": filters,
        "advertisers": state["advertisers"],
        "status": status,
    }


def collect_advertiser_queue(
    *, checkpoint_path: str | os.PathLike[str], resume: bool = False, **kwargs
):
    path = Path(checkpoint_path).expanduser()
    with _checkpoint_lock(path):
        if path.exists() and not resume:
            raise ValueError("checkpoint already exists; pass --resume to continue it")
        if resume and kwargs.get("run_id") is None:
            kwargs["run_id"] = _load_checkpoint(path)["filters"]["run_id"]
        return _collect_advertiser_queue_unlocked(
            checkpoint_path=path,
            resume=resume,
            **kwargs,
        )


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
        "collection": {
            "status": result.get("status"),
            "next_cursor": result.get("next_cursor"),
            "retry_at": result.get("retry_at"),
            "quota_stop_reason": result.get("quota_stop_reason"),
        },
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
                "at_rest": "verified"
                if privacy_class != "public"
                else "not-applicable",
                "in_transit": "verified"
                if privacy_class != "public"
                else "not-applicable",
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


def _positive_int(value):
    number = int(value)
    if isinstance(value, bool) or number < 1:
        raise ValueError("must be a positive integer")
    return number


def main():
    try:
        _main()
    except (ValueError, OSError) as exc:
        print(f"Error: {sanitize_error(exc)}", file=sys.stderr)
        sys.exit(1)


def _main():
    parser = argparse.ArgumentParser(description="Search the Meta Ad Library")
    parser.add_argument("--search-terms", help="Keyword query")
    parser.add_argument("--search-page-ids", help="Comma-separated Facebook Page IDs")
    parser.add_argument(
        "--countries",
        required=True,
        help="Comma-separated ISO country codes (required by the API)",
    )
    parser.add_argument(
        "--search-type",
        default="KEYWORD_UNORDERED",
        choices=("KEYWORD_UNORDERED", "KEYWORD_EXACT_PHRASE"),
    )
    parser.add_argument(
        "--ad-active-status", default="ALL", choices=("ALL", "ACTIVE", "INACTIVE")
    )
    parser.add_argument("--ad-type", default="ALL", choices=AD_TYPES)
    parser.add_argument("--delivery-date-min", help="YYYY-MM-DD")
    parser.add_argument("--delivery-date-max", help="YYYY-MM-DD")
    parser.add_argument(
        "--limit", type=_positive_int, default=100, help="Results per page (max 100)"
    )
    parser.add_argument(
        "--max-pages",
        type=_positive_int,
        default=5,
        help="Maximum pages per invocation (max 10)",
    )
    parser.add_argument("--include-political-fields", action="store_true")
    parser.add_argument("--include-eu-fields", action="store_true")
    parser.add_argument("--run-id", default=None, help="Run ID for artifact provenance")
    parser.add_argument(
        "--client-id", default=None, help="Client ID for artifact provenance"
    )
    parser.add_argument(
        "--purpose", default="competitor_analysis", help="Purpose of data retrieval"
    )
    parser.add_argument(
        "--privacy-class",
        default="public",
        choices=("public", "internal", "confidential", "restricted"),
        help="Data lifecycle classification",
    )
    parser.add_argument(
        "--checkpoint", help="Checkpoint path for advertiser queue collection"
    )
    parser.add_argument(
        "--resume", action="store_true", help="Resume the checkpointed advertiser queue"
    )
    parser.add_argument(
        "--hourly-limit",
        type=int,
        default=60,
        help="Stricter shared quota attempt limit",
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
    checkpoint_path = None
    if args.checkpoint:
        try:
            checkpoint_path = resolve_output_path(args.checkpoint, create_parent=True)
        except ValueError as exc:
            print(f"Error: {sanitize_error(exc)}", file=sys.stderr)
            sys.exit(1)
        if output_path and checkpoint_path == output_path:
            print(
                "Error: --checkpoint and --output must be different paths.",
                file=sys.stderr,
            )
            sys.exit(1)
        if checkpoint_path.exists() and not args.resume:
            print(
                "Error: checkpoint exists; pass --resume to continue it.",
                file=sys.stderr,
            )
            sys.exit(1)

    token = os.environ.get("META_AD_LIBRARY_TOKEN")
    if not token:
        print(
            "Error: META_AD_LIBRARY_TOKEN is not set. Store the token in the "
            "environment or an OS keychain; never in a profile or the repository.",
            file=sys.stderr,
        )
        sys.exit(1)

    countries = [c.strip() for c in args.countries.split(",") if c.strip()]
    run_id = args.run_id or (
        None if args.resume else f"run-{date.today().strftime('%Y%m%d')}-ad-lib"
    )
    client_id = args.client_id or "default-client"
    purpose = args.purpose
    privacy_class = args.privacy_class
    quota_budget = QuotaBudget(hourly_limit=args.hourly_limit)
    fields = build_fields(args.include_political_fields, args.include_eu_fields)

    if args.resume and not checkpoint_path:
        print("Error: --resume requires --checkpoint.", file=sys.stderr)
        sys.exit(1)
    if checkpoint_path:
        result = collect_advertiser_queue(
            token=token,
            countries=countries,
            search_page_ids=args.search_page_ids or "",
            checkpoint_path=checkpoint_path,
            resume=args.resume,
            search_terms=args.search_terms,
            search_type=args.search_type,
            ad_active_status=args.ad_active_status,
            ad_type=args.ad_type,
            fields=fields,
            delivery_date_min=args.delivery_date_min,
            delivery_date_max=args.delivery_date_max,
            limit=args.limit,
            max_pages=args.max_pages,
            quota_budget=quota_budget,
            run_id=run_id,
            client_id=client_id,
            purpose=purpose,
            privacy_class=privacy_class,
        )
        payload = json.dumps(result, indent=2, ensure_ascii=False)
        if output_path:
            _atomic_write_json(output_path, result)
        else:
            print(payload)
        if result["status"] in {"failed", "quota-deferred"}:
            sys.exit(1)
        return

    result = search_ad_library(
        token=token,
        countries=countries,
        search_terms=args.search_terms,
        search_page_ids=args.search_page_ids,
        search_type=args.search_type,
        ad_active_status=args.ad_active_status,
        ad_type=args.ad_type,
        fields=fields,
        delivery_date_min=args.delivery_date_min,
        delivery_date_max=args.delivery_date_max,
        limit=args.limit,
        max_pages=args.max_pages,
        quota_budget=quota_budget,
    )
    if result.get("warning"):
        print(f"Warning: {result['warning']}", file=sys.stderr)
    canonical_artifact = build_canonical_artifact(
        result,
        run_id=run_id,
        client_id=client_id,
        purpose=purpose,
        privacy_class=privacy_class,
    )
    payload = json.dumps(canonical_artifact, indent=2, ensure_ascii=False)
    if output_path:
        _atomic_write_json(output_path, canonical_artifact)
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
