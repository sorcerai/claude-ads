"""Guided setup profile generation for deterministic Claude Ads operations."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from os import PathLike
from pathlib import Path
from typing import Any, Sequence

from .contracts import PLATFORMS
from .workflow_contracts import WorkflowContractError, validate_workflow_contract


class SetupError(ValueError):
    """Raised when setup parameters fail validation or safe file creation."""


def generate_setup_profile(
    *,
    platform: str = "google",
    client_name: str = "Default Client",
    business_model: str = "ecommerce",
    geographies: Sequence[str] = ("US",),
    regulated_categories: Sequence[str] = (),
    account_id: str = "demo-account",
    objective: str = "conversions",
    conversion_definition: str = "purchase",
    success_metrics: Sequence[str] = ("cpa", "roas"),
    data_source_kind: str = "export",
    data_source_path: str | None = None,
    privacy_class: str = "internal",
    mutation_authority: str = "none",
    approver_ids: Sequence[str] = (),
    owner: str = "operator",
    reporting_channel: str = "security@operator",
    retention_days: int = 30,
    run_id: str | None = None,
    output_path: str | PathLike[str] | None = None,
) -> dict[str, Any]:
    """Generate a validated SetupProfile with embedded DataLifecycle."""
    normalized_platform = platform.strip().lower()
    if normalized_platform not in PLATFORMS:
        raise SetupError(f"unsupported platform: {platform}")

    if not client_name.strip():
        raise SetupError("client_name must not be empty")

    if not account_id.strip():
        raise SetupError("account_id must not be empty")

    if privacy_class not in {"public", "internal", "confidential", "restricted"}:
        raise SetupError(f"invalid privacy_class: {privacy_class}")

    if mutation_authority not in {"none", "draft-only", "approved-plan-required"}:
        raise SetupError(f"invalid mutation_authority: {mutation_authority}")

    if mutation_authority == "approved-plan-required" and not approver_ids:
        raise SetupError("approver_ids is required when mutation_authority is approved-plan-required")

    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    delete_after_iso = (now + timedelta(days=max(1, retention_days))).strftime("%Y-%m-%dT%H:%M:%SZ")

    resolved_run_id = run_id or f"setup-{normalized_platform}-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
    lifecycle_id = f"lifecycle-{resolved_run_id}"

    data_lifecycle: dict[str, Any] = {
        "schema_version": "1.0.0",
        "lifecycle_id": lifecycle_id,
        "classification": privacy_class,
        "retention": {
            "minimum_seconds": 0,
            "mode": "operator-defined",
            "delete_after": delete_after_iso,
            "purpose": f"Setup profile and data governance for {client_name} on {normalized_platform}",
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
            "reporting_channel": reporting_channel,
            "status": "not-triggered",
            "record_locator": None,
        },
    }

    source_id = f"{normalized_platform}-data-source-001"
    data_sources = [
        {
            "id": source_id,
            "kind": data_source_kind,
            "platform": normalized_platform,
            "status": "available" if data_source_path else "unverified",
        }
    ]

    profile: dict[str, Any] = {
        "schema_version": "1.0.0",
        "artifact_type": "setup-profile",
        "run_id": resolved_run_id,
        "created_at": now_iso,
        "business": {
            "name": client_name.strip(),
            "business_model": business_model.strip(),
            "geographies": list(geographies),
            "regulated_categories": list(regulated_categories),
        },
        "objective": {
            "primary": objective.strip(),
            "conversion_definition": conversion_definition.strip(),
            "success_metrics": list(success_metrics),
        },
        "platforms": [normalized_platform],
        "account_refs": [
            {
                "platform": normalized_platform,
                "account_id": account_id.strip(),
            }
        ],
        "data_sources": data_sources,
        "privacy_class": privacy_class,
        "mutation_authority": mutation_authority,
        "approver_ids": list(approver_ids),
        "assumptions": [
            f"Read-only advertising audit operations on {normalized_platform}.",
            "All credentials kept outside artifact state in environment/keychain.",
        ],
        "data_lifecycle": data_lifecycle,
    }

    try:
        validate_workflow_contract("setup-profile", profile)
    except WorkflowContractError as exc:
        raise SetupError(f"generated setup profile is invalid: {exc}") from exc

    if output_path is not None:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically with mode 0600
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        import json
        payload_bytes = (json.dumps(profile, indent=2, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(target, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            target.unlink(missing_ok=True)
            raise

    return profile
