"""Regression tests for snapshot locators and fanout single-writer identity.

Use synthetic credentials only. Exercise the public normalization and planning
entrypoints rather than reproducing the implementation in the tests.
"""

from urllib.parse import parse_qsl, urlsplit

import pytest

from claude_ads_core.competitor_fanout import normalize_archived_ads, plan_slices

CAPTURED_AT = "2026-09-29T12:00:00Z"


def _normalize(url):
    return normalize_archived_ads(
        [{"id": "review-ad", "ad_snapshot_url": url}],
        captured_at=CAPTURED_AT,
        provenance="ad-library-api",
    )[0]["snapshot_url"]


@pytest.mark.parametrize(
    "query",
    [
        "access_token=SYNTHETIC_A&access_token=SYNTHETIC_B&id=1",
        "id=1&access_token=SYNTHETIC_A&access_token=SYNTHETIC_B",
        "%61ccess_token=SYNTHETIC_A&id=1",
        "access%5Ftoken=SYNTHETIC_A&id=1",
        "ACCESS_TOKEN=SYNTHETIC_A&id=1",
        "access-token=SYNTHETIC_A&id=1",
        "access_token&%61ccess_token=&id=1",
        "refresh_token=SYNTHETIC_A&api_key=SYNTHETIC_B&id=1",
    ],
)
def test_snapshot_normalization_removes_every_credential_query_parameter(query):
    result = _normalize(f"https://example.com/render_ad/?{query}")
    assert result == "https://example.com/render_ad/?id=1"
    assert "SYNTHETIC_" not in result


def test_snapshot_normalization_preserves_public_query_values_and_is_idempotent():
    url = "https://example.com/render_ad/?id=1&tag=a-b&blank=&id=2"
    result = _normalize(url)
    assert parse_qsl(urlsplit(result).query, keep_blank_values=True) == [
        ("id", "1"), ("tag", "a-b"), ("blank", ""), ("id", "2")
    ]
    assert _normalize(result) == result


def test_snapshot_normalization_rejects_userinfo_without_echoing_credentials():
    url = (
        "https://operator:SYNTHETIC_PASSWORD@example.com/render_ad/"
        "?id=1#access_token=SYNTHETIC_FRAGMENT"
    )
    with pytest.raises(ValueError) as error:
        _normalize(url)
    assert "SYNTHETIC_" not in str(error.value)


def test_snapshot_normalization_drops_fragment_credentials():
    assert _normalize("https://example.com/render_ad/?id=1#access_token=SYNTHETIC_FRAGMENT") == "https://example.com/render_ad/?id=1"


@pytest.mark.parametrize(
    "url",
    [
        "https://[malformed/render_ad/?access_token=SYNTHETIC_A",
        "javascript:access_token=SYNTHETIC_A",
        "https://example.com:invalid/?access_token=SYNTHETIC_A",
        "https://example.com/\n?access_token=SYNTHETIC_A",
        "https://example.com\\@other.example/?access_token=SYNTHETIC_A",
    ],
)
def test_invalid_snapshot_locator_fails_without_echoing_raw_credentials(url):
    with pytest.raises(ValueError) as error:
        _normalize(url)
    assert "SYNTHETIC_" not in str(error.value)
    assert "snapshot URL" in str(error.value)


def test_absent_snapshot_stays_absent():
    assert _normalize(None) is None


def _plan(**overrides):
    arguments = {
        "run_id": "run-review",
        "competitors": ["Acme Corp", "Globex"],
        "countries": ["DE", "US"],
        "sources": ["meta-ad-library", "serp-paid"],
        "created_at": CAPTURED_AT,
    }
    arguments.update(overrides)
    return plan_slices(**arguments)


@pytest.mark.parametrize(
    "names", [["ACME Inc", "ACME-Inc"], ["AT&T", "AT T"], ["Brand.X", "Brand X"]]
)
def test_slug_collisions_fail_before_returning_any_dispatch_plan(names):
    with pytest.raises(ValueError, match="collid"):
        _plan(competitors=names)


def test_repeated_sources_do_not_duplicate_tasks_or_destinations():
    canonical = _plan()
    repeated = _plan(sources=["meta-ad-library", "serp-paid", "meta-ad-library"])
    assert repeated == canonical
    assert len({task["task_id"] for task in repeated}) == len(repeated)
    assert len({task["output_contract"]["destination"] for task in repeated}) == len(repeated)


def test_source_deduplication_precedes_slice_budget_accounting():
    tasks = _plan(
        competitors=[f"Brand {number}" for number in range(10)],
        countries=["DE", "US", "FR", "GB", "CA"],
        sources=["meta-ad-library"] * 5,
    )
    assert len(tasks) == 50
    assert len({task["task_id"] for task in tasks}) == 50


def test_unambiguous_task_ids_keep_the_existing_rerun_contract():
    task = _plan(competitors=["Acme Corp"], countries=["US"], sources=["serp-paid"])[0]
    assert task["task_id"] == "run-review.serp-paid.acme-corp.US"
    assert task["output_contract"]["destination"] == "results/run-review.serp-paid.acme-corp.US.json"
    assert task["mutation_authority"] == "none"
    assert task["output_contract"]["single_writer"] is True


def test_generators_and_repeated_case_variants_preserve_determinism():
    assert _plan(
        competitors=iter(["Acme Corp", "acme corp", "Globex"]),
        countries=iter(["DE", "de", "US"]),
        sources=iter(["meta-ad-library", "serp-paid", "serp-paid"]),
    ) == _plan()


def test_unknown_sources_are_still_rejected_after_deduplication():
    with pytest.raises(ValueError, match="unknown source"):
        _plan(sources=["missing-source", "missing-source"])
