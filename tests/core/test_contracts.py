from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path
from typing import Any, get_args, get_type_hints

import pytest
from claude_ads_core.contracts import CONTRACT_NAMES, ContractError, schema_path, validate_contract
import claude_ads_core as package
from claude_ads_core import models as contract_models
from claude_ads_core.models import (
    AccountSnapshot,
    AttributionWindow,
    BudgetRow,
    CampaignRow,
    CategoryScoreOutput,
    ControlDefinition,
    ConversionRow,
    CreativeRow,
    EvidenceRecord,
    Finding,
    MeasurementContext,
    ReportBundle,
    RunManifest,
)


V2_SCHEMA_DIR = Path(__file__).parents[2] / "claude_ads_core" / "schemas" / "v2"


def v2_schema(name: str) -> dict:
    return json.loads((V2_SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))


def data_lifecycle(classification: str = "confidential") -> dict:
    return {
        "schema_version": "1.0.0",
        "lifecycle_id": "test-lifecycle",
        "classification": classification,
        "retention": {
            "minimum_seconds": 0,
            "mode": "operator-defined",
            "delete_after": "2026-07-12T16:00:00Z" if classification != "public" else None,
            "purpose": "Complete and verify the sanitized test run",
            "exception_reason": None,
        },
        "encryption": {
            "at_rest": "verified" if classification != "public" else "not-applicable",
            "in_transit": "verified" if classification != "public" else "not-applicable",
            "evidence_refs": ["operator-attestation:test-encryption"] if classification != "public" else [],
        },
        "access": {"owner": "test-owner", "authorized_roles": ["test-operator"], "access_log_locator": None},
        "deletion": {"status": "scheduled", "method": "Operator-defined deletion", "verification_required": True, "verification_artifact_locator": None},
        "incident": {"owner": "test-owner", "reporting_channel": "Private security channel", "status": "not-triggered", "record_locator": None},
    }


def measurement_context() -> dict:
    return {
        "timezone": "America/Los_Angeles",
        "currency": "USD",
        "profile_id": "profile-1",
        "source_format": "google_ads_export",
        "source_ids": ["source-1"],
        "report_grain": ["campaign", "date"],
        "conversion_definition": "primary conversions",
        "conversion_actions": ["purchase"],
        "attribution_model": "data-driven",
        "click_attribution_window": {"value": 30, "unit": "day"},
        "view_attribution_window": None,
        "counting_behavior": "once_per_conversion",
        "as_of": "2026-07-01",
        "data_finalization": "final",
        "modeled_data_treatment": "excluded",
        "missing_fields": ["view_attribution_window"],
        "unsupported_fields": [],
    }


def account_snapshot() -> dict:
    return {
        "schema_version": "2.0.0",
        "account": {"platform": "google", "account_id": "acct-1"},
        "window": {"start": "2026-06-01", "end": "2026-06-30"},
        "currency": "USD",
        "measurement_context": measurement_context(),
        "spend": 250.5,
        "campaigns": [{"campaign_id": "campaign-1", "name": "Campaign", "status": "enabled", "spend": 100.0}],
        "creatives": [{"creative_id": "creative-1", "campaign_id": "campaign-1", "name": "Creative"}],
        "conversions": [{"action": "purchase", "count": 1.0}],
        "budgets": [{"campaign_id": "campaign-1", "date": "2026-06-30", "amount": 150.0}],
    }


def legacy_account_snapshot() -> dict:
    payload = account_snapshot()
    payload["schema_version"] = "1.0.0"
    payload.pop("measurement_context")
    return payload


def run_manifest() -> dict:
    return {
        "schema_version": "1.0.0",
        "run_id": "run-20260711-001",
        "started_at": "2026-07-11T16:00:00Z",
        "scopes": ["audit", "google"],
        "adapters": [{"platform": "google", "mode": "export"}],
        "sources": ["export.csv", "google-export"],
        "privacy_class": "confidential",
        "data_lifecycle": data_lifecycle(),
        "worker_status": {"google": "completed"},
        "completeness": "complete",
    }


def control(control_id: str = "G-1") -> dict:
    return {
        "schema_version": "1.0.0",
        "control_id": control_id,
        "category": "tracking",
        "severity": "critical",
        "required_inputs": ["conversions"],
        "source_ids": ["google-help-1"],
        "maturity": "source-grounded",
        "geographies": ["global"],
        "expires_at": "2026-08-01",
        "scoring_behavior": "health",
        "stability": "stable",
    }


def evidence_record() -> dict:
    return {
        "evidence_id": "evidence-1",
        "proof_kind": "observation",
        "source_id": "google-export",
        "locator": "conversions[0]",
        "sha256": None,
        "observed_at": "2026-07-11T16:00:00Z",
        "query_id": None,
        "report_id": "report-1",
        "window": {"start": "2026-06-01", "end": "2026-06-30"},
        "report_grain": ["conversion"],
        "input_field": "conversions",
        "redacted_value": "active",
        "observation_ref": None,
    }


def finding(control_id: str = "G-1") -> dict:
    return {
        "schema_version": "2.0.0",
        "control_id": control_id,
        "status": "pass",
        "evidence": [evidence_record()],
        "confidence": "high",
        "source_classification": "evidence_based",
        "observation": "Conversion action is active.",
        "diagnosis": "No fault found.",
        "recommendation": "Keep monitoring.",
    }


def legacy_finding() -> dict:
    payload = finding()
    payload["schema_version"] = "1.0.0"
    payload["evidence"] = [{"path": "conversions[0]"}]
    return payload


def category_score(
    category: str = "tracking",
    *,
    category_weight: float = 100.0,
    health_score: float | None = 100.0,
    evidence_coverage: float = 100.0,
    applicable_controls: int = 1,
    known_controls: int = 1,
    passed_controls: int = 1,
    failed_controls: int = 0,
    unknown_controls: int = 0,
) -> dict:
    return {
        "category": category,
        "category_weight": category_weight,
        "health_score": health_score,
        "evidence_coverage": evidence_coverage,
        "applicable_controls": applicable_controls,
        "known_controls": known_controls,
        "passed_controls": passed_controls,
        "failed_controls": failed_controls,
        "unknown_controls": unknown_controls,
    }


def report_bundle() -> dict:
    return {
        "schema_version": "2.0.0",
        "run_manifest": run_manifest(),
        "account_snapshot": account_snapshot(),
        "control_definitions": [control()],
        "findings": [finding()],
        "scoring": {
            "health_score": 100.0,
            "evidence_coverage": 100.0,
            "status": "normal",
            "categories": [category_score()],
        },
    }


def legacy_report_bundle() -> dict:
    payload = report_bundle()
    payload["schema_version"] = "1.0.0"
    payload["account_snapshot"] = legacy_account_snapshot()
    payload["findings"] = [legacy_finding()]
    return payload




def test_named_contract_types_are_public_interfaces():
    assert {
        item.__name__
        for item in (
            AccountSnapshot,
            RunManifest,
            ControlDefinition,
            EvidenceRecord,
            Finding,
            CategoryScoreOutput,
            ReportBundle,
        )
    } == {
        "AccountSnapshot",
        "RunManifest",
        "ControlDefinition",
        "EvidenceRecord",
        "Finding",
        "CategoryScoreOutput",
        "ReportBundle",
    }


@pytest.mark.parametrize(
    ("name", "payload"),
    [("run-manifest", run_manifest()), ("control-definition", control())],
)
def test_unchanged_v1_contracts_accept_valid_payloads(name: str, payload: dict):
    validate_contract(name, payload)


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("account-snapshot", legacy_account_snapshot()),
        ("finding", legacy_finding()),
        ("report-bundle", legacy_report_bundle()),
    ],
)
def test_v1_payloads_for_v2_contracts_are_rejected(name: str, payload: dict):
    with pytest.raises(ContractError, match="schema_version"):
        validate_contract(name, payload)


def test_schema_path_routes_only_changed_contracts_to_v2():
    for name in ("account-snapshot", "finding", "report-bundle"):
        assert f"/schemas/v2/{name}.schema.json" in str(schema_path(name))
    for name in ("run-manifest", "control-definition"):
        assert f"/schemas/v1/{name}.schema.json" in str(schema_path(name))

def test_v2_cutover_retires_only_changed_v1_schema_resources(repo_root: Path):
    v1_dir = repo_root / "claude_ads_core" / "schemas" / "v1"
    v2_dir = repo_root / "claude_ads_core" / "schemas" / "v2"
    retired = {"account-snapshot", "finding", "report-bundle"}
    retained_v1 = {
        "brand-profile",
        "control-definition",
        "creative-brief",
        "data-lifecycle",
        "experiment-artifact",
        "generation-manifest",
        "media-plan",
        "monitoring-bundle",
        "mutation-plan",
        "orchestration-gate",
        "orchestration-result",
        "orchestration-run",
        "orchestration-task",
        "run-manifest",
        "setup-profile",
        "workflow-common",
    }

    assert {path.stem.removesuffix(".schema") for path in v1_dir.glob("*.schema.json")} == retained_v1
    assert all((v2_dir / f"{name}.schema.json").is_file() for name in retired)
    assert all(not (v1_dir / f"{name}.schema.json").exists() for name in retired)
    assert all(f"/schemas/v2/{name}.schema.json" in str(schema_path(name)) for name in retired)

def test_new_measurement_types_are_top_level_exports_without_v1_staging_type():
    assert package.AttributionWindow is AttributionWindow
    assert package.MeasurementContext is MeasurementContext
    assert package.EvidenceRecord is EvidenceRecord
    assert not hasattr(package, "AccountSnapshotV1")


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("account-snapshot", account_snapshot()),
        ("finding", finding()),
        ("report-bundle", report_bundle()),
    ],
)
def test_v2_contracts_accept_valid_payloads(name: str, payload: dict):
    validate_contract(name, payload)


def test_package_data_includes_all_versioned_schemas(repo_root: Path):
    config = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = config["tool"]["setuptools"]["package-data"]["claude_ads_core"]

    assert "schemas/v1/*.json" in package_data
    assert "schemas/v2/*.json" in package_data


def test_installed_package_resource_set_contains_all_versioned_schemas():
    import importlib.resources as resources

    package_schemas = resources.files(package).joinpath("schemas")
    source_schemas = Path(__file__).parents[2] / "claude_ads_core" / "schemas"
    expected = sorted(path.relative_to(source_schemas) for path in source_schemas.rglob("*.json"))
    assert expected
    for relative in expected:
        resource = package_schemas.joinpath(*relative.parts)
        assert resource.is_file(), relative


def test_all_packaged_schemas_are_valid_json_and_versioned():
    for name in CONTRACT_NAMES:
        path = schema_path(name)
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        version = "v2" if name in {"account-snapshot", "finding", "report-bundle"} else "v1"
        assert schema["$id"] == (
            f"urn:ai-marketing-hub:claude-ads:schema:core:{version}:{name}.schema.json"
        )

def test_every_cross_file_schema_reference_is_absolute_and_registered(repo_root: Path):
    schema_roots = (
        repo_root / "claude_ads_core/schemas/v1",
        repo_root / "claude_ads_core/schemas/v2",
        repo_root / "control-plane/schemas",
        repo_root / "evals/schemas",
    )
    documents = []
    registered_ids = set()
    for schema_root in schema_roots:
        for path in schema_root.glob("*.json"):
            document = json.loads(path.read_text(encoding="utf-8"))
            documents.append((path, document))
            registered_ids.add(document["$id"])

    def references(value):
        if isinstance(value, dict):
            if "$ref" in value:
                yield value["$ref"]
            for child in value.values():
                yield from references(child)
        elif isinstance(value, list):
            for child in value:
                yield from references(child)

    for path, document in documents:
        for reference in references(document):
            if reference.startswith("#"):
                continue
            target = reference.split("#", 1)[0]
            assert target.startswith("urn:ai-marketing-hub:claude-ads:schema:"), (
                path,
                reference,
            )
            assert target in registered_ids, (path, reference)


def test_snapshot_rejects_reversed_window():
    payload = account_snapshot()
    payload["window"] = {"start": "2026-07-01", "end": "2026-06-01"}
    with pytest.raises(ContractError, match="on or after"):
        validate_contract("account-snapshot", payload)

def test_snapshot_rejects_uppercase_platform_without_normalizing():
    payload = account_snapshot()
    payload["account"]["platform"] = "Google"
    with pytest.raises(ContractError) as exc_info:
        validate_contract("account-snapshot", payload)
    assert str(exc_info.value) == (
        "$.account.platform must be one of: "
        "amazon, apple, google, linkedin, meta, microsoft, pinterest, reddit, snapchat, tiktok, x, youtube"
    )


def test_manifest_rejects_uppercase_adapter_platform_without_normalizing():
    payload = run_manifest()
    payload["adapters"][0]["platform"] = "Google"
    with pytest.raises(ContractError) as exc_info:
        validate_contract("run-manifest", payload)
    assert str(exc_info.value) == "$.adapters[0].platform is unsupported"


def test_report_rejects_duplicate_evidence_ids_across_distinct_findings():
    payload = report_bundle()
    payload["findings"].append(finding("G-2"))
    with pytest.raises(ContractError) as exc_info:
        validate_contract("report-bundle", payload)
    assert str(exc_info.value) == "$.findings[1].evidence[0].evidence_id must be unique"


def test_complete_report_requires_completed_worker_for_each_adapter_platform():
    payload = report_bundle()
    payload["run_manifest"]["worker_status"] = {"fake-worker": "completed"}
    with pytest.raises(ContractError) as exc_info:
        validate_contract("report-bundle", payload)
    assert str(exc_info.value) == (
        "$.run_manifest complete requires completed workers for every adapter platform"
    )



def test_snapshot_rejects_non_finite_spend():
    payload = account_snapshot()
    payload["spend"] = float("nan")
    with pytest.raises(ContractError, match="finite"):
        validate_contract("account-snapshot", payload)
@pytest.mark.parametrize(
    ("collection", "row", "message"),
    [
        ("campaigns", {"campaign_id": ""}, "campaign_id"),
        ("creatives", {"creative_id": "", "campaign_id": "campaign-1"}, "creative_id"),
        ("conversions", {"action": "", "count": 1}, "action"),
        ("budgets", {"campaign_id": "", "date": "2026-06-30", "amount": 1}, "campaign_id"),
    ],
)
def test_snapshot_rejects_empty_row_ids_and_text(collection: str, row: dict, message: str):
    payload = account_snapshot()
    payload[collection] = [row]
    with pytest.raises(ContractError, match=message):
        validate_contract("account-snapshot", payload)


@pytest.mark.parametrize(
    ("collection", "row"),
    [
        ("campaigns", {"campaign_id": "campaign-1", "unexpected": "x"}),
        ("creatives", {"creative_id": "creative-1", "campaign_id": "campaign-1", "unexpected": "x"}),
        ("conversions", {"action": "purchase", "count": 1, "unexpected": "x"}),
        ("budgets", {"campaign_id": "campaign-1", "date": "2026-06-30", "amount": 1, "unexpected": "x"}),
    ],
)
def test_snapshot_rejects_extra_row_keys(collection: str, row: dict):
    payload = account_snapshot()
    payload[collection] = [row]
    with pytest.raises(ContractError, match="exactly"):
        validate_contract("account-snapshot", payload)


@pytest.mark.parametrize(
    ("collection", "row"),
    [
        ("campaigns", {"campaign_id": "campaign-1", "spend": -1}),
        ("campaigns", {"campaign_id": "campaign-1", "spend": float("nan")}),
        ("conversions", {"action": "purchase", "count": -1}),
        ("conversions", {"action": "purchase", "count": float("inf")}),
        ("budgets", {"campaign_id": "campaign-1", "date": "2026-06-30", "amount": -1}),
        ("budgets", {"campaign_id": "campaign-1", "date": "2026-06-30", "amount": True}),
    ],
)
def test_snapshot_rejects_invalid_row_numeric_values(collection: str, row: dict):
    payload = account_snapshot()
    payload[collection] = [row]
    with pytest.raises(ContractError, match="number|finite|nonnegative|>= 0"):
        validate_contract("account-snapshot", payload)


@pytest.mark.parametrize("date_value", ["2026-6-30", "20260630", "not-a-date"])
def test_snapshot_rejects_noncanonical_budget_dates(date_value: str):
    payload = account_snapshot()
    payload["budgets"][0]["date"] = date_value
    with pytest.raises(ContractError, match="ISO 8601 date"):
        validate_contract("account-snapshot", payload)


@pytest.mark.parametrize("row", [{"action": "purchase"}])
def test_snapshot_rejects_invalid_conversion_count_status_shape(row: dict):
    payload = account_snapshot()
    payload["conversions"] = [row]
    with pytest.raises(ContractError, match="count|status"):
        validate_contract("account-snapshot", payload)


def test_snapshot_accepts_conversion_with_count_and_status():
    payload = account_snapshot()
    payload["conversions"] = [{"action": "purchase", "count": 1, "status": "active"}]
    validate_contract("account-snapshot", payload)

def test_snapshot_accepts_generic_native_and_sanitized_fixture_rows():
    payload = account_snapshot()
    validate_contract("account-snapshot", payload)

    payload["campaigns"] = [{"campaign_id": "campaign-native", "name": "Native", "status": "paused", "spend": 0}]
    payload["creatives"] = [{"creative_id": "creative-native", "campaign_id": "campaign-native"}]
    payload["conversions"] = [{"action": "purchase", "count": 0}]
    payload["budgets"] = [{"campaign_id": "campaign-native", "date": "2026-06-30", "amount": 0}]
    validate_contract("account-snapshot", payload)

    payload["campaigns"] = [{"campaign_id": "campaign-001", "policy_status": "eligible"}]
    payload["creatives"] = []
    payload["conversions"] = [{"action": "primary_conversion_action", "status": "inactive"}]
    payload["budgets"] = []
    validate_contract("account-snapshot", payload)



def test_snapshot_requires_measurement_context_and_exact_context_keys():
    payload = account_snapshot()
    del payload["measurement_context"]
    with pytest.raises(ContractError, match="measurement_context"):
        validate_contract("account-snapshot", payload)

    payload = account_snapshot()
    payload["measurement_context"]["unexpected"] = "x"
    with pytest.raises(ContractError, match="measurement_context"):
        validate_contract("account-snapshot", payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timezone", 1),
        ("currency", "usd"),
        ("profile_id", ""),
        ("source_format", ""),
        ("source_ids", ["source-1", "source-1"]),
        ("report_grain", []),
        ("report_grain", ["campaign", "campaign"]),
        ("conversion_definition", ""),
        ("conversion_actions", ["purchase", "purchase"]),
        ("attribution_model", ""),
        ("click_attribution_window", {"value": -1, "unit": "day"}),
        ("click_attribution_window", {"value": True, "unit": "day"}),
        ("click_attribution_window", {"value": 1, "unit": "week"}),
        ("view_attribution_window", {"value": -1, "unit": "hour"}),
        ("counting_behavior", ""),
        ("as_of", "2026-05-31"),
        ("data_finalization", "invalid"),
        ("modeled_data_treatment", "invalid"),
        ("unsupported_fields", ["campaign_name", "account_name"]),
        ("unsupported_fields", ["account_name", "account_name"]),
        ("unsupported_fields", ["unknown_field"]),
    ]
)
def test_snapshot_rejects_invalid_measurement_context_values(field: str, value: Any):
    payload = account_snapshot()
    payload["measurement_context"][field] = value
    with pytest.raises(ContractError):
        validate_contract("account-snapshot", payload)


def test_snapshot_requires_matching_measurement_currency():
    payload = account_snapshot()
    payload["measurement_context"]["currency"] = "EUR"
    with pytest.raises(ContractError, match="currency"):
        validate_contract("account-snapshot", payload)


def test_snapshot_missing_fields_is_exact_sorted_unavailable_set():
    payload = account_snapshot()
    context = payload["measurement_context"]
    context.update(
        timezone=None,
        source_ids=[],
        conversion_definition=None,
        conversion_actions=[],
        attribution_model=None,
        click_attribution_window=None,
        view_attribution_window=None,
        counting_behavior=None,
        data_finalization="unknown",
        modeled_data_treatment="unknown",
        missing_fields=[
            "attribution_model",
            "click_attribution_window",
            "conversion_actions",
            "conversion_definition",
            "counting_behavior",
            "data_finalization",
            "modeled_data_treatment",
            "source_ids",
            "timezone",
            "view_attribution_window",
        ],
    )
    validate_contract("account-snapshot", payload)

    context["missing_fields"] = list(reversed(context["missing_fields"]))
    with pytest.raises(ContractError, match="missing_fields"):
        validate_contract("account-snapshot", payload)

    context["missing_fields"] = ["profile_id"]
    with pytest.raises(ContractError, match="missing_fields"):
        validate_contract("account-snapshot", payload)


def test_snapshot_keeps_unsupported_fields_distinct_from_missing_fields():
    payload = account_snapshot()
    payload["measurement_context"]["unsupported_fields"] = ["campaign_name"]
    validate_contract("account-snapshot", payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("proof_kind", "invalid"),
        ("source_id", ""),
        ("locator", None),
        ("observed_at", "2026-07-11T16:00:00"),
        ("query_id", ""),
        ("report_id", ""),
        ("input_field", ""),
        ("window", {"start": "2026-07-01", "end": "2026-06-01"}),
        ("report_grain", ["conversion", "conversion"]),
        ("report_grain", [1]),
        ("redacted_value", None),
        ("observation_ref", None),
    ],
)
def test_finding_rejects_invalid_evidence_record_values(field: str, value: Any):
    payload = finding()
    payload["evidence"][0][field] = value
    if field in {"redacted_value", "observation_ref"}:
        payload["evidence"][0]["redacted_value"] = None
        payload["evidence"][0]["observation_ref"] = None
    with pytest.raises(ContractError):
        validate_contract("finding", payload)


def test_finding_requires_exact_evidence_keys_and_unique_ids():
    payload = finding()
    payload["evidence"][0]["extra"] = "nope"
    with pytest.raises(ContractError, match="fields"):
        validate_contract("finding", payload)

    payload = finding()
    duplicate = copy.deepcopy(payload["evidence"][0])
    payload["evidence"].append(duplicate)
    with pytest.raises(ContractError, match="evidence_id"):
        validate_contract("finding", payload)


def test_finding_rejects_legacy_and_arbitrary_top_level_keys():
    for key in ("result", "severity", "evidence_refs", "random"):
        payload = finding()
        payload[key] = "legacy"
        with pytest.raises(ContractError, match="fields"):
            validate_contract("finding", payload)



def test_finding_requires_locator_or_valid_sha256():
    payload = finding()
    record = payload["evidence"][0]
    record["locator"] = None
    record["sha256"] = "not-a-sha"
    with pytest.raises(ContractError, match="locator|sha256"):
        validate_contract("finding", payload)

    record["sha256"] = "a" * 64
    validate_contract("finding", payload)

def test_finding_allows_empty_report_grain_and_nullable_optional_fields():
    payload = finding()
    record = payload["evidence"][0]
    record.update(
        locator=None,
        sha256="a" * 64,
        query_id=None,
        report_id=None,
        window=None,
        report_grain=[],
        input_field=None,
        redacted_value=None,
        observation_ref="obs-1",
    )
    validate_contract("finding", payload)


def test_finding_validation_does_not_mutate_payload():
    payload = finding()
    original = copy.deepcopy(payload)
    validate_contract("finding", payload)
    assert payload == original


def test_snapshot_rejects_unavailable_field_with_wrong_missing_fields_state():
    payload = account_snapshot()
    context = payload["measurement_context"]
    context["timezone"] = None
    with pytest.raises(ContractError, match="missing_fields"):
        validate_contract("account-snapshot", payload)

def test_manifest_requires_timezone_aware_started_at():
    payload = run_manifest()
    payload["started_at"] = "2026-07-11T16:00:00"
    with pytest.raises(ContractError, match="UTC offset"):
        validate_contract("run-manifest", payload)


def test_manifest_requires_matching_data_lifecycle_classification():
    payload = run_manifest()
    payload["data_lifecycle"]["classification"] = "internal"
    with pytest.raises(ContractError, match="must match"):
        validate_contract("run-manifest", payload)


@pytest.mark.parametrize("invalid", [0.5, 0.0, True, False, "0", None, -1])
def test_run_manifest_embedded_lifecycle_requires_strict_nonnegative_integer_retention(
    invalid
):
    payload = run_manifest()
    payload["data_lifecycle"]["retention"]["minimum_seconds"] = invalid
    with pytest.raises(ContractError, match="integer|must be >="):
        validate_contract("run-manifest", payload)


def test_pass_or_fail_requires_evidence():
    payload = finding()
    payload["evidence"] = []
    with pytest.raises(ContractError, match="must not be empty"):
        validate_contract("finding", payload)


def test_unknown_may_have_no_evidence():
    payload = finding()
    payload.update(status="unknown", evidence=[], confidence="none")
    validate_contract("finding", payload)


def test_finding_schema_engine_requires_evidence_for_pass_or_fail():
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(v2_schema("finding"))

    for status in ("pass", "fail"):
        payload = finding()
        payload.update(status=status, evidence=[])
        assert not validator.is_valid(payload)

    payload = finding()
    payload.update(status="unknown", evidence=[], confidence="none")
    assert validator.is_valid(payload)


@pytest.mark.parametrize("classification", ["evidence_based", "practitioner", "contested", "folklore"])
def test_finding_accepts_source_classification_independently_of_confidence(classification: str):
    payload = finding()
    payload["source_classification"] = classification
    payload["confidence"] = "low"
    validate_contract("finding", payload)


def test_finding_rejects_unknown_source_classification():
    payload = finding()
    payload["source_classification"] = "high"
    with pytest.raises(ContractError, match="source_classification"):
        validate_contract("finding", payload)


def test_finding_requires_source_classification():
    payload = finding()
    del payload["source_classification"]
    with pytest.raises(ContractError, match="source_classification"):
        validate_contract("finding", payload)


def test_finding_rejects_score_contribution():
    payload = finding()
    payload["score_contribution"] = 1.0
    with pytest.raises(ContractError, match="fields"):
        validate_contract("finding", payload)

def test_v2_account_snapshot_schema_has_exact_contract_shape():
    schema = v2_schema("account-snapshot")
    assert schema["$id"] == "urn:ai-marketing-hub:claude-ads:schema:core:v2:account-snapshot.schema.json"
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["schema_version"] == {"const": "2.0.0"}
    assert schema["required"] == [
        "schema_version",
        "account",
        "window",
        "currency",
        "measurement_context",
        "campaigns",
        "creatives",
        "conversions",
        "budgets",
    ]

    context = schema["properties"]["measurement_context"]
    assert context["additionalProperties"] is False
    assert context["required"] == [
        "timezone",
        "currency",
        "profile_id",
        "source_format",
        "source_ids",
        "report_grain",
        "conversion_definition",
        "conversion_actions",
        "attribution_model",
        "click_attribution_window",
        "view_attribution_window",
        "counting_behavior",
        "as_of",
        "data_finalization",
        "modeled_data_treatment",
        "missing_fields",
        "unsupported_fields",
    ]

    assert set(context["properties"]) == set(context["required"])
    assert context["properties"]["source_ids"]["uniqueItems"] is True
    assert context["properties"]["report_grain"]["minItems"] == 1
    assert context["properties"]["report_grain"]["uniqueItems"] is True
    assert context["properties"]["conversion_actions"]["uniqueItems"] is True
    assert context["properties"]["unsupported_fields"]["uniqueItems"] is True
    assert context["properties"]["unsupported_fields"]["items"] == {
        "type": "string",
        "minLength": 1,
        "enum": [
            "account_name",
            "campaign_name",
            "campaign_status",
            "creative_id",
            "creative_name",
            "conversion_action",
            "conversions",
            "budget",
        ],
    }
    assert context["properties"]["unsupported_fields"]["$comment"].startswith("Sorted")
    assert context["properties"]["missing_fields"]["uniqueItems"] is True
    assert context["properties"]["missing_fields"]["$comment"].startswith("Sorted")
    for name in ("click_attribution_window", "view_attribution_window"):
        window = context["properties"][name]
        assert window["type"] == ["object", "null"]
        assert window["properties"]["value"] == {"type": "integer", "minimum": 0}
        assert window["properties"]["unit"] == {"enum": ["hour", "day"]}
        assert window["required"] == ["value", "unit"]
        assert window["additionalProperties"] is False

    campaign = schema["properties"]["campaigns"]["items"]
    assert campaign["additionalProperties"] is False
    assert campaign["required"] == ["campaign_id"]
    assert set(campaign["properties"]) == {"campaign_id", "name", "status", "policy_status", "spend"}
    assert campaign["properties"]["campaign_id"] == {"type": "string", "minLength": 1}
    for field in ("name", "status", "policy_status"):
        assert campaign["properties"][field] == {"type": "string", "minLength": 1}
    assert campaign["properties"]["spend"] == {"type": "number", "minimum": 0}

    creative = schema["properties"]["creatives"]["items"]
    assert creative["additionalProperties"] is False
    assert creative["required"] == ["creative_id", "campaign_id"]
    assert set(creative["properties"]) == {"creative_id", "campaign_id", "name"}
    conversion = schema["properties"]["conversions"]["items"]
    assert conversion["additionalProperties"] is False
    assert conversion["required"] == ["action"]
    assert set(conversion["properties"]) == {"action", "count", "status"}
    assert conversion["properties"]["action"] == {"type": "string", "minLength": 1}
    assert conversion["properties"]["count"] == {"type": "number", "minimum": 0}
    assert conversion["properties"]["status"] == {"type": "string", "minLength": 1}
    assert conversion["anyOf"] == [{"required": ["count"]}, {"required": ["status"]}]

    budget = schema["properties"]["budgets"]["items"]
    assert budget["additionalProperties"] is False
    assert budget["required"] == ["campaign_id", "date", "amount"]
    assert set(budget["properties"]) == {"campaign_id", "date", "amount"}
    assert budget["properties"]["campaign_id"] == {"type": "string", "minLength": 1}
    assert budget["properties"]["date"] == {"type": "string", "format": "date"}
    assert budget["properties"]["amount"] == {"type": "number", "minimum": 0}


def test_v2_finding_schema_has_exact_evidence_record_shape():
    schema = v2_schema("finding")
    assert schema["$id"] == "urn:ai-marketing-hub:claude-ads:schema:core:v2:finding.schema.json"
    assert schema["properties"]["schema_version"] == {"const": "2.0.0"}
    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "schema_version",
        "control_id",
        "status",
        "evidence",
        "confidence",
        "source_classification",
        "observation",
        "diagnosis",
        "recommendation",
    ]
    assert schema["allOf"] == [
        {
            "if": {"properties": {"status": {"enum": ["pass", "fail"]}}},
            "then": {"properties": {"evidence": {"minItems": 1}}},
        }
    ]

    evidence = schema["properties"]["evidence"]
    record = evidence["items"]
    assert evidence["type"] == "array"
    assert record["additionalProperties"] is False
    assert record["required"] == [
        "evidence_id",
        "proof_kind",
        "source_id",
        "locator",
        "sha256",
        "observed_at",
        "query_id",
        "report_id",
        "window",
        "report_grain",
        "input_field",
        "redacted_value",
        "observation_ref",
    ]
    assert set(record["properties"]) == set(record["required"])
    assert record["properties"]["proof_kind"]["enum"] == [
        "observation",
        "source_fact",
        "vendor_claim",
        "inference",
    ]
    assert record["properties"]["sha256"] == {
        "type": ["string", "null"],
        "pattern": "^[0-9a-f]{64}$",
    }
    assert record["properties"]["observed_at"] == {"type": "string", "format": "date-time"}
    assert "minItems" not in record["properties"]["report_grain"]
    assert record["properties"]["report_grain"]["uniqueItems"] is True
    constraints = record["allOf"]
    provenance = constraints[0]["anyOf"]
    assert provenance == [
        {"properties": {"locator": {"type": "string", "minLength": 1}}},
        {"properties": {"sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}},
    ]
    assert constraints[1]["anyOf"] == [
        {"properties": {"redacted_value": {"not": {"type": "null"}}}},
        {"properties": {"observation_ref": {"type": "string", "minLength": 1}}},
    ]
    date_window = record["properties"]["window"]
    assert date_window["type"] == ["object", "null"]
    assert date_window["additionalProperties"] is False
    assert date_window["required"] == ["start", "end"]
    assert date_window["properties"]["start"] == {"type": "string", "format": "date"}
    assert date_window["properties"]["end"] == {"type": "string", "format": "date"}


def test_report_bundle_schema_requires_exact_category_score_fields():
    schema = v2_schema("report-bundle")
    category = schema["properties"]["scoring"]["properties"]["categories"]["items"]
    expected_fields = {
        "category",
        "category_weight",
        "health_score",
        "evidence_coverage",
        "applicable_controls",
        "known_controls",
        "passed_controls",
        "failed_controls",
        "unknown_controls",
    }
    assert category["type"] == "object"
    assert set(category["required"]) == expected_fields
    assert set(category["properties"]) == expected_fields
    assert category["additionalProperties"] is False
    for field in ("category_weight", "evidence_coverage"):
        assert category["properties"][field] == {
            "type": "number",
            "minimum": 0,
            "maximum": 100,
        }
    assert category["properties"]["health_score"] == {
        "type": ["number", "null"],
        "minimum": 0,
        "maximum": 100,
    }
    for field in (
        "applicable_controls",
        "known_controls",
        "passed_controls",
        "failed_controls",
        "unknown_controls",
    ):
        assert category["properties"][field] == {"type": "integer", "minimum": 0}


@pytest.mark.parametrize(
    "field",
    ["applicable_controls", "known_controls", "passed_controls", "failed_controls", "unknown_controls"],
)
def test_report_rejects_category_missing_diagnostic_count(field: str):
    payload = report_bundle()
    payload["scoring"]["categories"][0].pop(field)
    with pytest.raises(ContractError, match=field):
        validate_contract("report-bundle", payload)


def test_report_rejects_category_extra_diagnostic_key():
    payload = report_bundle()
    payload["scoring"]["categories"][0]["diagnostics"] = {}
    with pytest.raises(ContractError, match="exactly"):
        validate_contract("report-bundle", payload)


@pytest.mark.parametrize(
    "value",
    [True, False, -1, 1.5, "1"],
)
def test_report_rejects_invalid_category_diagnostic_count_types(value: Any):
    payload = report_bundle()
    payload["scoring"]["categories"][0]["known_controls"] = value
    with pytest.raises(ContractError, match="known_controls"):
        validate_contract("report-bundle", payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("known_controls", 2),
        ("applicable_controls", 2),
        ("passed_controls", 2),
        ("failed_controls", 1),
        ("unknown_controls", 1),
    ],
)
def test_report_rejects_category_count_invariant_violations(field: str, value: int):
    payload = report_bundle()
    payload["scoring"]["categories"][0][field] = value
    with pytest.raises(ContractError, match="count invariant"):
        validate_contract("report-bundle", payload)


def test_report_rejects_duplicate_category_names():
    payload = report_bundle()
    payload["scoring"]["categories"].append(category_score())
    with pytest.raises(ContractError, match="duplicate"):
        validate_contract("report-bundle", payload)


def test_report_requires_nullable_health_score_for_categories_without_known_controls():
    payload = report_bundle()
    payload["scoring"]["categories"][0].update(
        applicable_controls=1,
        known_controls=0,
        passed_controls=0,
        failed_controls=0,
        unknown_controls=1,
        health_score=100.0,
    )
    with pytest.raises(ContractError, match="health_score"):
        validate_contract("report-bundle", payload)


def test_report_requires_numeric_health_score_for_categories_with_known_controls():
    payload = report_bundle()
    payload["scoring"]["categories"][0]["health_score"] = None
    with pytest.raises(ContractError, match="health_score"):
        validate_contract("report-bundle", payload)


def test_v2_report_bundle_mixes_v1_and_v2_references_and_gates_complete_runs():
    schema = v2_schema("report-bundle")
    assert schema["$id"] == "urn:ai-marketing-hub:claude-ads:schema:core:v2:report-bundle.schema.json"
    assert schema["properties"]["schema_version"] == {"const": "2.0.0"}
    properties = schema["properties"]
    assert properties["run_manifest"]["$ref"].endswith(":v1:run-manifest.schema.json")
    assert properties["control_definitions"]["items"]["$ref"].endswith(":v1:control-definition.schema.json")
    assert properties["account_snapshot"]["$ref"].endswith(":v2:account-snapshot.schema.json")
    assert properties["findings"]["items"]["$ref"].endswith(":v2:finding.schema.json")

    condition = schema["allOf"][0]
    assert condition["if"]["properties"]["run_manifest"]["properties"]["completeness"] == {
        "const": "complete"
    }
    then = condition["then"]["properties"]
    assert then["control_definitions"]["minItems"] == 1
    assert then["findings"]["minItems"] == 1
    assert then["run_manifest"]["properties"]["worker_status"]["minProperties"] == 1
    assert then["run_manifest"]["properties"]["worker_status"]["additionalProperties"] == {
        "const": "completed"
    }
    assert then["scoring"]["properties"]["status"]["not"] == {"const": "insufficient_evidence"}


def test_v2_typed_dicts_expose_exact_fields_and_schema_literals():
    attribution = get_type_hints(contract_models.AttributionWindow)
    assert set(attribution) == {"value", "unit"}
    assert get_args(attribution["unit"]) == ("hour", "day")
    campaign = get_type_hints(CampaignRow)
    assert set(campaign) == {"campaign_id", "name", "status", "policy_status", "spend"}
    assert get_type_hints(CreativeRow) == {
        "creative_id": str,
        "campaign_id": str,
        "name": str,
    }
    assert get_type_hints(ConversionRow) == {
        "action": str,
        "count": float,
        "status": str,
    }
    assert get_type_hints(BudgetRow) == {
        "campaign_id": str,
        "date": str,
        "amount": float,
    }

    context = get_type_hints(contract_models.MeasurementContext)
    assert set(context) == {
        "timezone",
        "currency",
        "profile_id",
        "source_format",
        "source_ids",
        "report_grain",
        "conversion_definition",
        "conversion_actions",
        "attribution_model",
        "click_attribution_window",
        "view_attribution_window",
        "counting_behavior",
        "as_of",
        "data_finalization",
        "modeled_data_treatment",
        "missing_fields",
        "unsupported_fields",
    }
    evidence = get_type_hints(contract_models.EvidenceRecord)
    assert set(evidence) == {
        "evidence_id",
        "proof_kind",
        "source_id",
        "locator",
        "sha256",
        "observed_at",
        "query_id",
        "report_id",
        "window",
        "report_grain",
        "input_field",
        "redacted_value",
        "observation_ref",
    }
    assert set(get_type_hints(Finding)) == {
        "schema_version",
        "control_id",
        "status",
        "evidence",
        "confidence",
        "source_classification",
        "observation",
        "diagnosis",
        "recommendation",
    }
    assert get_args(get_type_hints(AccountSnapshot)["schema_version"]) == ("2.0.0",)
    assert get_args(get_type_hints(Finding)["schema_version"]) == ("2.0.0",)
    assert get_args(get_type_hints(ReportBundle)["schema_version"]) == ("2.0.0",)
    assert get_args(get_type_hints(RunManifest)["schema_version"]) == ("1.0.0",)
    assert get_type_hints(contract_models.EvidenceRecord)["redacted_value"] == Any | None
    assert get_args(get_type_hints(ControlDefinition)["schema_version"]) == ("1.0.0",)
    assert set(get_type_hints(RunManifest)) == {
        "schema_version",
        "run_id",
        "started_at",
        "scopes",
        "adapters",
        "sources",
        "privacy_class",
        "data_lifecycle",
        "worker_status",
        "completeness",
    }
    assert set(get_type_hints(ControlDefinition)) == {
        "schema_version",
        "control_id",
        "category",
        "severity",
        "required_inputs",
        "source_ids",
        "maturity",
        "geographies",
        "expires_at",
        "scoring_behavior",
        "stability",
    }
    assert get_type_hints(Finding)["evidence"] == list[contract_models.EvidenceRecord]
    assert get_type_hints(AccountSnapshot)["measurement_context"] is contract_models.MeasurementContext

    snapshot = get_type_hints(AccountSnapshot)
    assert snapshot["campaigns"] == list[CampaignRow]
    assert snapshot["creatives"] == list[CreativeRow]
    assert snapshot["conversions"] == list[ConversionRow]
    assert snapshot["budgets"] == list[BudgetRow]

def test_category_score_output_typed_dict_exposes_exact_fields():
    category = get_type_hints(CategoryScoreOutput)
    assert set(category) == {
        "category",
        "category_weight",
        "health_score",
        "evidence_coverage",
        "applicable_controls",
        "known_controls",
        "passed_controls",
        "failed_controls",
        "unknown_controls",
    }
    assert category["category"] is str
    assert category["category_weight"] is float
    assert category["health_score"] == float | None
    assert category["evidence_coverage"] is float
    for field in (
        "applicable_controls",
        "known_controls",
        "passed_controls",
        "failed_controls",
        "unknown_controls",
    ):
        assert category[field] is int
    assert get_type_hints(ReportBundle)["scoring"] is contract_models.ScoringOutput
    assert get_type_hints(contract_models.ScoringOutput)["categories"] == list[CategoryScoreOutput]


def test_report_bundle_recursively_validates_nested_contracts():
    payload = report_bundle()
    payload["account_snapshot"]["account"]["platform"] = "unsupported"
    with pytest.raises(ContractError, match="platform"):
        validate_contract("report-bundle", payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("control_definitions", []),
        ("findings", []),
        ("run_manifest", {**run_manifest(), "worker_status": {}}),
        ("run_manifest", {**run_manifest(), "worker_status": {"google": "failed"}}),
        ("scoring", {**report_bundle()["scoring"], "status": "insufficient_evidence"}),
    ],
)
def test_complete_report_rejects_incomplete_or_insufficient_state(field: str, value: dict | list):
    payload = report_bundle()
    payload[field] = value
    with pytest.raises(ContractError):
        validate_contract("report-bundle", payload)


def test_complete_report_allows_provisional_scoring():
    payload = report_bundle()
    payload["scoring"]["status"] = "provisional"
    validate_contract("report-bundle", payload)
