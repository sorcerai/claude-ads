"""Safety tests for the durable Meta Ad Library quota ledger.

These tests intentionally use an injected clock and sleeper.  They never make
network requests; the collector owns the HTTP call performed inside
``QuotaBudget.attempt``.
"""

from __future__ import annotations

import email.utils
import json
import math
import multiprocessing
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import ad_library_quota  # noqa: E402


class FakeClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class ShortSleepClock(FakeClock):
    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += min(seconds, 1.0)


def _advance_shared_clock(shared_clock, seconds: float) -> None:
    with shared_clock.get_lock():
        shared_clock.value += seconds


def _process_attempt(path: str, shared_clock, entered) -> None:
    budget = ad_library_quota.QuotaBudget(
        path,
        clock=lambda: shared_clock.value,
        sleep=lambda seconds: _advance_shared_clock(shared_clock, seconds),
    )
    with budget.attempt():
        entered.put(shared_clock.value)


def _budget(tmp_path: Path, clock: FakeClock | None = None, **kwargs):
    clock = clock or FakeClock()
    return ad_library_quota.QuotaBudget(
        tmp_path / "quota.json",
        clock=clock.now,
        sleep=clock.sleep,
        **kwargs,
    ), clock


def _clear_headers() -> dict[str, str]:
    return {
        "X-App-Usage": json.dumps(
            {"call_count": 1, "total_cputime": 2, "total_time": 3}
        ),
    }


def test_rejects_unsafe_quota_configuration(tmp_path: Path):
    with pytest.raises(ValueError):
        ad_library_quota.QuotaBudget(tmp_path / "quota.json", hourly_limit=0)
    with pytest.raises(ValueError):
        ad_library_quota.QuotaBudget(tmp_path / "quota.json", hourly_limit=61)
    with pytest.raises(ValueError):
        ad_library_quota.QuotaBudget(tmp_path / "quota.json", min_interval=64.99)
    with pytest.raises(ValueError):
        ad_library_quota.QuotaBudget(tmp_path / "quota.json", usage_threshold=0)
    with pytest.raises(ValueError):
        ad_library_quota.QuotaBudget(tmp_path / "quota.json", usage_threshold=50.01)


def test_attempt_reserves_before_network_and_persists_failed_call(tmp_path: Path):
    budget, clock = _budget(tmp_path)

    with pytest.raises(RuntimeError):
        with budget.attempt():
            state = json.loads((tmp_path / "quota.json").read_text())
            assert state["attempts"] == [clock.value]
            raise RuntimeError("simulated transport failure")

    state = json.loads((tmp_path / "quota.json").read_text())
    assert state["attempts"] == [clock.value]


def test_spacing_is_enforced_between_retries_while_lock_is_held(tmp_path: Path):
    budget, clock = _budget(tmp_path)

    with budget.attempt():
        pass
    with budget.attempt():
        pass

    assert clock.sleeps == [65.0]
    state = json.loads((tmp_path / "quota.json").read_text())
    assert state["attempts"] == [1_000.0, 1_065.0]


def test_spacing_rechecks_after_short_sleep(tmp_path: Path):
    clock = ShortSleepClock()
    budget, _ = _budget(tmp_path, clock=clock)
    with budget.attempt():
        pass
    with budget.attempt():
        pass

    assert clock.value == 1_065.0
    assert len(clock.sleeps) == 65


def test_hourly_cap_blocks_without_sleeping_for_the_window(tmp_path: Path):
    budget, clock = _budget(tmp_path, hourly_limit=2)

    with budget.attempt():
        pass
    with budget.attempt():
        pass

    with pytest.raises(ad_library_quota.QuotaDeferred) as deferred:
        with budget.attempt():
            raise AssertionError("network context must not be entered")

    assert deferred.value.reason == "hourly-cap"
    assert deferred.value.retry_at == 4_600.0
    assert clock.sleeps == [65.0]


def test_every_retry_is_a_separate_reservation(tmp_path: Path):
    budget, clock = _budget(tmp_path, hourly_limit=3)

    for _ in range(3):
        with budget.attempt():
            pass

    state = json.loads((tmp_path / "quota.json").read_text())
    assert len(state["attempts"]) == 3
    assert state["attempts"] == [1_000.0, 1_065.0, 1_130.0]


def test_second_budget_uses_its_stricter_hourly_limit(tmp_path: Path):
    budget, _ = _budget(tmp_path)
    with budget.attempt():
        pass

    stricter = ad_library_quota.QuotaBudget(
        tmp_path / "quota.json",
        hourly_limit=1,
        clock=budget.clock,
        sleep=budget.sleep,
    )
    with pytest.raises(ad_library_quota.QuotaDeferred) as deferred:
        with stricter.attempt():
            raise AssertionError("stricter budget must block before network")
    assert deferred.value.reason == "hourly-cap"


def test_rolling_hour_expiry_allows_next_attempt_at_exact_boundary(tmp_path: Path):
    budget, clock = _budget(tmp_path, hourly_limit=1)
    with budget.attempt():
        pass

    clock.value = 4_600.0
    with budget.attempt():
        pass

    state = json.loads((tmp_path / "quota.json").read_text())
    assert state["attempts"] == [4_600.0]


def test_processes_serialize_reservations_in_one_shared_ledger(tmp_path: Path):
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("process-lock test requires fork")
    context = multiprocessing.get_context("fork")
    shared_clock = context.Value("d", 1_000.0)
    entered = context.Queue()
    path = str(tmp_path / "quota.json")
    processes = [
        context.Process(target=_process_attempt, args=(path, shared_clock, entered))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert sorted(entered.get() for _ in processes) == [1_000.0, 1_065.0]
    state = json.loads((tmp_path / "quota.json").read_text())
    assert state["attempts"] == [1_000.0, 1_065.0]


def test_official_account_usage_shape_is_numeric_and_thresholded(tmp_path: Path):
    budget, _ = _budget(tmp_path)
    headers = {
        "X-Ad-Account-Usage": json.dumps(
            {
                "acc_id_util_pct": 51,
                "reset_time_duration": 300,
                "ads_api_access_tier": "BASIC",
            }
        )
    }

    with budget.attempt():
        observation = budget.observe(headers, status_code=200)

    assert observation["stop_reason"] == "usage-threshold"
    assert observation["usage"]["x-ad-account-usage"]["acc_id_util_pct"] == 51.0
    assert observation["usage"]["x-ad-account-usage"]["reset_time_duration"] == 300.0
    assert "BASIC" not in json.dumps(observation)


def test_reset_duration_extends_provider_backoff_without_tier_false_positive(
    tmp_path: Path,
):
    budget, clock = _budget(tmp_path)
    headers = {
        "X-Ad-Account-Usage": json.dumps(
            {
                "acc_id_util_pct": 49,
                "reset_time_duration": 300,
                "ads_api_access_tier": "BASIC",
            }
        )
    }

    with budget.attempt():
        observation = budget.observe(headers, status_code=200)

    assert observation["stop_reason"] == "provider-backoff"
    assert observation["retry_at"] >= clock.value + 300


def test_worst_supported_header_dimension_requests_stop(tmp_path: Path):
    budget, _ = _budget(tmp_path)
    headers = {
        "X-App-Usage": json.dumps({"call_count": 1}),
        "X-Business-Use-Case-Usage": json.dumps({"key": [{"total_time": 51}]}),
        "X-Ad-Account-Usage": json.dumps({"acc_id_util_pct": 2}),
    }

    with budget.attempt():
        observation = budget.observe(headers, status_code=200)

    assert observation["stop_reason"] == "usage-threshold"
    assert observation["retry_at"] >= 4_600.0


def test_provider_regain_and_retry_after_are_never_shortened(tmp_path: Path):
    budget, clock = _budget(tmp_path)
    headers = {
        "X-Business-Use-Case-Usage": json.dumps(
            {"key": [{"call_count": 50, "estimated_time_to_regain_access": 120}]}
        ),
        "Retry-After": "30",
    }

    with budget.attempt():
        observation = budget.observe(headers, status_code=429)

    assert observation["stop_reason"] == "provider-throttle"
    assert observation["retry_at"] >= clock.value + 120 * 60


def test_missing_usage_header_fails_closed_for_successful_response(tmp_path: Path):
    budget, clock = _budget(tmp_path)

    with budget.attempt():
        observation = budget.observe({}, status_code=200)

    assert observation["stop_reason"] == "usage-unavailable"
    assert observation["retry_at"] >= clock.value + 3600


def test_malformed_usage_header_fails_closed_without_coercion(tmp_path: Path):
    budget, clock = _budget(tmp_path)
    headers = {"X-App-Usage": json.dumps({"call_count": "not-a-number"})}

    with budget.attempt():
        observation = budget.observe(headers, status_code=200)

    assert observation["usage"] is None
    assert observation["stop_reason"] == "usage-invalid"
    assert observation["retry_at"] >= clock.value + 3600


def test_nonfinite_usage_is_rejected(tmp_path: Path):
    budget, _ = _budget(tmp_path)
    headers = {"X-App-Usage": '{"call_count": NaN}'}

    with budget.attempt():
        observation = budget.observe(headers, status_code=200)

    assert observation["usage"] is None
    assert observation["stop_reason"] == "usage-invalid"


def test_throttle_error_persists_stop_across_restart(tmp_path: Path):
    budget, clock = _budget(tmp_path)
    with budget.attempt():
        observation = budget.observe(_clear_headers(), status_code=400, error_code=613)
    assert observation["stop_reason"] == "provider-throttle"

    restarted = ad_library_quota.QuotaBudget(
        tmp_path / "quota.json", clock=clock.now, sleep=clock.sleep
    )
    with pytest.raises(ad_library_quota.QuotaDeferred) as deferred:
        with restarted.attempt():
            raise AssertionError("persisted provider stop must block before network")
    assert deferred.value.reason == "provider-throttle"
    assert deferred.value.retry_at >= clock.value + 3600


def test_corrupt_ledger_fails_closed(tmp_path: Path):
    path = tmp_path / "quota.json"
    path.write_text('{"attempts": ["bad"]}', encoding="utf-8")
    budget = ad_library_quota.QuotaBudget(
        path, clock=lambda: 1_000.0, sleep=lambda _: None
    )

    with pytest.raises(ad_library_quota.QuotaDeferred) as deferred:
        with budget.attempt():
            raise AssertionError("corrupt ledger must block before network")
    assert deferred.value.reason == "ledger-invalid"
    assert math.isfinite(deferred.value.retry_at)


def test_symlinked_ledger_path_fails_closed(tmp_path: Path):
    target = tmp_path / "real.json"
    target.write_text("{}", encoding="utf-8")
    path = tmp_path / "quota.json"
    path.symlink_to(target)
    budget = ad_library_quota.QuotaBudget(
        path, clock=lambda: 1_000.0, sleep=lambda _: None
    )

    with pytest.raises(ad_library_quota.QuotaDeferred) as deferred:
        with budget.attempt():
            raise AssertionError("symlinked ledger must block before network")
    assert deferred.value.reason == "ledger-invalid"


def test_symlinked_lock_path_fails_closed(tmp_path: Path):
    path = tmp_path / "quota.json"
    lock_path = tmp_path / ".quota.json.lock"
    lock_path.symlink_to(tmp_path / "real.lock")
    budget = ad_library_quota.QuotaBudget(
        path, clock=lambda: 1_000.0, sleep=lambda _: None
    )

    with pytest.raises(ad_library_quota.QuotaDeferred) as deferred:
        with budget.attempt():
            raise AssertionError("symlinked lock must block before network")
    assert deferred.value.reason == "ledger-invalid"


def test_clock_rollback_fails_closed(tmp_path: Path):
    budget, clock = _budget(tmp_path)
    with budget.attempt():
        pass

    clock.value -= 1
    with pytest.raises(ad_library_quota.QuotaDeferred) as deferred:
        with budget.attempt():
            raise AssertionError("clock rollback must block before network")
    assert deferred.value.reason == "clock-rollback"
    assert deferred.value.retry_at >= 4_600.0


def test_observe_requires_an_active_attempt(tmp_path: Path):
    budget, _ = _budget(tmp_path)
    with pytest.raises(RuntimeError):
        budget.observe(_clear_headers(), status_code=200)


def test_authentication_errors_remain_non_retryable(tmp_path: Path):
    budget, _ = _budget(tmp_path)
    with budget.attempt():
        observation = budget.observe(_clear_headers(), status_code=401, error_code=190)

    assert observation["stop_reason"] == "authentication"


def test_one_percent_usage_is_below_configured_threshold(tmp_path: Path):
    budget, _ = _budget(tmp_path)
    with budget.attempt():
        observation = budget.observe(
            {"X-App-Usage": json.dumps({"call_count": 1})},
            status_code=200,
        )
    assert observation["stop_reason"] is None


def test_empty_or_unknown_only_usage_fails_closed(tmp_path: Path):
    for payload in ("{}", json.dumps({"type": "ads_archive"})):
        budget, _ = _budget(tmp_path / str(len(payload)))
        with budget.attempt():
            observation = budget.observe({"X-App-Usage": payload}, status_code=200)
        assert observation["stop_reason"] == "usage-invalid"


def test_business_usage_type_label_is_ignored_safely(tmp_path: Path):
    budget, _ = _budget(tmp_path)
    headers = {
        "X-Business-Use-Case-Usage": json.dumps(
            {"account": [{"type": "ads_archive", "call_count": 1}]}
        )
    }
    with budget.attempt():
        observation = budget.observe(headers, status_code=200)
    assert observation["stop_reason"] is None


def test_http_429_stops_even_with_low_valid_usage(tmp_path: Path):
    budget, clock = _budget(tmp_path)
    with budget.attempt():
        observation = budget.observe(
            {"X-App-Usage": json.dumps({"call_count": 1})},
            status_code=429,
        )
    assert observation["stop_reason"] == "provider-throttle"
    assert observation["retry_at"] >= clock.value + 3600


def test_http_date_retry_after_is_honored(tmp_path: Path):
    budget, clock = _budget(tmp_path)
    retry_at = clock.value + 7_200
    headers = {
        "X-App-Usage": json.dumps({"call_count": 1}),
        "Retry-After": email.utils.formatdate(retry_at, usegmt=True),
    }
    with budget.attempt():
        observation = budget.observe(headers, status_code=200)
    assert observation["stop_reason"] == "provider-backoff"
    assert observation["retry_at"] >= retry_at


def test_provider_retry_after_extends_throttle_stop(tmp_path: Path):
    budget, clock = _budget(tmp_path)
    with budget.attempt():
        observation = budget.observe(
            {
                "X-App-Usage": json.dumps({"call_count": 1}),
                "Retry-After": "7200",
            },
            status_code=429,
            error_code=613,
        )
    assert observation["stop_reason"] == "provider-throttle"
    assert observation["retry_at"] >= clock.value + 7_200


def test_valid_regain_survives_another_malformed_usage_header(tmp_path: Path):
    budget, clock = _budget(tmp_path)
    headers = {
        "X-App-Usage": '{"call_count":"bad"}',
        "X-Business-Use-Case-Usage": json.dumps(
            {"ads_archive": [{"estimated_time_to_regain_access": 120}]}
        ),
    }

    with budget.attempt():
        observation = budget.observe(headers, status_code=200)

    assert observation["stop_reason"] == "usage-invalid"
    assert observation["retry_at"] >= clock.value + 7_200


@pytest.mark.parametrize("payload", [{"unknown": 1}, {"call_count": -1}, 1])
def test_invalid_usage_cannot_authorize_another_request(tmp_path: Path, payload):
    budget, clock = _budget(tmp_path)
    with budget.attempt():
        observation = budget.observe(
            {"X-App-Usage": json.dumps(payload)}, status_code=200
        )
    assert observation["stop_reason"] == "usage-invalid"
    restarted = ad_library_quota.QuotaBudget(
        tmp_path / "quota.json", clock=clock.now, sleep=clock.sleep
    )
    with pytest.raises(ad_library_quota.QuotaDeferred):
        with restarted.attempt():
            pytest.fail("invalid usage allowed another request")


@pytest.mark.parametrize(
    "header,payload",
    [
        ("X-App-Usage", {"nested": {"call_count": 1}}),
        ("X-Business-Use-Case-Usage", {"call_count": 1}),
        ("X-Ad-Account-Usage", {"call_count": 1}),
    ],
)
def test_wrong_header_shape_cannot_authorize_requests(tmp_path, header, payload):
    budget, _ = _budget(tmp_path)
    with budget.attempt():
        observed = budget.observe({header: json.dumps(payload)}, status_code=200)
    assert observed["stop_reason"] == "usage-invalid"
    with pytest.raises(ad_library_quota.QuotaDeferred):
        with budget.attempt():
            pytest.fail("wrong-shaped header admitted a request")


def test_regain_survives_invalid_sibling_in_same_header(tmp_path):
    budget, clock = _budget(tmp_path)
    with budget.attempt():
        observed = budget.observe(
            {
                "X-Business-Use-Case-Usage": json.dumps(
                    {
                        "account": [
                            {
                                "call_count": "invalid",
                                "estimated_time_to_regain_access": 120,
                            }
                        ]
                    }
                ),
                "Retry-After": "30",
            },
            status_code=200,
        )
    assert observed["stop_reason"] == "usage-invalid"
    assert observed["retry_at"] >= clock.value + 7200


@pytest.mark.parametrize("until,reason", [(None, "provider-throttle"), (4600, None)])
def test_inconsistent_persisted_stop_fails_closed(tmp_path, until, reason):
    budget, _ = _budget(tmp_path)
    (tmp_path / "quota.json").write_text(
        json.dumps(
            {
                "version": 1,
                "attempts": [],
                "last_clock": 1000,
                "blocked_until": until,
                "blocked_reason": reason,
            }
        )
    )
    with pytest.raises(ad_library_quota.QuotaDeferred) as caught:
        with budget.attempt():
            pytest.fail("corrupt stop admitted a request")
    assert caught.value.reason == "ledger-invalid"


@pytest.mark.parametrize("now", [float("nan"), None])
def test_invalid_clock_returns_finite_recovery(tmp_path, now):
    budget = ad_library_quota.QuotaBudget(tmp_path / "quota.json", clock=lambda: now)
    with pytest.raises(ad_library_quota.QuotaDeferred) as caught:
        with budget.attempt():
            pytest.fail("invalid clock admitted a request")
    assert math.isfinite(caught.value.retry_at)


def test_query_error_does_not_block_corrected_request_as_authentication(tmp_path):
    budget, _ = _budget(tmp_path)
    with budget.attempt():
        observed = budget.observe(_clear_headers(), status_code=400, error_code=100)
    assert observed["stop_reason"] is None
    with budget.attempt():
        pass


def test_windows_lock_retries_contention_without_false_corruption(
    tmp_path, monkeypatch
):
    import errno

    class WindowsLocks:
        LK_LOCK, LK_NBLCK, LK_UNLCK = 1, 2, 3
        attempts = 0

        def locking(self, fd, mode, size):
            if mode == self.LK_UNLCK:
                return
            self.attempts += 1
            if self.attempts <= 12:
                raise OSError(errno.EACCES, "synthetic lock contention")

    locks = WindowsLocks()
    monkeypatch.setattr(ad_library_quota, "fcntl", None)
    monkeypatch.setattr(ad_library_quota, "msvcrt", locks)
    monkeypatch.setattr(ad_library_quota.time, "sleep", lambda _: None)
    budget, _ = _budget(tmp_path)
    with budget.attempt():
        assert locks.attempts == 13
