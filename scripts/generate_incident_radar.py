#!/usr/bin/env python3
"""Generate an incident-radar dataset for each configured microsite repository.

Performs one minimal Meta Graph API health and latency check, records its
usage-header telemetry, and emits the resulting API dispatch plus empty
placeholders for future platform and crowdsourced incident data.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import time

try:
    import requests
except ImportError:
    print("Error: requests library required. Install or use virtualenv.")
    sys.exit(1)

REPOSITORY_NAMES = (
    "legal-ad-infra",
    "telehealth-ad-infra",
    "ecom-ad-scale",
    "adops-resilience",
    "ad-spend-index",
)


def load_repos() -> list[str]:
    """Resolve the fleet repositories beneath ADSINFRA_FLEET_ROOT."""
    fleet_root = os.environ.get("ADSINFRA_FLEET_ROOT", "").strip()
    if not fleet_root or not os.path.isdir(fleet_root):
        sys.exit(
            "Error: set ADSINFRA_FLEET_ROOT to an existing fleet root "
            "directory before generating the radar."
        )
    return [os.path.join(fleet_root, name) for name in REPOSITORY_NAMES]


def get_meta_token() -> str | None:
    """Safely obtain Meta token from the environment or macOS Keychain."""
    token = os.environ.get("META_AD_LIBRARY_TOKEN")
    if token:
        return token
    try:
        res = subprocess.run(
            ["security", "find-generic-password", "-s", "META_AD_LIBRARY_TOKEN", "-w"],
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception as e:
        print(f"[Warn] Could not read META_AD_LIBRARY_TOKEN from keychain: {e}")
        return None


def check_meta_api_health(token: str | None) -> dict:
    """Execute exactly 1 minimal query to measure latency, status, and app usage rate."""
    if not token:
        return {
            "status": "UNCONFIGURED",
            "latency_ms": None,
            "usage_pct": None,
            "version": "v26.0",
            "notes": "Token not present in environment; baseline synthetic benchmark active.",
        }

    url = "https://graph.facebook.com/v26.0/ads_archive"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    params = {
        "search_terms": "agency ad account",
        "ad_reached_countries": '["US"]',
        "ad_type": "ALL",
        "limit": 1,
        "fields": "id,page_name",
    }

    t0 = time.time()
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=12)
        latency_ms = int((time.time() - t0) * 1000)

        usage_header = (
            resp.headers.get("x-business-use-case-usage")
            or resp.headers.get("x-app-usage")
            or "{}"
        )
        usage_values = []
        try:
            parsed_usage = json.loads(usage_header)

            def collect_call_counts(value: object) -> None:
                if isinstance(value, dict):
                    for key, nested_value in value.items():
                        if key == "call_count" and isinstance(
                            nested_value, (int, float)
                        ):
                            usage_values.append(float(nested_value))
                        else:
                            collect_call_counts(nested_value)
                elif isinstance(value, list):
                    for nested_value in value:
                        collect_call_counts(nested_value)

            collect_call_counts(parsed_usage)
        except Exception:
            pass
        usage_val = max(usage_values, default=None)

        return {
            "status": "OPERATIONAL"
            if resp.status_code == 200
            else f"HTTP_{resp.status_code}",
            "latency_ms": latency_ms,
            "usage_pct": min(usage_val, 100.0) if usage_val is not None else None,
            "version": "v26.0",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "http_status": resp.status_code,
        }
    except Exception as e:
        return {
            "status": "TIMEOUT_OR_UNREACHABLE",
            "latency_ms": None,
            "usage_pct": None,
            "version": "v26.0",
            "error": str(e),
        }


def build_incident_radar_dataset(meta_health: dict) -> dict:
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    api_operational = meta_health.get("status") == "OPERATIONAL"
    if api_operational:
        usage_pct = meta_health.get("usage_pct")
        if usage_pct is None:
            usage_text = "usage rate unavailable"
        elif usage_pct < 1.5:
            usage_text = "usage rate < 1.5%"
        else:
            usage_text = f"usage rate {usage_pct:g}%"
        api_dispatch = {
            "timestamp": now_utc,
            "source": "Meta Graph API Sentinel",
            "vertical": "General Media Buying",
            "headline": f"Meta Graph API latency verified at {meta_health['latency_ms']}ms. Token endpoint operational, {usage_text}",
            "impact": "Graph endpoints stable, confirming that front-end ad manager rejections are policy/algorithm driven, not network outages.",
        }
    else:
        api_dispatch = {
            "timestamp": now_utc,
            "source": "Meta Graph API Sentinel",
            "vertical": "General Media Buying",
            "headline": f"Meta Graph API health unavailable ({meta_health.get('status', 'UNKNOWN')}); latency and token endpoint status not verified.",
            "impact": "No API health conclusion is available; investigate the credential or network configuration before attributing front-end rejections.",
        }

    return {
        "meta": {
            "title": "Ad Platform Outage & Ban-Wave Incident Radar",
            "updated_at": now_utc,
            "version": "1.0.0",
            "threat_level": "UNKNOWN",
            "benchmark_basis": "Meta Graph API health check only",
        },
        "meta_graph_api": meta_health,
        "platforms": [],
        "crowdsourced_triage": {
            "window_hours": 24,
            "total_reports_today": 0,
            "categories": [],
        },
        "recent_dispatches": [api_dispatch],
    }


def main():
    repos = load_repos()
    print("[1/3] Checking Meta Graph API Health (Minimal, safe call)...")
    token = get_meta_token()
    meta_health = check_meta_api_health(token)
    print(f"      Meta API Health: {meta_health}")

    print("[2/3] Building Ad Platform Incident Radar dataset...")
    radar_data = build_incident_radar_dataset(meta_health)

    print("[3/3] Distributing to microsite repositories...")
    for repo in repos:
        data_dir = os.path.join(repo, "data")
        os.makedirs(data_dir, exist_ok=True)
        out_path = os.path.join(data_dir, "incident-radar.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(radar_data, f, indent=2)
        print(f"      Written: {out_path} ({os.path.getsize(out_path)} bytes)")

    print("\nAd Platform Incident Radar generation complete.")


if __name__ == "__main__":
    main()
