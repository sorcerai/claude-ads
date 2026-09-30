"""Regression tests for competitor identity and snapshot locator boundaries."""

from urllib.parse import parse_qsl, urlsplit

import pytest

from claude_ads_core.competitor_fanout import normalize_archived_ads, plan_slices


CREATED_AT = "2026-09-29T00:00:00Z"


def _normalize(url):
    return normalize_archived_ads(
        [{"id": "fixture-ad", "ad_snapshot_url": url}],
        captured_at=CREATED_AT,
        provenance="ad-library-api",
    )[0]["snapshot_url"]


@pytest.mark.parametrize(
    "query",
    [
        "access_token=fixture-a&access_token=fixture-b&id=1",
        "id=1&access_token=fixture-a&access_token=fixture-b",
        "%61ccess_token=fixture-a&id=1",
        "ACCESS_TOKEN=fixture-a&id=1",
        "access-token=fixture-a&id=1",
        "refresh_token=fixture-a&id=1",
        "api_key=fixture-a&id=1",
        "authorization=fixture-a&id=1",
        "id=1&access_token&access_token=fixture-b",
        "oauth_token=fixture-a&x-amz-security-token=fixture-b&id=1",
        "id=1&%6fauth_token=fixture-a&X-Amz-Signature=fixture-b",
        "id_token=fixture-a&session-token=fixture-b&id=1",
        "x-goog-credential=fixture-a&x-goog-signature=fixture-b&id=1",
        "service_api_key=fixture-a&id=1",
        "id=1&sig=fixture-a",
        "id=1&AWSAccessKeyId=fixture-a",
        "id=1&accessToken=fixture-a&clientSecret=fixture-b",
        "id=1&refreshToken=fixture-a&OAuthToken=fixture-b",
        "id=1&IDToken=fixture-a&XAmzSignature=fixture-b",
    ],
)
def test_snapshot_query_removes_all_explicit_credentials(query):
    result = _normalize(f"https://example.com/render_ad/?{query}")
    assert result == "https://example.com/render_ad/?id=1"
    assert "fixture-a" not in result and "fixture-b" not in result


@pytest.mark.parametrize(
    "credential_name",
    ["client_assertion", "clientAssertion", "code_verifier", "codeVerifier"],
)
def test_snapshot_drops_oauth_assertion_and_verifier_fields(credential_name):
    result = _normalize(f"https://example.com/render_ad/?id=1&{credential_name}=fixture-secret")
    assert result == "https://example.com/render_ad/?id=1"


@pytest.mark.parametrize(
    "query",
    [
        "id=1&state=%7B%22access_token%22%3A%22fixture-secret%22%7D",
        "id=%7B%22access_token%22%3A%22fixture-secret%22%7D",
        "id=%5Cu007b%5Cu0022access_token%5Cu0022%5Cu003a"
        "%5Cu0022fixture-secret%5Cu0022%5Cu007d",
        "id=%3Ctoken%3Efixture-secret%3C%2Ftoken%3E",
        "id=%20fixture-secret",
        "id=1&tag=a%0Ab",
        "id=1&blank=fixture-secret",
        "id=1&unrecognized_public_field=fixture-secret",
    ],
)
def test_snapshot_rejects_nonallowlisted_or_structured_query_values(query):
    with pytest.raises(ValueError) as failure:
        _normalize(f"https://example.com/render_ad/?{query}")
    assert "fixture-secret" not in str(failure.value)


def test_snapshot_preserves_repeated_safe_fields_and_missing_locator():
    result = _normalize("https://example.com/render_ad/?id=1&tag=a&tag=b&blank=")
    assert parse_qsl(urlsplit(result).query, keep_blank_values=True) == [
        ("id", "1"), ("tag", "a"), ("tag", "b"), ("blank", "")
    ]
    assert _normalize(None) is None


@pytest.mark.parametrize(
    "locator",
    ({"access_token": "fixture-secret"}, {}, ["fixture-secret"], [], 7, 0),
)
def test_snapshot_rejects_nonstring_locator_without_echoing_value(locator):
    with pytest.raises(ValueError) as error:
        _normalize(locator)
    assert "fixture-secret" not in str(error.value)


def test_snapshot_discards_fragment_credentials():
    assert _normalize("https://example.com/render_ad/?id=1#access_token=fixture-a") == (
        "https://example.com/render_ad/?id=1"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://fixture-user:fixture-password@example.com/render_ad/?id=1",
        "https://[malformed/render_ad/?id=1",
        "https://example.com:bad/render_ad/?id=1",
        "javascript:fixture-a",
        "https://example.com/\\render_ad/?id=1",
        "https://example.com/render_ad/?id=1\naccess_token=fixture-a",
        "https://example.com/render_ad/?id=1;access_token=fixture-a",
        "https://example.com/render_ad/?%2561ccess_token=fixture-a&id=1",
        "https://exa mple.com/render_ad/?id=1",
        "https://exa%20mple.com/render_ad/?id=1",
        "https://example.com/render ad/?id=1",
        "https://example.com/%ZZ?id=1",
        "https://example.com/render_ad/?id=%ZZ",
        "https://example.com/render_ad/?id=1#bad%ZQ",
        "https://example.com/out?target=https%3A%2F%2Ffacebook.com%2Frender_ad%2F%3Faccess_token%3Dfixture-a",
        "https://example.com/out?target=%2Frender_ad%2F%3Fsig%3Dfixture-a",
        "https://example.com/out?target=https%253A%252F%252Ffacebook.com%252Frender_ad%252F%253Faccess_token%253Dfixture-a",
    ],
)
def test_invalid_snapshot_locator_fails_without_echoing_input(url):
    with pytest.raises(ValueError) as error:
        _normalize(url)
    assert "fixture-password" not in str(error.value)
    assert "fixture-a" not in str(error.value)
    assert "fixture-secret" not in str(error.value)


def _plan(**overrides):
    parameters = {
        "run_id": "review-fixture",
        "competitors": ["Acme Corp"],
        "countries": ["DE"],
        "sources": ["meta-ad-library"],
        "created_at": CREATED_AT,
    }
    parameters.update(overrides)
    return plan_slices(**parameters)


def test_fanout_rejects_distinct_names_with_colliding_slugs_before_dispatch():
    with pytest.raises(ValueError, match="collid"):
        _plan(competitors=["ACME Inc", "ACME-Inc"])


def test_fanout_deduplicates_source_ids_without_duplicate_destinations():
    tasks = _plan(sources=["meta-ad-library", "meta-ad-library", "serp-paid"])
    assert len(tasks) == 2
    assert len({task["task_id"] for task in tasks}) == 2
    assert len({task["output_contract"]["destination"] for task in tasks}) == 2


def test_fanout_preserves_existing_noncolliding_task_ids():
    tasks = _plan(competitors=["Acme Corp", "ACME CORP"])
    assert len(tasks) == 1
    assert tasks[0]["task_id"] == "review-fixture.meta-ad-library.acme-corp.DE"
    assert tasks == _plan(competitors=["Acme Corp", "ACME CORP"])


def test_duplicate_sources_do_not_consume_slice_budget():
    assert len(_plan(sources=["meta-ad-library"] * 100)) == 1
