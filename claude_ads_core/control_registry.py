"""Versioned, fail-closed control registry and platform scoring profiles.

The audit catalog is useful for discovery, but a named check is not automatically
a scoreable control.  This loader enforces that distinction at runtime.  Only an
enabled profile whose health controls resolve to verified, load-bearing claims
may enter the deterministic scoring engine.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import PLATFORMS, ContractError, validate_contract
from .scoring import ScoreResult, ScoringError, score_account

REGISTRY_SCHEMA_VERSION = "1.0.0"
REGISTRY_VERSION = "1.0.0"
PROFILE_VERSION = "1.0.0"
DISPOSITIONS = {"conditional_watchlist", "source_refresh_discovery", "opportunity", "health"}
PROFILE_STATUSES = {"disabled", "enabled"}


class RegistryError(ValueError):
    """Raised when registry or scoring-profile state is unsafe or inconsistent."""


@dataclass(frozen=True)
class RegistryEntry:
    platform: str
    control_id: str
    intent: str
    disposition: str
    source_claim_ids: tuple[str, ...]
    control_definition: Mapping[str, Any]
    claim_refresh_due: tuple[str, ...] = ()
    source_refresh_due: tuple[tuple[str, Any], ...] = ()

@dataclass(frozen=True)
class ScoringProfile:
    profile_id: str
    platform: str
    status: str
    category_weights: Mapping[str, float]
    health_control_ids: tuple[str, ...]
    disabled_reason: str | None


@dataclass(frozen=True)
class ControlRegistry:
    entries: tuple[RegistryEntry, ...]
    profiles: tuple[ScoringProfile, ...]

    def entries_for(self, platform: str) -> tuple[RegistryEntry, ...]:
        normalized = platform.lower()
        if normalized not in PLATFORMS:
            raise RegistryError(f"unsupported platform: {platform}")
        return tuple(entry for entry in self.entries if entry.platform == normalized)

    def controls_for(self, platform: str) -> tuple[Mapping[str, Any], ...]:
        return tuple(entry.control_definition for entry in self.entries_for(platform))

    def profile_for(self, platform: str) -> ScoringProfile:
        normalized = platform.lower()
        for profile in self.profiles:
            if profile.platform == normalized:
                return profile
        raise RegistryError(f"no scoring profile for platform: {platform}")

    def scoring_inputs(self, platform: str) -> tuple[tuple[Mapping[str, Any], ...], Mapping[str, float]]:
        """Return approved health controls and weights, or fail closed."""

        profile = self.profile_for(platform)
        if profile.status != "enabled":
            raise RegistryError(
                f"scoring profile {profile.profile_id} is disabled: {profile.disabled_reason}"
            )
        entries = {entry.control_id: entry for entry in self.entries_for(platform)}
        return (
            tuple(entries[control_id].control_definition for control_id in profile.health_control_ids),
            profile.category_weights,
        )

    def score_platform(
        self,
        platform: str,
        findings: Sequence[Mapping[str, Any]],
    ) -> ScoreResult:
        """Score through the approved profile; disabled profiles yield no health.

        A disabled profile means no evidence obligation has been approved, so its
        evidence coverage is zero rather than the scoring engine's normal 100%
        result for an empty set of applicable controls.
        """

        profile = self.profile_for(platform)
        if profile.status != "enabled":
            return ScoreResult(
                health_score=None,
                evidence_coverage=0.0,
                status="insufficient_evidence",
                categories=(),
            )
        controls, weights = self.scoring_inputs(platform)
        return score_account(controls, findings, weights)

    def validate_report_scoring(self, bundle: Mapping[str, Any]) -> ScoreResult:
        """Validate and recompute the score embedded in a report bundle."""

        profile_id: str | None = None
        account_snapshot = bundle.get("account_snapshot") if isinstance(bundle, Mapping) else None
        if isinstance(account_snapshot, Mapping):
            account = account_snapshot.get("account")
            if isinstance(account, Mapping):
                raw_platform = account.get("platform")
                if isinstance(raw_platform, str):
                    normalized_platform = raw_platform.lower()
                    profile_id = next(
                        (
                            candidate.profile_id
                            for candidate in self.profiles
                            if candidate.platform == normalized_platform
                        ),
                        None,
                    )

        try:
            validate_contract("report-bundle", bundle)
        except ContractError as exc:
            message = str(exc)
            if profile_id is not None:
                message = f"{message} for scoring profile {profile_id}"
            raise RegistryError(message) from exc

        platform = str(bundle["account_snapshot"]["account"]["platform"])
        profile = self.profile_for(platform)
        approved_ids = set(profile.health_control_ids)
        entries = {entry.control_id: entry for entry in self.entries_for(platform)}
        definitions = bundle["control_definitions"]
        validated_definitions: dict[str, Mapping[str, Any]] = {}

        for definition in definitions:
            control_id = definition["control_id"]
            entry = entries.get(control_id)
            if entry is None:
                raise RegistryError(
                    f"report bundle has unknown control definition for scoring profile "
                    f"{profile.profile_id}: {control_id}"
                )
            if control_id in validated_definitions:
                raise RegistryError(
                    f"report bundle has duplicate control definition for scoring profile "
                    f"{profile.profile_id}: {control_id}"
                )
            if definition != entry.control_definition:
                raise RegistryError(
                    f"report bundle control definition does not match scoring profile "
                    f"{profile.profile_id}: {control_id}"
                )
            if control_id not in approved_ids and entry.disposition == "health":
                raise RegistryError(
                    f"report bundle includes unapproved health control for scoring profile "
                    f"{profile.profile_id}: {control_id}"
                )
            validated_definitions[control_id] = definition

        if profile.status == "enabled":
            for control_id in profile.health_control_ids:
                entry = entries.get(control_id)
                if (
                    entry is None
                    or entry.disposition != "health"
                    or validated_definitions.get(control_id) != entry.control_definition
                ):
                    raise RegistryError(
                        f"report bundle control definition does not match scoring profile "
                        f"{profile.profile_id}: {control_id}"
                    )

        snapshot = bundle["account_snapshot"]
        context = snapshot["measurement_context"]
        run_sources = set(bundle["run_manifest"]["sources"])
        context_sources = set(context["source_ids"])
        snapshot_start = _iso_date(snapshot["window"]["start"], "account snapshot window.start")
        snapshot_end = _iso_date(snapshot["window"]["end"], "account snapshot window.end")
        evidence_ids: set[str] = set()
        for finding in bundle["findings"]:
            definition = validated_definitions.get(finding["control_id"])
            definition_sources = set(definition["source_ids"]) if definition is not None else set()
            for evidence in finding["evidence"]:
                evidence_id = evidence["evidence_id"]
                if evidence_id in evidence_ids:
                    raise RegistryError(
                        f"scoring profile {profile.profile_id} has duplicate evidence_id: {evidence_id}"
                    )
                evidence_ids.add(evidence_id)
                source_id = evidence["source_id"]
                proof_kind = evidence["proof_kind"]
                if proof_kind == "observation":
                    _observation_digest(
                        evidence,
                        f"scoring profile {profile.profile_id} finding {finding['control_id']} evidence",
                    )
                source_bound = source_id in run_sources
                if proof_kind == "observation":
                    source_bound = source_bound and source_id in context_sources
                elif proof_kind in {"source_fact", "vendor_claim"}:
                    source_bound = source_bound and source_id in definition_sources
                elif proof_kind == "inference":
                    source_bound = source_bound and (
                        source_id in context_sources or source_id in definition_sources
                    )
                if not source_bound:
                    raise RegistryError(
                        f"scoring profile {profile.profile_id} finding {finding['control_id']} "
                        f"references unbound evidence source: {source_id}"
                    )
                if proof_kind == "observation":
                    if evidence["window"] is None:
                        raise RegistryError(
                            f"scoring profile {profile.profile_id} finding {finding['control_id']} "
                            "has observation evidence window missing"
                        )
                    evidence_start = _iso_date(evidence["window"]["start"], "evidence window.start")
                    evidence_end = _iso_date(evidence["window"]["end"], "evidence window.end")
                    if evidence_start < snapshot_start or evidence_end > snapshot_end:
                        raise RegistryError(
                            f"scoring profile {profile.profile_id} finding {finding['control_id']} "
                            "has observation evidence window outside snapshot window"
                        )
        if profile.status == "enabled":
            started_at = bundle["run_manifest"]["started_at"]
            run_date = datetime.fromisoformat(started_at.replace("Z", "+00:00")).date()
            for control_id in profile.health_control_ids:
                entry = entries[control_id]
                definition = entry.control_definition
                expires_at = definition.get("expires_at")
                if expires_at is None:
                    raise RegistryError(
                        f"scoring profile {profile.profile_id} control {control_id} "
                        "is missing expires_at"
                    )
                if run_date > _iso_date(expires_at, f"control {control_id}.expires_at"):
                    raise RegistryError(
                        f"scoring profile {profile.profile_id} control {control_id} is expired "
                        f"as of {expires_at}"
                    )
                for refresh_due in entry.claim_refresh_due:
                    if run_date > _iso_date(refresh_due, f"control {control_id} claim.refresh_due"):
                        raise RegistryError(
                            f"scoring profile {profile.profile_id} control {control_id} "
                            f"claim refresh is expired as of {refresh_due}"
                        )
                source_refresh_due = dict(entry.source_refresh_due)
                for source_id in definition["source_ids"]:
                    if source_id not in source_refresh_due or source_refresh_due[source_id] is None:
                        raise RegistryError(
                            f"scoring profile {profile.profile_id} control {control_id} "
                            f"source {source_id} is missing refresh_due"
                        )
                    refresh_due = source_refresh_due[source_id]
                    try:
                        source_deadline = _iso_date(
                            refresh_due,
                            f"control {control_id} source {source_id}.refresh_due",
                        )
                    except RegistryError as exc:
                        raise RegistryError(
                            f"scoring profile {profile.profile_id} control {control_id} "
                            f"source {source_id} has invalid refresh_due"
                        ) from exc
                    if run_date > source_deadline:
                        raise RegistryError(
                            f"scoring profile {profile.profile_id} control {control_id} "
                            f"source {source_id} refresh is expired as of {refresh_due}"
                        )

        findings_by_id: dict[str, Mapping[str, Any]] = {}
        for finding in bundle["findings"]:
            control_id = finding["control_id"]
            if control_id not in validated_definitions:
                raise RegistryError(
                    f"report bundle finding has no validated control definition for scoring profile "
                    f"{profile.profile_id}: {control_id}"
                )
            if control_id in findings_by_id:
                raise RegistryError(
                    f"report bundle has duplicate finding for scoring profile "
                    f"{profile.profile_id}: {control_id}"
                )
            _validate_finding_inputs(
                profile_id=profile.profile_id,
                definition=validated_definitions[control_id],
                finding=finding,
                snapshot=snapshot,
                source_claim_ids=entries[control_id].source_claim_ids,
            )

            findings_by_id[control_id] = finding

        findings = [finding for control_id, finding in findings_by_id.items() if control_id in approved_ids]
        try:
            recomputed = self.score_platform(platform, findings)
        except ScoringError as exc:
            raise RegistryError(
                f"cannot recompute scoring for profile {profile.profile_id}: {exc}"
            ) from exc


        def public_score(scoring: Mapping[str, Any]) -> tuple[Any, ...]:
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
            categories = [
                tuple(category[field] for field in category_fields)
                for category in scoring["categories"]
            ]
            categories.sort(key=lambda values: values[0])
            return (
                scoring["health_score"],
                scoring["evidence_coverage"],
                scoring["status"],
                tuple(categories),
            )

        if public_score(bundle["scoring"]) != public_score(recomputed.to_dict()):
            raise RegistryError(f"report scoring does not match profile {profile.profile_id}")
        return recomputed


def _read_json(path: Path | Traversable) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"cannot load {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise RegistryError(f"{path} must contain a JSON object")
    return payload


def _iso_date(value: Any, path: str) -> date:
    if not isinstance(value, str):
        raise RegistryError(f"{path} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise RegistryError(f"{path} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise RegistryError(f"{path} must be an ISO date")
    return parsed



_OBSERVATION_SOURCE_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")
_OBSERVATION_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _observation_digest(evidence: Mapping[str, Any], path: str) -> str:
    source_id = evidence["source_id"]
    source_match = _OBSERVATION_SOURCE_PATTERN.fullmatch(source_id)
    if source_match is None:
        raise RegistryError(f"{path}.source_id must be canonical sha256:<64 lowercase hex>")
    digest = evidence["sha256"]
    if not isinstance(digest, str) or _OBSERVATION_DIGEST_PATTERN.fullmatch(digest) is None:
        raise RegistryError(f"{path}.sha256 must be a 64-hex digest")
    if digest != source_match.group(1):
        raise RegistryError(f"{path}.sha256 does not match source_id digest")
    return digest

_MEASUREMENT_CONTEXT_FIELDS = {
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
_SNAPSHOT_COLLECTION_INPUTS = {"campaigns", "creatives", "conversions", "budgets"}
_UNSUPPORTED_INPUTS = {
    "account_name",
    "campaign_name",
    "campaign_status",
    "creative_id",
    "creative_name",
    "conversion_action",
    "conversions",
    "budget",
}


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False
_SPECIAL_INPUTS = {
    "applicability_context",
    "current_account_evidence",
    "current_source_support",
}
_KNOWN_REQUIRED_INPUTS = (
    _SNAPSHOT_COLLECTION_INPUTS
    | _UNSUPPORTED_INPUTS
    | _SPECIAL_INPUTS
    | {"spend"}
    | _MEASUREMENT_CONTEXT_FIELDS
)


def _usable_collection(snapshot: Mapping[str, Any], required_input: str) -> bool:
    rows = snapshot.get(required_input)
    if not isinstance(rows, list) or not rows:
        return False
    if required_input == "campaigns":
        return any(
            isinstance(row, Mapping)
            and isinstance(row.get("campaign_id"), str)
            and bool(row["campaign_id"].strip())
            for row in rows
        )
    if required_input == "creatives":
        return any(
            isinstance(row, Mapping)
            and isinstance(row.get("creative_id"), str)
            and bool(row["creative_id"].strip())
            for row in rows
        )
    if required_input == "conversions":
        return any(
            isinstance(row, Mapping)
            and isinstance(row.get("action"), str)
            and bool(row["action"].strip())
            for row in rows
        )
    return any(
        isinstance(row, Mapping)
        and isinstance(row.get("campaign_id"), str)
        and bool(row["campaign_id"].strip())
        and _finite_number(row.get("amount"))
        for row in rows
    )


def _missing_required_inputs(
    definition: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    finding: Mapping[str, Any],
    source_claim_ids: Sequence[str],
) -> tuple[str, ...]:
    """Resolve required inputs against explicit snapshot and evidence contracts."""

    context = snapshot["measurement_context"]
    missing_fields = set(context["missing_fields"])
    unsupported_fields = set(context["unsupported_fields"])
    missing: list[str] = []
    for raw_input in definition["required_inputs"]:
        required_input = str(raw_input)
        context_field = required_input
        if required_input.startswith("measurement_context."):
            context_field = required_input.removeprefix("measurement_context.")
        elif required_input.startswith("measurement_context:"):
            context_field = required_input.removeprefix("measurement_context:")
        if (
            required_input not in _KNOWN_REQUIRED_INPUTS
            and context_field not in _MEASUREMENT_CONTEXT_FIELDS
        ):
            missing.append(required_input)
            continue
        if required_input in _SNAPSHOT_COLLECTION_INPUTS:
            if not _usable_collection(snapshot, required_input):
                missing.append(required_input)
            continue
        if required_input == "spend":
            if snapshot.get("spend") is None:
                missing.append(required_input)
            continue
        if required_input == "applicability_context":
            if not context:
                missing.append(required_input)
            continue
        if required_input == "current_account_evidence":
            if not any(
                evidence["proof_kind"] in {"observation", "inference"}
                for evidence in finding["evidence"]
            ):
                missing.append(required_input)
            continue
        if required_input == "current_source_support":
            if not definition["source_ids"] and not source_claim_ids:
                missing.append(required_input)
            continue

        if context_field in _MEASUREMENT_CONTEXT_FIELDS and context_field in missing_fields:
            missing.append(required_input)
        elif context_field in unsupported_fields:
            missing.append(required_input)
    return tuple(dict.fromkeys(missing))


def _validate_finding_inputs(
    profile_id: str,
    definition: Mapping[str, Any],
    finding: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    source_claim_ids: Sequence[str],
) -> None:
    if finding["status"] not in {"pass", "fail"}:
        return
    missing = _missing_required_inputs(definition, snapshot, finding, source_claim_ids)
    if missing:
        control_id = definition["control_id"]
        raise RegistryError(
            f"scoring profile {profile_id} control {control_id} has missing required input: "
            f"{', '.join(missing)}"
        )

def _nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{path} must be a non-empty string")
    return value


def _lowercase_platform(value: Any, path: str) -> str:
    platform = _nonempty_string(value, path)
    if platform != platform.lower():
        raise RegistryError(f"{path} must be lowercase")
    return platform


def _string_tuple(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RegistryError(f"{path} must be an array")
    result = tuple(_nonempty_string(item, f"{path}[{index}]") for index, item in enumerate(value))
    if len(result) != len(set(result)):
        raise RegistryError(f"{path} must contain unique values")
    return result


def _verified_claims(claim_ledger: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    claims = claim_ledger.get("claims")
    if not isinstance(claims, list):
        raise RegistryError("claim ledger claims must be an array")
    result: dict[str, Mapping[str, Any]] = {}
    for index, raw_claim in enumerate(claims):
        if not isinstance(raw_claim, Mapping):
            raise RegistryError(f"claim ledger claims[{index}] must be an object")
        claim_id = _nonempty_string(raw_claim.get("id"), f"claim ledger claims[{index}].id")
        if claim_id in result:
            raise RegistryError(f"duplicate claim id: {claim_id}")
        result[claim_id] = raw_claim
    return result


def _known_sources(source_ledger: Mapping[str, Any]) -> dict[str, Any]:
    sources = source_ledger.get("sources")
    if not isinstance(sources, list):
        raise RegistryError("source ledger sources must be an array")
    result: dict[str, Any] = {}
    for index, raw_source in enumerate(sources):
        if not isinstance(raw_source, Mapping):
            raise RegistryError(f"source ledger sources[{index}] must be an object")
        source_id = _nonempty_string(raw_source.get("id"), f"source ledger sources[{index}].id")
        if source_id in result:
            raise RegistryError(f"duplicate source id: {source_id}")
        result[source_id] = raw_source.get("refresh_due")
    return result


def _validate_entry(
    raw_entry: Any,
    index: int,
    claims: Mapping[str, Mapping[str, Any]],
    sources: Mapping[str, Any],
) -> RegistryEntry:
    if not isinstance(raw_entry, Mapping):
        raise RegistryError(f"controls[{index}] must be an object")
    platform = _lowercase_platform(raw_entry.get("platform"), f"controls[{index}].platform")
    if platform not in PLATFORMS:
        raise RegistryError(f"controls[{index}].platform is unsupported")
    control_id = _nonempty_string(raw_entry.get("control_id"), f"controls[{index}].control_id")
    intent = _nonempty_string(raw_entry.get("intent"), f"controls[{index}].intent")
    disposition = _nonempty_string(raw_entry.get("disposition"), f"controls[{index}].disposition")
    if disposition not in DISPOSITIONS:
        raise RegistryError(f"controls[{index}].disposition is invalid")
    claim_ids = _string_tuple(raw_entry.get("source_claim_ids"), f"controls[{index}].source_claim_ids")
    definition = raw_entry.get("control_definition")
    if not isinstance(definition, Mapping):
        raise RegistryError(f"controls[{index}].control_definition must be an object")
    try:
        validate_contract("control-definition", definition)
    except ContractError as exc:
        raise RegistryError(f"controls[{index}].control_definition: {exc}") from exc
    if definition["control_id"] != control_id:
        raise RegistryError(f"controls[{index}] control_id does not match its definition")
    expected_behavior = "health" if disposition == "health" else (
        "opportunity" if disposition == "opportunity" else "watchlist"
    )
    if definition["scoring_behavior"] != expected_behavior:
        raise RegistryError(f"{control_id} disposition does not match scoring_behavior")
    definition_sources = set(_string_tuple(definition["source_ids"], f"{control_id}.source_ids"))
    unknown_sources = definition_sources - set(sources)
    if unknown_sources:
        raise RegistryError(f"{control_id} references unknown sources: {sorted(unknown_sources)}")
    source_refresh_due = tuple(
        sorted((source_id, sources[source_id]) for source_id in definition_sources)
    )
    claim_refresh_due: list[str] = []
    for claim_id in claim_ids:
        if claim_id not in claims:
            raise RegistryError(f"{control_id} references unknown claim: {claim_id}")
        claim_sources = set(claims[claim_id].get("source_ids", []))
        if not definition_sources <= claim_sources:
            raise RegistryError(f"{control_id} source_ids are not supported by claim {claim_id}")
        refresh_due = claims[claim_id].get("refresh_due")
        if refresh_due is None:
            raise RegistryError(f"claim {claim_id} is missing refresh_due")
        claim_refresh_due.append(
            _iso_date(refresh_due, f"claim {claim_id}.refresh_due").isoformat()
        )
    if disposition == "health":
        if definition.get("expires_at") is None:
            raise RegistryError(f"health control {control_id} is missing expires_at")
        if not claim_ids or not definition_sources or not definition["required_inputs"]:
            raise RegistryError(f"health control {control_id} lacks typed evidence grounding")
        if definition["severity"] == "informational":
            raise RegistryError(f"health control {control_id} has zero-weight severity")
        if definition["maturity"] not in {"source-grounded", "domain-integrated", "eval-verified", "release-ready"}:
            raise RegistryError(f"health control {control_id} is not source-grounded")
        if definition["stability"] != "stable":
            raise RegistryError(f"health control {control_id} is not stable")
        for claim_id in claim_ids:
            claim = claims[claim_id]
            if claim.get("verdict") != "verified" or claim.get("load_bearing") is not True:
                raise RegistryError(f"health control {control_id} uses an unverified claim: {claim_id}")
    elif definition["severity"] != "informational":
        raise RegistryError(f"unscored control {control_id} must remain informational")
    return RegistryEntry(
        platform,
        control_id,
        intent,
        disposition,
        claim_ids,
        definition,
        tuple(sorted(claim_refresh_due)),
        source_refresh_due,
    )


def _validate_profile(raw_profile: Any, index: int, entries: Mapping[tuple[str, str], RegistryEntry]) -> ScoringProfile:
    if not isinstance(raw_profile, Mapping):
        raise RegistryError(f"profiles[{index}] must be an object")
    profile_id = _nonempty_string(raw_profile.get("profile_id"), f"profiles[{index}].profile_id")
    platform = _lowercase_platform(raw_profile.get("platform"), f"profiles[{index}].platform")
    if platform not in PLATFORMS:
        raise RegistryError(f"profiles[{index}].platform is unsupported")
    status = _nonempty_string(raw_profile.get("status"), f"profiles[{index}].status")
    if status not in PROFILE_STATUSES:
        raise RegistryError(f"profiles[{index}].status is invalid")
    raw_weights = raw_profile.get("category_weights")
    if not isinstance(raw_weights, Mapping):
        raise RegistryError(f"profiles[{index}].category_weights must be an object")
    weights: dict[str, float] = {}
    for category, raw_weight in raw_weights.items():
        if not isinstance(category, str) or not category:
            raise RegistryError(f"profiles[{index}] category names must be non-empty strings")
        if isinstance(raw_weight, bool) or not isinstance(raw_weight, (int, float)):
            raise RegistryError(f"profiles[{index}].category_weights.{category} must be numeric")
        weights[category] = float(raw_weight)
    health_ids = _string_tuple(raw_profile.get("health_control_ids"), f"profiles[{index}].health_control_ids")
    disabled_reason = raw_profile.get("disabled_reason")
    if disabled_reason is not None:
        disabled_reason = _nonempty_string(disabled_reason, f"profiles[{index}].disabled_reason")
    if status == "disabled":
        if weights or health_ids or disabled_reason is None:
            raise RegistryError(f"disabled profile {profile_id} requires only an explicit reason")
    else:
        if disabled_reason is not None or not health_ids or sum(weights.values()) != 100.0:
            raise RegistryError(f"enabled profile {profile_id} requires controls and weights totaling 100")
        selected: list[RegistryEntry] = []
        for control_id in health_ids:
            entry = entries.get((platform, control_id))
            if entry is None:
                raise RegistryError(f"profile {profile_id} references unknown control {control_id}")
            if entry.disposition != "health":
                raise RegistryError(f"profile {profile_id} references unscored control {control_id}")
            selected.append(entry)
        categories = {str(entry.control_definition["category"]) for entry in selected}
        if set(weights) != categories:
            raise RegistryError(f"profile {profile_id} weights do not exactly match health-control categories")
    return ScoringProfile(profile_id, platform, status, weights, health_ids, disabled_reason)


def load_control_registry(root: str | Path | None = None) -> ControlRegistry:
    """Load and semantically validate the control-plane registry."""

    if root is None:
        manifest_root = resources.files("claude_ads_core").joinpath("manifests")
    else:
        repo_root = Path(root).resolve()
        manifest_root = repo_root / "control-plane" / "manifests"
    registry_payload = _read_json(manifest_root.joinpath("control-registry.json"))
    profile_payload = _read_json(manifest_root.joinpath("scoring-profiles.json"))
    claims = _verified_claims(_read_json(manifest_root.joinpath("claim-ledger.json")))
    sources = _known_sources(_read_json(manifest_root.joinpath("source-ledger.json")))
    if registry_payload.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise RegistryError("unsupported control-registry schema_version")
    if registry_payload.get("registry_version") != REGISTRY_VERSION:
        raise RegistryError("unsupported control-registry registry_version")
    if profile_payload.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise RegistryError("unsupported scoring-profiles schema_version")
    if profile_payload.get("profile_version") != PROFILE_VERSION:
        raise RegistryError("unsupported scoring-profiles profile_version")
    raw_entries = registry_payload.get("controls")
    if not isinstance(raw_entries, list):
        raise RegistryError("control registry controls must be an array")
    entries = tuple(_validate_entry(raw, index, claims, sources) for index, raw in enumerate(raw_entries))
    by_key: dict[tuple[str, str], RegistryEntry] = {}
    for entry in entries:
        key = (entry.platform, entry.control_id)
        if key in by_key:
            raise RegistryError(f"duplicate platform control: {entry.platform}/{entry.control_id}")
        by_key[key] = entry
    if {entry.platform for entry in entries} != PLATFORMS:
        raise RegistryError("control registry must cover exactly the twelve supported platforms")
    raw_profiles = profile_payload.get("profiles")
    if not isinstance(raw_profiles, list):
        raise RegistryError("scoring profiles must be an array")
    profiles = tuple(_validate_profile(raw, index, by_key) for index, raw in enumerate(raw_profiles))
    profile_platforms = [profile.platform for profile in profiles]
    if set(profile_platforms) != PLATFORMS or len(profile_platforms) != len(PLATFORMS):
        raise RegistryError("scoring profiles must define exactly one profile per supported platform")
    return ControlRegistry(entries, profiles)
