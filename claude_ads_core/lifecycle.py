"""Truthful v2 lifecycle construction for in-memory workflow records."""

from __future__ import annotations

from typing import Any, Sequence

from .workflow_contracts import validate_workflow_contract


__all__ = ["make_pending_lifecycle"]


def make_pending_lifecycle(
    *,
    lifecycle_id: str,
    classification: str,
    delete_after: str | None,
    purpose: str,
    owner: str,
    authorized_roles: Sequence[str],
    reporting_channel: str,
) -> dict[str, Any]:
    """Build and validate a v2 lifecycle without claiming unavailable controls."""
    lifecycle = {
        "schema_version": "2.0.0",
        "lifecycle_id": lifecycle_id,
        "classification": classification,
        "retention": {
            "minimum_seconds": 0,
            "mode": "unassigned" if delete_after is None else "operator-defined",
            "delete_after": delete_after,
            "purpose": purpose,
            "exception_reason": None,
        },
        "encryption": {
            "at_rest": "unknown",
            "in_transit": "unknown",
            "evidence_refs": [],
        },
        "access": {
            "owner": owner,
            "authorized_roles": list(authorized_roles),
            "access_log_locator": None,
        },
        "deletion": {
            "status": "pending",
            "method": "file-removal",
            "verification_required": True,
            "verification_artifact_locator": None,
            "scheduler_receipt_locator": None,
        },
        "incident": {
            "owner": owner,
            "reporting_channel": reporting_channel,
            "status": "not-triggered",
            "record_locator": None,
        },
    }
    validate_workflow_contract("data-lifecycle", lifecycle)
    return lifecycle
