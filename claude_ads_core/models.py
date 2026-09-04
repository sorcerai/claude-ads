"""Typed public interfaces for mixed-version JSON contracts."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


class AccountIdentity(TypedDict):
    platform: str
    account_id: str
    name: NotRequired[str]


class DateWindow(TypedDict):
    start: str
    end: str


class AttributionWindow(TypedDict):
    value: int
    unit: Literal["hour", "day"]


class MeasurementContext(TypedDict):
    timezone: str | None
    currency: str
    profile_id: str
    source_format: str
    source_ids: list[str]
    report_grain: list[str]
    conversion_definition: str | None
    conversion_actions: list[str]
    attribution_model: str | None
    click_attribution_window: AttributionWindow | None
    view_attribution_window: AttributionWindow | None
    counting_behavior: str | None
    as_of: str
    data_finalization: Literal["final", "provisional", "unknown"]
    modeled_data_treatment: Literal["included", "excluded", "mixed", "unknown"]
    missing_fields: list[str]
    unsupported_fields: list[str]


class CampaignRow(TypedDict):
    campaign_id: str
    name: NotRequired[str]
    status: NotRequired[str]
    policy_status: NotRequired[str]
    spend: NotRequired[float]


class CreativeRow(TypedDict):
    creative_id: str
    campaign_id: str
    name: NotRequired[str]


class ConversionRow(TypedDict):
    action: str
    count: NotRequired[float]
    status: NotRequired[str]


class BudgetRow(TypedDict):
    campaign_id: str
    date: str
    amount: float


class AccountSnapshot(TypedDict):
    schema_version: Literal["2.0.0"]
    account: AccountIdentity
    window: DateWindow
    currency: str
    measurement_context: MeasurementContext
    spend: NotRequired[float | None]
    campaigns: list[CampaignRow]
    creatives: list[CreativeRow]
    conversions: list[ConversionRow]
    budgets: list[BudgetRow]


class AdapterRecord(TypedDict):
    platform: str
    mode: Literal["export", "live_read", "write_preview", "write_apply"]


class RunManifest(TypedDict):
    schema_version: Literal["1.0.0"]
    run_id: str
    started_at: str
    scopes: list[str]
    adapters: list[AdapterRecord]
    sources: list[str]
    privacy_class: Literal["public", "internal", "confidential", "restricted"]
    data_lifecycle: dict[str, Any]
    worker_status: dict[str, Literal["pending", "running", "completed", "failed", "skipped"]]
    completeness: Literal["complete", "partial", "failed"]


class ControlDefinition(TypedDict):
    schema_version: Literal["1.0.0"]
    control_id: str
    category: str
    severity: Literal["critical", "high", "medium", "informational"]
    required_inputs: list[str]
    source_ids: list[str]
    maturity: Literal[
        "inventory-baselined",
        "source-grounded",
        "domain-integrated",
        "eval-verified",
        "release-ready",
    ]
    geographies: list[str]

    expires_at: NotRequired[str | None]
    scoring_behavior: Literal["health", "opportunity", "watchlist"]
    stability: Literal["stable", "experimental"]


class EvidenceRecord(TypedDict):
    evidence_id: str
    proof_kind: Literal["observation", "source_fact", "vendor_claim", "inference"]
    source_id: str
    locator: str | None
    sha256: str | None
    observed_at: str
    query_id: str | None
    report_id: str | None
    window: DateWindow | None
    report_grain: list[str]
    input_field: str | None
    redacted_value: Any | None
    observation_ref: str | None


class Finding(TypedDict):
    schema_version: Literal["2.0.0"]
    control_id: str
    status: Literal["pass", "fail", "unknown", "not_applicable"]
    evidence: list[EvidenceRecord]
    confidence: Literal["high", "medium", "low", "none"]
    source_classification: Literal["evidence_based", "practitioner", "contested", "folklore"]
    observation: str
    diagnosis: str
    recommendation: str


class CategoryScoreOutput(TypedDict):
    category: str
    category_weight: float
    health_score: float | None
    evidence_coverage: float
    applicable_controls: int
    known_controls: int
    passed_controls: int
    failed_controls: int
    unknown_controls: int


class ScoringOutput(TypedDict):
    health_score: float | None
    evidence_coverage: float
    status: Literal["normal", "provisional", "insufficient_evidence"]
    categories: list[CategoryScoreOutput]

class ReportBundle(TypedDict):
    schema_version: Literal["2.0.0"]
    run_manifest: RunManifest
    account_snapshot: AccountSnapshot
    control_definitions: list[ControlDefinition]
    findings: list[Finding]
    scoring: ScoringOutput
