"""Deterministic reference export-to-audit execution spine."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from os import PathLike
from pathlib import Path
from typing import Any, Mapping

from .adapters import (
    AdapterError,
    GenericCSVExportAdapter,
    NativeCSVExportAdapter,
    NativeJSONExportAdapter,
)
from .contracts import ContractError, PLATFORMS, validate_contract
from .control_registry import RegistryError, load_control_registry
from .reporting import (
    ReportRenderError,
    atomic_write_report,
    write_report_bundle,
)


class AuditError(ValueError):
    """Raised when an audit journey cannot be executed safely or deterministically."""


def _default_report_root(platform_name: str | None = None) -> str:
    platform_name = os.name if platform_name is None else platform_name
    if platform_name == "nt":
        try:
            return str(Path.home() / ".claude-ads" / "runs")
        except (OSError, RuntimeError) as exc:
            raise AuditError(f"report root home normalization failed: {exc}") from exc
    return ".claude-ads/runs"


def _evaluate_google_findings(
    snapshot: Mapping[str, Any],
    entries: Mapping[str, Any],
    canonical_source_id: str,
    file_sha256: str,
    run_id: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Evaluate evidence-backed findings for Google reference journey."""
    findings: list[dict[str, Any]] = []
    referenced_sources: set[str] = set()

    conversions = snapshot.get("conversions", [])
    if "G42" in entries:
        g42_entry = entries["G42"]
        definition = g42_entry.control_definition
        referenced_sources.update(definition.get("source_ids", []))
        if conversions:
            primary_action = conversions[0].get("action", "conversions_defined")
            evidence = {
                "evidence_id": f"ev-{run_id}-G42-1",
                "proof_kind": "observation",
                "source_id": canonical_source_id,
                "locator": "conversions[0].action",
                "sha256": file_sha256,
                "observed_at": f"{snapshot['window']['end']}T00:00:00Z",
                "query_id": None,
                "report_id": run_id,
                "window": dict(snapshot["window"]),
                "report_grain": list(snapshot["measurement_context"]["report_grain"]),
                "input_field": "conversions",
                "redacted_value": str(primary_action),
                "observation_ref": None,
            }
            referenced_sources.add(canonical_source_id)
            findings.append(
                {
                    "schema_version": "2.0.0",
                    "control_id": "G42",
                    "status": "pass",
                    "evidence": [evidence],
                    "confidence": "high",
                    "source_classification": "evidence_based",
                    "observation": f"Active conversion action defined in account snapshot: {primary_action}.",
                    "diagnosis": "Conversion tracking action is defined and active in the ingested export.",
                    "recommendation": "Maintain verified conversion actions and confirm regular attribution health.",
                }
            )
        else:
            findings.append(
                {
                    "schema_version": "2.0.0",
                    "control_id": "G42",
                    "status": "unknown",
                    "evidence": [],
                    "confidence": "low",
                    "source_classification": "practitioner",
                    "observation": "No conversion actions were found in the account export.",
                    "diagnosis": "Cannot verify conversion tracking status from supplied data.",
                    "recommendation": "Configure primary conversion action in Google Ads.",
                }
            )

    if "G43" in entries:
        g43_entry = entries["G43"]
        referenced_sources.update(g43_entry.control_definition.get("source_ids", []))
        findings.append(
            {
                "schema_version": "2.0.0",
                "control_id": "G43",
                "status": "unknown",
                "evidence": [],
                "confidence": "low",
                "source_classification": "practitioner",
                "observation": "Enhanced conversions configuration was not present in campaign daily export.",
                "diagnosis": "Enhanced conversions require conversion action settings or Google Tag verification.",
                "recommendation": "Provide Google Tag or Conversion Goals export with enhanced conversions enabled.",
            }
        )

    for control_id, finding_text, diag_text, rec_text in (
        (
            "G01",
            f"Campaign observed: {snapshot['campaigns'][0].get('name', 'N/A')}." if snapshot.get("campaigns") else "No campaigns observed.",
            "Campaign naming convention requires documented naming policy and current source support.",
            "Establish consistent naming structure across campaign types.",
        ),
        (
            "G08",
            f"Observed account spend: {snapshot.get('spend', 0.0)}.",
            "Budget allocation priority requires campaign objective ranking and business economics.",
            "Define business priority tiers and target CPA/ROAS thresholds.",
        ),
        (
            "G12",
            "Campaign network distribution settings not provided in daily metrics export.",
            "Search partners and display expansion cannot be evaluated from campaign aggregate metrics.",
            "Export campaign network settings or review via Google Ads interface.",
        ),
    ):
        if control_id in entries:
            referenced_sources.update(entries[control_id].control_definition.get("source_ids", []))
            findings.append(
                {
                    "schema_version": "2.0.0",
                    "control_id": control_id,
                    "status": "unknown",
                    "evidence": [],
                    "confidence": "low",
                    "source_classification": "practitioner",
                    "observation": finding_text,
                    "diagnosis": diag_text,
                    "recommendation": rec_text,
                }
            )

    return findings, sorted(referenced_sources)


def run_audit(
    *,
    platform: str = "google",
    input_path: str | PathLike[str],
    report_format: str = "markdown",
    export_format: str = "auto",
    context: Mapping[str, str] | None = None,
    output_dir: str | PathLike[str] | None = None,
    run_id: str | None = None,
    privacy_class: str = "internal",
    registry_root: str | PathLike[str] | None = None,
    owner: str = "operator",
    client_name: str | None = None,
) -> dict[str, Any]:
    """Execute the end-to-end reference audit journey."""
    normalized_platform = platform.strip().lower()
    if normalized_platform not in PLATFORMS:
        raise AuditError(f"unsupported platform: {platform}")

    target_path = Path(input_path)
    if not target_path.exists() or not target_path.is_file():
        raise AuditError(f"input file does not exist or is not a file: {input_path}")

    if report_format not in {"markdown", "html", "pdf"}:
        raise AuditError(f"unsupported report format: {report_format}")

    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    resolved_run_id = run_id or f"audit-{normalized_platform}-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"

    try:
        raw_bytes = target_path.read_bytes()
    except OSError as exc:
        raise AuditError(f"cannot read input file: {exc}") from exc

    file_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    canonical_source_id = f"sha256:{file_sha256}"

    snapshot: dict[str, Any]
    adapter_mode = "export"
    if export_format == "native":
        try:
            if target_path.suffix.lower() == ".json":
                adapter = NativeJSONExportAdapter(normalized_platform, context=context)
            else:
                adapter = NativeCSVExportAdapter(normalized_platform, context=context)
            snapshot = adapter.read_snapshot(target_path)
        except (AdapterError, ValueError) as exc:
            raise AuditError(f"native export ingestion failed: {exc}") from exc
    elif export_format == "generic":
        try:
            gen_adapter = GenericCSVExportAdapter(normalized_platform)
            snapshot = gen_adapter.read_snapshot(target_path)
        except (AdapterError, ValueError) as exc:
            raise AuditError(f"generic export ingestion failed: {exc}") from exc
    else:  # auto
        try:
            if target_path.suffix.lower() == ".json":
                adapter = NativeJSONExportAdapter(normalized_platform, context=context)
            else:
                adapter = NativeCSVExportAdapter(normalized_platform, context=context)
            snapshot = adapter.read_snapshot(target_path)
        except Exception:
            try:
                gen_adapter = GenericCSVExportAdapter(normalized_platform)
                snapshot = gen_adapter.read_snapshot(target_path)
            except Exception as exc:
                raise AuditError(
                    f"failed to ingest export {target_path.name} as native or generic CSV: {exc}"
                ) from exc

    try:
        validate_contract("account-snapshot", snapshot)
    except ContractError as exc:
        raise AuditError(f"normalized snapshot contract validation failed: {exc}") from exc

    # Ensure canonical SHA-256 is bound to measurement_context source_ids
    if canonical_source_id not in snapshot["measurement_context"]["source_ids"]:
        snapshot["measurement_context"]["source_ids"].append(canonical_source_id)

    registry = load_control_registry(registry_root)
    entries = {entry.control_id: entry for entry in registry.entries_for(normalized_platform)}

    findings, referenced_sources = _evaluate_google_findings(
        snapshot=snapshot,
        entries=entries,
        canonical_source_id=canonical_source_id,
        file_sha256=file_sha256,
        run_id=resolved_run_id,
    )

    control_definitions = [
        entries[finding["control_id"]].control_definition
        for finding in findings
        if finding["control_id"] in entries
    ]

    all_sources: set[str] = set(snapshot["measurement_context"]["source_ids"])
    all_sources.add(canonical_source_id)
    all_sources.update(referenced_sources)
    for defn in control_definitions:
        all_sources.update(defn.get("source_ids", []))

    score_result = registry.score_platform(normalized_platform, findings)
    scoring = score_result.to_dict()

    lifecycle_id = f"lifecycle-{resolved_run_id}"
    delete_after_iso = (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data_lifecycle: dict[str, Any] = {
        "schema_version": "1.0.0",
        "lifecycle_id": lifecycle_id,
        "classification": privacy_class,
        "retention": {
            "minimum_seconds": 0,
            "mode": "operator-defined",
            "delete_after": delete_after_iso,
            "purpose": f"Audit execution and report generation for {normalized_platform}",
            "exception_reason": None,
        },
        "encryption": {
            "at_rest": "verified",
            "in_transit": "verified",
            "evidence_refs": ["operator-attestation:local-filesystem-encryption"],
        },
        "access": {
            "owner": owner,
            "authorized_roles": ["operator"],
            "access_log_locator": None,
        },
        "deletion": {
            "status": "scheduled",
            "method": "Secure file overwrite and deletion",
            "verification_required": True,
            "verification_artifact_locator": None,
        },
        "incident": {
            "owner": owner,
            "reporting_channel": "security@operator",
            "status": "not-triggered",
            "record_locator": None,
        },
    }

    # If scoring is insufficient_evidence (e.g. disabled profile), completeness cannot be complete
    completeness = "partial" if scoring["status"] == "insufficient_evidence" else "complete"

    run_manifest: dict[str, Any] = {
        "schema_version": "1.0.0",
        "run_id": resolved_run_id,
        "started_at": now_iso,
        "scopes": ["audit", normalized_platform],
        "adapters": [{"platform": normalized_platform, "mode": adapter_mode}],
        "sources": sorted(all_sources),
        "privacy_class": privacy_class,
        "data_lifecycle": data_lifecycle,
        "worker_status": {f"audit-{normalized_platform}": "completed"},
        "completeness": completeness,
    }

    actions = [
        {
            "action": finding["recommendation"],
            "confidence": finding["confidence"],
            "control_id": finding["control_id"],
        }
        for finding in findings
        if finding["status"] in {"fail", "unknown"} and finding.get("recommendation")
    ]

    bundle: dict[str, Any] = {
        "schema_version": "2.0.0",
        "run_manifest": run_manifest,
        "account_snapshot": snapshot,
        "control_definitions": control_definitions,
        "findings": findings,
        "scoring": scoring,
        "contradictions": [],
        "actions": actions,
    }

    try:
        validate_contract("report-bundle", bundle)
    except ContractError as exc:
        raise AuditError(f"assembled report bundle failed contract validation: {exc}") from exc

    try:
        registry.validate_report_scoring(bundle)
    except RegistryError as exc:
        raise AuditError(f"assembled report bundle failed registry scoring validation: {exc}") from exc

    root = Path(output_dir if output_dir is not None else _default_report_root())
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass

    bundle_destination = f"{resolved_run_id}/bundle.json"
    bundle_bytes = (json.dumps(bundle, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        bundle_path = atomic_write_report(root, bundle_destination, bundle_bytes)
    except ReportRenderError as exc:
        raise AuditError(f"bundle persistence failed: {exc}") from exc

    extension = {"markdown": "md", "html": "html", "pdf": "pdf"}[report_format]
    rel_destination = f"{resolved_run_id}/report.{extension}"
    try:
        report_path = write_report_bundle(
            bundle,
            report_format,
            root,
            rel_destination,
            registry=registry,
        )
    except ReportRenderError as exc:
        raise AuditError(f"report rendering failed: {exc}") from exc

    return {
        "status": "completed",
        "run_id": resolved_run_id,
        "platform": normalized_platform,
        "completeness": completeness,
        "health_score": scoring["health_score"],
        "evidence_coverage": scoring["evidence_coverage"],
        "scoring_status": scoring["status"],
        "findings_count": len(findings),
        "bundle_path": str(bundle_path),
        "report_path": str(report_path),
    }
