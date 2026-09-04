from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import pytest

from claude_ads_core.warehouse import (
    ApiReadPurpose,
    DirectApiReadForbiddenError,
    MetaBudgetManager,
    MetaQuerySpec,
    MetaUsageMonitor,
    MetaUsageStats,
    MetaWarehouseReadPlane,
    MutationDisabledError,
    Provenance,
    RateBudgetExceededError,
    StaleDataError,
    WarehouseError,
    WarehouseSnapshot,
)


def _sample_provenance() -> Provenance:
    return Provenance(
        run_id="run-20260904-001",
        client_id="client-demo",
        purpose="ingestion_service",
        query_digest="sha256:" + "a" * 64,
        source_digest="sha256:" + "b" * 64,
    )


def _sample_account_snapshot(window_end: str = "2026-08-01") -> dict:
    return {
        "schema_version": "2.0.0",
        "account": {"platform": "meta", "account_id": "act_12345678"},
        "window": {"start": "2026-07-01", "end": window_end},
        "currency": "USD",
        "spend": 1500.0,
        "measurement_context": {
            "timezone": "America/New_York",
            "currency": "USD",
            "profile_id": "meta-marketing-api-v1",
            "source_format": "meta_insights_api",
            "source_ids": ["sha256:" + "b" * 64],
            "report_grain": ["campaign", "date"],
            "conversion_definition": "purchase",
            "conversion_actions": ["purchase"],
            "attribution_model": "7d_click_1d_view",
            "click_attribution_window": {"value": 7, "unit": "day"},
            "view_attribution_window": {"value": 1, "unit": "day"},
            "counting_behavior": "standard",
            "as_of": window_end,
            "data_finalization": "unknown",
            "modeled_data_treatment": "excluded",
            "missing_fields": [],
            "unsupported_fields": [],
        },
        "campaigns": [{"campaign_id": "cmp-1", "name": "Prospecting", "spend": 1500.0}],
        "creatives": [],
        "conversions": [{"action": "purchase", "count": 25.0}],
        "budgets": [],
    }


def test_meta_query_spec_canonical_digest_is_deterministic():
    spec1 = MetaQuerySpec(
        ad_account_id="act_12345678",
        level="campaign",
        fields=("spend", "campaign_name", "campaign_id"),
        time_range={"until": "2026-08-01", "since": "2026-07-01"},
    )
    spec2 = MetaQuerySpec(
        ad_account_id="act_12345678",
        level="campaign",
        fields=("campaign_id", "campaign_name", "spend"),
        time_range={"since": "2026-07-01", "until": "2026-08-01"},
    )
    assert spec1.canonical_digest() == spec2.canonical_digest()
    assert spec1.canonical_digest().startswith("sha256:")


def test_meta_query_spec_rejects_invalid_inputs():
    with pytest.raises(WarehouseError, match="ad_account_id must be a non-empty string"):
        MetaQuerySpec(ad_account_id="   ")
    with pytest.raises(WarehouseError, match="invalid query level"):
        MetaQuerySpec(ad_account_id="act_123", level="invalid_level")


def test_provenance_validation_fails_closed():
    with pytest.raises(WarehouseError, match="provenance.run_id"):
        Provenance(
            run_id="",
            client_id="client-1",
            purpose="test",
            query_digest="sha256:" + "a" * 64,
            source_digest="sha256:" + "b" * 64,
        )
    with pytest.raises(WarehouseError, match="provenance.query_digest"):
        Provenance(
            run_id="run-1",
            client_id="client-1",
            purpose="test",
            query_digest="not-a-sha256",
            source_digest="sha256:" + "b" * 64,
        )


def test_warehouse_snapshot_freshness_sla_enforcement():
    fetch_time = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    fetch_iso = fetch_time.isoformat()
    snapshot = WarehouseSnapshot(
        schema_version="1.0.0",
        snapshot_id="wh-test-1",
        platform="meta",
        account_id="act_12345678",
        account_snapshot=_sample_account_snapshot(),
        fetched_at=fetch_iso,
        extracted_at=fetch_iso,
        freshness_sla_seconds=900,  # 15 minutes
        finalization_status="final",
        provenance=_sample_provenance(),
    )

    # 10 minutes later: still fresh
    eval_time_fresh = fetch_time + timedelta(minutes=10)
    assert not snapshot.is_stale(as_of=eval_time_fresh)
    snapshot.validate_freshness(as_of=eval_time_fresh)

    # 16 minutes later: stale!
    eval_time_stale = fetch_time + timedelta(minutes=16)
    assert snapshot.is_stale(as_of=eval_time_stale)
    with pytest.raises(StaleDataError, match="exceeds freshness SLA"):
        snapshot.validate_freshness(as_of=eval_time_stale)


def test_freshness_is_not_inferred_from_coverage_as_of_or_observed_at():
    # Historical data window from months ago
    fetch_time = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    fetch_iso = fetch_time.isoformat()
    acc_snap = _sample_account_snapshot(window_end="2026-05-01")
    # measurement_context.as_of is "2026-05-01"
    assert acc_snap["measurement_context"]["as_of"] == "2026-05-01"

    snapshot = WarehouseSnapshot(
        schema_version="1.0.0",
        snapshot_id="wh-test-2",
        platform="meta",
        account_id="act_12345678",
        account_snapshot=acc_snap,
        fetched_at=fetch_iso,
        extracted_at=fetch_iso,
        freshness_sla_seconds=900,
        finalization_status="final",
        provenance=_sample_provenance(),
    )

    # Evaluated at fetch time + 5 minutes: must be FRESH even though window.end is months old
    eval_time = fetch_time + timedelta(minutes=5)
    assert not snapshot.is_stale(as_of=eval_time)


def test_warehouse_snapshot_to_dict_and_from_dict_roundtrip():
    fetch_time = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc).isoformat()
    usage = MetaUsageStats(
        call_count_pct=25.0,
        cpu_time_pct=15.0,
        total_time_pct=20.0,
        ad_account_util_pct=30.0,
        estimated_time_to_regain_access_minutes=0,
    )
    snapshot = WarehouseSnapshot(
        schema_version="1.0.0",
        snapshot_id="wh-roundtrip",
        platform="meta",
        account_id="act_12345678",
        account_snapshot=_sample_account_snapshot(),
        fetched_at=fetch_time,
        extracted_at=fetch_time,
        freshness_sla_seconds=900,
        finalization_status="provisional",
        provenance=_sample_provenance(),
        usage=usage,
    )

    serialized = snapshot.to_dict()
    assert serialized["snapshot_id"] == "wh-roundtrip"
    assert serialized["usage"]["max_utilization_pct"] == 30.0

    restored = WarehouseSnapshot.from_dict(serialized)
    assert restored.snapshot_id == snapshot.snapshot_id
    assert restored.usage is not None
    assert restored.usage.ad_account_util_pct == 30.0
    assert restored.finalization_status == "provisional"


def test_meta_usage_monitor_header_parsing_and_backoff():
    headers = {
        "x-app-usage": json.dumps({"call_count": 82, "total_cputime": 40, "total_time": 50}),
        "x-business-use-case-usage": json.dumps({
            "act_12345678": [
                {
                    "type": "ads_insights",
                    "call_count": 70,
                    "total_cputime": 30,
                    "total_time": 40,
                    "estimated_time_to_regain_access": 3,
                }
            ]
        }),
        "x-ad-account-usage": json.dumps({"acc_id_util_pct": 88.5}),
    }
    usage = MetaUsageMonitor.parse_headers(headers)
    assert usage.call_count_pct == 82.0
    assert usage.ad_account_util_pct == 88.5
    assert usage.estimated_time_to_regain_access_minutes == 3
    assert usage.max_utilization_pct == 88.5

    # Should backoff according to estimated_time_to_regain_access_minutes (3 min = 180s)
    backoff = MetaUsageMonitor.calculate_backoff(usage)
    assert backoff == 180.0

    # Test without regain_access, but with high utilization (>= 80%)
    usage_no_regain = MetaUsageStats(call_count_pct=85.0)
    assert MetaUsageMonitor.calculate_backoff(usage_no_regain) == 5.0

    # Test throttling response code 429
    assert MetaUsageMonitor.calculate_backoff(MetaUsageStats(), status_code=429, retry_attempt=1) == 10.0


def test_meta_budget_manager_enforces_limits():
    budget = MetaBudgetManager(app_call_budget=2, account_call_budget=2, account_complexity_budget=10)
    app_id = "app_test"
    account_id = "act_1"

    # Call 1: OK
    budget.check_budget(app_id, account_id, estimated_complexity=3)
    budget.record_call(app_id, account_id, complexity=3)

    # Call 2: OK
    budget.check_budget(app_id, account_id, estimated_complexity=3)
    budget.record_call(app_id, account_id, complexity=3)

    # Call 3: Exceeds call budget
    with pytest.raises(RateBudgetExceededError, match="exceeded hourly call budget"):
        budget.check_budget(app_id, account_id, estimated_complexity=3)

    # Complexity limit test
    budget2 = MetaBudgetManager(app_call_budget=10, account_call_budget=10, account_complexity_budget=5)
    budget2.check_budget("app_1", "act_2", estimated_complexity=3)
    budget2.record_call("app_1", "act_2", complexity=3)
    with pytest.raises(RateBudgetExceededError, match="complexity budget exceeded"):
        budget2.check_budget("app_1", "act_2", estimated_complexity=4)


def test_analytical_agent_direct_read_is_forbidden():
    read_plane = MetaWarehouseReadPlane()

    # Analytical agent cannot perform direct API reads
    with pytest.raises(DirectApiReadForbiddenError, match="Analytical agents cannot call Meta account-read tools directly"):
        read_plane.verify_read_permitted(ApiReadPurpose.ANALYSIS)

    with pytest.raises(DirectApiReadForbiddenError, match="Analytical agents cannot call Meta account-read tools directly"):
        read_plane.verify_read_permitted("analysis")

    # Ingestion service and cache recovery are permitted
    read_plane.verify_read_permitted(ApiReadPurpose.INGESTION_SERVICE)
    read_plane.verify_read_permitted(ApiReadPurpose.CACHE_RECOVERY)
    read_plane.verify_read_permitted(ApiReadPurpose.MUTATION_PRE_POST_VERIFICATION)


def test_28_day_finalization_semantics():
    read_plane = MetaWarehouseReadPlane()
    fetch_time = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)

    # Window end 10 days before fetch: within 28 days -> provisional
    window_provisional = "2026-08-25"
    assert read_plane.compute_finalization_status(window_provisional, fetch_time) == "provisional"

    # Window end 35 days before fetch: older than 28 days -> final
    window_final = "2026-07-20"
    assert read_plane.compute_finalization_status(window_final, fetch_time) == "final"


def test_read_plane_ingest_and_query_deduplication():
    read_plane = MetaWarehouseReadPlane()
    spec = MetaQuerySpec(ad_account_id="act_12345678", level="campaign")
    acc_snap = _sample_account_snapshot(window_end="2026-08-25")
    fetch_time = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)

    headers = {
        "x-app-usage": json.dumps({"call_count": 10, "total_cputime": 5, "total_time": 5})
    }
    snapshot = read_plane.ingest_snapshot(
        query_spec=spec,
        account_snapshot=acc_snap,
        provenance=_sample_provenance(),
        fetch_time=fetch_time,
        response_headers=headers,
    )

    assert snapshot.platform == "meta"
    assert snapshot.account_id == "act_12345678"
    assert snapshot.finalization_status == "provisional"
    assert snapshot.usage is not None
    assert snapshot.usage.call_count_pct == 10.0
    assert snapshot.account_snapshot["measurement_context"]["data_finalization"] == "provisional"

    # Retrieval from warehouse store
    retrieved = read_plane.get_snapshot(snapshot.snapshot_id, as_of=fetch_time + timedelta(minutes=5))
    assert retrieved.snapshot_id == snapshot.snapshot_id

    # Pre-scoring precondition: fresh snapshot passes
    read_plane.evaluate_scoring_precondition(retrieved, as_of=fetch_time + timedelta(minutes=10))

    # Pre-scoring precondition: stale snapshot fails closed
    with pytest.raises(StaleDataError, match="exceeds freshness SLA"):
        read_plane.evaluate_scoring_precondition(retrieved, as_of=fetch_time + timedelta(minutes=20))


def test_read_plane_mutations_permanently_disabled():
    read_plane = MetaWarehouseReadPlane()
    assert read_plane.writes_enabled is False
    with pytest.raises(MutationDisabledError, match="Account writes remain disabled in v2"):
        read_plane.apply_mutation()
