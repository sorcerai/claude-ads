"""Campaign metrics must not establish conversion-action configuration health."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from claude_ads_core.audit import _evaluate_google_findings


DIGEST = "a" * 64
SOURCE = f"sha256:{DIGEST}"


def _evaluate(conversions, controls=("G42",)):
    snapshot = {
        "window": {"start": "2026-09-01", "end": "2026-09-02"},
        "measurement_context": {"report_grain": ["campaign_id", "date"]},
        "conversions": deepcopy(conversions),
        "campaigns": [{"campaign_id": "fixture-campaign", "name": "Fixture"}],
        "spend": 10.0,
    }
    before = deepcopy(snapshot)
    entries = {
        control_id: SimpleNamespace(control_definition={"source_ids": ["fixture-source"]})
        for control_id in controls
    }
    findings, sources = _evaluate_google_findings(
        snapshot, entries, SOURCE, DIGEST, "fixture-run"
    )
    assert snapshot == before
    return {finding["control_id"]: finding for finding in findings}, sources


@pytest.mark.parametrize(
    "row",
    [
        {"action": "conversions", "status": "unknown"},
        {"action": "conversions", "count": 0},
        {"action": "conversions", "count": 12},
        {"action": "purchase", "status": "inactive"},
        {"action": "purchase", "status": "active", "count": 12},
    ],
)
def test_g42_never_passes_from_an_aggregate_metric_row(row):
    findings, _ = _evaluate([row])
    finding = findings["G42"]
    assert finding["status"] == "unknown"
    assert finding["confidence"] != "high"
    assert "defined and active" not in finding["diagnosis"]
    assert "Active conversion action" not in finding["observation"]
    assert "configuration" in finding["recommendation"].lower()


def test_g42_retains_observed_metric_provenance_without_promoting_it_to_proof():
    findings, sources = _evaluate([{"action": "conversions", "count": 2}])
    evidence = findings["G42"]["evidence"][0]
    assert evidence["source_id"] == SOURCE
    assert evidence["sha256"] == DIGEST
    assert evidence["redacted_value"] == "conversions"
    assert SOURCE in sources
    assert findings["G42"]["status"] == "unknown"


def test_g42_missing_metrics_requests_evidence_not_a_configuration_change():
    findings, _ = _evaluate([])
    finding = findings["G42"]
    assert finding["status"] == "unknown"
    assert finding["evidence"] == []
    assert "Configure primary" not in finding["recommendation"]
    assert "configuration" in finding["recommendation"].lower()


def test_g42_change_preserves_other_discovery_controls():
    controls = ("G42", "G43", "G01", "G08", "G12")
    findings, _ = _evaluate([{"action": "conversions", "count": 5}], controls)
    assert set(findings) == set(controls)
    assert all(finding["status"] == "unknown" for finding in findings.values())


def test_no_g42_entry_produces_no_g42_finding():
    findings, _ = _evaluate([{"action": "conversions", "count": 5}], ("G43",))
    assert set(findings) == {"G43"}
