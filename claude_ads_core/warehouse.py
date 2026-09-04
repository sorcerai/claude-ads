"""Warehouse-first Meta read plane and bounded ingestion service.

This module separates the data plane from the reasoning plane.
Analytical agents cannot call Meta account-read tools directly; they read
immutable warehouse snapshots with provenance and freshness validation.

Direct API reads are strictly limited to bounded ingestion, cache recovery,
and future mutation precondition/postcondition verification.
Account writes remain disabled.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone, timedelta
from enum import Enum
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "1.0.0"
META_FRESHNESS_SLA_SECONDS = 900  # 15 minutes platform-native freshness
META_FINALIZATION_WINDOW_DAYS = 28  # 28 days attribution restatement window
DEFAULT_APP_CALL_BUDGET_PER_HOUR = 200
DEFAULT_ACCOUNT_CALL_BUDGET_PER_HOUR = 50
DEFAULT_ACCOUNT_COMPLEXITY_BUDGET = 1000
USAGE_SLOWDOWN_THRESHOLD = 0.80
USAGE_THROTTLE_THRESHOLD = 0.95


class WarehouseError(Exception):
    """Base exception for warehouse read plane errors."""


class StaleDataError(WarehouseError):
    """Raised when a warehouse snapshot exceeds its freshness SLA."""


class RateBudgetExceededError(WarehouseError):
    """Raised when an app or ad-account rate or complexity budget is exceeded."""


class DirectApiReadForbiddenError(WarehouseError):
    """Raised when an analytical agent or unauthorized caller attempts a direct API read."""


class MutationDisabledError(WarehouseError):
    """Raised when an account mutation is attempted on a read-only plane."""


class ApiReadPurpose(str, Enum):
    """Authorized purposes for direct Meta API reads."""

    INGESTION_SERVICE = "ingestion_service"
    CACHE_RECOVERY = "cache_recovery"
    MUTATION_PRE_POST_VERIFICATION = "mutation_pre_post_verification"
    ANALYSIS = "analysis"  # Explicitly forbidden for direct API reads


@dataclass(frozen=True)
class MetaQuerySpec:
    """Deterministic specification for a Meta Marketing API query."""

    ad_account_id: str
    level: str = "campaign"
    fields: tuple[str, ...] = (
        "campaign_id",
        "campaign_name",
        "spend",
        "impressions",
        "clicks",
        "actions",
    )
    time_range: dict[str, str] | None = None
    time_increment: int | str = 1
    filtering: tuple[dict[str, Any], ...] = ()
    breakdowns: tuple[str, ...] = ()
    date_preset: str | None = None

    def __post_init__(self) -> None:
        if not self.ad_account_id or not self.ad_account_id.strip():
            raise WarehouseError("ad_account_id must be a non-empty string")
        if self.level not in {"account", "campaign", "adset", "ad"}:
            raise WarehouseError(f"invalid query level: {self.level}")

    def canonical_dict(self) -> dict[str, Any]:
        """Return a sorted, deterministic dictionary representation."""
        return {
            "ad_account_id": self.ad_account_id,
            "breakdowns": sorted(self.breakdowns),
            "date_preset": self.date_preset,
            "fields": sorted(self.fields),
            "filtering": sorted(
                self.filtering,
                key=lambda item: json.dumps(item, sort_keys=True),
            ),
            "level": self.level,
            "time_increment": self.time_increment,
            "time_range": dict(sorted(self.time_range.items())) if self.time_range else None,
        }

    def canonical_digest(self) -> str:
        """Return canonical SHA-256 digest of the query specification."""
        payload = json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True)
class Provenance:
    """Run and client binding for warehouse artifacts."""

    run_id: str
    client_id: str
    purpose: str
    query_digest: str
    source_digest: str

    def __post_init__(self) -> None:
        for key, val in (
            ("run_id", self.run_id),
            ("client_id", self.client_id),
            ("purpose", self.purpose),
            ("query_digest", self.query_digest),
            ("source_digest", self.source_digest),
        ):
            if not isinstance(val, str) or not val.strip():
                raise WarehouseError(f"provenance.{key} must be a non-empty string")
        if not re.match(r"^sha256:[0-9a-f]{64}$", self.query_digest):
            raise WarehouseError("provenance.query_digest must be sha256:<64 hex>")
        if not re.match(r"^sha256:[0-9a-f]{64}$", self.source_digest):
            raise WarehouseError("provenance.source_digest must be sha256:<64 hex>")

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class MetaUsageStats:
    """Observed usage from Meta Marketing API response headers."""

    call_count_pct: float = 0.0
    cpu_time_pct: float = 0.0
    total_time_pct: float = 0.0
    ad_account_util_pct: float = 0.0
    estimated_time_to_regain_access_minutes: int = 0

    @property
    def max_utilization_pct(self) -> float:
        return max(
            self.call_count_pct,
            self.cpu_time_pct,
            self.total_time_pct,
            self.ad_account_util_pct,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_count_pct": self.call_count_pct,
            "cpu_time_pct": self.cpu_time_pct,
            "total_time_pct": self.total_time_pct,
            "ad_account_util_pct": self.ad_account_util_pct,
            "estimated_time_to_regain_access_minutes": self.estimated_time_to_regain_access_minutes,
            "max_utilization_pct": self.max_utilization_pct,
        }


@dataclass
class WarehouseSnapshot:
    """Immutable warehouse-stored snapshot with provenance and freshness SLAs."""

    schema_version: str
    snapshot_id: str
    platform: str
    account_id: str
    account_snapshot: dict[str, Any]
    fetched_at: str
    extracted_at: str
    freshness_sla_seconds: int
    finalization_status: str
    provenance: Provenance
    usage: MetaUsageStats | None = None

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise WarehouseError(f"unsupported schema_version: {self.schema_version}")
        if self.platform != "meta":
            raise WarehouseError(f"warehouse read plane currently supports 'meta', got: {self.platform}")
        if self.finalization_status not in {"final", "provisional"}:
            raise WarehouseError(f"invalid finalization_status: {self.finalization_status}")
        _validate_iso_datetime(self.fetched_at, "fetched_at")
        _validate_iso_datetime(self.extracted_at, "extracted_at")

    @property
    def fetched_datetime(self) -> datetime:
        return datetime.fromisoformat(self.fetched_at.replace("Z", "+00:00"))

    def is_stale(
        self,
        as_of: datetime | str | None = None,
        max_staleness_seconds: int | None = None,
    ) -> bool:
        """Check whether the snapshot has exceeded its freshness SLA.

        Freshness is evaluated strictly against `fetched_at` / `extracted_at`,
        never against data coverage window `as_of` or `observed_at`.
        """
        ref_dt = _parse_reference_datetime(as_of)
        sla = max_staleness_seconds if max_staleness_seconds is not None else self.freshness_sla_seconds
        age = (ref_dt - self.fetched_datetime).total_seconds()
        return age > sla

    def validate_freshness(
        self,
        as_of: datetime | str | None = None,
        max_staleness_seconds: int | None = None,
    ) -> None:
        """Raise StaleDataError if the snapshot exceeds its freshness SLA."""
        ref_dt = _parse_reference_datetime(as_of)
        sla = max_staleness_seconds if max_staleness_seconds is not None else self.freshness_sla_seconds
        age = (ref_dt - self.fetched_datetime).total_seconds()
        if age > sla:
            raise StaleDataError(
                f"Warehouse snapshot {self.snapshot_id} is stale: age {age:.1f}s exceeds "
                f"freshness SLA {sla}s (fetched_at={self.fetched_at}, evaluated_at={ref_dt.isoformat()}). "
                "Fresh warehouse ingestion is required before analysis or scoring."
            )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "platform": self.platform,
            "account_id": self.account_id,
            "account_snapshot": self.account_snapshot,
            "fetched_at": self.fetched_at,
            "extracted_at": self.extracted_at,
            "freshness_sla_seconds": self.freshness_sla_seconds,
            "finalization_status": self.finalization_status,
            "provenance": self.provenance.to_dict(),
        }
        if self.usage is not None:
            result["usage"] = self.usage.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WarehouseSnapshot:
        provenance_data = data["provenance"]
        provenance = Provenance(
            run_id=provenance_data["run_id"],
            client_id=provenance_data["client_id"],
            purpose=provenance_data["purpose"],
            query_digest=provenance_data["query_digest"],
            source_digest=provenance_data["source_digest"],
        )
        usage_data = data.get("usage")
        usage = None
        if usage_data:
            usage = MetaUsageStats(
                call_count_pct=float(usage_data.get("call_count_pct", 0.0)),
                cpu_time_pct=float(usage_data.get("cpu_time_pct", 0.0)),
                total_time_pct=float(usage_data.get("total_time_pct", 0.0)),
                ad_account_util_pct=float(usage_data.get("ad_account_util_pct", 0.0)),
                estimated_time_to_regain_access_minutes=int(usage_data.get("estimated_time_to_regain_access_minutes", 0)),
            )
        return cls(
            schema_version=data["schema_version"],
            snapshot_id=data["snapshot_id"],
            platform=data["platform"],
            account_id=data["account_id"],
            account_snapshot=data["account_snapshot"],
            fetched_at=data["fetched_at"],
            extracted_at=data["extracted_at"],
            freshness_sla_seconds=int(data["freshness_sla_seconds"]),
            finalization_status=data["finalization_status"],
            provenance=provenance,
            usage=usage,
        )


class MetaUsageMonitor:
    """Parses Meta rate-limit headers and calculates recommended backoff."""

    @staticmethod
    def parse_headers(headers: Mapping[str, str]) -> MetaUsageStats:
        """Parse x-business-use-case-usage, x-app-usage, and x-ad-account-usage headers."""
        headers_lower = {k.lower(): v for k, v in headers.items()}
        call_count_pct = 0.0
        cpu_time_pct = 0.0
        total_time_pct = 0.0
        ad_account_util_pct = 0.0
        regain_access_min = 0

        # Parse x-app-usage: {"call_count": 50, "total_cputime": 10, "total_time": 15}
        app_usage_str = headers_lower.get("x-app-usage")
        if app_usage_str:
            try:
                parsed = json.loads(app_usage_str)
                call_count_pct = max(call_count_pct, float(parsed.get("call_count", 0)))
                cpu_time_pct = max(cpu_time_pct, float(parsed.get("total_cputime", 0)))
                total_time_pct = max(total_time_pct, float(parsed.get("total_time", 0)))
            except (json.JSONDecodeError, ValueError):
                pass

        # Parse x-business-use-case-usage:
        # {"<act_id>": [{"type": "ads_insights", "call_count": 80, "total_cputime": 20, "total_time": 30, "estimated_time_to_regain_access": 5}]}
        buc_str = headers_lower.get("x-business-use-case-usage")
        if buc_str:
            try:
                parsed_buc = json.loads(buc_str)
                if isinstance(parsed_buc, dict):
                    for items in parsed_buc.values():
                        if isinstance(items, list):
                            for entry in items:
                                if isinstance(entry, dict):
                                    call_count_pct = max(call_count_pct, float(entry.get("call_count", 0)))
                                    cpu_time_pct = max(cpu_time_pct, float(entry.get("total_cputime", 0)))
                                    total_time_pct = max(total_time_pct, float(entry.get("total_time", 0)))
                                    regain_access_min = max(
                                        regain_access_min,
                                        int(entry.get("estimated_time_to_regain_access", 0)),
                                    )
            except (json.JSONDecodeError, ValueError):
                pass

        # Parse x-ad-account-usage: {"acc_id_util_pct": 75.0}
        ad_account_str = headers_lower.get("x-ad-account-usage")
        if ad_account_str:
            try:
                parsed_acc = json.loads(ad_account_str)
                ad_account_util_pct = max(ad_account_util_pct, float(parsed_acc.get("acc_id_util_pct", 0)))
            except (json.JSONDecodeError, ValueError):
                pass

        return MetaUsageStats(
            call_count_pct=call_count_pct,
            cpu_time_pct=cpu_time_pct,
            total_time_pct=total_time_pct,
            ad_account_util_pct=ad_account_util_pct,
            estimated_time_to_regain_access_minutes=regain_access_min,
        )

    @classmethod
    def calculate_backoff(
        cls,
        usage: MetaUsageStats,
        status_code: int = 200,
        retry_attempt: int = 0,
    ) -> float:
        """Calculate recommended backoff delay in seconds."""
        if usage.estimated_time_to_regain_access_minutes > 0:
            return float(usage.estimated_time_to_regain_access_minutes * 60)

        # Meta throttling status codes: 429 Too Many Requests, or error codes 17, 613, 80004
        if status_code in {429, 503, 504}:
            return min(300.0, (2.0 ** retry_attempt) * 5.0)

        # Proactive slowdown when approaching thresholds
        if usage.max_utilization_pct >= USAGE_THROTTLE_THRESHOLD * 100:
            return 60.0
        if usage.max_utilization_pct >= USAGE_SLOWDOWN_THRESHOLD * 100:
            return 5.0

        return 0.0


class MetaBudgetManager:
    """Manages per-app and per-ad-account rate and complexity budgets."""

    def __init__(
        self,
        app_call_budget: int = DEFAULT_APP_CALL_BUDGET_PER_HOUR,
        account_call_budget: int = DEFAULT_ACCOUNT_CALL_BUDGET_PER_HOUR,
        account_complexity_budget: int = DEFAULT_ACCOUNT_COMPLEXITY_BUDGET,
    ) -> None:
        self.app_call_budget = app_call_budget
        self.account_call_budget = account_call_budget
        self.account_complexity_budget = account_complexity_budget
        self._app_calls: list[datetime] = []
        self._account_calls: dict[str, list[datetime]] = {}
        self._account_complexity: dict[str, int] = {}

    def check_budget(self, app_id: str, account_id: str, estimated_complexity: int = 1) -> None:
        """Check whether dispatching a query would violate call or complexity limits."""
        now = datetime.now(timezone.utc)
        one_hour_ago = now - timedelta(hours=1)

        # Clean old call timestamps
        self._app_calls = [t for t in self._app_calls if t > one_hour_ago]
        if account_id in self._account_calls:
            self._account_calls[account_id] = [t for t in self._account_calls[account_id] if t > one_hour_ago]

        if len(self._app_calls) >= self.app_call_budget:
            raise RateBudgetExceededError(
                f"App {app_id} exceeded hourly call budget ({len(self._app_calls)}/{self.app_call_budget})"
            )

        account_calls = len(self._account_calls.get(account_id, []))
        if account_calls >= self.account_call_budget:
            raise RateBudgetExceededError(
                f"Ad account {account_id} exceeded hourly call budget ({account_calls}/{self.account_call_budget})"
            )

        current_complexity = self._account_complexity.get(account_id, 0)
        if current_complexity + estimated_complexity > self.account_complexity_budget:
            raise RateBudgetExceededError(
                f"Ad account {account_id} complexity budget exceeded "
                f"({current_complexity + estimated_complexity}/{self.account_complexity_budget})"
            )

    def record_call(self, app_id: str, account_id: str, complexity: int = 1) -> None:
        """Record an executed query against app and account budgets."""
        now = datetime.now(timezone.utc)
        self._app_calls.append(now)
        self._account_calls.setdefault(account_id, []).append(now)
        self._account_complexity[account_id] = self._account_complexity.get(account_id, 0) + complexity

    def reset(self) -> None:
        """Reset tracking state for testing or window boundaries."""
        self._app_calls.clear()
        self._account_calls.clear()
        self._account_complexity.clear()


class MetaWarehouseReadPlane:
    """Bounded, warehouse-first Meta read plane service.

    Features:
    - Dedicated ingestion service owning API reads and query deduplication
    - Queue pacing and usage-header monitoring
    - Platform-native 15-minute freshness SLA and 28-day finalization semantics
    - Stale-data hard stop before analysis and scoring
    - Strict read permissions: analysis agents cannot call direct read APIs
    - Writes permanently disabled
    """

    def __init__(
        self,
        budget_manager: MetaBudgetManager | None = None,
        freshness_sla_seconds: int = META_FRESHNESS_SLA_SECONDS,
    ) -> None:
        self.budget_manager = budget_manager or MetaBudgetManager()
        self.freshness_sla_seconds = freshness_sla_seconds
        self.usage_monitor = MetaUsageMonitor()
        self.writes_enabled: bool = False
        self._store: dict[str, WarehouseSnapshot] = {}
        self._in_flight: set[str] = set()

    @staticmethod
    def verify_read_permitted(purpose: ApiReadPurpose | str) -> None:
        """Enforce separation between data plane and reasoning plane.

        Analytical workers must consume warehouse snapshots and are forbidden
        from calling direct API read tools.
        """
        purpose_val = purpose.value if isinstance(purpose, ApiReadPurpose) else str(purpose)
        if purpose_val == ApiReadPurpose.ANALYSIS.value:
            raise DirectApiReadForbiddenError(
                "Analytical agents cannot call Meta account-read tools directly. "
                "Analysis must consume immutable warehouse snapshots with provenance."
            )
        valid_purposes = {
            ApiReadPurpose.INGESTION_SERVICE.value,
            ApiReadPurpose.CACHE_RECOVERY.value,
            ApiReadPurpose.MUTATION_PRE_POST_VERIFICATION.value,
        }
        if purpose_val not in valid_purposes:
            raise DirectApiReadForbiddenError(
                f"Unauthorized read purpose: {purpose_val}. "
                f"Authorized purposes are: {', '.join(sorted(valid_purposes))}"
            )

    def compute_finalization_status(
        self,
        window_end: str | date,
        fetch_time: datetime,
    ) -> str:
        """Apply 28-day finalization semantics.

        Data within the last 28 days of fetch time is subject to attribution
        adjustments and restatements, so it is marked 'provisional'. Data older
        than 28 days is marked 'final'.
        """
        if isinstance(window_end, str):
            end_date = date.fromisoformat(window_end)
        else:
            end_date = window_end

        fetch_date = fetch_time.date()
        if (fetch_date - end_date).days <= META_FINALIZATION_WINDOW_DAYS:
            return "provisional"
        return "final"

    def store_snapshot(self, snapshot: WarehouseSnapshot) -> str:
        """Store an immutable snapshot in the warehouse."""
        self._store[snapshot.snapshot_id] = snapshot
        return snapshot.snapshot_id

    def get_snapshot(
        self,
        snapshot_id: str,
        as_of: datetime | str | None = None,
        max_staleness_seconds: int | None = None,
    ) -> WarehouseSnapshot:
        """Retrieve a snapshot by ID with freshness verification."""
        snapshot = self._store.get(snapshot_id)
        if snapshot is None:
            raise WarehouseError(f"snapshot not found: {snapshot_id}")
        snapshot.validate_freshness(as_of=as_of, max_staleness_seconds=max_staleness_seconds)
        return snapshot

    def get_latest_snapshot_for_account(
        self,
        account_id: str,
        as_of: datetime | str | None = None,
        max_staleness_seconds: int | None = None,
    ) -> WarehouseSnapshot | None:
        """Find the latest snapshot for an account, verifying freshness."""
        candidates = [s for s in self._store.values() if s.account_id == account_id]
        if not candidates:
            return None
        candidates.sort(key=lambda s: s.fetched_datetime, reverse=True)
        latest = candidates[0]
        latest.validate_freshness(as_of=as_of, max_staleness_seconds=max_staleness_seconds)
        return latest

    def ingest_snapshot(
        self,
        *,
        query_spec: MetaQuerySpec,
        account_snapshot: dict[str, Any],
        provenance: Provenance,
        fetch_time: datetime | str | None = None,
        response_headers: Mapping[str, str] | None = None,
        app_id: str = "default_app",
    ) -> WarehouseSnapshot:
        """Ingest raw/normalized Meta data into an immutable warehouse snapshot.

        Enforces:
        - Query deduplication / in-flight tracking
        - App and account rate budgets
        - 28-day finalization calculation
        - Header usage extraction
        - Freshness SLA binding
        """
        # Ensure read authorization
        self.verify_read_permitted(provenance.purpose)

        # Check rate budgets
        self.budget_manager.check_budget(app_id, query_spec.ad_account_id)

        digest = query_spec.canonical_digest()
        if digest in self._in_flight:
            raise WarehouseError(f"query {digest} is already in-flight (deduplication active)")

        self._in_flight.add(digest)
        try:
            # Parse fetch datetime
            fetch_dt = _parse_reference_datetime(fetch_time)
            fetch_iso = fetch_dt.isoformat()

            # Parse headers for usage
            usage = None
            if response_headers:
                usage = self.usage_monitor.parse_headers(response_headers)

            # Record call in budget
            self.budget_manager.record_call(app_id, query_spec.ad_account_id)

            # Determine finalization status from snapshot window
            window_end = account_snapshot.get("window", {}).get("end")
            if not window_end:
                finalization = "provisional"
            else:
                finalization = self.compute_finalization_status(window_end, fetch_dt)

            # Ensure account_snapshot measurement_context records finalization
            if "measurement_context" in account_snapshot:
                account_snapshot["measurement_context"]["data_finalization"] = finalization

            snapshot_id = f"meta-wh-{hashlib.sha256(f'{digest}-{fetch_iso}'.encode('utf-8')).hexdigest()[:16]}"

            snapshot = WarehouseSnapshot(
                schema_version=SCHEMA_VERSION,
                snapshot_id=snapshot_id,
                platform="meta",
                account_id=query_spec.ad_account_id,
                account_snapshot=account_snapshot,
                fetched_at=fetch_iso,
                extracted_at=fetch_iso,
                freshness_sla_seconds=self.freshness_sla_seconds,
                finalization_status=finalization,
                provenance=provenance,
                usage=usage,
            )

            self.store_snapshot(snapshot)
            return snapshot
        finally:
            self._in_flight.discard(digest)

    def evaluate_scoring_precondition(
        self,
        snapshot: WarehouseSnapshot,
        as_of: datetime | str | None = None,
    ) -> None:
        """Pre-scoring hard stop: fail closed if the snapshot is stale."""
        snapshot.validate_freshness(as_of=as_of)

    def apply_mutation(self, *args: Any, **kwargs: Any) -> None:
        """Mutations are permanently disabled on this read plane."""
        raise MutationDisabledError(
            "Account writes remain disabled in v2. "
            "The Meta read plane provides bounded, read-only warehouse ingestion only."
        )


def _validate_iso_datetime(value: Any, field_name: str) -> None:
    if not isinstance(value, str):
        raise WarehouseError(f"{field_name} must be an ISO 8601 string")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WarehouseError(f"{field_name} must be an ISO 8601 date-time: {exc}") from exc
    if dt.tzinfo is None:
        raise WarehouseError(f"{field_name} must include a UTC timezone offset")


def _parse_reference_datetime(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise WarehouseError(f"invalid datetime value: {type(value)}")
