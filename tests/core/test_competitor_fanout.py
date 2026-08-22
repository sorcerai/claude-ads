"""Competitor fanout slice planning: contract validity, independence, coverage."""

from __future__ import annotations

import pytest

from claude_ads_core.competitor_fanout import (
    SOURCES,
    coverage_summary,
    meta_coverage_note,
    plan_slices,
    slugify,
)
from claude_ads_core.workflow_contracts import validate_workflow_contract

CREATED_AT = "2026-08-22T00:00:00Z"


def _plan(**overrides):
    kwargs = {
        "run_id": "run-2026-08-22",
        "competitors": ["Acme Corp", "Globex"],
        "countries": ["DE", "US"],
        "sources": ["meta-ad-library", "serp-paid"],
        "created_at": CREATED_AT,
    }
    kwargs.update(overrides)
    return plan_slices(**kwargs)


def test_every_slice_validates_against_the_orchestration_task_contract():
    for task in _plan():
        validate_workflow_contract("orchestration-task", task)


def test_slice_count_is_the_full_cross_product():
    assert len(_plan()) == 2 * 2 * 2


def test_slices_are_independent_and_never_share_a_destination():
    tasks = _plan()
    assert all(task["depends_on"] == [] for task in tasks)
    destinations = [task["output_contract"]["destination"] for task in tasks]
    assert len(set(destinations)) == len(destinations)
    assert all(task["output_contract"]["single_writer"] for task in tasks)


def test_task_ids_are_stable_across_identical_reruns():
    """Unstable IDs would break the supersedes chain on every rerun."""
    assert [t["task_id"] for t in _plan()] == [t["task_id"] for t in _plan()]


def test_meta_slice_outside_the_eu_carries_its_coverage_limit():
    tasks = _plan(competitors=["Acme"], countries=["US"], sources=["meta-ad-library"])
    scope = " ".join(tasks[0]["scope"])
    assert "Coverage limit" in scope
    assert "expected to be empty" in scope


def test_meta_slice_inside_the_eu_carries_no_coverage_limit():
    tasks = _plan(competitors=["Acme"], countries=["DE"], sources=["meta-ad-library"])
    assert not any("Coverage limit" in item for item in tasks[0]["scope"])


def test_special_category_slice_is_unconfirmed_not_empty():
    tasks = _plan(
        competitors=["Acme"],
        countries=["US"],
        sources=["meta-ad-library"],
        ad_type="HOUSING_ADS",
    )
    scope = " ".join(tasks[0]["scope"])
    assert "unconfirmed coverage" in scope
    assert "expected to be empty" not in scope


def test_token_backed_source_recovers_as_needs_input():
    """A missing token must not silently look like a competitor with no ads."""
    tasks = _plan(competitors=["Acme"], countries=["DE"], sources=["meta-ad-library"])
    assert any("META_AD_LIBRARY_TOKEN" in hint for hint in tasks[0]["recovery"])


def test_tokenless_source_has_no_secret_recovery_hint():
    tasks = _plan(competitors=["Acme"], countries=["US"], sources=["serp-paid"])
    assert not any("TOKEN" in hint for hint in tasks[0]["recovery"])
    assert SOURCES["serp-paid"]["secret_ref"] is None


def test_unknown_source_is_rejected():
    with pytest.raises(ValueError, match="unknown source"):
        _plan(sources=["meta-ad-library", "definitely-not-a-source"])


def test_empty_dimension_is_rejected():
    with pytest.raises(ValueError, match="non-empty"):
        _plan(competitors=[])


def test_slugify_rejects_names_with_no_usable_characters():
    with pytest.raises(ValueError):
        slugify("!!!")


def test_coverage_summary_refuses_to_call_a_partial_fanout_complete():
    results = [{"status": "ok"}, {"status": "ok"}, {"status": "blocked"}]
    summary = coverage_summary(results)
    assert summary["slices"] == 3
    assert summary["by_status"]["blocked"] == 1
    assert summary["complete"] is False


def test_coverage_summary_is_complete_only_when_every_slice_is_ok():
    assert coverage_summary([{"status": "ok"}, {"status": "ok"}])["complete"] is True
    assert coverage_summary([])["complete"] is False


def test_political_type_is_in_scope_everywhere():
    assert meta_coverage_note("POLITICAL_AND_ISSUE_ADS", ["US"]) is None


def test_cli_emits_validated_packets(capsys):
    import json

    from claude_ads_core.cli import main

    exit_code = main(
        [
            "plan-fanout",
            "--run-id", "run-cli",
            "--competitors", "Acme Corp",
            "--countries", "DE",
            "--sources", "meta-ad-library",
            "--created-at", CREATED_AT,
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["slices"] == 1
    validate_workflow_contract("orchestration-task", payload["tasks"][0])


def test_cli_rejects_an_unknown_source(capsys):
    from claude_ads_core.cli import main

    assert main(
        [
            "plan-fanout",
            "--run-id", "run-cli",
            "--competitors", "Acme",
            "--countries", "DE",
            "--sources", "not-a-source",
            "--created-at", CREATED_AT,
        ]
    ) == 2
    assert "unknown source" in capsys.readouterr().err
