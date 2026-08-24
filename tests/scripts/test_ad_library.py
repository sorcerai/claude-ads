"""Meta Ad Library search: scope disclosure, credential handling, and paging."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import fetch_ad_library  # noqa: E402

FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "ad_library"
    / "meta_ads_archive.json"
)


class _Response:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def json(self) -> dict:
        return self._payload


def _fixture_payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_non_eu_commercial_search_is_disclosed_as_out_of_scope():
    """Meta returns only political ads outside the EU; an empty list must be explained."""
    warning = fetch_ad_library.scope_warning("ALL", ["US"])
    assert warning is not None
    assert "EU" in warning
    assert "partial results" in warning


def test_special_ad_categories_follow_the_same_rule_as_all():
    """Live-verified 2026-08-23: HOUSING_ADS/US returns EU/UK-reaching rows."""
    warning = fetch_ad_library.scope_warning("HOUSING_ADS", ["US"])
    assert warning is not None
    assert "partial results" in warning


def test_graph_api_host_passes_the_ssrf_guard():
    """The guard was written for landing pages; confirm it admits the API host.

    Every other test monkeypatches guarded_request, so without this the guard
    path never executes and a policy rejection would surface only in production.
    """
    from url_utils import _validate_and_resolve_url

    validated, parsed, addresses = _validate_and_resolve_url(fetch_ad_library.ENDPOINT)
    assert parsed.hostname == fetch_ad_library.API_HOST
    assert addresses


def test_eu_country_or_political_type_is_in_scope():
    assert fetch_ad_library.scope_warning("ALL", ["DE"]) is None
    assert fetch_ad_library.scope_warning("POLITICAL_AND_ISSUE_ADS", ["US"]) is None


def test_paging_cursor_off_host_is_refused():
    """A server-supplied cursor must not redirect our bearer token to another host."""
    with pytest.raises(ValueError, match="refusing to send credentials"):
        fetch_ad_library._validate_next_url("https://evil.test/v26.0/ads_archive?after=x")


def test_paging_cursor_on_host_is_accepted():
    cursor = "https://graph.facebook.com/v26.0/ads_archive?after=FIXTURECURSOR"
    assert fetch_ad_library._validate_next_url(cursor) == cursor


def test_search_pages_through_results_and_respects_max_pages(monkeypatch):
    calls: list[dict] = []

    def fake_guarded_request(session, method, url, **kwargs):
        calls.append({"url": url, "headers": kwargs.get("headers"), "params": kwargs.get("params")})
        return _Response(_fixture_payload())

    monkeypatch.setattr(fetch_ad_library, "guarded_request", fake_guarded_request)

    result = fetch_ad_library.search_ad_library(
        token="test-token-value",
        countries=["DE"],
        search_terms="project management",
        max_pages=2,
    )

    assert result["error"] is None
    assert result["pages_fetched"] == 2
    assert len(result["ads"]) == 4  # two fixture ads per page
    assert calls[1]["url"].startswith("https://graph.facebook.com/")
    # The cursor already carries the query; re-sending params would double-apply them.
    assert calls[1]["params"] is None


def test_token_travels_in_header_never_in_params(monkeypatch):
    """A token in the query string would land in logs, referrers, and error text."""
    captured: list[dict] = []

    def fake_guarded_request(session, method, url, **kwargs):
        captured.append(kwargs)
        payload = _fixture_payload()
        payload["paging"] = {}
        return _Response(payload)

    monkeypatch.setattr(fetch_ad_library, "guarded_request", fake_guarded_request)

    result = fetch_ad_library.search_ad_library(
        token="test-token-value",
        countries=["FR"],
        search_terms="crm",
    )

    assert result["error"] is None
    assert captured[0]["headers"]["Authorization"] == "Bearer test-token-value"
    assert "access_token" not in json.dumps(captured[0]["params"])
    assert "test-token-value" not in json.dumps(captured[0]["params"])


def test_missing_query_returns_error_without_request(monkeypatch):
    def explode(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("no request should be dispatched")

    monkeypatch.setattr(fetch_ad_library, "guarded_request", explode)

    result = fetch_ad_library.search_ad_library(token="t", countries=["DE"])
    assert "search-terms" in result["error"]


def test_non_200_reports_verification_hint(monkeypatch):
    monkeypatch.setattr(
        fetch_ad_library,
        "guarded_request",
        lambda *a, **k: _Response({}, status_code=401),
    )
    result = fetch_ad_library.search_ad_library(
        token="t", countries=["DE"], search_terms="x"
    )
    assert "401" in result["error"]
    assert "user access token" in result["error"]


def test_api_error_body_is_surfaced(monkeypatch):
    """A live app token returned 400 with the real cause only in the body."""
    payload = {
        "error": {
            "message": "Application does not have permission for this action",
            "code": 10,
            "error_subcode": 2332004,
            "error_user_title": "App role required",
        }
    }
    monkeypatch.setattr(
        fetch_ad_library,
        "guarded_request",
        lambda *a, **k: _Response(payload, status_code=400),
    )
    result = fetch_ad_library.search_ad_library(
        token="t", countries=["DE"], search_terms="x"
    )
    assert "App role required" in result["error"]
    # `code=` collides with the OAuth-code redaction pattern in url_utils.
    assert "error_code 10" in result["error"]
    assert "subcode 2332004" in result["error"]


def test_field_sets_are_opt_in():
    base = fetch_ad_library.build_fields(False, False)
    assert "spend" not in base
    assert "target_gender" not in base

    widened = fetch_ad_library.build_fields(True, True)
    assert "spend" in widened
    assert "target_gender" in widened


class _ResponseWithHeaders(_Response):
    def __init__(self, payload, status_code=200, headers=None):
        super().__init__(payload, status_code)
        self.headers = headers or {}


def test_usage_header_is_surfaced_for_throttle_headroom(monkeypatch):
    """ads_archive reports via x-business-use-case-usage, not X-App-Usage."""
    payload = {"data": [], "paging": {}}
    headers = {
        "x-business-use-case-usage": json.dumps(
            {"123": [{"type": "ads_archive", "call_count": 7, "estimated_time_to_regain_access": 0}]}
        )
    }
    monkeypatch.setattr(
        fetch_ad_library,
        "guarded_request",
        lambda *a, **k: _ResponseWithHeaders(payload, 200, headers),
    )
    result = fetch_ad_library.search_ad_library(token="t", countries=["DE"], search_terms="x")
    assert result["usage"]["header"] == "x-business-use-case-usage"
    assert result["usage"]["value"]["123"][0]["call_count"] == 7


def test_rate_limit_error_is_named_as_throttling_not_a_penalty(monkeypatch):
    """Error 613 is the Ad Library throttle code; it must not read as a ban."""
    payload = {"error": {"message": "rate limit", "code": 613}}
    monkeypatch.setattr(
        fetch_ad_library,
        "guarded_request",
        lambda *a, **k: _ResponseWithHeaders(payload, 400, {}),
    )
    result = fetch_ad_library.search_ad_library(token="t", countries=["DE"], search_terms="x")
    assert "Rate limited" in result["error"]
    assert "not a penalty" in result["error"]
    assert "app tokens are rejected" not in result["error"]


def test_transient_error_is_retried_exactly_once(monkeypatch):
    """ads/SKILL.md allows one retry, not the source pattern's four."""
    calls = []
    payload_err = {"error": {"message": "temporary", "code": 2, "is_transient": True}}
    payload_ok = {"data": [{"id": "1"}], "paging": {}}

    def fake(session, method, url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return _ResponseWithHeaders(payload_err, 500, {})
        return _ResponseWithHeaders(payload_ok, 200, {})

    monkeypatch.setattr(fetch_ad_library, "guarded_request", fake)
    result = fetch_ad_library.search_ad_library(
        token="t", countries=["DE"], search_terms="x", sleep=lambda _: None
    )
    assert len(calls) == 2
    assert result["error"] is None
    assert len(result["ads"]) == 1


def test_throttle_code_is_never_retried(monkeypatch):
    """Retrying a rate limit deepens the throttle it is reporting."""
    calls = []

    def fake(session, method, url, **kwargs):
        calls.append(url)
        return _ResponseWithHeaders(
            {"error": {"message": "rate limit", "code": 613, "is_transient": True}}, 400, {}
        )

    monkeypatch.setattr(fetch_ad_library, "guarded_request", fake)
    result = fetch_ad_library.search_ad_library(
        token="t", countries=["DE"], search_terms="x", sleep=lambda _: None
    )
    assert len(calls) == 1, "a 613 must not be retried"
    assert "Rate limited" in result["error"]


def test_auth_failure_is_not_retried(monkeypatch):
    """Authentication failures are excluded from retry by contract."""
    calls = []

    def fake(session, method, url, **kwargs):
        calls.append(url)
        return _ResponseWithHeaders(
            {"error": {"message": "bad token", "code": 190, "is_transient": False}}, 401, {}
        )

    monkeypatch.setattr(fetch_ad_library, "guarded_request", fake)
    fetch_ad_library.search_ad_library(
        token="t", countries=["DE"], search_terms="x", sleep=lambda _: None
    )
    assert len(calls) == 1


def test_retry_does_not_double_count_pages_or_resend_params(monkeypatch):
    """A retried page must not inflate pages_fetched or re-apply params."""
    seen = []
    ok = {"data": [{"id": "1"}], "paging": {}}

    def fake(session, method, url, **kwargs):
        seen.append(kwargs.get("params"))
        if len(seen) == 1:
            return _ResponseWithHeaders({"error": {"code": 2, "is_transient": True}}, 500, {})
        return _ResponseWithHeaders(ok, 200, {})

    monkeypatch.setattr(fetch_ad_library, "guarded_request", fake)
    result = fetch_ad_library.search_ad_library(
        token="t", countries=["DE"], search_terms="x", sleep=lambda _: None
    )
    assert result["pages_fetched"] == 1
    assert seen[0] is not None and seen[1] is not None  # same first page, params kept
