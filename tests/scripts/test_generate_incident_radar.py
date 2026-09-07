"""Behavioral checks for incident radar telemetry normalization."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import generate_incident_radar  # noqa: E402


class _Response:
    status_code = 200
    headers = {
        "x-business-use-case-usage": json.dumps(
            {"business_use_case": {"call_count": 42, "total_time": 4}}
        )
    }


def test_health_check_aggregates_nested_business_use_case_metrics(monkeypatch):
    monkeypatch.setattr(
        generate_incident_radar.requests, "get", lambda *args, **kwargs: _Response()
    )

    health = generate_incident_radar.check_meta_api_health("synthetic-token")

    assert health["status"] == "OPERATIONAL"
    assert health["usage_pct"] == 42.0
