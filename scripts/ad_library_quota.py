"""Durable, conservative quota enforcement for Meta Ad Library requests.

The collector owns the HTTP request.  It must execute that request inside
``QuotaBudget.attempt()`` and call ``QuotaBudget.observe`` for every response
before deciding whether to retry or page again.
"""

from __future__ import annotations

import email.utils
import errno
import json
import math
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

_IGNORED_USAGE = object()


def _normalise_usage(value: Any, key: str | None = None) -> Any:
    """Keep safe numeric usage fields and known non-sensitive labels."""
    if isinstance(value, bool):
        raise ValueError("boolean usage value")
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError("usage must be finite and nonnegative")
        if key not in {
            "call_count",
            "total_cputime",
            "total_time",
            "acc_id_util_pct",
            "estimated_time_to_regain_access",
            "reset_time_duration",
        }:
            raise ValueError("unknown usage metric")
        return number
    if isinstance(value, list):
        normalised_list: list[Any] = []
        for item in value:
            normalised = _normalise_usage(item, key)
            if normalised is not _IGNORED_USAGE:
                normalised_list.append(normalised)
        if not normalised_list:
            raise ValueError("empty usage list")
        return normalised_list
    if isinstance(value, dict):
        normalised: dict[str, Any] = {}
        for child_key, item in value.items():
            if not isinstance(child_key, str):
                raise ValueError("non-string usage key")
            child = _normalise_usage(item, child_key)
            if child is not _IGNORED_USAGE:
                normalised[child_key] = child
        if not normalised:
            raise ValueError("empty usage object")
        return normalised
    if (
        isinstance(value, str)
        and key
        and key.lower()
        in {
            "ads_api_access_tier",
            "access_tier",
            "type",
        }
    ):
        return _IGNORED_USAGE
    raise ValueError("non-numeric usage value")


def _normalise_header(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ValueError("usage header must be an object")
    metrics = {"call_count", "total_cputime", "total_time"}
    if name == "x-business-use-case-usage":
        records = []
        for entries in value.values():
            if not isinstance(entries, list) or not entries:
                raise ValueError("business usage must map IDs to nonempty record lists")
            records.extend(entries)
    else:
        records = [value]
        if name == "x-ad-account-usage":
            metrics = {"acc_id_util_pct"}
    for record in records:
        if (
            not isinstance(record, dict)
            or not metrics.intersection(record)
            or any(isinstance(item, (list, dict)) for item in record.values())
        ):
            raise ValueError("usage record lacks valid percentage metrics")
    return _normalise_usage(value)


def _usage_percentage_signal(
    value: Any,
    key: str | None = None,
    threshold_limit: float = 50.0,
) -> bool:
    if isinstance(value, dict):
        return any(
            _usage_percentage_signal(item, child_key, threshold_limit)
            for child_key, item in value.items()
        )
    if isinstance(value, list):
        return any(
            _usage_percentage_signal(item, key, threshold_limit) for item in value
        )
    return (
        key in {"call_count", "total_cputime", "total_time", "acc_id_util_pct"}
        and value >= threshold_limit
    )


try:  # Unix, including macOS.
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows only.
    fcntl = None  # type: ignore[assignment]

try:  # Windows fallback; imported only where available.
    import msvcrt
except ImportError:  # pragma: no cover - exercised on Unix only.
    msvcrt = None  # type: ignore[assignment]


_SUPPORTED_HEADERS = (
    "x-app-usage",
    "x-business-use-case-usage",
    "x-ad-account-usage",
)
_THROTTLE_CODES = frozenset({4, 17, 32, 613})
_LEDGER_VERSION = 1
_HOUR = 3600.0


class QuotaDeferred(Exception):
    """The next request must wait until ``retry_at``."""

    def __init__(self, reason: str, retry_at: float) -> None:
        self.reason = str(reason)
        self.retry_at = float(retry_at)
        super().__init__(f"quota deferred ({self.reason}) until {self.retry_at}")


class _LedgerInvalid(ValueError):
    pass


class _UnsupportedPlatform(OSError):
    pass


def _finite_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _LedgerInvalid("expected finite number")
    result = float(value)
    if not math.isfinite(result):
        raise _LedgerInvalid("expected finite number")
    return result


def _find_provider_wait_seconds(value: Any, key: str | None = None) -> float:
    result = 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value) or value < 0:
            return 0.0
        if key == "estimated_time_to_regain_access":
            result = float(value) * 60.0
        elif key == "reset_time_duration":
            result = float(value)
    elif isinstance(value, list):
        for item in value:
            result = max(result, _find_provider_wait_seconds(item, key))
    elif isinstance(value, dict):
        for child_key, item in value.items():
            result = max(result, _find_provider_wait_seconds(item, child_key))
    return result


def _retry_after_seconds(value: Any, now: float) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = None
    if seconds is not None and math.isfinite(seconds):
        return max(0.0, seconds)
    if not isinstance(value, str):
        return 0.0
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        timestamp = parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return max(0.0, timestamp - now) if math.isfinite(timestamp) else 0.0


class _FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "_FileLock":
        if fcntl is None and msvcrt is None:
            raise _UnsupportedPlatform
        _ensure_safe_parent(self.path.parent)
        _reject_symlink(self.path)
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            _reject_symlink(self.path)
            if fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_EX)
            else:  # pragma: no cover - Windows only.
                while True:
                    try:
                        os.lseek(self.fd, 0, os.SEEK_SET)
                        msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                            raise
                        time.sleep(0.1)
        except OSError:
            self.close()
            raise
        return self

    def close(self) -> None:
        if self.fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows only.
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(self.fd)
            self.fd = None

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def _reject_symlink(path: Path) -> None:
    try:
        if path.is_symlink():
            raise _LedgerInvalid("symlink path")
    except OSError as exc:
        raise _LedgerInvalid("unreadable path") from exc


def _ensure_safe_parent(parent: Path) -> None:
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _LedgerInvalid("cannot create ledger directory") from exc
    current = parent
    while True:
        _reject_symlink(current)
        if current.parent == current:
            break
        current = current.parent


def _atomic_write(path: Path, state: dict[str, Any]) -> None:
    _reject_symlink(path)
    _ensure_safe_parent(path.parent)
    fd: int | None = None
    temporary: Path | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        _reject_symlink(temporary)
        payload = json.dumps(
            state, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _reject_symlink(path)
        os.replace(temporary, path)
        temporary = None
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _empty_state(now: float) -> dict[str, Any]:
    return {
        "version": _LEDGER_VERSION,
        "attempts": [],
        "blocked_until": None,
        "blocked_reason": None,
        "last_clock": now,
    }


def _read_state(path: Path) -> dict[str, Any]:
    try:
        _reject_symlink(path)
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty_state(0.0)
    except (OSError, UnicodeError) as exc:
        raise _LedgerInvalid("ledger unreadable") from exc
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise _LedgerInvalid("ledger malformed") from exc
    if not isinstance(value, dict) or value.get("version") != _LEDGER_VERSION:
        raise _LedgerInvalid("ledger version")
    attempts = value.get("attempts")
    if not isinstance(attempts, list):
        raise _LedgerInvalid("ledger attempts")
    normal_attempts = [_finite_number(item) for item in attempts]
    if any(item < 0 for item in normal_attempts) or normal_attempts != sorted(
        normal_attempts
    ):
        raise _LedgerInvalid("ledger attempt ordering")
    blocked_until = value.get("blocked_until")
    if blocked_until is not None:
        blocked_until = _finite_number(blocked_until)
        if blocked_until < 0:
            raise _LedgerInvalid("ledger blocked time")
    blocked_reason = value.get("blocked_reason")
    if blocked_reason is not None and (
        not isinstance(blocked_reason, str) or not blocked_reason
    ):
        raise _LedgerInvalid("ledger blocked reason")
    if (blocked_until is None) != (blocked_reason is None):
        raise _LedgerInvalid("ledger stop deadline and reason must be paired")
    last_clock = _finite_number(value.get("last_clock"))
    if last_clock < 0:
        raise _LedgerInvalid("ledger clock")
    return {
        "version": _LEDGER_VERSION,
        "attempts": normal_attempts,
        "blocked_until": blocked_until,
        "blocked_reason": blocked_reason,
        "last_clock": last_clock,
    }


class _Attempt:
    def __init__(self, budget: "QuotaBudget") -> None:
        self.budget = budget
        self.lock: _FileLock | None = None

    def __enter__(self) -> "_Attempt":
        if self.budget._active:
            raise RuntimeError("attempt contexts cannot be nested")
        self.lock = _FileLock(self.budget.lock_path)
        try:
            self.lock.__enter__()
            self.budget._begin_attempt()
            return self
        except QuotaDeferred:
            if self.lock is not None:
                self.lock.close()
            raise
        except (OSError, _LedgerInvalid, _UnsupportedPlatform) as exc:
            if self.lock is not None:
                self.lock.close()
            try:
                now = self.budget._now()
            except _LedgerInvalid:
                now = time.time()
            reason = (
                "platform-unsupported"
                if isinstance(exc, _UnsupportedPlatform)
                else "ledger-invalid"
            )
            raise QuotaDeferred(reason, now + _HOUR) from exc

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.budget._active = False
        if self.lock is not None:
            self.lock.close()
            self.lock = None


class QuotaBudget:
    """A shared, conservative rolling-hour request budget."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        hourly_limit: int = 60,
        min_interval: float = 65.0,
        usage_threshold: float = 50.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            isinstance(hourly_limit, bool)
            or not isinstance(hourly_limit, int)
            or not 1 <= hourly_limit <= 60
        ):
            raise ValueError("hourly_limit must be an integer from 1 through 60")
        if (
            isinstance(min_interval, bool)
            or not isinstance(min_interval, (int, float))
            or not math.isfinite(float(min_interval))
            or min_interval < 65
        ):
            raise ValueError("min_interval must be finite and at least 65 seconds")
        if (
            isinstance(usage_threshold, bool)
            or not isinstance(usage_threshold, (int, float))
            or not math.isfinite(float(usage_threshold))
            or not 0 < usage_threshold <= 50
        ):
            raise ValueError(
                "usage_threshold must be finite, greater than 0, and at most 50"
            )
        self.path = (
            Path(path).expanduser()
            if path is not None
            else Path.home() / ".claude-ads" / "ad-library-quota.json"
        )
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.hourly_limit = hourly_limit
        self.min_interval = float(min_interval)
        self.usage_threshold = float(usage_threshold)
        self.clock = clock
        self.sleep = sleep
        self._active = False
        self._state: dict[str, Any] | None = None

    def attempt(self) -> _Attempt:
        return _Attempt(self)

    def _now(self) -> float:
        now = self.clock()
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(float(now))
            or float(now) < 0
        ):
            raise _LedgerInvalid("clock invalid")
        return float(now)

    def _begin_attempt(self) -> None:
        now = self._now()
        state = _read_state(self.path)
        if state["last_clock"] and now < state["last_clock"]:
            raise QuotaDeferred(
                "clock-rollback", max(now + _HOUR, state["last_clock"] + _HOUR)
            )
        attempts = [item for item in state["attempts"] if now - item < _HOUR]
        if any(item > now for item in attempts):
            raise QuotaDeferred(
                "clock-rollback", max(item + _HOUR for item in attempts)
            )
        blocked_until = state["blocked_until"]
        if blocked_until is not None and blocked_until > now:
            raise QuotaDeferred(
                state["blocked_reason"] or "provider-backoff", blocked_until
            )
        if len(attempts) >= self.hourly_limit:
            raise QuotaDeferred("hourly-cap", attempts[0] + _HOUR)
        while attempts:
            wait = attempts[-1] + self.min_interval - now
            if wait <= 0:
                break
            before_sleep = now
            self.sleep(wait)
            now = self._now()
            if now < state["last_clock"]:
                raise QuotaDeferred(
                    "clock-rollback", max(now + _HOUR, state["last_clock"] + _HOUR)
                )
            if now <= before_sleep:
                raise QuotaDeferred("spacing", attempts[-1] + self.min_interval)
            attempts = [item for item in attempts if now - item < _HOUR]
            if len(attempts) >= self.hourly_limit:
                raise QuotaDeferred("hourly-cap", attempts[0] + _HOUR)
        attempts.append(now)
        self._state = {
            "version": _LEDGER_VERSION,
            "attempts": attempts,
            "blocked_until": state["blocked_until"],
            "blocked_reason": state["blocked_reason"],
            "last_clock": now,
        }
        _atomic_write(self.path, self._state)
        self._active = True

    def observe(
        self,
        headers: Mapping[str, Any],
        *,
        status_code: int,
        error_code: int | None = None,
    ) -> dict[str, Any]:
        """Parse one response and durably record any conservative stop."""
        if not self._active or self._state is None:
            raise RuntimeError("observe must be called inside attempt")
        now = self._now()
        items: dict[str, Any] = {}
        parse_invalid = False
        decoded_provider_wait = 0.0
        for key, raw in headers.items():
            if str(key).lower() not in _SUPPORTED_HEADERS:
                continue
            header_name = str(key).lower()
            if header_name in items:
                parse_invalid = True
                continue
            try:
                if not isinstance(raw, str):
                    raise ValueError("usage header is not text")
                parsed = json.loads(
                    raw,
                    parse_constant=lambda token: (_ for _ in ()).throw(
                        ValueError(token)
                    ),
                )
                decoded_provider_wait = max(
                    decoded_provider_wait, _find_provider_wait_seconds(parsed)
                )
                items[header_name] = _normalise_header(header_name, parsed)
            except (ValueError, TypeError):
                parse_invalid = True
        usage = {key: items[key] for key in sorted(items)} if items else None
        parse_reason = "usage-invalid" if parse_invalid else None

        raw_retry_after = next(
            (
                value
                for key, value in headers.items()
                if str(key).lower() == "retry-after"
            ),
            None,
        )
        provider_wait = (
            _retry_after_seconds(raw_retry_after, now)
            if raw_retry_after is not None
            else 0.0
        )
        provider_wait = max(provider_wait, decoded_provider_wait)
        threshold_reached = False
        if usage is not None:
            for value in usage.values():
                child_threshold = _usage_percentage_signal(
                    value, threshold_limit=self.usage_threshold
                )
                threshold_reached = threshold_reached or child_threshold
                provider_wait = max(provider_wait, _find_provider_wait_seconds(value))

        stop_reason: str | None = None
        base_wait = 0.0
        if error_code in _THROTTLE_CODES or status_code == 429:
            stop_reason = "provider-throttle"
            base_wait = _HOUR
        elif status_code in (401, 403) or error_code in (10, 190, 200, 2500):
            stop_reason = "authentication"
            base_wait = _HOUR
        elif parse_reason is not None:
            stop_reason = parse_reason
            base_wait = _HOUR
        elif usage is None:
            stop_reason = "usage-unavailable"
            base_wait = _HOUR
        elif threshold_reached:
            stop_reason = "usage-threshold"
            base_wait = _HOUR
        elif provider_wait > 0:
            stop_reason = "provider-backoff"
            base_wait = provider_wait

        retry_at: float | None = None
        if stop_reason is not None:
            retry_at = now + max(base_wait, provider_wait)
            old_until = self._state["blocked_until"]
            if old_until is not None:
                retry_at = max(retry_at, old_until)
            self._state["blocked_until"] = retry_at
            self._state["blocked_reason"] = stop_reason
            self._state["last_clock"] = now
            _atomic_write(self.path, self._state)

        return {"usage": usage, "stop_reason": stop_reason, "retry_at": retry_at}
