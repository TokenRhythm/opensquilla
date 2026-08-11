"""Persistent, policy-scoped safety state for live canary rollouts.

This module is deliberately infrastructure-only.  It does not read serving
configuration and no production path constructs the ledger yet.  A later
integration must place its admission check immediately beside the physical
provider boundary; importing this module alone changes no routing behaviour.

The ledger answers a narrower question than ``ProviderHealthLedger``:
whether one ``(policy, role, deployment)`` canary rollout scope is active,
latched off, or performing a controlled recovery probe.  State is stored in a
small owner-only SQLite database.  ``BEGIN IMMEDIATE`` serializes mutations
across processes, while WAL/FULL provides atomic durable commits.  Storage
errors, an unknown schema, corruption, and lock contention all reject canary
admission.  There is intentionally no in-memory fallback and no automatic
database replacement.

Only SHA-256 deployment identities are persisted.  Provider/model/upstream
text, prompts, provider errors, credentials, and admission tokens are never
written to the database or returned in snapshots.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

from opensquilla.provider.deployment import canonicalize_provider_routing_upstream

_SCHEMA_VERSION: Final[int] = 1
_DATABASE_FILENAME: Final[str] = "canary_rollout.sqlite3"
_SHA256_HEX_LENGTH: Final[int] = 64


class CanaryRolloutRole(StrEnum):
    """A role whose canary traffic is governed independently."""

    PROPOSER = "proposer"
    AGGREGATOR = "aggregator"


class CanaryRolloutState(StrEnum):
    """Durable rollout state for one scope."""

    ACTIVE = "active"
    ROLLED_BACK = "rolled_back"
    HALF_OPEN = "half_open"


class CanaryProviderOutcome(StrEnum):
    """Secret-free classification of one physical provider attempt."""

    SUCCESS = "success"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_5XX = "upstream_5xx"
    TRANSPORT_FAILURE = "transport_failure"
    INVALID_RESPONSE = "invalid_response"
    CONFIGURATION_FAILURE = "configuration_failure"
    UNKNOWN_FAILURE = "unknown_failure"


class CanaryUsageOutcome(StrEnum):
    """Whether billable usage was proven for the physical attempt."""

    OBSERVED = "observed"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"


class CanaryQualityOutcome(StrEnum):
    """Quality evidence supplied by a trusted evaluator, when available."""

    PASSED = "passed"
    FAILED = "failed"
    UNOBSERVED = "unobserved"
    NOT_APPLICABLE = "not_applicable"


class CanaryLatchReason(StrEnum):
    """Fixed, low-cardinality reasons that can latch a rollout off."""

    CONFIGURATION_FAILURE = "configuration_failure"
    RATE_LIMITED = "rate_limited"
    USAGE_MISSING = "usage_missing"
    CONSECUTIVE_PROVIDER_FAILURES = "consecutive_provider_failures"
    PROVIDER_FAILURE_RATE = "provider_failure_rate"
    QUALITY_COVERAGE_INSUFFICIENT = "quality_coverage_insufficient"
    QUALITY_FAILURE_RATE = "quality_failure_rate"
    ATTEMPT_ABANDONED = "attempt_abandoned"
    PROBE_FAILED = "probe_failed"
    PROBE_ABANDONED = "probe_abandoned"
    POLICY_CONTRACT_MISMATCH = "policy_contract_mismatch"


class CanaryAdmissionReason(StrEnum):
    """Fixed admission outcomes safe for trace/metric projection."""

    ACTIVE = "active"
    HALF_OPEN_PROBE = "half_open_probe"
    LATCHED = "latched"
    HALF_OPEN_BUSY = "half_open_busy"
    RECOVERY_WAIT = "recovery_wait"
    PENDING_CAPACITY = "pending_capacity"
    SCOPE_CAPACITY = "scope_capacity"
    LEDGER_UNAVAILABLE = "ledger_unavailable"


class CanaryMutationReason(StrEnum):
    """Fixed settlement/reset outcomes."""

    APPLIED = "applied"
    DUPLICATE = "duplicate"
    CANCELLED = "cancelled"
    UNKNOWN_TOKEN = "unknown_token"
    TOKEN_CONFLICT = "token_conflict"
    UNKNOWN_SCOPE = "unknown_scope"
    LEDGER_UNAVAILABLE = "ledger_unavailable"


_PROVIDER_FAILURES: Final[frozenset[CanaryProviderOutcome]] = frozenset(
    {
        CanaryProviderOutcome.RATE_LIMITED,
        CanaryProviderOutcome.UPSTREAM_5XX,
        CanaryProviderOutcome.TRANSPORT_FAILURE,
        CanaryProviderOutcome.INVALID_RESPONSE,
        CanaryProviderOutcome.CONFIGURATION_FAILURE,
        CanaryProviderOutcome.UNKNOWN_FAILURE,
    }
)
_MANUAL_RESET_ONLY_REASONS: Final[frozenset[CanaryLatchReason]] = frozenset(
    {
        CanaryLatchReason.CONFIGURATION_FAILURE,
        CanaryLatchReason.POLICY_CONTRACT_MISMATCH,
    }
)


def _is_sha256(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == _SHA256_HEX_LENGTH
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def canary_deployment_sha256(
    provider: object,
    model: object,
    upstream: object = "",
) -> str:
    """Hash a canonical deployment tuple without retaining identity text."""

    normalized_provider = str(provider or "").strip().casefold()
    normalized_model = str(model or "").strip()
    if not normalized_provider or not normalized_model:
        raise ValueError("provider and model are required for a canary deployment")
    payload = json.dumps(
        {
            "provider": normalized_provider,
            "model": normalized_model,
            "upstream": canonicalize_provider_routing_upstream(str(upstream or "")),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class CanaryRolloutScope:
    """Opaque durable key for one rollout policy, role, and deployment."""

    policy_sha256: str
    role: CanaryRolloutRole
    deployment_sha256: str

    def __post_init__(self) -> None:
        if not _is_sha256(self.policy_sha256):
            raise ValueError("policy_sha256 must be lowercase SHA-256 hex")
        if not isinstance(self.role, CanaryRolloutRole):
            raise ValueError("role must be a CanaryRolloutRole")
        if not _is_sha256(self.deployment_sha256):
            raise ValueError("deployment_sha256 must be lowercase SHA-256 hex")

    @classmethod
    def from_identity(
        cls,
        *,
        policy_sha256: str,
        role: CanaryRolloutRole,
        provider: object,
        model: object,
        upstream: object = "",
    ) -> CanaryRolloutScope:
        return cls(
            policy_sha256=policy_sha256,
            role=role,
            deployment_sha256=canary_deployment_sha256(
                provider,
                model,
                upstream,
            ),
        )


@dataclass(frozen=True)
class CanaryRolloutPolicy:
    """Bounded deterministic thresholds used by the ledger kernel."""

    window_max_attempts: int = 50
    window_max_age_s: int = 300
    min_attempts: int = 20
    max_provider_failure_basis_points: int = 500
    max_consecutive_provider_failures: int = 3
    max_rate_limited_count: int = 0
    max_usage_missing_count: int = 0
    quality_gate_enabled: bool = False
    min_quality_attempts: int = 20
    min_quality_coverage_basis_points: int = 0
    max_quality_failure_basis_points: int = 500
    rollback_cooldown_s: int = 900
    half_open_successes_required: int = 3
    half_open_probe_spacing_s: int = 30
    half_open_probe_lease_s: int = 60
    active_attempt_lease_s: int = 300
    scope_retention_s: int = 7 * 24 * 60 * 60
    max_scopes: int = 1024
    max_pending_per_scope: int = 64

    def __post_init__(self) -> None:
        integer_fields = {
            key: value for key, value in asdict(self).items() if key != "quality_gate_enabled"
        }
        if not isinstance(self.quality_gate_enabled, bool):
            raise ValueError("quality_gate_enabled must be a boolean")
        for name, value in integer_fields.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
        if not 1 <= self.window_max_attempts <= 10_000:
            raise ValueError("window_max_attempts must be in [1, 10000]")
        if not 1 <= self.window_max_age_s <= 30 * 24 * 60 * 60:
            raise ValueError("window_max_age_s is outside its safe bound")
        if not 1 <= self.min_attempts <= self.window_max_attempts:
            raise ValueError("min_attempts must be within the outcome window")
        for name in (
            "max_provider_failure_basis_points",
            "min_quality_coverage_basis_points",
            "max_quality_failure_basis_points",
        ):
            value = integer_fields[name]
            if not 0 <= value <= 10_000:
                raise ValueError(f"{name} must be in [0, 10000]")
        if not 1 <= self.max_consecutive_provider_failures <= self.window_max_attempts:
            raise ValueError("max_consecutive_provider_failures must be within the outcome window")
        if not 0 <= self.max_rate_limited_count <= self.window_max_attempts:
            raise ValueError("max_rate_limited_count is outside its safe bound")
        if not 0 <= self.max_usage_missing_count <= self.window_max_attempts:
            raise ValueError("max_usage_missing_count is outside its safe bound")
        if not 1 <= self.min_quality_attempts <= self.window_max_attempts:
            raise ValueError("min_quality_attempts must be within the outcome window")
        if not 1 <= self.rollback_cooldown_s <= 30 * 24 * 60 * 60:
            raise ValueError("rollback_cooldown_s is outside its safe bound")
        if not 1 <= self.half_open_successes_required <= 100:
            raise ValueError("half_open_successes_required must be in [1, 100]")
        if self.half_open_successes_required - 1 > self.window_max_attempts:
            raise ValueError("outcome window cannot retain the recovery proof")
        if not 0 <= self.half_open_probe_spacing_s <= self.rollback_cooldown_s:
            raise ValueError("half_open_probe_spacing_s is outside its safe bound")
        if not 1 <= self.half_open_probe_lease_s <= 24 * 60 * 60:
            raise ValueError("half_open_probe_lease_s is outside its safe bound")
        if not 1 <= self.active_attempt_lease_s <= 24 * 60 * 60:
            raise ValueError("active_attempt_lease_s is outside its safe bound")
        if self.rollback_cooldown_s < self.active_attempt_lease_s:
            raise ValueError("rollback_cooldown_s must cover every active attempt lease")
        if not self.window_max_age_s <= self.scope_retention_s <= 365 * 24 * 60 * 60:
            raise ValueError("scope_retention_s must cover the window and be at most one year")
        if not 1 <= self.max_scopes <= 100_000:
            raise ValueError("max_scopes must be in [1, 100000]")
        if not 1 <= self.max_pending_per_scope <= 10_000:
            raise ValueError("max_pending_per_scope must be in [1, 10000]")

    def _canonical_contract_json(self) -> str:
        return json.dumps(
            asdict(self),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def contract_sha256(self) -> str:
        return hashlib.sha256(self._canonical_contract_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CanaryAttemptOutcome:
    """Normalized terminal evidence for one admitted physical attempt."""

    provider: CanaryProviderOutcome
    usage: CanaryUsageOutcome
    quality: CanaryQualityOutcome = CanaryQualityOutcome.UNOBSERVED

    def __post_init__(self) -> None:
        if not isinstance(self.provider, CanaryProviderOutcome):
            raise ValueError("provider must be a CanaryProviderOutcome")
        if not isinstance(self.usage, CanaryUsageOutcome):
            raise ValueError("usage must be a CanaryUsageOutcome")
        if not isinstance(self.quality, CanaryQualityOutcome):
            raise ValueError("quality must be a CanaryQualityOutcome")

    @property
    def complete_probe_success(self) -> bool:
        return bool(
            self.provider is CanaryProviderOutcome.SUCCESS
            and self.usage is CanaryUsageOutcome.OBSERVED
            and self.quality
            in {
                CanaryQualityOutcome.PASSED,
                CanaryQualityOutcome.UNOBSERVED,
            }
        )


@dataclass(frozen=True)
class CanaryRolloutSnapshot:
    """Identity-free, low-cardinality projection of one scope."""

    available: bool
    found: bool
    state: CanaryRolloutState
    latch_reason: CanaryLatchReason | None = None
    latched_at_ms: int | None = None
    window_attempts: int = 0
    provider_failures: int = 0
    rate_limited: int = 0
    usage_missing: int = 0
    quality_observed: int = 0
    quality_failures: int = 0
    recovery_successes: int = 0
    probe_inflight: bool = False

    @classmethod
    def unavailable(cls) -> CanaryRolloutSnapshot:
        return cls(
            available=False,
            found=False,
            state=CanaryRolloutState.ROLLED_BACK,
        )


@dataclass(frozen=True)
class CanaryAdmission:
    """Result of an atomic admission attempt."""

    available: bool
    allowed: bool
    reason: CanaryAdmissionReason
    snapshot: CanaryRolloutSnapshot
    probe: bool = False
    token: str = field(default="", repr=False)


@dataclass(frozen=True)
class CanaryMutationResult:
    """Result of settlement, cancellation, or manual reset."""

    available: bool
    applied: bool
    reason: CanaryMutationReason
    snapshot: CanaryRolloutSnapshot


class _StorageError(RuntimeError):
    def __init__(self, *, transient: bool) -> None:
        self.transient = transient
        super().__init__("canary rollout ledger storage unavailable")


class _ContractMismatchError(RuntimeError):
    pass


class _ClockRegressionError(RuntimeError):
    pass


def _token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_recovery_epoch_sha256() -> str:
    return hashlib.sha256(secrets.token_bytes(32)).hexdigest()


def _now_ms(clock: Callable[[], float], supplied: int | None) -> int:
    if supplied is None:
        value = int(clock() * 1000)
    else:
        if isinstance(supplied, bool) or not isinstance(supplied, int):
            raise ValueError("now_ms must be an integer")
        value = supplied
    if value < 0:
        raise ValueError("now_ms must be non-negative")
    return value


def _sqlite_is_busy(exc: sqlite3.Error) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    busy_codes = {
        getattr(sqlite3, "SQLITE_BUSY", 5),
        getattr(sqlite3, "SQLITE_LOCKED", 6),
    }
    if isinstance(code, int):
        return bool((code & 0xFF) in busy_codes)
    # Older Python/sqlite builds do not expose sqlite_errorcode.
    return "locked" in str(exc).casefold() or "busy" in str(exc).casefold()


def _db_enum[EnumT: StrEnum](
    enum_type: type[EnumT],
    value: object,
) -> EnumT:
    try:
        return enum_type(str(value))
    except ValueError as exc:
        raise sqlite3.DatabaseError("invalid canary rollout enum in storage") from exc


def _db_bool(value: object) -> bool:
    if value not in (0, 1):
        raise sqlite3.DatabaseError("invalid canary rollout boolean in storage")
    return bool(value)


def _db_non_negative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise sqlite3.DatabaseError("invalid canary rollout integer in storage")
    return value


_SCOPE_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "policy_sha256",
        "role",
        "deployment_sha256",
        "policy_contract_sha256",
        "policy_contract_json",
        "scope_retention_s",
        "state",
        "latch_reason",
        "latched_at_ms",
        "recovery_epoch_sha256",
        "recovery_successes",
        "last_probe_at_ms",
        "created_at_ms",
        "updated_at_ms",
    }
)
_ATTEMPT_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "policy_sha256",
        "role",
        "deployment_sha256",
        "token_sha256",
        "admitted_at_ms",
        "lease_expires_at_ms",
        "is_probe",
        "recovery_epoch_sha256",
        "probe_ordinal",
    }
)
_OUTCOME_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "outcome_id",
        "policy_sha256",
        "role",
        "deployment_sha256",
        "token_sha256",
        "observed_at_ms",
        "provider_outcome",
        "usage_outcome",
        "quality_outcome",
        "is_probe",
        "recovery_epoch_sha256",
        "probe_ordinal",
    }
)


class CanaryRolloutLedger:
    """SQLite-backed rollout latch with cross-process recovery leases."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        clock: Callable[[], float] = time.time,
        busy_timeout_ms: int = 250,
    ) -> None:
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int):
            raise ValueError("busy_timeout_ms must be an integer")
        if not 0 <= busy_timeout_ms <= 30_000:
            raise ValueError("busy_timeout_ms must be in [0, 30000]")
        if not callable(clock):
            raise ValueError("clock must be callable")
        target = Path(path)
        if target.name in {"", ".", ".."}:
            raise ValueError("ledger path must name a database file")
        self._path = target
        self._clock = clock
        self._busy_timeout_ms = busy_timeout_ms
        self._initialization_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._initialized = False
        self._sticky_unavailable = False
        self._ensure_initialized()

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def default_path(state_root: str | os.PathLike[str]) -> Path:
        return Path(state_root) / _DATABASE_FILENAME

    def _mark_sticky_unavailable(self) -> None:
        with self._state_lock:
            self._sticky_unavailable = True

    def _is_sticky_unavailable(self) -> bool:
        with self._state_lock:
            return self._sticky_unavailable

    def _prepare_owner_only_path(self) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            current = self._path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
            raise OSError("canary rollout ledger path is not a regular file")
        os.chmod(self._path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            os.fspath(self._path),
            timeout=self._busy_timeout_ms / 1000.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            connection.execute("PRAGMA trusted_schema=OFF")
        except sqlite3.DatabaseError:
            pass
        return connection

    @staticmethod
    def _rollback_quietly(connection: sqlite3.Connection) -> None:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass

    def _ensure_initialized(self) -> bool:
        if self._is_sticky_unavailable():
            return False
        if self._initialized:
            return True
        with self._initialization_lock:
            if self._initialized:
                return True
            if self._is_sticky_unavailable():
                return False
            connection: sqlite3.Connection | None = None
            try:
                self._prepare_owner_only_path()
                connection = self._connect()
                journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
                if journal_mode is None or str(journal_mode[0]).casefold() != "wal":
                    raise sqlite3.DatabaseError("WAL mode was not established")
                connection.execute("BEGIN IMMEDIATE")
                version_row = connection.execute("PRAGMA user_version").fetchone()
                version = int(version_row[0]) if version_row is not None else -1
                if version == 0:
                    self._create_schema(connection)
                    connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
                elif version != _SCHEMA_VERSION:
                    raise sqlite3.DatabaseError("unsupported canary rollout schema")
                self._validate_schema(connection)
                connection.commit()
                check = connection.execute("PRAGMA quick_check(1)").fetchone()
                if check is None or str(check[0]).casefold() != "ok":
                    raise sqlite3.DatabaseError("canary rollout quick_check failed")
                os.chmod(self._path, 0o600)
            except sqlite3.Error as exc:
                if connection is not None:
                    self._rollback_quietly(connection)
                if not _sqlite_is_busy(exc):
                    self._mark_sticky_unavailable()
                return False
            except OSError:
                if connection is not None:
                    self._rollback_quietly(connection)
                self._mark_sticky_unavailable()
                return False
            finally:
                if connection is not None:
                    connection.close()
            self._initialized = True
            return True

    @staticmethod
    def _schema_statements() -> tuple[str, ...]:
        """Return the single source of truth for the v1 SQLite contract."""

        schema = """
            CREATE TABLE canary_rollout_scopes (
                policy_sha256 TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('proposer', 'aggregator')),
                deployment_sha256 TEXT NOT NULL,
                policy_contract_sha256 TEXT NOT NULL,
                policy_contract_json TEXT NOT NULL CHECK (
                    length(policy_contract_json) BETWEEN 2 AND 4096
                ),
                scope_retention_s INTEGER NOT NULL CHECK (
                    scope_retention_s BETWEEN 1 AND 31536000
                ),
                state TEXT NOT NULL CHECK (
                    state IN ('active', 'rolled_back', 'half_open')
                ),
                latch_reason TEXT CHECK (
                    latch_reason IS NULL OR latch_reason IN (
                        'configuration_failure', 'rate_limited',
                        'usage_missing', 'consecutive_provider_failures',
                        'provider_failure_rate',
                        'quality_coverage_insufficient',
                        'quality_failure_rate', 'attempt_abandoned',
                        'probe_failed', 'probe_abandoned',
                        'policy_contract_mismatch'
                    )
                ),
                latched_at_ms INTEGER,
                recovery_epoch_sha256 TEXT,
                recovery_successes INTEGER NOT NULL DEFAULT 0
                    CHECK (recovery_successes >= 0),
                last_probe_at_ms INTEGER,
                created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
                updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0),
                PRIMARY KEY (policy_sha256, role, deployment_sha256),
                CHECK (length(policy_sha256) = 64),
                CHECK (length(deployment_sha256) = 64),
                CHECK (length(policy_contract_sha256) = 64),
                CHECK (
                    recovery_epoch_sha256 IS NULL
                    OR length(recovery_epoch_sha256) = 64
                ),
                CHECK (
                    (
                        state = 'active'
                        AND latch_reason IS NULL
                        AND latched_at_ms IS NULL
                        AND recovery_epoch_sha256 IS NULL
                    )
                    OR
                    (
                        state != 'active'
                        AND latch_reason IS NOT NULL
                        AND latched_at_ms IS NOT NULL
                        AND recovery_epoch_sha256 IS NOT NULL
                    )
                )
            );

            CREATE TABLE canary_rollout_attempts (
                policy_sha256 TEXT NOT NULL,
                role TEXT NOT NULL,
                deployment_sha256 TEXT NOT NULL,
                token_sha256 TEXT NOT NULL CHECK (length(token_sha256) = 64),
                admitted_at_ms INTEGER NOT NULL CHECK (admitted_at_ms >= 0),
                lease_expires_at_ms INTEGER NOT NULL CHECK (
                    lease_expires_at_ms >= admitted_at_ms
                ),
                is_probe INTEGER NOT NULL CHECK (is_probe IN (0, 1)),
                recovery_epoch_sha256 TEXT,
                probe_ordinal INTEGER,
                PRIMARY KEY (
                    policy_sha256,
                    role,
                    deployment_sha256,
                    token_sha256
                ),
                UNIQUE (
                    policy_sha256,
                    role,
                    deployment_sha256,
                    recovery_epoch_sha256,
                    probe_ordinal
                ),
                CHECK (
                    (
                        is_probe = 0
                        AND recovery_epoch_sha256 IS NULL
                        AND probe_ordinal IS NULL
                    )
                    OR
                    (
                        is_probe = 1
                        AND length(recovery_epoch_sha256) = 64
                        AND probe_ordinal >= 1
                    )
                ),
                FOREIGN KEY (policy_sha256, role, deployment_sha256)
                    REFERENCES canary_rollout_scopes (
                        policy_sha256,
                        role,
                        deployment_sha256
                    ) ON DELETE CASCADE
            );

            CREATE INDEX canary_rollout_attempts_expiry_idx
                ON canary_rollout_attempts (lease_expires_at_ms);

            CREATE TABLE canary_rollout_outcomes (
                outcome_id INTEGER PRIMARY KEY AUTOINCREMENT,
                policy_sha256 TEXT NOT NULL,
                role TEXT NOT NULL,
                deployment_sha256 TEXT NOT NULL,
                token_sha256 TEXT NOT NULL CHECK (length(token_sha256) = 64),
                observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
                provider_outcome TEXT NOT NULL CHECK (
                    provider_outcome IN (
                        'success', 'rate_limited', 'upstream_5xx',
                        'transport_failure', 'invalid_response',
                        'configuration_failure', 'unknown_failure'
                    )
                ),
                usage_outcome TEXT NOT NULL CHECK (
                    usage_outcome IN ('observed', 'missing', 'not_applicable')
                ),
                quality_outcome TEXT NOT NULL CHECK (
                    quality_outcome IN (
                        'passed', 'failed', 'unobserved', 'not_applicable'
                    )
                ),
                is_probe INTEGER NOT NULL CHECK (is_probe IN (0, 1)),
                recovery_epoch_sha256 TEXT,
                probe_ordinal INTEGER,
                UNIQUE (
                    policy_sha256,
                    role,
                    deployment_sha256,
                    token_sha256
                ),
                UNIQUE (
                    policy_sha256,
                    role,
                    deployment_sha256,
                    recovery_epoch_sha256,
                    probe_ordinal
                ),
                CHECK (
                    (
                        is_probe = 0
                        AND recovery_epoch_sha256 IS NULL
                        AND probe_ordinal IS NULL
                    )
                    OR
                    (
                        is_probe = 1
                        AND length(recovery_epoch_sha256) = 64
                        AND probe_ordinal >= 1
                    )
                ),
                FOREIGN KEY (policy_sha256, role, deployment_sha256)
                    REFERENCES canary_rollout_scopes (
                        policy_sha256,
                        role,
                        deployment_sha256
                    ) ON DELETE CASCADE
            );

            CREATE INDEX canary_rollout_outcomes_window_idx
                ON canary_rollout_outcomes (
                    policy_sha256,
                    role,
                    deployment_sha256,
                    observed_at_ms DESC,
                    outcome_id DESC
                );
            """
        return tuple(statement.strip() for statement in schema.split(";") if statement.strip())

    @classmethod
    def _create_schema(cls, connection: sqlite3.Connection) -> None:
        # ``sqlite3.Connection.executescript`` issues an implicit COMMIT.
        # Execute the fixed statements one by one so schema creation and
        # ``user_version`` remain inside the caller's BEGIN IMMEDIATE.
        for statement in cls._schema_statements():
            connection.execute(statement)

    @staticmethod
    def _normalize_schema_sql(value: object) -> str:
        """Canonicalize SQL syntax without changing quoted literal bytes."""

        sql = str(value or "").strip()
        if sql.endswith(";"):
            sql = sql[:-1].rstrip()
        tokens: list[str] = []
        punctuation = frozenset("(),.;=<>+-*/%|&~")
        compound_operators = ("->>", ">=", "<=", "<>", "!=", "==", "||", "<<", ">>", "->")
        index = 0
        while index < len(sql):
            character = sql[index]
            if character.isspace():
                index += 1
                continue
            if sql.startswith("--", index):
                end = index + 2
                while end < len(sql) and sql[end] not in "\r\n":
                    end += 1
                tokens.append(sql[index:end])
                index = end
                continue
            if sql.startswith("/*", index):
                end = sql.find("*/", index + 2)
                end = len(sql) if end < 0 else end + 2
                tokens.append(sql[index:end])
                index = end
                continue
            if character == "'":
                # SQL string literals escape a quote by doubling it.  Preserve
                # the complete token exactly: CHECK literals are schema
                # semantics, not case-insensitive SQL identifiers.
                start = index
                index += 1
                while index < len(sql):
                    if sql[index] != "'":
                        index += 1
                        continue
                    if index + 1 < len(sql) and sql[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                tokens.append(sql[start:index])
                continue
            if character in {'"', "`", "["}:
                # Quoted identifiers do not occur in the built-in contract.
                # Preserve them exactly so a compatibility quoting mode cannot
                # silently reinterpret an identifier as a string literal.
                start = index
                closing = "]" if character == "[" else character
                index += 1
                while index < len(sql):
                    if sql[index] != closing:
                        index += 1
                        continue
                    if index + 1 < len(sql) and sql[index + 1] == closing:
                        index += 2
                        continue
                    index += 1
                    break
                tokens.append(sql[start:index])
                continue
            compound = next(
                (operator for operator in compound_operators if sql.startswith(operator, index)),
                None,
            )
            if compound is not None:
                tokens.append(compound)
                index += len(compound)
                continue
            if character in punctuation:
                tokens.append(character)
                index += 1
                continue
            start = index
            while (
                index < len(sql)
                and not sql[index].isspace()
                and sql[index] not in punctuation
                and sql[index] not in {'"', "'", "`", "["}
            ):
                index += 1
            tokens.append(sql[start:index].casefold())
        return " ".join(tokens)

    @classmethod
    def _validate_schema(cls, connection: sqlite3.Connection) -> None:
        expected = {
            "canary_rollout_scopes": _SCOPE_COLUMNS,
            "canary_rollout_attempts": _ATTEMPT_COLUMNS,
            "canary_rollout_outcomes": _OUTCOME_COLUMNS,
        }
        for table, columns in expected.items():
            observed = {
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if observed != columns:
                raise sqlite3.DatabaseError("canary rollout schema columns mismatch")
        expected_objects: dict[tuple[str, str], str] = {}
        for statement in cls._schema_statements():
            tokens = statement.split()
            if len(tokens) < 3 or tokens[0].casefold() != "create":
                raise sqlite3.DatabaseError("invalid built-in canary schema contract")
            object_type = tokens[1].casefold()
            object_name = tokens[2].strip('"`[]').casefold()
            expected_objects[(object_type, object_name)] = cls._normalize_schema_sql(statement)
        observed_objects = {
            (str(row[0]).casefold(), str(row[1]).casefold()): cls._normalize_schema_sql(row[2])
            for row in connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE sql IS NOT NULL AND (name LIKE 'canary_rollout_%' "
                "OR tbl_name LIKE 'canary_rollout_%')"
            ).fetchall()
        }
        if observed_objects != expected_objects:
            raise sqlite3.DatabaseError("canary rollout SQLite contract mismatch")
        violations = connection.execute("PRAGMA foreign_key_check").fetchone()
        if violations is not None:
            raise sqlite3.DatabaseError("canary rollout foreign key check failed")

    def _open_write(self) -> sqlite3.Connection:
        if not self._ensure_initialized():
            raise _StorageError(transient=not self._is_sticky_unavailable())
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            return connection
        except sqlite3.Error as exc:
            try:
                connection.close()
            except (NameError, sqlite3.Error):
                pass
            if not _sqlite_is_busy(exc):
                self._mark_sticky_unavailable()
            raise _StorageError(transient=_sqlite_is_busy(exc)) from None

    def _storage_failed(self, connection: sqlite3.Connection, exc: sqlite3.Error) -> None:
        self._rollback_quietly(connection)
        if not _sqlite_is_busy(exc):
            self._mark_sticky_unavailable()
        raise _StorageError(transient=_sqlite_is_busy(exc)) from None

    @staticmethod
    def _scope_args(scope: CanaryRolloutScope) -> tuple[str, str, str]:
        return scope.policy_sha256, scope.role.value, scope.deployment_sha256

    def _scope_row(
        self,
        connection: sqlite3.Connection,
        scope: CanaryRolloutScope,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM canary_rollout_scopes "
            "WHERE policy_sha256=? AND role=? AND deployment_sha256=?",
            self._scope_args(scope),
        ).fetchone()

    @staticmethod
    def _require_monotonic_scope_time(row: sqlite3.Row, now_ms: int) -> None:
        """Reject a regressed caller clock before mutating this scope."""

        if now_ms < _db_non_negative_int(row["updated_at_ms"]):
            raise _ClockRegressionError

    @staticmethod
    def _authenticated_scope_policy(row: sqlite3.Row) -> CanaryRolloutPolicy:
        """Rebuild and authenticate the row-owned policy used by cross-scope GC."""

        raw_contract = row["policy_contract_json"]
        raw_sha256 = row["policy_contract_sha256"]
        if not isinstance(raw_contract, str) or not _is_sha256(raw_sha256):
            raise sqlite3.DatabaseError("invalid persisted canary policy contract")
        try:
            values = json.loads(raw_contract)
        except (TypeError, json.JSONDecodeError) as exc:
            raise sqlite3.DatabaseError("invalid persisted canary policy JSON") from exc
        if not isinstance(values, dict):
            raise sqlite3.DatabaseError("persisted canary policy must be an object")
        try:
            policy = CanaryRolloutPolicy(**values)
        except (TypeError, ValueError) as exc:
            raise sqlite3.DatabaseError("invalid persisted canary policy values") from exc
        if (
            policy._canonical_contract_json() != raw_contract
            or policy.contract_sha256 != raw_sha256
            or _db_non_negative_int(row["scope_retention_s"]) != policy.scope_retention_s
        ):
            raise sqlite3.DatabaseError("persisted canary policy authentication failed")
        return policy

    @staticmethod
    def _is_complete_probe_success(
        provider: CanaryProviderOutcome,
        usage: CanaryUsageOutcome,
        quality: CanaryQualityOutcome,
        policy: CanaryRolloutPolicy,
    ) -> bool:
        outcome = CanaryAttemptOutcome(
            provider=provider,
            usage=usage,
            quality=quality,
        )
        return bool(
            outcome.complete_probe_success
            and (not policy.quality_gate_enabled or quality is CanaryQualityOutcome.PASSED)
        )

    def _validate_scope_policy_state(
        self,
        connection: sqlite3.Connection,
        scope: CanaryRolloutScope,
        policy: CanaryRolloutPolicy,
        row: sqlite3.Row,
    ) -> None:
        """Reject persisted counters or relationships outside this policy."""

        state = _db_enum(CanaryRolloutState, row["state"])
        raw_reason = row["latch_reason"]
        reason = _db_enum(CanaryLatchReason, raw_reason) if raw_reason is not None else None
        latched_at_ms = (
            _db_non_negative_int(row["latched_at_ms"]) if row["latched_at_ms"] is not None else None
        )
        raw_recovery_epoch = row["recovery_epoch_sha256"]
        recovery_epoch_sha256 = str(raw_recovery_epoch) if raw_recovery_epoch is not None else None
        if recovery_epoch_sha256 is not None and not _is_sha256(recovery_epoch_sha256):
            raise sqlite3.DatabaseError("invalid canary recovery epoch hash")
        recovery_successes = _db_non_negative_int(row["recovery_successes"])
        last_probe_at_ms = (
            _db_non_negative_int(row["last_probe_at_ms"])
            if row["last_probe_at_ms"] is not None
            else None
        )
        created_at_ms = _db_non_negative_int(row["created_at_ms"])
        updated_at_ms = _db_non_negative_int(row["updated_at_ms"])
        if created_at_ms > updated_at_ms:
            raise sqlite3.DatabaseError("canary scope timestamps are inconsistent")
        if recovery_successes >= policy.half_open_successes_required:
            raise sqlite3.DatabaseError("canary recovery counter exceeds policy")
        if state is CanaryRolloutState.ACTIVE:
            if (
                reason is not None
                or latched_at_ms is not None
                or recovery_epoch_sha256 is not None
                or recovery_successes != 0
                or last_probe_at_ms is not None
            ):
                raise sqlite3.DatabaseError("active canary scope has recovery state")
        else:
            if reason is None or latched_at_ms is None or recovery_epoch_sha256 is None:
                raise sqlite3.DatabaseError("inactive canary scope lacks latch state")
            if not created_at_ms <= latched_at_ms <= updated_at_ms:
                raise sqlite3.DatabaseError("canary latch timestamp is inconsistent")
            if reason in _MANUAL_RESET_ONLY_REASONS and recovery_successes != 0:
                raise sqlite3.DatabaseError("manual-only latch has recovery progress")
        if last_probe_at_ms is not None and not (
            created_at_ms <= last_probe_at_ms <= updated_at_ms
        ):
            raise sqlite3.DatabaseError("canary probe timestamp is inconsistent")
        if recovery_successes > 0 and last_probe_at_ms is None:
            raise sqlite3.DatabaseError("canary recovery progress lacks a probe")
        if state is CanaryRolloutState.HALF_OPEN and last_probe_at_ms is None:
            raise sqlite3.DatabaseError("half-open canary scope lacks a probe epoch")

        attempts = connection.execute(
            "SELECT token_sha256, admitted_at_ms, lease_expires_at_ms, is_probe, "
            "recovery_epoch_sha256, probe_ordinal "
            "FROM canary_rollout_attempts WHERE policy_sha256=? AND role=? "
            "AND deployment_sha256=?",
            self._scope_args(scope),
        ).fetchall()
        if len(attempts) > policy.max_pending_per_scope:
            raise sqlite3.DatabaseError("canary pending counter exceeds policy")
        probe_count = 0
        active_count = 0
        probe_admitted_at_ms: int | None = None
        pending_token_hashes: set[str] = set()
        for attempt in attempts:
            token_sha256 = str(attempt["token_sha256"])
            if not _is_sha256(token_sha256):
                raise sqlite3.DatabaseError("invalid canary attempt token hash")
            pending_token_hashes.add(token_sha256)
            admitted_at_ms = _db_non_negative_int(attempt["admitted_at_ms"])
            lease_expires_at_ms = _db_non_negative_int(attempt["lease_expires_at_ms"])
            is_probe = _db_bool(attempt["is_probe"])
            raw_attempt_epoch = attempt["recovery_epoch_sha256"]
            raw_probe_ordinal = attempt["probe_ordinal"]
            if not created_at_ms <= admitted_at_ms <= updated_at_ms:
                raise sqlite3.DatabaseError("canary attempt timestamp is inconsistent")
            expected_lease_ms = (
                policy.half_open_probe_lease_s if is_probe else policy.active_attempt_lease_s
            ) * 1000
            if lease_expires_at_ms - admitted_at_ms != expected_lease_ms:
                raise sqlite3.DatabaseError("canary attempt lease violates policy")
            if is_probe:
                attempt_epoch = str(raw_attempt_epoch)
                if not _is_sha256(attempt_epoch) or attempt_epoch != recovery_epoch_sha256:
                    raise sqlite3.DatabaseError("canary probe lease has the wrong epoch")
                probe_ordinal = _db_non_negative_int(raw_probe_ordinal)
                if probe_ordinal != recovery_successes + 1:
                    raise sqlite3.DatabaseError("canary probe lease has the wrong ordinal")
                probe_count += 1
                probe_admitted_at_ms = admitted_at_ms
            else:
                if raw_attempt_epoch is not None or raw_probe_ordinal is not None:
                    raise sqlite3.DatabaseError("active attempt has recovery metadata")
                active_count += 1
        if probe_count > 1:
            raise sqlite3.DatabaseError("multiple canary recovery probes are persisted")
        if probe_count and state is not CanaryRolloutState.HALF_OPEN:
            raise sqlite3.DatabaseError("canary probe exists outside half-open state")
        if state is CanaryRolloutState.HALF_OPEN and active_count:
            raise sqlite3.DatabaseError("half-open scope retains active attempts")
        if state is CanaryRolloutState.HALF_OPEN:
            if probe_count == 0 and recovery_successes == 0:
                raise sqlite3.DatabaseError("half-open scope has no recovery progress")
            if probe_count and probe_admitted_at_ms != last_probe_at_ms:
                raise sqlite3.DatabaseError("canary probe lease has the wrong epoch")

        outcomes = connection.execute(
            "SELECT outcome_id, token_sha256, observed_at_ms, provider_outcome, "
            "usage_outcome, quality_outcome, is_probe, recovery_epoch_sha256, "
            "probe_ordinal "
            "FROM canary_rollout_outcomes "
            "WHERE policy_sha256=? AND role=? AND deployment_sha256=? "
            "ORDER BY outcome_id",
            self._scope_args(scope),
        ).fetchall()
        if len(outcomes) > policy.window_max_attempts:
            raise sqlite3.DatabaseError("canary outcome counter exceeds policy")
        previous_outcome_id = 0
        previous_observed_at_ms = -1
        current_probe_ordinals: list[int] = []
        for outcome in outcomes:
            outcome_id = _db_non_negative_int(outcome["outcome_id"])
            if outcome_id <= previous_outcome_id:
                raise sqlite3.DatabaseError("canary outcome order is inconsistent")
            previous_outcome_id = outcome_id
            token_sha256 = str(outcome["token_sha256"])
            if not _is_sha256(token_sha256):
                raise sqlite3.DatabaseError("invalid canary outcome token hash")
            if token_sha256 in pending_token_hashes:
                raise sqlite3.DatabaseError("canary token is both pending and settled")
            observed_at_ms = _db_non_negative_int(outcome["observed_at_ms"])
            if not created_at_ms <= observed_at_ms <= updated_at_ms:
                raise sqlite3.DatabaseError("canary outcome timestamp is inconsistent")
            if observed_at_ms < previous_observed_at_ms:
                raise sqlite3.DatabaseError("canary outcome chronology is inconsistent")
            previous_observed_at_ms = observed_at_ms
            provider = _db_enum(CanaryProviderOutcome, outcome["provider_outcome"])
            usage = _db_enum(CanaryUsageOutcome, outcome["usage_outcome"])
            quality = _db_enum(CanaryQualityOutcome, outcome["quality_outcome"])
            is_probe = _db_bool(outcome["is_probe"])
            if not is_probe:
                if (
                    outcome["recovery_epoch_sha256"] is not None
                    or outcome["probe_ordinal"] is not None
                ):
                    raise sqlite3.DatabaseError("active outcome has recovery metadata")
                continue
            if state is CanaryRolloutState.ACTIVE:
                raise sqlite3.DatabaseError("active canary scope retains a probe outcome")
            outcome_epoch = str(outcome["recovery_epoch_sha256"])
            if not _is_sha256(outcome_epoch):
                raise sqlite3.DatabaseError("invalid canary probe outcome epoch")
            probe_ordinal = _db_non_negative_int(outcome["probe_ordinal"])
            if probe_ordinal < 1:
                raise sqlite3.DatabaseError("invalid canary probe outcome ordinal")
            if outcome_epoch != recovery_epoch_sha256:
                if latched_at_ms is None or observed_at_ms > latched_at_ms:
                    raise sqlite3.DatabaseError("historical probe outcome crosses epochs")
                continue
            if latched_at_ms is None or observed_at_ms <= latched_at_ms:
                raise sqlite3.DatabaseError("canary probe outcome is outside its epoch")
            if not self._is_complete_probe_success(provider, usage, quality, policy):
                raise sqlite3.DatabaseError("canary recovery proof is not a complete success")
            if probe_admitted_at_ms is not None and observed_at_ms > probe_admitted_at_ms:
                raise sqlite3.DatabaseError("canary probe outcome follows an inflight probe")
            current_probe_ordinals.append(probe_ordinal)
        expected_ordinals = list(range(1, recovery_successes + 1))
        if current_probe_ordinals != expected_ordinals:
            raise sqlite3.DatabaseError("canary recovery counter lacks exact outcome proof")

    def _persist_contract_mismatch(
        self,
        connection: sqlite3.Connection,
        scope: CanaryRolloutScope,
        now_ms: int,
    ) -> None:
        """Durably stop every worker when one policy hash has two contracts."""

        connection.execute(
            "DELETE FROM canary_rollout_attempts WHERE policy_sha256=? "
            "AND role=? AND deployment_sha256=?",
            self._scope_args(scope),
        )
        connection.execute(
            "UPDATE canary_rollout_scopes SET state='rolled_back', "
            "latch_reason=?, latched_at_ms=?, recovery_epoch_sha256=?, "
            "recovery_successes=0, "
            "updated_at_ms=? WHERE policy_sha256=? AND role=? "
            "AND deployment_sha256=?",
            (
                CanaryLatchReason.POLICY_CONTRACT_MISMATCH.value,
                now_ms,
                _new_recovery_epoch_sha256(),
                now_ms,
                *self._scope_args(scope),
            ),
        )
        # Commit the cross-worker safety latch before making this mismatched
        # process sticky-unavailable.  The caller's later rollback is a no-op.
        connection.commit()
        self._mark_sticky_unavailable()

    def _ensure_scope(
        self,
        connection: sqlite3.Connection,
        scope: CanaryRolloutScope,
        policy: CanaryRolloutPolicy,
        now_ms: int,
    ) -> sqlite3.Row:
        row = self._scope_row(connection, scope)
        if row is not None:
            self._require_monotonic_scope_time(row, now_ms)
            stored_policy = self._authenticated_scope_policy(row)
            if stored_policy.contract_sha256 != policy.contract_sha256:
                self._persist_contract_mismatch(connection, scope, now_ms)
                raise _ContractMismatchError
            self._validate_scope_policy_state(connection, scope, policy, row)
            return row

        same_policy_rows = connection.execute(
            "SELECT * FROM canary_rollout_scopes WHERE policy_sha256=?",
            (scope.policy_sha256,),
        ).fetchall()
        for same_policy_row in same_policy_rows:
            stored_policy = self._authenticated_scope_policy(same_policy_row)
            if stored_policy.contract_sha256 != policy.contract_sha256:
                raise sqlite3.DatabaseError("one canary policy identity has two contracts")

        self._collect_stale_scopes(connection, now_ms)
        count_row = connection.execute("SELECT COUNT(*) FROM canary_rollout_scopes").fetchone()
        count = int(count_row[0]) if count_row is not None else policy.max_scopes
        if count >= policy.max_scopes:
            raise OverflowError("canary rollout scope capacity exhausted")
        connection.execute(
            "INSERT INTO canary_rollout_scopes ("
            "policy_sha256, role, deployment_sha256, policy_contract_sha256, "
            "policy_contract_json, scope_retention_s, "
            "state, latch_reason, latched_at_ms, recovery_epoch_sha256, "
            "recovery_successes, "
            "last_probe_at_ms, created_at_ms, updated_at_ms"
            ") VALUES (?, ?, ?, ?, ?, ?, 'active', NULL, NULL, NULL, 0, NULL, ?, ?)",
            (
                *self._scope_args(scope),
                policy.contract_sha256,
                policy._canonical_contract_json(),
                policy.scope_retention_s,
                now_ms,
                now_ms,
            ),
        )
        created = self._scope_row(connection, scope)
        if created is None:
            raise sqlite3.DatabaseError("canary rollout scope insert disappeared")
        self._validate_scope_policy_state(connection, scope, policy, created)
        return created

    def _collect_stale_scopes(
        self,
        connection: sqlite3.Connection,
        now_ms: int,
    ) -> None:
        rows = connection.execute(
            "SELECT * FROM canary_rollout_scopes WHERE state='active' "
            "ORDER BY policy_sha256, role, deployment_sha256"
        ).fetchall()
        for row in rows:
            try:
                candidate_scope = CanaryRolloutScope(
                    policy_sha256=str(row["policy_sha256"]),
                    role=_db_enum(CanaryRolloutRole, row["role"]),
                    deployment_sha256=str(row["deployment_sha256"]),
                )
            except ValueError as exc:
                raise sqlite3.DatabaseError("invalid persisted canary scope identity") from exc
            candidate_policy = self._authenticated_scope_policy(row)
            self._validate_scope_policy_state(
                connection,
                candidate_scope,
                candidate_policy,
                row,
            )
            updated_at_ms = _db_non_negative_int(row["updated_at_ms"])
            expires_at_ms = updated_at_ms + candidate_policy.scope_retention_s * 1000
            # Preserve the scope at the exact expiry boundary.  Deletion is
            # allowed only after its own authenticated retention has elapsed.
            if now_ms <= expires_at_ms:
                continue
            pending = connection.execute(
                "SELECT 1 FROM canary_rollout_attempts WHERE policy_sha256=? "
                "AND role=? AND deployment_sha256=? LIMIT 1",
                self._scope_args(candidate_scope),
            ).fetchone()
            if pending is not None:
                continue
            connection.execute(
                "DELETE FROM canary_rollout_scopes WHERE policy_sha256=? "
                "AND role=? AND deployment_sha256=? AND state='active'",
                self._scope_args(candidate_scope),
            )

    def _prune_window(
        self,
        connection: sqlite3.Connection,
        scope: CanaryRolloutScope,
        policy: CanaryRolloutPolicy,
        now_ms: int,
    ) -> None:
        args = self._scope_args(scope)
        cutoff = max(0, now_ms - policy.window_max_age_s * 1000)
        row = self._scope_row(connection, scope)
        if row is None:
            raise sqlite3.DatabaseError("canary rollout scope disappeared")
        current_epoch = row["recovery_epoch_sha256"]
        if current_epoch is None:
            connection.execute(
                "DELETE FROM canary_rollout_outcomes "
                "WHERE policy_sha256=? AND role=? AND deployment_sha256=? "
                "AND observed_at_ms < ?",
                (*args, cutoff),
            )
        else:
            connection.execute(
                "DELETE FROM canary_rollout_outcomes "
                "WHERE policy_sha256=? AND role=? AND deployment_sha256=? "
                "AND observed_at_ms < ? AND (is_probe=0 "
                "OR recovery_epoch_sha256<>?)",
                (*args, cutoff, str(current_epoch)),
            )
        connection.execute(
            "DELETE FROM canary_rollout_outcomes "
            "WHERE policy_sha256=? AND role=? AND deployment_sha256=? "
            "AND outcome_id NOT IN ("
            "  SELECT outcome_id FROM canary_rollout_outcomes "
            "  WHERE policy_sha256=? AND role=? AND deployment_sha256=? "
            "  ORDER BY observed_at_ms DESC, outcome_id DESC LIMIT ?"
            ")",
            (*args, *args, policy.window_max_attempts),
        )
        # ``updated_at_ms`` is the per-scope clock watermark, not merely a
        # state-transition timestamp.  Pruning at a future time must prevent
        # a later caller from replaying an earlier window after evidence was
        # already aged out.
        connection.execute(
            "UPDATE canary_rollout_scopes SET updated_at_ms=? "
            "WHERE policy_sha256=? AND role=? AND deployment_sha256=?",
            (now_ms, *args),
        )

    def _insert_outcome(
        self,
        connection: sqlite3.Connection,
        scope: CanaryRolloutScope,
        *,
        token_sha256: str,
        outcome: CanaryAttemptOutcome,
        observed_at_ms: int,
        is_probe: bool,
        recovery_epoch_sha256: str | None,
        probe_ordinal: int | None,
    ) -> None:
        connection.execute(
            "INSERT INTO canary_rollout_outcomes ("
            "policy_sha256, role, deployment_sha256, token_sha256, "
            "observed_at_ms, provider_outcome, usage_outcome, quality_outcome, "
            "is_probe, recovery_epoch_sha256, probe_ordinal) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                *self._scope_args(scope),
                token_sha256,
                observed_at_ms,
                outcome.provider.value,
                outcome.usage.value,
                outcome.quality.value,
                int(is_probe),
                recovery_epoch_sha256,
                probe_ordinal,
            ),
        )

    def _expire_attempts(
        self,
        connection: sqlite3.Connection,
        scope: CanaryRolloutScope,
        policy: CanaryRolloutPolicy,
        now_ms: int,
    ) -> None:
        rows = connection.execute(
            "SELECT token_sha256, is_probe, recovery_epoch_sha256, probe_ordinal "
            "FROM canary_rollout_attempts "
            "WHERE policy_sha256=? AND role=? AND deployment_sha256=? "
            "AND lease_expires_at_ms <= ? ORDER BY lease_expires_at_ms, token_sha256",
            (*self._scope_args(scope), now_ms),
        ).fetchall()
        if not rows:
            return
        unknown = CanaryAttemptOutcome(
            provider=CanaryProviderOutcome.UNKNOWN_FAILURE,
            usage=CanaryUsageOutcome.NOT_APPLICABLE,
            quality=CanaryQualityOutcome.NOT_APPLICABLE,
        )
        for row in rows:
            token_hash = str(row["token_sha256"])
            is_probe = _db_bool(row["is_probe"])
            self._insert_outcome(
                connection,
                scope,
                token_sha256=token_hash,
                outcome=unknown,
                observed_at_ms=now_ms,
                is_probe=is_probe,
                recovery_epoch_sha256=row["recovery_epoch_sha256"],
                probe_ordinal=row["probe_ordinal"],
            )
            connection.execute(
                "DELETE FROM canary_rollout_attempts "
                "WHERE policy_sha256=? AND role=? AND deployment_sha256=? "
                "AND token_sha256=?",
                (*self._scope_args(scope), token_hash),
            )
            current = self._scope_row(connection, scope)
            if current is None:
                raise sqlite3.DatabaseError("canary rollout scope disappeared")
            current_state = _db_enum(CanaryRolloutState, current["state"])
            if is_probe and current_state is CanaryRolloutState.HALF_OPEN:
                self._latch(
                    connection,
                    scope,
                    CanaryLatchReason.PROBE_ABANDONED,
                    now_ms,
                    force_new_epoch=True,
                )
            elif not is_probe and current_state is CanaryRolloutState.ACTIVE:
                self._latch(
                    connection,
                    scope,
                    CanaryLatchReason.ATTEMPT_ABANDONED,
                    now_ms,
                )
            elif not is_probe:
                # An attempt admitted before a rollback may expire after the
                # latch.  Preserve that latch epoch while keeping the scope's
                # update boundary at least as new as every persisted outcome.
                connection.execute(
                    "UPDATE canary_rollout_scopes SET updated_at_ms=? "
                    "WHERE policy_sha256=? AND role=? AND deployment_sha256=?",
                    (now_ms, *self._scope_args(scope)),
                )
        self._prune_window(connection, scope, policy, now_ms)

    def _window_rows(
        self,
        connection: sqlite3.Connection,
        scope: CanaryRolloutScope,
    ) -> list[sqlite3.Row]:
        return connection.execute(
            "SELECT provider_outcome, usage_outcome, quality_outcome "
            "FROM canary_rollout_outcomes "
            "WHERE policy_sha256=? AND role=? AND deployment_sha256=? "
            "ORDER BY observed_at_ms DESC, outcome_id DESC",
            self._scope_args(scope),
        ).fetchall()

    @staticmethod
    def _counts(rows: list[sqlite3.Row]) -> dict[str, int]:
        provider_failures = sum(
            1
            for row in rows
            if _db_enum(CanaryProviderOutcome, row["provider_outcome"]) in _PROVIDER_FAILURES
        )
        return {
            "attempts": len(rows),
            "provider_failures": provider_failures,
            "rate_limited": sum(
                1
                for row in rows
                if _db_enum(CanaryProviderOutcome, row["provider_outcome"])
                is CanaryProviderOutcome.RATE_LIMITED
            ),
            "usage_missing": sum(
                1
                for row in rows
                if _db_enum(CanaryUsageOutcome, row["usage_outcome"]) is CanaryUsageOutcome.MISSING
            ),
            "quality_observed": sum(
                1
                for row in rows
                if _db_enum(CanaryQualityOutcome, row["quality_outcome"])
                in {
                    CanaryQualityOutcome.PASSED,
                    CanaryQualityOutcome.FAILED,
                }
            ),
            "quality_failures": sum(
                1
                for row in rows
                if _db_enum(CanaryQualityOutcome, row["quality_outcome"])
                is CanaryQualityOutcome.FAILED
            ),
        }

    @staticmethod
    def _consecutive_provider_failures(rows: list[sqlite3.Row]) -> int:
        count = 0
        for row in rows:
            provider_outcome = _db_enum(
                CanaryProviderOutcome,
                row["provider_outcome"],
            )
            if provider_outcome not in _PROVIDER_FAILURES:
                break
            count += 1
        return count

    def _threshold_reason(
        self,
        rows: list[sqlite3.Row],
        policy: CanaryRolloutPolicy,
    ) -> CanaryLatchReason | None:
        counts = self._counts(rows)
        if any(
            _db_enum(CanaryProviderOutcome, row["provider_outcome"])
            is CanaryProviderOutcome.CONFIGURATION_FAILURE
            for row in rows
        ):
            return CanaryLatchReason.CONFIGURATION_FAILURE
        if counts["rate_limited"] > policy.max_rate_limited_count:
            return CanaryLatchReason.RATE_LIMITED
        if counts["usage_missing"] > policy.max_usage_missing_count:
            return CanaryLatchReason.USAGE_MISSING
        if self._consecutive_provider_failures(rows) >= policy.max_consecutive_provider_failures:
            return CanaryLatchReason.CONSECUTIVE_PROVIDER_FAILURES
        if (
            counts["attempts"] >= policy.min_attempts
            and counts["provider_failures"] * 10_000
            > policy.max_provider_failure_basis_points * counts["attempts"]
        ):
            return CanaryLatchReason.PROVIDER_FAILURE_RATE
        if policy.quality_gate_enabled:
            if (
                counts["attempts"] >= policy.min_attempts
                and counts["quality_observed"] * 10_000
                < policy.min_quality_coverage_basis_points * counts["attempts"]
            ):
                return CanaryLatchReason.QUALITY_COVERAGE_INSUFFICIENT
            if (
                counts["quality_observed"] >= policy.min_quality_attempts
                and counts["quality_failures"] * 10_000
                > policy.max_quality_failure_basis_points * counts["quality_observed"]
            ):
                return CanaryLatchReason.QUALITY_FAILURE_RATE
        return None

    def _latch(
        self,
        connection: sqlite3.Connection,
        scope: CanaryRolloutScope,
        reason: CanaryLatchReason,
        now_ms: int,
        *,
        force_new_epoch: bool = False,
    ) -> None:
        row = self._scope_row(connection, scope)
        if row is None:
            raise sqlite3.DatabaseError("canary rollout scope disappeared")
        current_state = _db_enum(CanaryRolloutState, row["state"])
        if current_state is CanaryRolloutState.ROLLED_BACK and not force_new_epoch:
            # Late outcomes from attempts admitted before the latch must not
            # rewrite the root cause or timestamp of the current rollback.
            connection.execute(
                "UPDATE canary_rollout_scopes SET updated_at_ms=? "
                "WHERE policy_sha256=? AND role=? AND deployment_sha256=?",
                (now_ms, *self._scope_args(scope)),
            )
            return
        connection.execute(
            "UPDATE canary_rollout_scopes SET state='rolled_back', "
            "latch_reason=?, latched_at_ms=?, recovery_epoch_sha256=?, "
            "recovery_successes=0, "
            "updated_at_ms=? "
            "WHERE policy_sha256=? AND role=? AND deployment_sha256=?",
            (
                reason.value,
                now_ms,
                _new_recovery_epoch_sha256(),
                now_ms,
                *self._scope_args(scope),
            ),
        )

    def _snapshot_locked(
        self,
        connection: sqlite3.Connection,
        scope: CanaryRolloutScope,
    ) -> CanaryRolloutSnapshot:
        row = self._scope_row(connection, scope)
        if row is None:
            return CanaryRolloutSnapshot(
                available=True,
                found=False,
                state=CanaryRolloutState.ACTIVE,
            )
        rows = self._window_rows(connection, scope)
        counts = self._counts(rows)
        probe_row = connection.execute(
            "SELECT 1 FROM canary_rollout_attempts "
            "WHERE policy_sha256=? AND role=? AND deployment_sha256=? "
            "AND is_probe=1 LIMIT 1",
            self._scope_args(scope),
        ).fetchone()
        raw_reason = row["latch_reason"]
        return CanaryRolloutSnapshot(
            available=True,
            found=True,
            state=_db_enum(CanaryRolloutState, row["state"]),
            latch_reason=(
                _db_enum(CanaryLatchReason, raw_reason) if raw_reason is not None else None
            ),
            latched_at_ms=(
                _db_non_negative_int(row["latched_at_ms"])
                if row["latched_at_ms"] is not None
                else None
            ),
            window_attempts=counts["attempts"],
            provider_failures=counts["provider_failures"],
            rate_limited=counts["rate_limited"],
            usage_missing=counts["usage_missing"],
            quality_observed=counts["quality_observed"],
            quality_failures=counts["quality_failures"],
            recovery_successes=_db_non_negative_int(row["recovery_successes"]),
            probe_inflight=probe_row is not None,
        )

    @staticmethod
    def _unavailable_admission() -> CanaryAdmission:
        return CanaryAdmission(
            available=False,
            allowed=False,
            reason=CanaryAdmissionReason.LEDGER_UNAVAILABLE,
            snapshot=CanaryRolloutSnapshot.unavailable(),
        )

    @staticmethod
    def _unavailable_mutation() -> CanaryMutationResult:
        return CanaryMutationResult(
            available=False,
            applied=False,
            reason=CanaryMutationReason.LEDGER_UNAVAILABLE,
            snapshot=CanaryRolloutSnapshot.unavailable(),
        )

    def begin_attempt(
        self,
        scope: CanaryRolloutScope,
        policy: CanaryRolloutPolicy,
        *,
        now_ms: int | None = None,
    ) -> CanaryAdmission:
        """Atomically admit an active attempt or the sole half-open probe."""

        timestamp = _now_ms(self._clock, now_ms)
        connection: sqlite3.Connection | None = None
        try:
            connection = self._open_write()
            self._ensure_scope(connection, scope, policy, timestamp)
            self._expire_attempts(connection, scope, policy, timestamp)
            self._prune_window(connection, scope, policy, timestamp)
            row = self._scope_row(connection, scope)
            if row is None:
                raise sqlite3.DatabaseError("canary rollout scope disappeared")
            state = _db_enum(CanaryRolloutState, row["state"])
            if state is CanaryRolloutState.ROLLED_BACK:
                latch_reason = _db_enum(
                    CanaryLatchReason,
                    row["latch_reason"],
                )
                latched_at_ms = _db_non_negative_int(row["latched_at_ms"])
                ready_at = latched_at_ms + policy.rollback_cooldown_s * 1000
                if latch_reason in _MANUAL_RESET_ONLY_REASONS or timestamp < ready_at:
                    snapshot = self._snapshot_locked(connection, scope)
                    connection.commit()
                    return CanaryAdmission(
                        available=True,
                        allowed=False,
                        reason=CanaryAdmissionReason.LATCHED,
                        snapshot=snapshot,
                    )
                state = CanaryRolloutState.HALF_OPEN

            pending_row = connection.execute(
                "SELECT COUNT(*) FROM canary_rollout_attempts "
                "WHERE policy_sha256=? AND role=? AND deployment_sha256=?",
                self._scope_args(scope),
            ).fetchone()
            pending_count = int(pending_row[0]) if pending_row is not None else 0
            if pending_count >= policy.max_pending_per_scope:
                snapshot = self._snapshot_locked(connection, scope)
                connection.commit()
                return CanaryAdmission(
                    available=True,
                    allowed=False,
                    reason=CanaryAdmissionReason.PENDING_CAPACITY,
                    snapshot=snapshot,
                )

            probe = state is CanaryRolloutState.HALF_OPEN
            if probe:
                probe_row = connection.execute(
                    "SELECT 1 FROM canary_rollout_attempts "
                    "WHERE policy_sha256=? AND role=? AND deployment_sha256=? "
                    "AND is_probe=1 LIMIT 1",
                    self._scope_args(scope),
                ).fetchone()
                if probe_row is not None:
                    snapshot = self._snapshot_locked(connection, scope)
                    connection.commit()
                    return CanaryAdmission(
                        available=True,
                        allowed=False,
                        reason=CanaryAdmissionReason.HALF_OPEN_BUSY,
                        snapshot=snapshot,
                    )
                last_probe = row["last_probe_at_ms"]
                if (
                    last_probe is not None
                    and timestamp
                    < _db_non_negative_int(last_probe) + policy.half_open_probe_spacing_s * 1000
                ):
                    snapshot = self._snapshot_locked(connection, scope)
                    connection.commit()
                    return CanaryAdmission(
                        available=True,
                        allowed=False,
                        reason=CanaryAdmissionReason.RECOVERY_WAIT,
                        snapshot=snapshot,
                    )

            token = secrets.token_urlsafe(32)
            token_hash = _token_sha256(token)
            lease_s = policy.half_open_probe_lease_s if probe else policy.active_attempt_lease_s
            recovery_epoch_sha256: str | None = None
            probe_ordinal: int | None = None
            if probe:
                recovery_epoch_sha256 = str(row["recovery_epoch_sha256"])
                if not _is_sha256(recovery_epoch_sha256):
                    raise sqlite3.DatabaseError("invalid canary recovery epoch hash")
                probe_ordinal = _db_non_negative_int(row["recovery_successes"]) + 1
            connection.execute(
                "INSERT INTO canary_rollout_attempts ("
                "policy_sha256, role, deployment_sha256, token_sha256, "
                "admitted_at_ms, lease_expires_at_ms, is_probe, "
                "recovery_epoch_sha256, probe_ordinal"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    *self._scope_args(scope),
                    token_hash,
                    timestamp,
                    timestamp + lease_s * 1000,
                    int(probe),
                    recovery_epoch_sha256,
                    probe_ordinal,
                ),
            )
            if probe:
                connection.execute(
                    "UPDATE canary_rollout_scopes SET state='half_open', "
                    "last_probe_at_ms=?, updated_at_ms=? "
                    "WHERE policy_sha256=? AND role=? AND deployment_sha256=?",
                    (timestamp, timestamp, *self._scope_args(scope)),
                )
            else:
                connection.execute(
                    "UPDATE canary_rollout_scopes SET updated_at_ms=? "
                    "WHERE policy_sha256=? AND role=? AND deployment_sha256=?",
                    (timestamp, *self._scope_args(scope)),
                )
            snapshot = self._snapshot_locked(connection, scope)
            connection.commit()
            return CanaryAdmission(
                available=True,
                allowed=True,
                reason=(
                    CanaryAdmissionReason.HALF_OPEN_PROBE if probe else CanaryAdmissionReason.ACTIVE
                ),
                snapshot=snapshot,
                probe=probe,
                token=token,
            )
        except OverflowError:
            if connection is not None:
                self._rollback_quietly(connection)
            snapshot = CanaryRolloutSnapshot(
                available=True,
                found=False,
                state=CanaryRolloutState.ROLLED_BACK,
            )
            return CanaryAdmission(
                available=True,
                allowed=False,
                reason=CanaryAdmissionReason.SCOPE_CAPACITY,
                snapshot=snapshot,
            )
        except (_StorageError, _ContractMismatchError, _ClockRegressionError):
            if connection is not None:
                self._rollback_quietly(connection)
            return self._unavailable_admission()
        except sqlite3.Error as exc:
            if connection is not None:
                try:
                    self._storage_failed(connection, exc)
                except _StorageError:
                    pass
            return self._unavailable_admission()
        finally:
            if connection is not None:
                connection.close()

    def settle_attempt(
        self,
        scope: CanaryRolloutScope,
        policy: CanaryRolloutPolicy,
        token: str,
        outcome: CanaryAttemptOutcome,
        *,
        now_ms: int | None = None,
    ) -> CanaryMutationResult:
        """Settle one token exactly once and apply latch/recovery transitions."""

        if not isinstance(token, str) or not token:
            raise ValueError("token must be a non-empty string")
        timestamp = _now_ms(self._clock, now_ms)
        token_hash = _token_sha256(token)
        connection: sqlite3.Connection | None = None
        try:
            connection = self._open_write()
            self._ensure_scope(connection, scope, policy, timestamp)
            self._expire_attempts(connection, scope, policy, timestamp)
            self._prune_window(connection, scope, policy, timestamp)
            attempt = connection.execute(
                "SELECT is_probe, recovery_epoch_sha256, probe_ordinal "
                "FROM canary_rollout_attempts "
                "WHERE policy_sha256=? AND role=? AND deployment_sha256=? "
                "AND token_sha256=?",
                (*self._scope_args(scope), token_hash),
            ).fetchone()
            if attempt is None:
                prior = connection.execute(
                    "SELECT provider_outcome, usage_outcome, quality_outcome "
                    "FROM canary_rollout_outcomes WHERE policy_sha256=? "
                    "AND role=? AND deployment_sha256=? AND token_sha256=?",
                    (*self._scope_args(scope), token_hash),
                ).fetchone()
                snapshot = self._snapshot_locked(connection, scope)
                connection.commit()
                if prior is None:
                    return CanaryMutationResult(
                        available=True,
                        applied=False,
                        reason=CanaryMutationReason.UNKNOWN_TOKEN,
                        snapshot=snapshot,
                    )
                same = bool(
                    _db_enum(CanaryProviderOutcome, prior["provider_outcome"]) is outcome.provider
                    and _db_enum(CanaryUsageOutcome, prior["usage_outcome"]) is outcome.usage
                    and _db_enum(CanaryQualityOutcome, prior["quality_outcome"]) is outcome.quality
                )
                return CanaryMutationResult(
                    available=True,
                    applied=False,
                    reason=(
                        CanaryMutationReason.DUPLICATE
                        if same
                        else CanaryMutationReason.TOKEN_CONFLICT
                    ),
                    snapshot=snapshot,
                )

            is_probe = _db_bool(attempt["is_probe"])
            self._insert_outcome(
                connection,
                scope,
                token_sha256=token_hash,
                outcome=outcome,
                observed_at_ms=timestamp,
                is_probe=is_probe,
                recovery_epoch_sha256=attempt["recovery_epoch_sha256"],
                probe_ordinal=attempt["probe_ordinal"],
            )
            connection.execute(
                "DELETE FROM canary_rollout_attempts "
                "WHERE policy_sha256=? AND role=? AND deployment_sha256=? "
                "AND token_sha256=?",
                (*self._scope_args(scope), token_hash),
            )
            self._prune_window(connection, scope, policy, timestamp)
            row = self._scope_row(connection, scope)
            if row is None:
                raise sqlite3.DatabaseError("canary rollout scope disappeared")
            state = _db_enum(CanaryRolloutState, row["state"])
            if is_probe:
                if state is CanaryRolloutState.HALF_OPEN:
                    complete_success = self._is_complete_probe_success(
                        outcome.provider,
                        outcome.usage,
                        outcome.quality,
                        policy,
                    )
                    if not complete_success:
                        self._latch(
                            connection,
                            scope,
                            CanaryLatchReason.PROBE_FAILED,
                            timestamp,
                            force_new_epoch=True,
                        )
                    else:
                        successes = _db_non_negative_int(row["recovery_successes"]) + 1
                        if successes >= policy.half_open_successes_required:
                            connection.execute(
                                "UPDATE canary_rollout_scopes SET state='active', "
                                "latch_reason=NULL, latched_at_ms=NULL, "
                                "recovery_epoch_sha256=NULL, recovery_successes=0, "
                                "last_probe_at_ms=NULL, "
                                "updated_at_ms=? WHERE policy_sha256=? "
                                "AND role=? AND deployment_sha256=?",
                                (timestamp, *self._scope_args(scope)),
                            )
                            connection.execute(
                                "DELETE FROM canary_rollout_outcomes "
                                "WHERE policy_sha256=? AND role=? "
                                "AND deployment_sha256=?",
                                self._scope_args(scope),
                            )
                        else:
                            connection.execute(
                                "UPDATE canary_rollout_scopes "
                                "SET recovery_successes=?, updated_at_ms=? "
                                "WHERE policy_sha256=? AND role=? "
                                "AND deployment_sha256=?",
                                (
                                    successes,
                                    timestamp,
                                    *self._scope_args(scope),
                                ),
                            )
            elif state is CanaryRolloutState.ACTIVE:
                reason = self._threshold_reason(
                    self._window_rows(connection, scope),
                    policy,
                )
                if reason is not None:
                    self._latch(connection, scope, reason, timestamp)
                else:
                    connection.execute(
                        "UPDATE canary_rollout_scopes SET updated_at_ms=? "
                        "WHERE policy_sha256=? AND role=? AND deployment_sha256=?",
                        (timestamp, *self._scope_args(scope)),
                    )
            else:
                # Late settlement of an active attempt admitted before the
                # latch is audit data, not a new rollback epoch.
                connection.execute(
                    "UPDATE canary_rollout_scopes SET updated_at_ms=? "
                    "WHERE policy_sha256=? AND role=? AND deployment_sha256=?",
                    (timestamp, *self._scope_args(scope)),
                )
            snapshot = self._snapshot_locked(connection, scope)
            connection.commit()
            return CanaryMutationResult(
                available=True,
                applied=True,
                reason=CanaryMutationReason.APPLIED,
                snapshot=snapshot,
            )
        except (_StorageError, _ContractMismatchError, _ClockRegressionError):
            if connection is not None:
                self._rollback_quietly(connection)
            return self._unavailable_mutation()
        except sqlite3.Error as exc:
            if connection is not None:
                try:
                    self._storage_failed(connection, exc)
                except _StorageError:
                    pass
            return self._unavailable_mutation()
        finally:
            if connection is not None:
                connection.close()

    def cancel_attempt(
        self,
        scope: CanaryRolloutScope,
        policy: CanaryRolloutPolicy,
        token: str,
        *,
        now_ms: int | None = None,
    ) -> CanaryMutationResult:
        """Cancel an admission that did not cross the physical boundary."""

        if not isinstance(token, str) or not token:
            raise ValueError("token must be a non-empty string")
        timestamp = _now_ms(self._clock, now_ms)
        token_hash = _token_sha256(token)
        connection: sqlite3.Connection | None = None
        try:
            connection = self._open_write()
            self._ensure_scope(connection, scope, policy, timestamp)
            # Cancellation is valid only while the exact admission lease is
            # live.  Advance every expired lease in the same write
            # transaction before looking up the caller's token; otherwise a
            # worker could erase an abandoned active attempt or recovery
            # probe arbitrarily long after its deadline.
            self._expire_attempts(connection, scope, policy, timestamp)
            self._prune_window(connection, scope, policy, timestamp)
            attempt = connection.execute(
                "SELECT admitted_at_ms, lease_expires_at_ms, is_probe "
                "FROM canary_rollout_attempts "
                "WHERE policy_sha256=? AND role=? AND deployment_sha256=? "
                "AND token_sha256=?",
                (*self._scope_args(scope), token_hash),
            ).fetchone()
            if attempt is None:
                snapshot = self._snapshot_locked(connection, scope)
                connection.commit()
                return CanaryMutationResult(
                    available=True,
                    applied=False,
                    reason=CanaryMutationReason.UNKNOWN_TOKEN,
                    snapshot=snapshot,
                )
            admitted_at_ms = _db_non_negative_int(attempt["admitted_at_ms"])
            lease_expires_at_ms = _db_non_negative_int(attempt["lease_expires_at_ms"])
            if not admitted_at_ms <= timestamp < lease_expires_at_ms:
                raise sqlite3.DatabaseError("canary cancellation observed an inconsistent lease")
            is_probe = _db_bool(attempt["is_probe"])
            connection.execute(
                "DELETE FROM canary_rollout_attempts "
                "WHERE policy_sha256=? AND role=? AND deployment_sha256=? "
                "AND token_sha256=?",
                (*self._scope_args(scope), token_hash),
            )
            row = self._scope_row(connection, scope)
            if (
                is_probe
                and row is not None
                and _db_enum(CanaryRolloutState, row["state"]) is CanaryRolloutState.HALF_OPEN
            ):
                # Preserve the original latch epoch.  Probe spacing prevents
                # a cancellation loop from immediately reacquiring the lease.
                connection.execute(
                    "UPDATE canary_rollout_scopes SET state='rolled_back', "
                    "updated_at_ms=? WHERE policy_sha256=? AND role=? "
                    "AND deployment_sha256=?",
                    (timestamp, *self._scope_args(scope)),
                )
            snapshot = self._snapshot_locked(connection, scope)
            connection.commit()
            return CanaryMutationResult(
                available=True,
                applied=True,
                reason=CanaryMutationReason.CANCELLED,
                snapshot=snapshot,
            )
        except (_StorageError, _ContractMismatchError, _ClockRegressionError):
            if connection is not None:
                self._rollback_quietly(connection)
            return self._unavailable_mutation()
        except sqlite3.Error as exc:
            if connection is not None:
                try:
                    self._storage_failed(connection, exc)
                except _StorageError:
                    pass
            return self._unavailable_mutation()
        finally:
            if connection is not None:
                connection.close()

    def manual_reset(
        self,
        scope: CanaryRolloutScope,
        policy: CanaryRolloutPolicy,
        *,
        now_ms: int | None = None,
    ) -> CanaryMutationResult:
        """Explicitly clear one exact scope; never broadens the reset target."""

        timestamp = _now_ms(self._clock, now_ms)
        connection: sqlite3.Connection | None = None
        try:
            connection = self._open_write()
            row = self._scope_row(connection, scope)
            if row is None:
                snapshot = self._snapshot_locked(connection, scope)
                connection.commit()
                return CanaryMutationResult(
                    available=True,
                    applied=False,
                    reason=CanaryMutationReason.UNKNOWN_SCOPE,
                    snapshot=snapshot,
                )
            self._require_monotonic_scope_time(row, timestamp)
            stored_policy = self._authenticated_scope_policy(row)
            if stored_policy.contract_sha256 != policy.contract_sha256:
                self._persist_contract_mismatch(connection, scope, timestamp)
                raise _ContractMismatchError
            self._validate_scope_policy_state(connection, scope, policy, row)
            connection.execute(
                "DELETE FROM canary_rollout_attempts WHERE policy_sha256=? "
                "AND role=? AND deployment_sha256=?",
                self._scope_args(scope),
            )
            connection.execute(
                "DELETE FROM canary_rollout_outcomes WHERE policy_sha256=? "
                "AND role=? AND deployment_sha256=?",
                self._scope_args(scope),
            )
            connection.execute(
                "UPDATE canary_rollout_scopes SET state='active', "
                "latch_reason=NULL, latched_at_ms=NULL, "
                "recovery_epoch_sha256=NULL, recovery_successes=0, "
                "last_probe_at_ms=NULL, updated_at_ms=? "
                "WHERE policy_sha256=? AND role=? AND deployment_sha256=?",
                (timestamp, *self._scope_args(scope)),
            )
            snapshot = self._snapshot_locked(connection, scope)
            connection.commit()
            return CanaryMutationResult(
                available=True,
                applied=True,
                reason=CanaryMutationReason.APPLIED,
                snapshot=snapshot,
            )
        except (_StorageError, _ContractMismatchError, _ClockRegressionError):
            if connection is not None:
                self._rollback_quietly(connection)
            return self._unavailable_mutation()
        except sqlite3.Error as exc:
            if connection is not None:
                try:
                    self._storage_failed(connection, exc)
                except _StorageError:
                    pass
            return self._unavailable_mutation()
        finally:
            if connection is not None:
                connection.close()

    def snapshot(
        self,
        scope: CanaryRolloutScope,
        policy: CanaryRolloutPolicy,
        *,
        now_ms: int | None = None,
    ) -> CanaryRolloutSnapshot:
        """Return a bounded snapshot, expiring abandoned leases first."""

        timestamp = _now_ms(self._clock, now_ms)
        connection: sqlite3.Connection | None = None
        try:
            connection = self._open_write()
            row = self._scope_row(connection, scope)
            if row is None:
                snapshot = self._snapshot_locked(connection, scope)
                connection.commit()
                return snapshot
            self._require_monotonic_scope_time(row, timestamp)
            stored_policy = self._authenticated_scope_policy(row)
            if stored_policy.contract_sha256 != policy.contract_sha256:
                self._persist_contract_mismatch(connection, scope, timestamp)
                raise _ContractMismatchError
            self._validate_scope_policy_state(connection, scope, policy, row)
            self._expire_attempts(connection, scope, policy, timestamp)
            self._prune_window(connection, scope, policy, timestamp)
            snapshot = self._snapshot_locked(connection, scope)
            connection.commit()
            return snapshot
        except (_StorageError, _ContractMismatchError, _ClockRegressionError):
            if connection is not None:
                self._rollback_quietly(connection)
            return CanaryRolloutSnapshot.unavailable()
        except sqlite3.Error as exc:
            if connection is not None:
                try:
                    self._storage_failed(connection, exc)
                except _StorageError:
                    pass
            return CanaryRolloutSnapshot.unavailable()
        finally:
            if connection is not None:
                connection.close()


__all__ = [
    "CanaryAdmission",
    "CanaryAdmissionReason",
    "CanaryAttemptOutcome",
    "CanaryLatchReason",
    "CanaryMutationReason",
    "CanaryMutationResult",
    "CanaryProviderOutcome",
    "CanaryQualityOutcome",
    "CanaryRolloutLedger",
    "CanaryRolloutPolicy",
    "CanaryRolloutRole",
    "CanaryRolloutScope",
    "CanaryRolloutSnapshot",
    "CanaryRolloutState",
    "CanaryUsageOutcome",
    "canary_deployment_sha256",
]
