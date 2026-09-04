"""Versioned contract validation with no runtime dependencies.

The JSON Schema files are the portable contract.  This module provides strict
semantic validation for the current fields used by the deterministic engine so the
CLI remains useful in environments where a JSON Schema library is unavailable.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .workflow_contracts import (
    WORKFLOW_CONTRACT_NAMES,
    WorkflowContractError,
    validate_workflow_contract,
)

CURRENT_CONTRACT_VERSIONS = {
    "account-snapshot": "2.0.0",
    "run-manifest": "1.0.0",
    "control-definition": "1.0.0",
    "finding": "2.0.0",
    "report-bundle": "2.0.0",
}
CURRENT_SCHEMA_DIRECTORIES = {
    "account-snapshot": "v2",
    "run-manifest": "v1",
    "control-definition": "v1",
    "finding": "v2",
    "report-bundle": "v2",
}
CORE_CONTRACT_NAMES = tuple(CURRENT_CONTRACT_VERSIONS)
CONTRACT_NAMES = CORE_CONTRACT_NAMES + WORKFLOW_CONTRACT_NAMES
PLATFORMS = {
    "google",
    "meta",
    "youtube",
    "linkedin",
    "tiktok",
    "microsoft",
    "apple",
    "amazon",
    "reddit",
    "pinterest",
    "snapchat",
    "x",
}
FINDING_STATUSES = {"pass", "fail", "unknown", "not_applicable"}
SEVERITIES = {"critical", "high", "medium", "informational"}
UNSUPPORTED_FIELDS = {
    "account_name",
    "campaign_name",
    "campaign_status",
    "creative_id",
    "creative_name",
    "conversion_action",
    "conversions",
    "budget",
}


class ContractError(ValueError):
    """Raised when a payload does not satisfy its versioned contract."""


def _require_object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{path} must be an object")
    return value


def _require_string(value: Any, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ContractError(f"{path} must be a non-empty string")
    return value


def _require_enum(value: Any, path: str, choices: set[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ContractError(f"{path} is invalid")
    return value


def _require_number(value: Any, path: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{path} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ContractError(f"{path} must be finite")
    if minimum is not None and result < minimum:
        raise ContractError(f"{path} must be >= {minimum}")
    return result


def _require_nonnegative_integer(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{path} must be a nonnegative integer")
    if value < 0:
        raise ContractError(f"{path} must be a nonnegative integer")
    return value


def _require_list(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ContractError(f"{path} must be an array")
    return value


def _require_keys(payload: Mapping[str, Any], keys: Sequence[str], path: str = "$") -> None:
    missing = [key for key in keys if key not in payload]
    if missing:
        raise ContractError(f"{path} missing required field(s): {', '.join(missing)}")

def _require_exact_keys(
    payload: Mapping[str, Any],
    required: Sequence[str],
    optional: Sequence[str],
    path: str,
) -> None:
    _require_keys(payload, required, path)
    extras = sorted(set(payload) - (set(required) | set(optional)))
    if extras:
        raise ContractError(f"{path} must have exactly the contract fields; unexpected: {', '.join(extras)}")


def _validate_canonical_date(value: Any, path: str) -> None:
    text = _require_string(value, path)
    parsed = _validate_date(text, path)
    if parsed.isoformat() != text:
        raise ContractError(f"{path} must be an ISO 8601 date")


def _validate_campaign_row(value: Any, path: str) -> None:
    row = _require_object(value, path)
    _require_exact_keys(row, ("campaign_id",), ("name", "status", "policy_status", "spend"), path)
    _require_string(row["campaign_id"], f"{path}.campaign_id")
    for field in ("name", "status", "policy_status"):
        if field in row:
            _require_string(row[field], f"{path}.{field}")
    if "spend" in row:
        _require_number(row["spend"], f"{path}.spend", minimum=0)


def _validate_creative_row(value: Any, path: str) -> None:
    row = _require_object(value, path)
    _require_exact_keys(row, ("creative_id", "campaign_id"), ("name",), path)
    _require_string(row["creative_id"], f"{path}.creative_id")
    _require_string(row["campaign_id"], f"{path}.campaign_id")
    if "name" in row:
        _require_string(row["name"], f"{path}.name")


def _validate_conversion_row(value: Any, path: str) -> None:
    row = _require_object(value, path)
    _require_exact_keys(row, ("action",), ("count", "status"), path)
    _require_string(row["action"], f"{path}.action")
    has_count = "count" in row
    has_status = "status" in row
    if not has_count and not has_status:
        raise ContractError(f"{path} must include at least one of count or status")
    if has_count:
        _require_number(row["count"], f"{path}.count", minimum=0)
    if has_status:
        _require_string(row["status"], f"{path}.status")

def _validate_budget_row(value: Any, path: str) -> None:
    row = _require_object(value, path)
    _require_exact_keys(row, ("campaign_id", "date", "amount"), (), path)
    _require_string(row["campaign_id"], f"{path}.campaign_id")
    _validate_canonical_date(row["date"], f"{path}.date")
    _require_number(row["amount"], f"{path}.amount", minimum=0)

def _validate_version(payload: Mapping[str, Any], expected_version: str) -> None:
    if payload.get("schema_version") != expected_version:
        raise ContractError(f"$.schema_version must equal {expected_version!r}")


def _validate_date(value: Any, path: str) -> date:
    text = _require_string(value, path)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ContractError(f"{path} must be an ISO 8601 date") from exc


def _validate_datetime(value: Any, path: str) -> None:
    text = _require_string(value, path)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{path} must be an ISO 8601 date-time") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{path} must include a UTC offset")


def _require_unique_strings(value: Any, path: str, *, nonempty: bool = True) -> list[str]:
    values = _require_list(value, path)
    result = [_require_string(item, f"{path}[{index}]", nonempty=nonempty) for index, item in enumerate(values)]
    if len(result) != len(set(result)):
        raise ContractError(f"{path} must not contain duplicate values")
    return result


def _validate_attribution_window(value: Any, path: str) -> None:
    if value is None:
        return
    window = _require_object(value, path)
    if set(window) != {"value", "unit"}:
        raise ContractError(f"{path} must have exactly value and unit")
    _require_number(window["value"], f"{path}.value", minimum=0)
    if not isinstance(window["value"], int) or isinstance(window["value"], bool):
        raise ContractError(f"{path}.value must be an integer")
    _require_enum(window["unit"], f"{path}.unit", {"hour", "day"})


def _validate_measurement_context(payload: Mapping[str, Any], currency: str, window_end: date) -> None:
    fields = (
        "timezone", "currency", "profile_id", "source_format", "source_ids", "report_grain",
        "conversion_definition", "conversion_actions", "attribution_model", "click_attribution_window",
        "view_attribution_window", "counting_behavior", "as_of", "data_finalization",
        "modeled_data_treatment", "missing_fields", "unsupported_fields",
    )
    context = _require_object(payload, "$.measurement_context")
    if set(context) != set(fields):
        raise ContractError("$.measurement_context must have exactly the required fields")
    timezone = context["timezone"]
    if timezone is not None:
        _require_string(timezone, "$.measurement_context.timezone")
    if context["currency"] != currency:
        raise ContractError("$.measurement_context.currency must match $.currency")
    if not re.fullmatch(r"[A-Z]{3}", _require_string(context["currency"], "$.measurement_context.currency")):
        raise ContractError("$.measurement_context.currency must be a three-letter uppercase currency code")
    for field in ("profile_id", "source_format"):
        _require_string(context[field], f"$.measurement_context.{field}")
    source_ids = _require_unique_strings(context["source_ids"], "$.measurement_context.source_ids")
    report_grain = _require_unique_strings(context["report_grain"], "$.measurement_context.report_grain")
    if not report_grain:
        raise ContractError("$.measurement_context.report_grain must not be empty")
    for field in ("conversion_definition", "attribution_model", "counting_behavior"):
        if context[field] is not None:
            _require_string(context[field], f"$.measurement_context.{field}")
    conversion_actions = _require_unique_strings(context["conversion_actions"], "$.measurement_context.conversion_actions")
    unsupported_fields = _require_unique_strings(context["unsupported_fields"], "$.measurement_context.unsupported_fields")
    if set(unsupported_fields) - UNSUPPORTED_FIELDS or unsupported_fields != sorted(unsupported_fields):
        raise ContractError("$.measurement_context.unsupported_fields must contain sorted values from the native unsupported field vocabulary")
    for field in ("click_attribution_window", "view_attribution_window"):
        _validate_attribution_window(context[field], f"$.measurement_context.{field}")
    as_of = _validate_date(context["as_of"], "$.measurement_context.as_of")
    if as_of < window_end:
        raise ContractError("$.measurement_context.as_of must be on or after $.window.end")
    _require_enum(context["data_finalization"], "$.measurement_context.data_finalization", {"final", "provisional", "unknown"})
    _require_enum(context["modeled_data_treatment"], "$.measurement_context.modeled_data_treatment", {"included", "excluded", "mixed", "unknown"})
    missing_fields = _require_unique_strings(context["missing_fields"], "$.measurement_context.missing_fields")
    unavailable = {
        field for field, available in (
            ("timezone", timezone is not None),
            ("source_ids", bool(source_ids)),
            ("conversion_definition", context["conversion_definition"] is not None),
            ("conversion_actions", bool(conversion_actions)),
            ("attribution_model", context["attribution_model"] is not None),
            ("click_attribution_window", context["click_attribution_window"] is not None),
            ("view_attribution_window", context["view_attribution_window"] is not None),
            ("counting_behavior", context["counting_behavior"] is not None),
            ("data_finalization", context["data_finalization"] != "unknown"),
            ("modeled_data_treatment", context["modeled_data_treatment"] != "unknown"),
        ) if not available
    }
    if set(missing_fields) != unavailable or missing_fields != sorted(missing_fields):
        raise ContractError("$.measurement_context.missing_fields must equal the sorted unavailable field set")
    if set(missing_fields) & {"profile_id", "source_format", "report_grain", "currency", "as_of"}:
        raise ContractError("$.measurement_context.missing_fields contains an always-required field")


def _validate_account_snapshot(payload: Mapping[str, Any]) -> None:
    _validate_version(payload, CURRENT_CONTRACT_VERSIONS["account-snapshot"])
    _require_keys(
        payload,
        (
            "schema_version",
            "account",
            "window",
            "currency",
            "measurement_context",
            "campaigns",
            "creatives",
            "conversions",
            "budgets",
        ),
    )
    account = _require_object(payload["account"], "$.account")
    _require_keys(account, ("platform", "account_id"), "$.account")
    platform = _require_string(account["platform"], "$.account.platform")
    if platform not in PLATFORMS:
        raise ContractError(f"$.account.platform must be one of: {', '.join(sorted(PLATFORMS))}")
    _require_string(account["account_id"], "$.account.account_id")
    window = _require_object(payload["window"], "$.window")
    _require_keys(window, ("start", "end"), "$.window")
    start = _validate_date(window["start"], "$.window.start")
    end = _validate_date(window["end"], "$.window.end")
    if end < start:
        raise ContractError("$.window.end must be on or after $.window.start")
    currency = _require_string(payload["currency"], "$.currency")
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ContractError("$.currency must be a three-letter uppercase currency code")
    _validate_measurement_context(payload["measurement_context"], currency, end)
    if "spend" in payload and payload["spend"] is not None:
        _require_number(payload["spend"], "$.spend", minimum=0)
    row_validators = {
        "campaigns": _validate_campaign_row,
        "creatives": _validate_creative_row,
        "conversions": _validate_conversion_row,
        "budgets": _validate_budget_row,
    }
    for field, validator in row_validators.items():
        items = _require_list(payload[field], f"$.{field}")
        for index, item in enumerate(items):
            validator(item, f"$.{field}[{index}]")


def _validate_run_manifest(payload: Mapping[str, Any]) -> None:
    _require_keys(
        payload,
        ("schema_version", "run_id", "started_at", "scopes", "adapters", "sources", "privacy_class", "data_lifecycle", "worker_status", "completeness"),
    )
    _validate_version(payload, CURRENT_CONTRACT_VERSIONS["run-manifest"])
    _require_string(payload["run_id"], "$.run_id")
    _validate_datetime(payload["started_at"], "$.started_at")
    for field in ("scopes", "sources"):
        values = _require_list(payload[field], f"$.{field}")
        for index, value in enumerate(values):
            _require_string(value, f"$.{field}[{index}]")
    adapters = _require_list(payload["adapters"], "$.adapters")
    for index, adapter_value in enumerate(adapters):
        adapter = _require_object(adapter_value, f"$.adapters[{index}]")
        _require_keys(adapter, ("platform", "mode"), f"$.adapters[{index}]")
        if _require_string(adapter["platform"], f"$.adapters[{index}].platform") not in PLATFORMS:
            raise ContractError(f"$.adapters[{index}].platform is unsupported")
        if adapter["mode"] not in {"export", "live_read", "write_preview", "write_apply"}:
            raise ContractError(f"$.adapters[{index}].mode is invalid")
    if payload["privacy_class"] not in {"public", "internal", "confidential", "restricted"}:
        raise ContractError("$.privacy_class is invalid")
    try:
        validate_workflow_contract("data-lifecycle", payload["data_lifecycle"])
    except WorkflowContractError as exc:
        raise ContractError(f"$.data_lifecycle: {exc}") from exc
    lifecycle = _require_object(payload["data_lifecycle"], "$.data_lifecycle")
    if lifecycle.get("classification") != payload["privacy_class"]:
        raise ContractError("$.privacy_class must match $.data_lifecycle.classification")
    statuses = _require_object(payload["worker_status"], "$.worker_status")
    for worker, status in statuses.items():
        _require_string(worker, "$.worker_status key")
        if status not in {"pending", "running", "completed", "failed", "skipped"}:
            raise ContractError(f"$.worker_status.{worker} is invalid")
    if payload["completeness"] not in {"complete", "partial", "failed"}:
        raise ContractError("$.completeness is invalid")


def _validate_control_definition(payload: Mapping[str, Any]) -> None:
    _require_keys(
        payload,
        (
            "schema_version",
            "control_id",
            "category",
            "severity",
            "required_inputs",
            "source_ids",
            "maturity",
            "geographies",
            "scoring_behavior",
            "stability",
        ),
    )
    _validate_version(payload, CURRENT_CONTRACT_VERSIONS["control-definition"])
    for field in ("control_id", "category"):
        _require_string(payload[field], f"$.{field}")
    if payload["severity"] not in SEVERITIES:
        raise ContractError(f"$.severity must be one of: {', '.join(sorted(SEVERITIES))}")
    for field in ("required_inputs", "source_ids", "geographies"):
        values = _require_list(payload[field], f"$.{field}")
        for index, value in enumerate(values):
            _require_string(value, f"$.{field}[{index}]")
    if payload["maturity"] not in {
        "inventory-baselined",
        "source-grounded",
        "domain-integrated",
        "eval-verified",
        "release-ready",
    }:
        raise ContractError("$.maturity is invalid")
    if payload["scoring_behavior"] not in {"health", "opportunity", "watchlist"}:
        raise ContractError("$.scoring_behavior is invalid")
    if payload["stability"] not in {"stable", "experimental"}:
        raise ContractError("$.stability is invalid")
    if "expires_at" in payload and payload["expires_at"] is not None:
        _validate_date(payload["expires_at"], "$.expires_at")


def _validate_evidence_record(value: Any, path: str, evidence_ids: set[str]) -> None:
    record = _require_object(value, path)
    fields = (
        "evidence_id", "proof_kind", "source_id", "locator", "sha256", "observed_at",
        "query_id", "report_id", "window", "report_grain", "input_field",
        "redacted_value", "observation_ref",
    )
    if set(record) != set(fields):
        raise ContractError(f"{path} must have exactly the required fields")
    evidence_id = _require_string(record["evidence_id"], f"{path}.evidence_id")
    if evidence_id in evidence_ids:
        raise ContractError(f"{path}.evidence_id must be unique")
    evidence_ids.add(evidence_id)
    _require_enum(record["proof_kind"], f"{path}.proof_kind", {"observation", "source_fact", "vendor_claim", "inference"})
    _require_string(record["source_id"], f"{path}.source_id")
    locator = record["locator"]
    if locator is not None:
        _require_string(locator, f"{path}.locator")
    sha256 = record["sha256"]
    if sha256 is not None and (not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256)):
        raise ContractError(f"{path}.sha256 must be a lowercase SHA-256 digest")
    if locator is None and sha256 is None:
        raise ContractError(f"{path} must provide locator or sha256")
    _validate_datetime(record["observed_at"], f"{path}.observed_at")
    for field in ("query_id", "report_id", "input_field", "observation_ref"):
        if record[field] is not None:
            _require_string(record[field], f"{path}.{field}")
    if record["window"] is not None:
        window = _require_object(record["window"], f"{path}.window")
        if set(window) != {"start", "end"}:
            raise ContractError(f"{path}.window must have exactly start and end")
        start = _validate_date(window["start"], f"{path}.window.start")
        end = _validate_date(window["end"], f"{path}.window.end")
        if end < start:
            raise ContractError(f"{path}.window.end must be on or after start")
    _require_unique_strings(record["report_grain"], f"{path}.report_grain")
    if record["redacted_value"] is None and record["observation_ref"] is None:
        raise ContractError(f"{path} must provide redacted_value or observation_ref")


def _validate_finding(
    payload: Mapping[str, Any],
    *,
    evidence_ids: set[str] | None = None,
    evidence_path: str = "$.evidence",
) -> None:
    _validate_version(payload, CURRENT_CONTRACT_VERSIONS["finding"])
    required_fields = (
        "schema_version", "control_id", "status", "evidence", "confidence",
        "source_classification", "observation", "diagnosis", "recommendation",
    )
    if set(payload) - set(required_fields):
        raise ContractError("$.finding must have exactly the required fields")
    _require_keys(payload, required_fields)
    _require_string(payload["control_id"], "$.control_id")
    _require_enum(payload["status"], "$.status", FINDING_STATUSES)
    evidence = _require_list(payload["evidence"], evidence_path)
    evidence_ids = set() if evidence_ids is None else evidence_ids
    for index, item in enumerate(evidence):
        _validate_evidence_record(item, f"{evidence_path}[{index}]", evidence_ids)
    _require_enum(payload["confidence"], "$.confidence", {"high", "medium", "low", "none"})
    _require_enum(payload["source_classification"], "$.source_classification", {"evidence_based", "practitioner", "contested", "folklore"})
    for field in ("observation", "diagnosis", "recommendation"):
        _require_string(payload[field], f"$.{field}", nonempty=False)
    if payload["status"] in {"pass", "fail"} and not evidence:
        raise ContractError("$.evidence must not be empty for pass/fail findings")

def _validate_report_bundle(payload: Mapping[str, Any]) -> None:
    _validate_version(payload, CURRENT_CONTRACT_VERSIONS["report-bundle"])
    _require_keys(payload, ("schema_version", "run_manifest", "account_snapshot", "control_definitions", "findings", "scoring"))
    validate_contract("run-manifest", payload["run_manifest"])
    validate_contract("account-snapshot", payload["account_snapshot"])
    controls = _require_list(payload["control_definitions"], "$.control_definitions")
    findings = _require_list(payload["findings"], "$.findings")
    for item in controls:
        validate_contract("control-definition", item)
    evidence_ids: set[str] = set()
    for index, item in enumerate(findings):
        finding_payload = _require_object(item, f"$.findings[{index}]")
        _validate_finding(
            finding_payload,
            evidence_ids=evidence_ids,
            evidence_path=f"$.findings[{index}].evidence",
        )
    scoring = _require_object(payload["scoring"], "$.scoring")
    _require_keys(scoring, ("health_score", "evidence_coverage", "status", "categories"), "$.scoring")
    if scoring["health_score"] is not None:
        value = _require_number(scoring["health_score"], "$.scoring.health_score", minimum=0)
        if value > 100:
            raise ContractError("$.scoring.health_score must be <= 100")
    coverage = _require_number(scoring["evidence_coverage"], "$.scoring.evidence_coverage", minimum=0)
    if coverage > 100:
        raise ContractError("$.scoring.evidence_coverage must be <= 100")
    _require_enum(scoring["status"], "$.scoring.status", {"normal", "provisional", "insufficient_evidence"})
    categories = _require_list(scoring["categories"], "$.scoring.categories")
    category_fields = (
        "category",
        "category_weight",
        "health_score",
        "evidence_coverage",
        "applicable_controls",
        "known_controls",
        "passed_controls",
        "failed_controls",
        "unknown_controls",
    )
    category_names: set[str] = set()
    for index, item in enumerate(categories):
        path = f"$.scoring.categories[{index}]"
        category = _require_object(item, path)
        if set(category) - set(category_fields):
            raise ContractError(f"{path} must have exactly the category score fields")
        _require_keys(category, category_fields, path)
        name = _require_string(category["category"], f"{path}.category")
        if name in category_names:
            raise ContractError(f"{path}.category must not contain duplicate category names")
        category_names.add(name)
        weight = _require_number(category["category_weight"], f"{path}.category_weight", minimum=0)
        if weight > 100:
            raise ContractError(f"{path}.category_weight must be <= 100")
        if category["health_score"] is None:
            health_score = None
        else:
            health_score = _require_number(category["health_score"], f"{path}.health_score", minimum=0)
            if health_score > 100:
                raise ContractError(f"{path}.health_score must be <= 100")
        coverage = _require_number(category["evidence_coverage"], f"{path}.evidence_coverage", minimum=0)
        if coverage > 100:
            raise ContractError(f"{path}.evidence_coverage must be <= 100")
        counts = {
            field: _require_nonnegative_integer(category[field], f"{path}.{field}")
            for field in (
                "applicable_controls",
                "known_controls",
                "passed_controls",
                "failed_controls",
                "unknown_controls",
            )
        }
        if counts["known_controls"] != counts["passed_controls"] + counts["failed_controls"]:
            raise ContractError(f"{path} count invariant known_controls=passed_controls+failed_controls violated")
        if counts["applicable_controls"] != counts["known_controls"] + counts["unknown_controls"]:
            raise ContractError(f"{path} count invariant applicable_controls=known_controls+unknown_controls violated")
        if counts["known_controls"] == 0 and health_score is not None:
            raise ContractError(f"{path}.health_score must be null when known_controls is 0")
        if counts["known_controls"] > 0 and health_score is None:
            raise ContractError(f"{path}.health_score must be numeric when known_controls is greater than 0")
    manifest = payload["run_manifest"]
    if manifest["completeness"] == "complete":
        statuses = manifest["worker_status"]
        if not controls or not findings or not statuses or any(status != "completed" for status in statuses.values()):
            raise ContractError("$.run_manifest complete requires controls, findings, and completed workers")
        if any(
            statuses.get(adapter["platform"]) != "completed"
            for adapter in manifest["adapters"]
        ):
            raise ContractError("$.run_manifest complete requires completed workers for every adapter platform")
        if scoring["status"] == "insufficient_evidence":
            raise ContractError("$.scoring.status cannot be insufficient_evidence for complete runs")


_VALIDATORS: dict[str, Callable[[Mapping[str, Any]], None]] = {
    "account-snapshot": _validate_account_snapshot,
    "run-manifest": _validate_run_manifest,
    "control-definition": _validate_control_definition,
    "finding": _validate_finding,
    "report-bundle": _validate_report_bundle,
}


def validate_contract(name: str, payload: Any) -> None:
    """Validate *payload* against its current versioned semantic contract."""

    if name in WORKFLOW_CONTRACT_NAMES:
        try:
            validate_workflow_contract(name, payload)
        except WorkflowContractError as exc:
            raise ContractError(str(exc)) from exc
        return
    if name not in _VALIDATORS:
        raise ContractError(f"unknown contract {name!r}; expected one of: {', '.join(CONTRACT_NAMES)}")
    _VALIDATORS[name](_require_object(payload, "$"))


def load_contract(name: str, path: str | Path) -> dict[str, Any]:
    """Load JSON from *path*, validate it, and return the decoded object."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load {path}: {exc}") from exc
    validate_contract(name, payload)
    return payload


def schema_path(name: str) -> Path:
    """Return the installed JSON Schema path for a contract."""

    if name not in CONTRACT_NAMES:
        raise ContractError(f"unknown contract {name!r}")
    return Path(str(files("claude_ads_core").joinpath("schemas", CURRENT_SCHEMA_DIRECTORIES.get(name, "v1"), f"{name}.schema.json")))
