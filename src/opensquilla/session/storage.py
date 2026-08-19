"""Async database operations for sessions using aiosqlite + SQLModel."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
import sqlite3
import time
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import TYPE_CHECKING, Any, Concatenate, cast

from opensquilla.compat import aiosqlite
from opensquilla.session.cost_rollup import rollup_cost_source
from opensquilla.session.goals import (
    GOAL_EFFECTIVE_CONTEXT_DETAIL_KEY,
    GOAL_OBJECTIVE_UPDATE_DETAIL_KEY,
    GOAL_UNFINISHED_STATUSES,
    ClaimCurrentGoalMutation,
    ClaimGoalMutation,
    ExpectedGoal,
    GoalClaimCandidate,
    GoalCommandRequest,
    GoalCommandResult,
    GoalConflictError,
    GoalGuardrailPause,
    GoalObjectiveUpdate,
    GoalStatus,
    GoalTaskAcceptance,
    GoalTurnContext,
    GoalValidationError,
    StartGoalMutation,
    automatic_goal_task_id,
    effective_goal_turn_context,
    goal_snapshot,
    goal_turn_context,
    normalize_goal_objective,
    normalize_goal_progress,
    normalize_goal_reason,
)
from opensquilla.session.keys import canonicalize_session_key, normalize_agent_id, parse_agent_id
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    CollaborationMode,
    GoalCommandReceiptRecord,
    GoalRecord,
    MemoryDurableReceipt,
    MetaControlIntent,
    MetaLaunchDraft,
    PlanRevisionRecord,
    PlanRunRecord,
    PlanRunStatus,
    ProjectWorkspace,
    SessionContextState,
    SessionNode,
    SessionStatus,
    SessionSummary,
    TranscriptEntry,
    TurnIngressReceipt,
)
from opensquilla.session.plans import (
    PLAN_RUN_ACTIVE_STATUSES,
    PlanConflictError,
    PlanRunConflictError,
    PlanValidationError,
    checkpoint_plan_step_states,
    prepare_plan_revision,
    prepare_plan_run,
)
from opensquilla.session.usage_ledger import (
    UsageBackfillBatch,
    UsageBackfillCursor,
    UsageBackfillEntry,
    UsageBackfillStatus,
    UsageBackfillWrite,
    UsageBillingReceiptState,
    UsageBillingReceiptStatus,
    UsageEventCompletion,
    UsageEventItem,
    UsageEventRecord,
    UsageEventStart,
    UsageEventStatus,
    UsageItemBillingReceipt,
    UsageLedgerConflictError,
    UsageLedgerState,
    UsageLegacyBaseline,
    nanos_to_usd,
    usd_to_nanos,
    validate_usage_billing_receipt,
    validate_usage_completion,
    validate_usage_event_start,
    validate_usage_item,
)
from opensquilla.turn_outcome_projection import (
    attach_fork_terminal_outcome_projection,
    turn_id_from_context,
)
from opensquilla.usage_reasons import normalize_usage_unknown_reason

if TYPE_CHECKING:
    from opensquilla.persistence.meta_run_writer import MetaRunWriter
    from opensquilla.project_workspaces import ProjectWorkspaceGuard

log = logging.getLogger(__name__)


class StaleEpochError(Exception):
    """Raised when a write is rejected because the session epoch has advanced."""


@dataclass(frozen=True, slots=True)
class CanonicalTranscriptCoverage:
    """Canonical archive coverage and its session metadata snapshot."""

    canonical_complete: bool
    compaction_count: int
    inherited_compactions: bool


class StorageBusyError(RuntimeError):
    """Raised when a session-storage operation outlives its bounded busy budget."""

    def __init__(
        self,
        operation: str,
        *,
        waited_ms: int,
        retry_after_ms: int,
        stage: str | None = None,
        resource: str | None = None,
    ) -> None:
        super().__init__("Session storage is temporarily busy")
        self.operation = operation
        self.waited_ms = waited_ms
        self.retry_after_ms = retry_after_ms
        self.stage = stage
        self.resource = resource


class StorageConnectionPoisonedError(RuntimeError):
    """Raised after transaction cleanup failed and the connection was retired."""


class TurnIngressConflictError(ValueError):
    """Raised when a client request id is reused for a different turn payload."""


class SessionRoutingConflictError(ValueError):
    """Raised when a session routing compare-and-set fence is stale."""


class PendingChatInputConflictError(ValueError):
    """Raised when a staged-input identity or compare-and-set fence conflicts."""


class PendingChatInputCapacityError(RuntimeError):
    """Raised when a session already owns the maximum staged inputs."""


class PendingChatInputNotFoundError(KeyError):
    """Raised when a staged input disappeared before it could be dispatched."""


class PendingChatInputCancelledError(RuntimeError):
    """Raised when a durable cancellation tombstone rejects a delayed enqueue."""


class PendingChatInputAlreadyDispatchedError(RuntimeError):
    """Raised when a delayed enqueue targets an already accepted staged input."""


class MetaControlIntentConflictError(ValueError):
    """Raised when a durable MetaSkill control identity is reused incompatibly."""


class MetaLaunchDraftConflictError(ValueError):
    """Raised when a durable MetaSkill draft identity is reused incompatibly."""


class MetaLaunchDraftCapacityError(RuntimeError):
    """Raised when the bounded durable MetaSkill draft outbox is full."""


class MetaLaunchDraftUnavailableError(RuntimeError):
    """Raised when a draft expired before control promotion."""


class MetaLaunchDraftDiscardedError(RuntimeError):
    """Raised when a cancelled draft identity is reused before its tombstone expires."""


class TaskCollectionUnavailableError(RuntimeError):
    """Raised when a queued task stopped being collectable before acceptance."""


class PlanImplementationSessionBusyError(RuntimeError):
    """A current-session Plan implementation requires an idle task ledger."""

    def __init__(self, *, task_id: str, task_status: str) -> None:
        super().__init__("current-session plan implementation requires an idle session")
        self.task_id = task_id
        self.task_status = task_status


class ProjectSessionSnapshotMismatchError(RuntimeError):
    """Raised when a locked project-session snapshot changed before deletion."""


@dataclass(frozen=True)
class ResetArchiveSnapshot:
    """Pre-reset session state captured under the acceptance write transaction."""

    node: SessionNode
    entries: tuple[TranscriptEntry, ...]
    summaries: tuple[SessionSummary, ...]


@dataclass(frozen=True)
class TurnAcceptanceResult:
    """Outcome of the durable turn-acceptance transaction."""

    receipt: TurnIngressReceipt
    replayed: bool
    fresh_user_session: bool
    task_status: AgentTaskStatus | None = None
    reset_archive_snapshot: ResetArchiveSnapshot | None = None
    collaboration_mode: str | None = None
    collaboration_revision: int | None = None
    active_plan_revision_id: str | None = None
    goal: GoalRecord | None = None
    goal_context: GoalTurnContext | None = None
    goal_candidate: GoalClaimCandidate | None = None
    goal_command_response: dict[str, Any] | None = None


@dataclass(frozen=True)
class StrandedSteerInput:
    """One durable same-turn input whose target task is already terminal."""

    entry: TranscriptEntry
    receipt: TurnIngressReceipt
    target_task: AgentTaskRecord


@dataclass(frozen=True, slots=True)
class PendingChatInput:
    """One server-staged follow-up awaiting exactly-once dispatch."""

    pending_input_id: str
    session_key: str
    source_scope: str
    client_request_id: str
    client_message_id: str
    request_fingerprint: str
    payload: dict[str, Any]
    position: int
    state_revision: int
    created_at: int
    updated_at: int
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class PendingChatInputDispatchReceipt:
    """Durable binding between one staged identity and its accepted turn receipt."""

    pending_input_id: str
    session_key: str
    source_scope: str
    client_request_id: str
    client_message_id: str
    request_fingerprint: str
    accepted_at: int
    schema_version: int = 1


async def _verify_project_workspace_guard(
    conn: aiosqlite.Connection,
    *,
    session_node: SessionNode | None,
    entry_session_key: str,
    workspace_guard: ProjectWorkspaceGuard | None,
) -> None:
    from opensquilla.project_workspaces import ProjectWorkspaceStateError

    async with conn.execute(
        "SELECT workspace_id FROM sessions WHERE session_key = ?",
        (entry_session_key,),
    ) as cursor:
        session_row = await cursor.fetchone()
    persisted_bound_id = (
        session_row["workspace_id"] if session_row is not None else None
    )
    prepared_bound_id = (
        session_node.workspace_id
        if session_node is not None
        else persisted_bound_id
    )
    if (
        session_row is not None
        and session_node is not None
        and persisted_bound_id != prepared_bound_id
    ):
        raise ProjectWorkspaceStateError("binding_changed")
    bound_id = prepared_bound_id
    if bound_id is None:
        if workspace_guard is not None:
            raise ProjectWorkspaceStateError("binding_changed")
        return
    if workspace_guard is None:
        raise ProjectWorkspaceStateError("guard_required")
    if workspace_guard.workspace_id != bound_id:
        raise ProjectWorkspaceStateError("binding_changed")
    async with conn.execute(
        """
        SELECT workspace_id, path, path_key, removed_at, trusted_at
        FROM project_workspaces
        WHERE workspace_id = ?
        """,
        (bound_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise ProjectWorkspaceStateError("not_found")
    if row["removed_at"] is not None:
        raise ProjectWorkspaceStateError("removed")
    if row["trusted_at"] is None:
        raise ProjectWorkspaceStateError("untrusted")
    if row["path"] != workspace_guard.path or row["path_key"] != workspace_guard.path_key:
        raise ProjectWorkspaceStateError("binding_changed")


async def _next_project_workspace_order_value(
    conn: aiosqlite.Connection,
    *,
    column: str,
    now_ms: int,
) -> int:
    """Return a transaction-local order value that cannot tie an older action."""

    if column not in {"position_at", "pinned_at"}:
        raise ValueError(f"Unsupported project workspace order column: {column}")
    async with conn.execute(
        f"SELECT MAX({column}) FROM project_workspaces"  # noqa: S608 - allowlisted column
    ) as cursor:
        row = await cursor.fetchone()
    previous = row[0] if row is not None else None
    return max(now_ms, int(previous) + 1) if previous is not None else now_ms


@dataclass(frozen=True)
class RecoverableMetaControlTask:
    """A never-started accepted control task claimed for restart recovery."""

    task: AgentTaskRecord
    entry: TranscriptEntry


_SQLITE_BUSY_TIMEOUT_MS = 100
_INTERACTIVE_BUSY_BUDGET_SECONDS = 2.0
_BUSY_RETRY_INITIAL_SECONDS = 0.025
_BUSY_RETRY_MAX_SECONDS = 0.250
_META_CONTROL_STAGED_RETENTION_MS = 30 * 24 * 60 * 60 * 1000
_META_CONTROL_STAGED_GC_BATCH = 128
_META_CONTROL_RECOVERY_INVALID_REASON = "meta_control_recovery_invalid"
_META_LAUNCH_DRAFT_RETENTION_MS = 7 * 24 * 60 * 60 * 1000
_META_LAUNCH_DRAFT_PER_SESSION_LIMIT = 20
_META_LAUNCH_DRAFT_GLOBAL_LIMIT = 512
_META_LAUNCH_DRAFT_GC_BATCH = 512
_META_LAUNCH_DRAFT_GC_INTERVAL_SECONDS = 60.0
_META_LAUNCH_DISCARD_PER_SESSION_LIMIT = 64
_META_LAUNCH_DISCARD_GLOBAL_LIMIT = 2048
_META_LAUNCH_ACCEPTED_PER_SESSION_LIMIT = 20
_META_LAUNCH_SESSION_KEY_MAX_LENGTH = 512
_META_LAUNCH_REQUEST_ID_MAX_LENGTH = 256


def normalize_meta_launch_coordinates(
    session_key: object,
    client_request_id: object,
) -> tuple[str, str]:
    """Validate bounded, content-free coordinates for draft and tombstone rows."""

    if not isinstance(session_key, str) or not isinstance(client_request_id, str):
        raise ValueError("meta launch draft coordinates must be strings")
    if len(session_key.strip()) > _META_LAUNCH_SESSION_KEY_MAX_LENGTH:
        raise ValueError("meta launch draft session is invalid")
    normalized_session = canonicalize_session_key(session_key)
    normalized_request_id = client_request_id.strip()
    if not normalized_session or len(normalized_session) > _META_LAUNCH_SESSION_KEY_MAX_LENGTH:
        raise ValueError("meta launch draft session is invalid")
    if (
        not normalized_request_id
        or len(normalized_request_id) > _META_LAUNCH_REQUEST_ID_MAX_LENGTH
        or any(character.isspace() for character in normalized_request_id)
    ):
        raise ValueError("meta launch draft request identity is invalid")
    return normalized_session, normalized_request_id


def _clear_pending_meta_launch_boundary(
    session_key: str,
    *,
    preserve_client_request_id: str | None = None,
    preserve_message: object = None,
) -> int:
    """Clear the process compatibility cache after a committed session boundary."""

    from opensquilla.engine.steps.meta_command import pending_meta_launch_clear_session

    return pending_meta_launch_clear_session(
        session_key,
        preserve_client_request_id=preserve_client_request_id,
        preserve_message=preserve_message,
    )
_BOUNDED_INTERACTIVE_READS: ContextVar[bool] = ContextVar(
    "opensquilla_bounded_interactive_storage_reads",
    default=False,
)


def _is_sqlite_busy(exc: BaseException) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


@contextlib.contextmanager
def bounded_interactive_storage_reads() -> Iterator[None]:
    """Bound shared-connection read-gate waits for an interactive RPC scope."""

    token = _BOUNDED_INTERACTIVE_READS.set(True)
    try:
        yield
    finally:
        _BOUNDED_INTERACTIVE_READS.reset(token)


def _serialized_read[**P, R](
    method: Callable[Concatenate[SessionStorage, P], Awaitable[R]],
) -> Callable[Concatenate[SessionStorage, P], Awaitable[R]]:
    """Serialize a public read against multi-statement writes on the shared connection."""

    @wraps(method)
    async def _wrapped(self: SessionStorage, *args: P.args, **kwargs: P.kwargs) -> R:
        if not _BOUNDED_INTERACTIVE_READS.get():
            async with self._operation_lock:
                self._raise_if_poisoned()
                return await method(self, *args, **kwargs)

        started = self._monotonic()
        acquired = False
        try:
            try:
                async with asyncio.timeout(self._busy_budget_seconds):
                    await self._operation_lock.acquire()
            except TimeoutError as exc:
                raise StorageBusyError(
                    method.__name__,
                    waited_ms=max(0, int((self._monotonic() - started) * 1000)),
                    retry_after_ms=_SQLITE_BUSY_TIMEOUT_MS,
                    stage="lock_acquire",
                    resource="session_storage_operation_lock",
                ) from exc
            acquired = True
            self._raise_if_poisoned()
            return await method(self, *args, **kwargs)
        finally:
            if acquired:
                self._operation_lock.release()

    return _wrapped


# Bumped whenever the schema is widened or narrowed via migration.
# Version 2 added the epoch column. Version 3 added transcript reasoning replay.
# Version 4 added transcript turn usage metadata.
# Version 5 added structured compaction summary metadata.
# Version 6 added portable/provider context state records.
# Version 7 added archived transcript rows for canonical recovery after compaction.
# Version 8 added the derived_title column for LLM-generated session titles.
# Version 9 added durable turn-ingress receipts.
# Version 10 added the durable provider usage ledger and content-free daily usage
# telemetry aggregates. Version 11 added per-item provider-native billing receipts.
# Version 12 added persistent project workspaces and optional session bindings.
# Version 13 added backend-owned runtime preferences. Version 14 added durable
# collaboration-mode state. Version 15 added immutable plan revisions. Version
# 16 added mutable, compare-and-set plan runs. Version 17 added durable hidden
# MetaSkill control intents. Version 18 added the bounded MetaSkill launch outbox
# and discard tombstones. Version 19 added the generation-fenced current Goal
# and Goal command idempotency ledger. Version 20 added the durable Goal origin
# message anchor used by reconnect-safe transcript presentation. Version 21
# added the bounded durable pending-chat-input outbox. Version 22 added the
# persistent per-session model-routing mode and its compare-and-set revision.
SCHEMA_VERSION = 22
MAX_PENDING_CHAT_INPUTS = 5

# These fields have their own atomic resolver/CAS API.  Whole-session writes
# may seed them when inserting a new row, but must never replace the value of
# an existing row from a stale ``SessionNode`` snapshot.
_SESSION_DEDICATED_WRITER_COLUMNS = frozenset(
    {"model_routing_mode", "model_routing_revision"}
)

# Session rows at or above this semantic version were created by fork logic
# that records enough existing metadata for canonical coverage to be checked
# without guessing about legacy prefix forks. This reuses the persisted row
# version and does not widen or rewrite the database schema.
CANONICAL_FORK_PROOF_SCHEMA_VERSION = 2

# SQLite CREATE statements derived from SQLModel metadata
_CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    session_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    started_at INTEGER,
    ended_at INTEGER,
    runtime_ms INTEGER,
    last_channel TEXT,
    last_to TEXT,
    last_account_id TEXT,
    last_thread_id TEXT,
    delivery_context TEXT,
    model TEXT,
    model_provider TEXT,
    provider_override TEXT,
    model_override TEXT,
    auth_profile_override TEXT,
    auth_profile_override_source TEXT,
    context_tokens INTEGER,
    model_routing_mode TEXT,
    model_routing_revision INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens_fresh INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
    total_cost_usd REAL NOT NULL DEFAULT 0.0,
    billed_cost_usd REAL NOT NULL DEFAULT 0.0,
    estimated_cost_component_usd REAL NOT NULL DEFAULT 0.0,
    cost_source TEXT NOT NULL DEFAULT 'none',
    missing_cost_entries INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0,
    cache_write INTEGER NOT NULL DEFAULT 0,
    compaction_count INTEGER NOT NULL DEFAULT 0,
    session_file TEXT,
    spawned_by TEXT,
    parent_session_key TEXT,
    forked_from_parent INTEGER NOT NULL DEFAULT 0,
    spawn_depth INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    chat_type TEXT NOT NULL DEFAULT 'unknown',
    thinking_level TEXT,
    fast_mode INTEGER NOT NULL DEFAULT 0,
    verbose_level TEXT,
    reasoning_level TEXT,
    send_policy TEXT NOT NULL DEFAULT 'allow',
    queue_mode TEXT NOT NULL DEFAULT 'steer',
    collaboration_mode TEXT NOT NULL DEFAULT 'default',
    collaboration_revision INTEGER NOT NULL DEFAULT 0,
    active_plan_revision_id TEXT,
    label TEXT,
    display_name TEXT,
    derived_title TEXT,
    channel TEXT,
    group_id TEXT,
    subject TEXT,
    origin TEXT,
    workspace_id TEXT,
    agent_id TEXT NOT NULL DEFAULT 'main',
    schema_version INTEGER NOT NULL DEFAULT 1,
    epoch INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_PROJECT_WORKSPACES = """
CREATE TABLE IF NOT EXISTS project_workspaces (
    workspace_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    path_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    position_at INTEGER NOT NULL,
    pinned_at INTEGER,
    removed_at INTEGER,
    trusted_at INTEGER
)
"""

_CREATE_IDX_PROJECT_WORKSPACES_ORDER = """
CREATE INDEX IF NOT EXISTS idx_project_workspaces_order
ON project_workspaces(removed_at, pinned_at DESC, position_at DESC)
"""

_CREATE_IDX_SESSIONS_WORKSPACE = """
CREATE INDEX IF NOT EXISTS idx_sessions_workspace_id ON sessions(workspace_id)
"""

# Recency ordering for list_sessions and the title search (ORDER BY updated_at
# DESC LIMIT). Without it both do a full table sort on every call.
_CREATE_IDX_SESSIONS_UPDATED = (
    "CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON sessions(updated_at)"
)

_CREATE_RUNTIME_PREFERENCES = """
CREATE TABLE IF NOT EXISTS runtime_preferences (
    preference_key TEXT PRIMARY KEY,
    preference_value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
)
"""

_CREATE_PLAN_REVISIONS = """
CREATE TABLE IF NOT EXISTS plan_revisions (
    revision_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    parent_revision_id TEXT,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    source_session_key TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    source_epoch INTEGER NOT NULL DEFAULT 0 CHECK (source_epoch >= 0),
    source_turn_id TEXT,
    source_message_id TEXT,
    title TEXT NOT NULL,
    markdown TEXT NOT NULL,
    steps TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1)
)
"""

_CREATE_IDX_PLAN_REVISIONS_PLAN_GENERATION = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_revisions_plan_generation
ON plan_revisions(plan_id, generation)
"""

_CREATE_IDX_PLAN_REVISIONS_SOURCE_SESSION = """
CREATE INDEX IF NOT EXISTS idx_plan_revisions_source_session
ON plan_revisions(source_session_key, created_at)
"""

_CREATE_IDX_PLAN_REVISIONS_SOURCE_MESSAGE = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_revisions_source_message
ON plan_revisions(source_session_id, source_message_id)
WHERE source_message_id IS NOT NULL
"""

_CREATE_PLAN_REVISIONS_IMMUTABLE_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS plan_revisions_immutable
BEFORE UPDATE ON plan_revisions
BEGIN
    SELECT RAISE(ABORT, 'plan revisions are immutable');
END
"""

_CREATE_PLAN_RUNS = """
CREATE TABLE IF NOT EXISTS plan_runs (
    run_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    session_epoch INTEGER NOT NULL DEFAULT 0 CHECK (session_epoch >= 0),
    plan_revision_id TEXT NOT NULL,
    supersedes_run_id TEXT,
    driver_kind TEXT NOT NULL DEFAULT 'manual'
        CHECK (driver_kind IN ('manual', 'goal')),
    driver_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (
            status IN (
                'queued', 'running', 'paused', 'blocked',
                'completed', 'cancelled', 'superseded'
            )
        ),
    step_states TEXT NOT NULL,
    current_step_id TEXT,
    state_revision INTEGER NOT NULL DEFAULT 0 CHECK (state_revision >= 0),
    active_task_id TEXT,
    pause_reason TEXT,
    terminal_reason TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1)
)
"""

_CREATE_IDX_PLAN_RUNS_ACTIVE_SESSION = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_runs_active_session
ON plan_runs(session_key)
WHERE status IN ('queued', 'running', 'paused', 'blocked')
"""

_CREATE_IDX_PLAN_RUNS_SESSION_HISTORY = """
CREATE INDEX IF NOT EXISTS idx_plan_runs_session_history
ON plan_runs(session_key, created_at)
"""

_CREATE_IDX_PLAN_RUNS_REVISION = """
CREATE INDEX IF NOT EXISTS idx_plan_runs_revision
ON plan_runs(plan_revision_id, created_at)
"""

_CREATE_IDX_PLAN_RUNS_DRIVER = """
CREATE INDEX IF NOT EXISTS idx_plan_runs_driver
ON plan_runs(driver_id)
WHERE driver_id IS NOT NULL
"""

_CREATE_SESSION_GOALS = """
CREATE TABLE IF NOT EXISTS session_goals (
    session_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    session_epoch INTEGER NOT NULL DEFAULT 0 CHECK (session_epoch >= 0),
    goal_id TEXT NOT NULL UNIQUE,
    objective TEXT NOT NULL CHECK (length(objective) BETWEEN 1 AND 4000),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'blocked', 'usage_limited', 'complete')),
    state_revision INTEGER NOT NULL DEFAULT 1 CHECK (state_revision >= 1),
    objective_revision INTEGER NOT NULL DEFAULT 1 CHECK (objective_revision >= 1),
    progress_revision INTEGER NOT NULL DEFAULT 0 CHECK (progress_revision >= 0),
    progress_json TEXT,
    continuation_seq INTEGER NOT NULL DEFAULT 0 CHECK (continuation_seq >= 0),
    active_task_id TEXT,
    source_user_message_id TEXT,
    terminal_task_id TEXT,
    turns_started INTEGER NOT NULL DEFAULT 0 CHECK (turns_started >= 0),
    turns_settled INTEGER NOT NULL DEFAULT 0 CHECK (turns_settled >= 0),
    window_turns_started INTEGER NOT NULL DEFAULT 0 CHECK (window_turns_started >= 0),
    active_time_ms INTEGER NOT NULL DEFAULT 0 CHECK (active_time_ms >= 0),
    window_active_time_ms INTEGER NOT NULL DEFAULT 0 CHECK (window_active_time_ms >= 0),
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    reasoning_tokens INTEGER NOT NULL DEFAULT 0 CHECK (reasoning_tokens >= 0),
    cache_read_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    pause_reason TEXT,
    blocked_reason TEXT,
    terminal_reason TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1),
    FOREIGN KEY (session_key) REFERENCES sessions(session_key) ON DELETE CASCADE
)
"""

_CREATE_IDX_SESSION_GOALS_ACTIVE_TASK = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_session_goals_active_task
ON session_goals(active_task_id)
WHERE active_task_id IS NOT NULL
"""

_CREATE_IDX_SESSION_GOALS_STATUS = """
CREATE INDEX IF NOT EXISTS idx_session_goals_status
ON session_goals(status, updated_at_ms)
"""

_CREATE_GOAL_COMMAND_RECEIPTS = """
CREATE TABLE IF NOT EXISTS goal_command_receipts (
    receipt_id TEXT PRIMARY KEY,
    source_scope TEXT NOT NULL,
    request_session_key TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    action TEXT NOT NULL
        CHECK (action IN ('set', 'edit', 'pause', 'resume', 'clear')),
    request_fingerprint TEXT NOT NULL,
    accepted_session_id TEXT NOT NULL,
    accepted_session_epoch INTEGER NOT NULL DEFAULT 0
        CHECK (accepted_session_epoch >= 0),
    response_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    FOREIGN KEY (request_session_key) REFERENCES sessions(session_key) ON DELETE CASCADE
)
"""

_CREATE_IDX_GOAL_COMMAND_RECEIPTS_REQUEST = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_goal_command_receipts_request
ON goal_command_receipts(source_scope, request_session_key, client_request_id)
"""

_CREATE_IDX_GOAL_COMMAND_RECEIPTS_SESSION = """
CREATE INDEX IF NOT EXISTS idx_goal_command_receipts_session
ON goal_command_receipts(request_session_key, created_at_ms)
"""

_CREATE_TRANSCRIPT = """
CREATE TABLE IF NOT EXISTS transcript_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    message_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    reasoning_content TEXT,
    turn_usage TEXT,
    turn_context TEXT,
    created_at INTEGER NOT NULL,
    token_count INTEGER,
    provenance_kind TEXT,
    provenance_origin_session_id TEXT,
    provenance_source_session_key TEXT,
    provenance_source_channel TEXT,
    provenance_source_tool TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1
)
"""

_CREATE_IDX_TRANSCRIPT_SESSION = (
    "CREATE INDEX IF NOT EXISTS idx_transcript_session_id ON transcript_entries(session_id)"
)
_CREATE_IDX_TRANSCRIPT_KEY = (
    "CREATE INDEX IF NOT EXISTS idx_transcript_session_key ON transcript_entries(session_key)"
)
_CREATE_IDX_TRANSCRIPT_CURSOR = """
CREATE INDEX IF NOT EXISTS idx_transcript_session_cursor
ON transcript_entries(session_id, created_at, id)
"""

_CREATE_COMPACTED_TRANSCRIPT = """
CREATE TABLE IF NOT EXISTS compacted_transcript_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    compaction_id TEXT,
    compaction_index INTEGER,
    original_entry_id INTEGER,
    message_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    reasoning_content TEXT,
    turn_usage TEXT,
    turn_context TEXT,
    created_at INTEGER NOT NULL,
    token_count INTEGER,
    provenance_kind TEXT,
    provenance_origin_session_id TEXT,
    provenance_source_session_key TEXT,
    provenance_source_channel TEXT,
    provenance_source_tool TEXT,
    archived_at INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1
)
"""

_CREATE_IDX_COMPACTED_TRANSCRIPT_SESSION = """
CREATE INDEX IF NOT EXISTS idx_compacted_transcript_session_id
ON compacted_transcript_entries(session_id)
"""

_CREATE_IDX_COMPACTED_TRANSCRIPT_KEY = """
CREATE INDEX IF NOT EXISTS idx_compacted_transcript_session_key
ON compacted_transcript_entries(session_key)
"""
_CREATE_IDX_COMPACTED_TRANSCRIPT_CURSOR = """
CREATE INDEX IF NOT EXISTS idx_compacted_transcript_session_cursor
ON compacted_transcript_entries(session_id, created_at, original_entry_id, id)
"""

_CREATE_IDX_COMPACTED_TRANSCRIPT_COMPACTION = """
CREATE INDEX IF NOT EXISTS idx_compacted_transcript_session_compaction
ON compacted_transcript_entries(session_id, compaction_id)
"""

# FTS5 full-text search on transcript content
_CREATE_TRANSCRIPT_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts
USING fts5(content, content=transcript_entries, content_rowid=id)
"""

_CREATE_FTS_TRIGGER_INSERT = """
CREATE TRIGGER IF NOT EXISTS transcript_fts_ai AFTER INSERT ON transcript_entries BEGIN
    INSERT INTO transcript_fts(rowid, content) VALUES (new.id, new.content);
END
"""

_CREATE_FTS_TRIGGER_DELETE = """
CREATE TRIGGER IF NOT EXISTS transcript_fts_ad AFTER DELETE ON transcript_entries BEGIN
    INSERT INTO transcript_fts(transcript_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END
"""

_CREATE_FTS_TRIGGER_UPDATE = """
CREATE TRIGGER IF NOT EXISTS transcript_fts_au AFTER UPDATE ON transcript_entries BEGIN
    INSERT INTO transcript_fts(transcript_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
    INSERT INTO transcript_fts(rowid, content) VALUES (new.id, new.content);
END
"""

_CREATE_SUMMARIES = """
CREATE TABLE IF NOT EXISTS session_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    compaction_index INTEGER NOT NULL DEFAULT 0,
    compaction_id TEXT,
    trigger_reason TEXT,
    summary_text TEXT NOT NULL,
    summary_payload TEXT,
    summary_format TEXT NOT NULL DEFAULT 'text',
    summary_source TEXT NOT NULL DEFAULT 'unknown',
    coverage_status TEXT NOT NULL DEFAULT 'unknown',
    missing_obligations TEXT,
    critical_carry_forward TEXT,
    tokens_before INTEGER,
    tokens_after INTEGER,
    removed_count INTEGER NOT NULL DEFAULT 0,
    kept_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    flush_receipt_status TEXT NOT NULL DEFAULT 'unknown',
    covered_through_id INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1
)
"""

_CREATE_IDX_SUMMARIES = (
    "CREATE INDEX IF NOT EXISTS idx_summaries_session_id ON session_summaries(session_id)"
)

_CREATE_CONTEXT_STATES = """
CREATE TABLE IF NOT EXISTS session_context_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'portable',
    model TEXT,
    state_kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    covered_through_id INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    expires_at INTEGER,
    portable INTEGER NOT NULL DEFAULT 0,
    cacheable INTEGER NOT NULL DEFAULT 0,
    valid INTEGER NOT NULL DEFAULT 1,
    invalid_reason TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1
)
"""

_CREATE_IDX_CONTEXT_STATES_SESSION = """
CREATE INDEX IF NOT EXISTS idx_context_states_session_id
ON session_context_states(session_id)
"""

_CREATE_IDX_CONTEXT_STATES_KEY_VALID = """
CREATE INDEX IF NOT EXISTS idx_context_states_key_valid
ON session_context_states(session_key, valid, state_kind, provider)
"""

_CREATE_AGENT_TASKS = """
CREATE TABLE IF NOT EXISTS agent_tasks (
    task_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    agent_id TEXT NOT NULL DEFAULT 'main',
    source_kind TEXT NOT NULL,
    queue_mode TEXT NOT NULL,
    run_kind TEXT NOT NULL DEFAULT 'default',
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    terminal_reason TEXT,
    error_class TEXT,
    error_message TEXT,
    details TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1
)
"""

_CREATE_IDX_AGENT_TASKS_SESSION_STATUS = """
CREATE INDEX IF NOT EXISTS idx_agent_tasks_session_status
ON agent_tasks(session_key, status)
"""

_CREATE_IDX_AGENT_TASKS_STATUS_UPDATED = """
CREATE INDEX IF NOT EXISTS idx_agent_tasks_status_updated
ON agent_tasks(status, updated_at)
"""

_CREATE_TURN_INGRESS_RECEIPTS = """
CREATE TABLE IF NOT EXISTS turn_ingress_receipts (
    receipt_id TEXT PRIMARY KEY,
    source_scope TEXT NOT NULL,
    request_session_key TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    accepted_session_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    task_id TEXT,
    accepted_at INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1
)
"""

_CREATE_IDX_TURN_INGRESS_REQUEST = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_turn_ingress_receipts_request
ON turn_ingress_receipts(source_scope, request_session_key, client_request_id)
"""

_CREATE_IDX_TURN_INGRESS_ACCEPTED_SESSION = """
CREATE INDEX IF NOT EXISTS idx_turn_ingress_receipts_accepted_session
ON turn_ingress_receipts(accepted_session_key, accepted_at)
"""

_CREATE_PENDING_CHAT_INPUTS = """
CREATE TABLE IF NOT EXISTS pending_chat_inputs (
    pending_input_id       TEXT PRIMARY KEY,
    session_key            TEXT NOT NULL,
    source_scope           TEXT NOT NULL,
    client_request_id      TEXT NOT NULL,
    client_message_id      TEXT NOT NULL,
    request_fingerprint    TEXT NOT NULL,
    payload_json           TEXT NOT NULL,
    position               INTEGER NOT NULL DEFAULT 0,
    state_revision         INTEGER NOT NULL DEFAULT 1 CHECK (state_revision >= 1),
    created_at             INTEGER NOT NULL,
    updated_at             INTEGER NOT NULL,
    schema_version         INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1)
)
"""

_CREATE_IDX_PENDING_CHAT_INPUT_REQUEST = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_chat_inputs_request
ON pending_chat_inputs(session_key, client_request_id)
"""

_CREATE_IDX_PENDING_CHAT_INPUT_MESSAGE = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_chat_inputs_message
ON pending_chat_inputs(session_key, client_message_id)
"""

_CREATE_IDX_PENDING_CHAT_INPUT_SESSION_ORDER = """
CREATE INDEX IF NOT EXISTS idx_pending_chat_inputs_session_order
ON pending_chat_inputs(session_key, position, created_at, pending_input_id)
"""

_CREATE_PENDING_CHAT_INPUT_CANCELLATIONS = """
CREATE TABLE IF NOT EXISTS pending_chat_input_cancellations (
    pending_input_id       TEXT PRIMARY KEY,
    session_key            TEXT NOT NULL,
    cancelled_at           INTEGER NOT NULL,
    schema_version         INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1)
)
"""

_CREATE_IDX_PENDING_CHAT_INPUT_CANCELLATIONS_SESSION = """
CREATE INDEX IF NOT EXISTS idx_pending_chat_input_cancellations_session
ON pending_chat_input_cancellations(session_key, cancelled_at, pending_input_id)
"""

_CREATE_PENDING_CHAT_INPUT_DISPATCH_RECEIPTS = """
CREATE TABLE IF NOT EXISTS pending_chat_input_dispatch_receipts (
    pending_input_id       TEXT PRIMARY KEY,
    session_key            TEXT NOT NULL,
    source_scope           TEXT NOT NULL,
    client_request_id      TEXT NOT NULL,
    client_message_id      TEXT NOT NULL,
    request_fingerprint    TEXT NOT NULL,
    accepted_at            INTEGER NOT NULL,
    schema_version         INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1)
)
"""

_CREATE_IDX_PENDING_CHAT_INPUT_DISPATCH_REQUEST = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_chat_input_dispatch_request
ON pending_chat_input_dispatch_receipts(source_scope, session_key, client_request_id)
"""

_CREATE_IDX_PENDING_CHAT_INPUT_DISPATCH_MESSAGE = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_chat_input_dispatch_message
ON pending_chat_input_dispatch_receipts(session_key, client_message_id)
"""

_CREATE_IDX_PENDING_CHAT_INPUT_DISPATCH_SESSION = """
CREATE INDEX IF NOT EXISTS idx_pending_chat_input_dispatch_session
ON pending_chat_input_dispatch_receipts(session_key, accepted_at, pending_input_id)
"""

_CREATE_META_CONTROL_INTENTS = """
CREATE TABLE IF NOT EXISTS meta_control_intents (
    intent_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    control_kind TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    meta_skill_name TEXT NOT NULL,
    replay_run_id TEXT,
    replay_mode TEXT,
    status TEXT NOT NULL DEFAULT 'staged',
    accepted_source_scope TEXT,
    accepted_request_session_key TEXT,
    accepted_client_request_id TEXT,
    accepted_request_fingerprint TEXT,
    accepted_message_id TEXT,
    accepted_task_id TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    CHECK (control_kind IN ('manual', 'replay')),
    CHECK (status IN ('staged', 'accepted'))
)
"""

_CREATE_IDX_META_CONTROL_CORRELATION = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_meta_control_intents_correlation
ON meta_control_intents(session_key, control_kind, correlation_id)
"""

_CREATE_IDX_META_CONTROL_SESSION_STATUS = """
CREATE INDEX IF NOT EXISTS idx_meta_control_intents_session_status
ON meta_control_intents(session_key, status, created_at)
"""

_CREATE_META_LAUNCH_DRAFTS = """
CREATE TABLE IF NOT EXISTS meta_launch_drafts (
    draft_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    meta_skill_name TEXT NOT NULL,
    launch_text TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1
)
"""

_CREATE_IDX_META_LAUNCH_DRAFT_REQUEST = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_meta_launch_drafts_request
ON meta_launch_drafts(session_key, client_request_id)
"""

_CREATE_IDX_META_LAUNCH_DRAFT_SESSION_EXPIRY = """
CREATE INDEX IF NOT EXISTS idx_meta_launch_drafts_session_expiry
ON meta_launch_drafts(session_key, expires_at, created_at)
"""

_CREATE_META_LAUNCH_DISCARD_TOMBSTONES = """
CREATE TABLE IF NOT EXISTS meta_launch_discard_tombstones (
    session_key TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (session_key, client_request_id)
)
"""

_CREATE_IDX_META_LAUNCH_DISCARD_TOMBSTONES_EXPIRY = """
CREATE INDEX IF NOT EXISTS idx_meta_launch_discard_tombstones_expiry
ON meta_launch_discard_tombstones(expires_at, created_at)
"""

_CREATE_MEMORY_DURABLE_RECEIPTS = """
CREATE TABLE IF NOT EXISTS memory_durable_receipts (
    receipt_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT,
    scope TEXT NOT NULL,
    source_path TEXT,
    target_path TEXT,
    content_hash TEXT,
    coverage_turn_id TEXT,
    coverage_hash TEXT,
    coverage_entry_count INTEGER,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    reason TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at_ms INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1
)
"""

_CREATE_IDX_MEMORY_DURABLE_RECEIPTS_SESSION = (
    "CREATE INDEX IF NOT EXISTS idx_memory_durable_receipts_session "
    "ON memory_durable_receipts(session_key, status, created_at)"
)

_CREATE_IDX_MEMORY_DURABLE_RECEIPTS_COVERAGE = (
    "CREATE INDEX IF NOT EXISTS idx_memory_durable_receipts_coverage "
    "ON memory_durable_receipts("
    "session_key, session_id, scope, status, coverage_turn_id, coverage_hash, "
    "coverage_entry_count"
    ")"
)

_CREATE_TELEMETRY_DAILY_USAGE = """
CREATE TABLE IF NOT EXISTS telemetry_daily_usage (
    day TEXT PRIMARY KEY,
    conversation_turns INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    uploaded_at INTEGER
)
"""

_CREATE_USAGE_EVENTS = """
CREATE TABLE IF NOT EXISTS usage_events (
    event_id                    TEXT PRIMARY KEY,
    execution_id                TEXT NOT NULL,
    call_index                  INTEGER NOT NULL CHECK (call_index >= 0),
    turn_id                     TEXT,
    agent_run_id                TEXT,
    parent_turn_id              TEXT,
    session_id                  TEXT NOT NULL,
    session_epoch               INTEGER NOT NULL DEFAULT 0 CHECK (session_epoch >= 0),
    agent_id                    TEXT NOT NULL DEFAULT 'main',
    run_kind                    TEXT NOT NULL DEFAULT 'default',
    provider                    TEXT,
    model                       TEXT,
    started_at_ms               INTEGER NOT NULL CHECK (started_at_ms >= 0),
    completed_at_ms             INTEGER,
    status                      TEXT NOT NULL DEFAULT 'started'
                                CHECK (status IN ('started', 'finalized', 'unknown')),
    input_tokens                INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens               INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    reasoning_tokens            INTEGER NOT NULL DEFAULT 0 CHECK (reasoning_tokens >= 0),
    cache_read_tokens           INTEGER NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens          INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    total_tokens                INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    cost_nanos                  INTEGER NOT NULL DEFAULT 0 CHECK (cost_nanos >= 0),
    billed_cost_nanos           INTEGER NOT NULL DEFAULT 0 CHECK (billed_cost_nanos >= 0),
    estimated_cost_nanos        INTEGER NOT NULL DEFAULT 0 CHECK (estimated_cost_nanos >= 0),
    cost_source                 TEXT NOT NULL DEFAULT 'none',
    estimate_basis              TEXT,
    price_source                TEXT,
    coverage_status             TEXT NOT NULL DEFAULT 'pending',
    missing_cost_entries        INTEGER NOT NULL DEFAULT 0
                                CHECK (missing_cost_entries >= 0),
    unknown_reason              TEXT,
    origin                      TEXT NOT NULL,
    schema_version              INTEGER NOT NULL DEFAULT 1,
    UNIQUE (execution_id, call_index),
    CHECK (completed_at_ms IS NULL OR completed_at_ms >= started_at_ms),
    CHECK (cost_nanos = billed_cost_nanos + estimated_cost_nanos)
)
"""

_CREATE_USAGE_EVENT_ITEMS = """
CREATE TABLE IF NOT EXISTS usage_event_items (
    event_id                    TEXT NOT NULL,
    ordinal                     INTEGER NOT NULL CHECK (ordinal >= 0),
    provider                    TEXT,
    model                       TEXT,
    input_tokens                INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens               INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    reasoning_tokens            INTEGER NOT NULL DEFAULT 0 CHECK (reasoning_tokens >= 0),
    cache_read_tokens           INTEGER NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens          INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    total_tokens                INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    cost_nanos                  INTEGER NOT NULL DEFAULT 0 CHECK (cost_nanos >= 0),
    billed_cost_nanos           INTEGER NOT NULL DEFAULT 0 CHECK (billed_cost_nanos >= 0),
    estimated_cost_nanos        INTEGER NOT NULL DEFAULT 0 CHECK (estimated_cost_nanos >= 0),
    cost_source                 TEXT NOT NULL DEFAULT 'none',
    estimate_basis              TEXT,
    price_source                TEXT,
    schema_version              INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (event_id, ordinal),
    FOREIGN KEY (event_id) REFERENCES usage_events(event_id) ON DELETE CASCADE,
    CHECK (cost_nanos = billed_cost_nanos + estimated_cost_nanos)
)
"""

_CREATE_USAGE_ITEM_BILLING_RECEIPTS = """
CREATE TABLE IF NOT EXISTS usage_item_billing_receipts (
    event_id                    TEXT NOT NULL,
    ordinal                     INTEGER NOT NULL CHECK (ordinal >= 0),
    currency                    TEXT NOT NULL
                                CHECK (length(currency) = 3 AND currency = upper(currency)),
    status                      TEXT NOT NULL
                                CHECK (status IN ('confirmed', 'pending')),
    amount_nanos                INTEGER CHECK (amount_nanos >= 0),
    usd_equivalent_nanos        INTEGER CHECK (usd_equivalent_nanos >= 0),
    fx_native_per_usd_nanos     INTEGER NOT NULL
                                CHECK (fx_native_per_usd_nanos > 0),
    schema_version              INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1),
    PRIMARY KEY (event_id, ordinal),
    FOREIGN KEY (event_id, ordinal)
        REFERENCES usage_event_items(event_id, ordinal) ON DELETE CASCADE,
    CHECK (
        (status = 'confirmed' AND amount_nanos IS NOT NULL
         AND usd_equivalent_nanos IS NOT NULL)
        OR
        (status = 'pending' AND usd_equivalent_nanos IS NULL)
    )
)
"""

_CREATE_USAGE_BILLING_RECEIPT_STATE = """
CREATE TABLE IF NOT EXISTS usage_billing_receipt_state (
    singleton_id                INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    tracking_started_at_ms      INTEGER NOT NULL CHECK (tracking_started_at_ms >= 0),
    schema_version              INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1)
)
"""

_CREATE_USAGE_LEDGER_STATE = """
CREATE TABLE IF NOT EXISTS usage_ledger_state (
    singleton_id                INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    ledger_started_at_ms        INTEGER NOT NULL CHECK (ledger_started_at_ms >= 0),
    backfill_status             TEXT NOT NULL DEFAULT 'pending'
                                CHECK (backfill_status IN
                                       ('pending', 'running', 'complete',
                                        'partial', 'failed')),
    cursor_created_at_ms        INTEGER,
    cursor_session_id           TEXT,
    cursor_message_id           TEXT,
    backfilled_event_count      INTEGER NOT NULL DEFAULT 0
                                CHECK (backfilled_event_count >= 0),
    backfilled_cost_nanos       INTEGER NOT NULL DEFAULT 0
                                CHECK (backfilled_cost_nanos >= 0),
    anomaly_count               INTEGER NOT NULL DEFAULT 0 CHECK (anomaly_count >= 0),
    last_error_code             TEXT,
    updated_at_ms               INTEGER NOT NULL CHECK (updated_at_ms >= 0),
    schema_version              INTEGER NOT NULL DEFAULT 1,
    CHECK (
        (cursor_created_at_ms IS NULL AND cursor_session_id IS NULL
         AND cursor_message_id IS NULL)
        OR
        (cursor_created_at_ms IS NOT NULL AND cursor_session_id IS NOT NULL
         AND cursor_message_id IS NOT NULL)
    )
)
"""

_CREATE_USAGE_LEGACY_BASELINES = """
CREATE TABLE IF NOT EXISTS usage_legacy_baselines (
    session_id                  TEXT NOT NULL,
    session_epoch               INTEGER NOT NULL DEFAULT 0 CHECK (session_epoch >= 0),
    agent_id                    TEXT NOT NULL DEFAULT 'main',
    captured_at_ms              INTEGER NOT NULL CHECK (captured_at_ms >= 0),
    input_tokens                INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens               INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    total_tokens                INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    cache_read_tokens           INTEGER NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens          INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    cost_nanos                  INTEGER NOT NULL DEFAULT 0 CHECK (cost_nanos >= 0),
    billed_cost_nanos           INTEGER NOT NULL DEFAULT 0 CHECK (billed_cost_nanos >= 0),
    estimated_cost_nanos        INTEGER NOT NULL DEFAULT 0 CHECK (estimated_cost_nanos >= 0),
    cost_source                 TEXT NOT NULL DEFAULT 'none',
    missing_cost_entries        INTEGER NOT NULL DEFAULT 0
                                CHECK (missing_cost_entries >= 0),
    schema_version              INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (session_id, session_epoch),
    CHECK (cost_nanos = billed_cost_nanos + estimated_cost_nanos)
)
"""

_CREATE_IDX_USAGE_EVENTS_COMPLETED = """
CREATE INDEX IF NOT EXISTS idx_usage_events_completed
ON usage_events(completed_at_ms, event_id)
"""
_CREATE_IDX_USAGE_EVENTS_SESSION_COMPLETED = """
CREATE INDEX IF NOT EXISTS idx_usage_events_session_completed
ON usage_events(session_id, completed_at_ms, event_id)
"""
_CREATE_IDX_USAGE_EVENTS_AGENT_COMPLETED = """
CREATE INDEX IF NOT EXISTS idx_usage_events_agent_completed
ON usage_events(agent_id, completed_at_ms, event_id)
"""
_CREATE_IDX_USAGE_EVENTS_STATUS_COMPLETED = """
CREATE INDEX IF NOT EXISTS idx_usage_events_status_completed
ON usage_events(status, completed_at_ms, event_id)
"""
_CREATE_IDX_USAGE_EVENTS_STATUS_STARTED = """
CREATE INDEX IF NOT EXISTS idx_usage_events_status_started
ON usage_events(status, started_at_ms, event_id)
"""
_CREATE_IDX_USAGE_EVENT_ITEMS_MODEL = """
CREATE INDEX IF NOT EXISTS idx_usage_event_items_model
ON usage_event_items(model, event_id, ordinal)
"""
_CREATE_IDX_USAGE_EVENT_ITEMS_PROVIDER = """
CREATE INDEX IF NOT EXISTS idx_usage_event_items_provider
ON usage_event_items(provider, event_id, ordinal)
"""
_CREATE_IDX_USAGE_LEGACY_BASELINES_CAPTURED = """
CREATE INDEX IF NOT EXISTS idx_usage_legacy_baselines_captured
ON usage_legacy_baselines(captured_at_ms, session_id)
"""
_CREATE_IDX_TRANSCRIPT_USAGE_BACKFILL = """
CREATE INDEX IF NOT EXISTS idx_transcript_usage_backfill
ON transcript_entries(created_at, session_id, message_id)
WHERE role = 'assistant' AND turn_usage IS NOT NULL
"""
_CREATE_IDX_COMPACTED_USAGE_BACKFILL = """
CREATE INDEX IF NOT EXISTS idx_compacted_usage_backfill
ON compacted_transcript_entries(created_at, session_id, message_id)
WHERE role = 'assistant' AND turn_usage IS NOT NULL
"""
_CREATE_IDX_SESSIONS_ID_KEY = """
CREATE INDEX IF NOT EXISTS idx_sessions_id_key
ON sessions(session_id, session_key)
"""

_CREATE_EPOCH_ROLLBACK_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS prevent_epoch_rollback
BEFORE UPDATE OF epoch ON sessions
WHEN NEW.epoch < OLD.epoch
BEGIN
    SELECT RAISE(ABORT, 'epoch can only increase');
END
"""

_SQLITE_VARIABLE_CHUNK_SIZE = 900


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _serialize(value: Any) -> Any:
    """Serialize dict/list fields to JSON string for SQLite TEXT columns."""
    if isinstance(value, dict | list):
        return json.dumps(value)
    if isinstance(value, bool):
        return int(value)
    return value


def _transcript_preimage(
    entries: Sequence[TranscriptEntry],
) -> tuple[tuple[Any, ...], ...]:
    """Return the context-relevant identity used by compaction CAS checks."""

    def _stable_json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    return tuple(
        (
            entry.session_id,
            entry.session_key,
            entry.id,
            entry.message_id,
            entry.role,
            entry.content,
            entry.tool_call_id,
            entry.reasoning_content,
            entry.created_at,
            entry.token_count,
            entry.provenance_kind,
            entry.provenance_origin_session_id,
            entry.provenance_source_session_key,
            entry.provenance_source_channel,
            entry.provenance_source_tool,
            entry.schema_version,
            _stable_json(entry.tool_calls),
            _stable_json(entry.turn_usage),
            _stable_json(entry.turn_context),
        )
        for entry in entries
    )


def _ordered_detail_message_ids(*values: Any) -> list[str]:
    """Normalize persisted-message detail fields without changing order."""

    ordered: list[str] = []
    for value in values:
        candidates = value if isinstance(value, list | tuple) else (value,)
        for candidate in candidates:
            if (
                isinstance(candidate, str)
                and candidate
                and candidate not in ordered
            ):
                ordered.append(candidate)
    return ordered


def _deserialize_row(row: dict[str, Any]) -> dict[str, Any]:
    """Deserialize JSON text fields back to Python objects."""
    json_fields = {
        "delivery_context",
        "tool_calls",
        "turn_usage",
        "turn_context",
        "origin",
        "details",
        "summary_payload",
        "missing_obligations",
        "critical_carry_forward",
        "payload",
        "steps",
        "step_states",
        "progress",
        "progress_json",
        "response_json",
    }
    bool_fields = {
        "total_tokens_fresh",
        "forked_from_parent",
        "fast_mode",
        "portable",
        "cacheable",
        "valid",
    }
    result = {}
    for k, v in row.items():
        if k in json_fields and isinstance(v, str):
            try:
                result[k] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                result[k] = None
        elif k in bool_fields:
            result[k] = bool(v)
        else:
            result[k] = v
    return result


def _py_lower(value: Any) -> Any:
    """Unicode-aware lowercase for the ``py_lower`` SQL function.

    SQLite's built-in LIKE / lower() only case-fold ASCII, so non-ASCII title /
    content search (Cyrillic, Greek, accented Latin, …) would otherwise be
    case-sensitive. Registered per connection in ``connect``.
    """
    return value.lower() if isinstance(value, str) else value


def _legacy_nonnegative_integer(value: Any) -> tuple[int, bool]:
    """Return a SQLite-safe counter and whether the source was invalid."""

    if value is None or isinstance(value, bool):
        return 0, True
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return 0, True
    if not parsed.is_finite() or parsed < 0 or parsed != parsed.to_integral_value():
        return 0, True
    integer = int(parsed)
    if integer > (1 << 63) - 1:
        return 0, True
    return integer, False


def _legacy_cost_triplet(
    total_usd: Any,
    billed_usd: Any,
    estimated_usd: Any,
) -> tuple[int, int, int, bool]:
    """Normalize old float columns while preserving the known total.

    A valid legacy total remains authoritative. The billed component is capped
    at that total and the estimate becomes the residual, so every persisted
    baseline satisfies ``cost = billed + estimated``. Any repair is surfaced as
    an anomaly/missing entry rather than silently claiming exact history.
    """

    def parse(value: Any) -> tuple[int, bool]:
        if value is None or isinstance(value, bool):
            return 0, True
        try:
            return usd_to_nanos(value), False
        except (TypeError, ValueError, OverflowError):
            return 0, True

    raw_total, invalid_total = parse(total_usd)
    raw_billed, invalid_billed = parse(billed_usd)
    raw_estimated, invalid_estimated = parse(estimated_usd)
    total = raw_billed + raw_estimated if invalid_total else raw_total
    billed = min(raw_billed, total)
    estimated = total - billed
    anomaly = (
        invalid_total
        or invalid_billed
        or invalid_estimated
        or raw_total != raw_billed + raw_estimated
    )
    return total, billed, estimated, anomaly


def _sqlite_usage_nonnegative_int(value: Any) -> int:
    return _legacy_nonnegative_integer(value)[0]


def _sqlite_usage_invalid_int(value: Any) -> int:
    return int(_legacy_nonnegative_integer(value)[1])


def _sqlite_usage_cost_total(total: Any, billed: Any, estimated: Any) -> int:
    return _legacy_cost_triplet(total, billed, estimated)[0]


def _sqlite_usage_cost_billed(total: Any, billed: Any, estimated: Any) -> int:
    return _legacy_cost_triplet(total, billed, estimated)[1]


def _sqlite_usage_cost_estimated(total: Any, billed: Any, estimated: Any) -> int:
    return _legacy_cost_triplet(total, billed, estimated)[2]


def _sqlite_usage_cost_anomaly(total: Any, billed: Any, estimated: Any) -> int:
    return int(_legacy_cost_triplet(total, billed, estimated)[3])


def _usage_event_from_row(row: Any) -> UsageEventRecord:
    data = dict(row)
    data["status"] = cast(UsageEventStatus, data["status"])
    return UsageEventRecord(**data)


def _usage_item_from_row(row: Any) -> UsageEventItem:
    return UsageEventItem(**dict(row))


def _usage_billing_receipt_from_row(row: Any) -> UsageItemBillingReceipt:
    data = dict(row)
    data["status"] = cast(UsageBillingReceiptStatus, data["status"])
    return UsageItemBillingReceipt(**data)


def _usage_billing_receipt_state_from_row(row: Any) -> UsageBillingReceiptState:
    data = dict(row)
    data.pop("singleton_id", None)
    return UsageBillingReceiptState(**data)


def _usage_state_from_row(row: Any) -> UsageLedgerState:
    data = dict(row)
    data.pop("singleton_id", None)
    data["backfill_status"] = cast(UsageBackfillStatus, data["backfill_status"])
    return UsageLedgerState(**data)


def _usage_baseline_from_row(row: Any) -> UsageLegacyBaseline:
    return UsageLegacyBaseline(**dict(row))


def _json_object_or_none(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


@dataclass(frozen=True, slots=True)
class SessionListCursor:
    """Stable descending sort position for a session-list page."""

    activity_at: int
    updated_at: int
    session_key: str


@dataclass(frozen=True, slots=True)
class SessionListPage:
    """One keyset-paginated page of sessions."""

    sessions: list[SessionNode]
    next_cursor: SessionListCursor | None
    has_more: bool


class SessionStorage:
    """Low-level async SQLite operations for session persistence."""

    def __init__(
        self,
        db_path: str = ":memory:",
        *,
        meta_run_writer: MetaRunWriter | None = None,
    ) -> None:
        self._db_path = db_path
        self._conn: Any | None = None
        self._meta_run_writer = meta_run_writer
        self._operation_lock = asyncio.Lock()
        self._usage_backfill_index_lock = asyncio.Lock()
        self._usage_backfill_indexes_ready = False
        self._legacy_project_adoption_lock = asyncio.Lock()
        self._legacy_project_adoption_generation = 0
        self._legacy_project_adoption_completed_generation = -1
        self._poisoned = False
        self._busy_budget_seconds = _INTERACTIVE_BUSY_BUDGET_SECONDS
        self._sleep = asyncio.sleep
        self._monotonic = time.monotonic
        self._random = random.random
        self._meta_launch_draft_gc_task: asyncio.Task[None] | None = None
        self._restart_abandoned_session_keys: tuple[str, ...] = ()

    @property
    def restart_abandoned_session_keys(self) -> tuple[str, ...]:
        """Sessions whose in-flight tasks were orphaned by process restart."""

        return self._restart_abandoned_session_keys

    def take_restart_abandoned_session_keys(self) -> tuple[str, ...]:
        """Return and clear the one-shot restart recovery signal."""

        session_keys = self._restart_abandoned_session_keys
        self._restart_abandoned_session_keys = ()
        return session_keys

    async def run_legacy_project_adoption_once(
        self,
        adoption: Callable[[], Awaitable[None]],
    ) -> None:
        """Single-flight legacy project adoption for this storage connection."""

        if (
            self._legacy_project_adoption_completed_generation
            == self._legacy_project_adoption_generation
        ):
            return
        async with self._legacy_project_adoption_lock:
            while (
                self._legacy_project_adoption_completed_generation
                != self._legacy_project_adoption_generation
            ):
                generation = self._legacy_project_adoption_generation
                await adoption()
                if generation == self._legacy_project_adoption_generation:
                    self._legacy_project_adoption_completed_generation = generation

    def invalidate_legacy_project_adoption(self) -> None:
        """Require the next workspace listing to re-check legacy session origins."""

        self._legacy_project_adoption_generation += 1

    async def connect(
        self,
        *,
        goal_pause_reason: str = "process_restart",
    ) -> None:
        self._conn = await aiosqlite.connect(self._db_path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        # Unicode-aware case folding for non-ASCII LIKE search (see _py_lower).
        # aiosqlite proxies create_function to sqlite3 at runtime; its stub omits it.
        await self._conn.create_function(  # type: ignore[attr-defined]
            "py_lower", 1, _py_lower, deterministic=True
        )
        for name, arity, function in (
            ("usage_nonnegative_int", 1, _sqlite_usage_nonnegative_int),
            ("usage_invalid_int", 1, _sqlite_usage_invalid_int),
            ("usage_cost_total", 3, _sqlite_usage_cost_total),
            ("usage_cost_billed", 3, _sqlite_usage_cost_billed),
            ("usage_cost_estimated", 3, _sqlite_usage_cost_estimated),
            ("usage_cost_anomaly", 3, _sqlite_usage_cost_anomaly),
        ):
            await self._conn.create_function(  # type: ignore[attr-defined]
                name, arity, function, deterministic=True
            )
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        await self._initialize_schema(goal_pause_reason=goal_pause_reason)
        self._meta_launch_draft_gc_task = asyncio.create_task(
            self._run_meta_launch_draft_gc(),
            name="session-storage-meta-launch-draft-gc",
        )

    @classmethod
    async def open(cls, db_path: str) -> SessionStorage:
        storage = cls(str(db_path))
        await storage.connect()
        return storage

    async def close(self) -> None:
        gc_task, self._meta_launch_draft_gc_task = self._meta_launch_draft_gc_task, None
        if gc_task is not None:
            gc_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await gc_task
        async with self._operation_lock:
            if self._conn:
                await self._conn.close()
                self._conn = None

    async def _run_meta_launch_draft_gc(self) -> None:
        """Physically enforce raw-draft retention while the Gateway stays up."""

        while True:
            await asyncio.sleep(_META_LAUNCH_DRAFT_GC_INTERVAL_SECONDS)
            try:
                async with self._write_transaction("meta_launch_draft_periodic_gc") as conn:
                    await self._purge_expired_meta_launch_drafts(
                        conn,
                        now_ms=_now_ms(),
                        limit=_META_LAUNCH_DRAFT_GC_BATCH,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Periodic MetaSkill draft retention cleanup failed", exc_info=True)

    def _raise_if_poisoned(self) -> None:
        if self._poisoned:
            raise StorageConnectionPoisonedError(
                "Session storage connection is unavailable after rollback failure"
            )

    async def _retire_poisoned_connection(self) -> None:
        self._poisoned = True
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(BaseException):
                await conn.close()

    async def _finish_sqlite_call(self, awaitable: Awaitable[Any]) -> Any:
        """Do not release the operation gate while a cancelled DB call is still queued."""

        task = asyncio.ensure_future(awaitable)
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                # aiosqlite cancellation does not cancel work already queued on
                # its worker. Keep shielding through repeated cancellation until
                # the call settles, then propagate cancellation to the caller.
                cancellation = cancellation or exc
        if cancellation is not None:
            # Retrieve a settled child result so an operation error is not left
            # unobserved. Cancellation still wins for the interrupted caller;
            # rollback verifies the connection state before deciding it failed.
            with contextlib.suppress(BaseException):
                task.result()
            raise cancellation
        return task.result()

    async def _rollback_transaction(self, conn: Any, operation: str) -> None:
        if not bool(getattr(conn, "in_transaction", False)):
            return
        try:
            await self._finish_sqlite_call(conn.rollback())
        except asyncio.CancelledError as exc:
            # _finish_sqlite_call waits for rollback to settle even through
            # repeated cancellation. A cleared transaction is therefore a
            # successful cleanup, not a poisoned connection.
            if not bool(getattr(conn, "in_transaction", False)):
                raise
            log.error(
                "session_storage.rollback_failed operation=%s error=%s",
                operation,
                type(exc).__name__,
            )
            await self._retire_poisoned_connection()
            raise StorageConnectionPoisonedError(
                f"Session storage rollback failed during {operation}"
            ) from exc
        except BaseException as exc:
            log.error(
                "session_storage.rollback_failed operation=%s error=%s",
                operation,
                type(exc).__name__,
            )
            await self._retire_poisoned_connection()
            raise StorageConnectionPoisonedError(
                f"Session storage rollback failed during {operation}"
            ) from exc

    async def _retry_delay(self, attempt: int, deadline: float) -> None:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            return
        cap = min(
            _BUSY_RETRY_MAX_SECONDS,
            _BUSY_RETRY_INITIAL_SECONDS * (2 ** min(attempt, 8)),
            remaining,
        )
        await self._sleep(self._random() * cap)

    async def _begin_immediate(
        self,
        conn: Any,
        operation: str,
        deadline: float,
        started: float,
    ) -> None:
        attempt = 0
        while True:
            try:
                await self._finish_sqlite_call(conn.execute("BEGIN IMMEDIATE"))
                return
            except asyncio.CancelledError:
                await self._rollback_transaction(conn, operation)
                raise
            except BaseException as exc:
                if not _is_sqlite_busy(exc):
                    raise
                if self._monotonic() >= deadline:
                    waited_ms = max(0, int((self._monotonic() - started) * 1000))
                    raise StorageBusyError(
                        operation,
                        waited_ms=waited_ms,
                        retry_after_ms=_SQLITE_BUSY_TIMEOUT_MS,
                    ) from exc
                await self._retry_delay(attempt, deadline)
                attempt += 1

    async def _commit_transaction(
        self,
        conn: Any,
        operation: str,
        deadline: float,
        started: float,
    ) -> None:
        attempt = 0
        while True:
            try:
                await self._finish_sqlite_call(conn.commit())
                return
            except asyncio.CancelledError:
                # The shielded commit has settled. If it did not commit, clean up;
                # if it did, the request-id layer above provides replay safety.
                await self._rollback_transaction(conn, operation)
                raise
            except BaseException as exc:
                if not _is_sqlite_busy(exc):
                    raise
                if self._monotonic() >= deadline:
                    waited_ms = max(0, int((self._monotonic() - started) * 1000))
                    raise StorageBusyError(
                        operation,
                        waited_ms=waited_ms,
                        retry_after_ms=_SQLITE_BUSY_TIMEOUT_MS,
                    ) from exc
                await self._retry_delay(attempt, deadline)
                attempt += 1

    @asynccontextmanager
    async def _write_transaction(
        self,
        operation: str,
        *,
        budget_seconds: float | None = None,
    ) -> AsyncIterator[Any]:
        started = self._monotonic()
        budget = self._busy_budget_seconds if budget_seconds is None else budget_seconds
        deadline = started + max(0.0, budget)
        acquired = False
        try:
            remaining = max(0.0, deadline - self._monotonic())
            try:
                # asyncio.timeout(0) still permits an uncontended Lock.acquire
                # to complete synchronously, while refusing to queue behind an
                # existing holder or waiter once the budget is exhausted.
                async with asyncio.timeout(remaining):
                    await self._operation_lock.acquire()
            except TimeoutError as exc:
                raise StorageBusyError(
                    operation,
                    waited_ms=max(0, int((self._monotonic() - started) * 1000)),
                    retry_after_ms=_SQLITE_BUSY_TIMEOUT_MS,
                ) from exc
            acquired = True
            self._raise_if_poisoned()
            conn = self.conn
            await self._begin_immediate(conn, operation, deadline, started)
            try:
                yield conn
                await self._commit_transaction(conn, operation, deadline, started)
            except BaseException:
                await self._rollback_transaction(conn, operation)
                raise
        finally:
            if acquired:
                self._operation_lock.release()

    async def _initialize_schema(
        self,
        *,
        goal_pause_reason: str = "process_restart",
    ) -> None:
        assert self._conn is not None
        await self._conn.execute(_CREATE_SESSIONS)
        await self._conn.execute(_CREATE_PROJECT_WORKSPACES)
        await self._conn.execute(_CREATE_IDX_PROJECT_WORKSPACES_ORDER)
        await self._conn.execute(_CREATE_RUNTIME_PREFERENCES)
        await self._conn.execute(_CREATE_PLAN_REVISIONS)
        await self._conn.execute(_CREATE_IDX_PLAN_REVISIONS_PLAN_GENERATION)
        await self._conn.execute(_CREATE_IDX_PLAN_REVISIONS_SOURCE_SESSION)
        await self._conn.execute(_CREATE_IDX_PLAN_REVISIONS_SOURCE_MESSAGE)
        await self._conn.execute(_CREATE_PLAN_REVISIONS_IMMUTABLE_TRIGGER)
        await self._conn.execute(_CREATE_PLAN_RUNS)
        await self._conn.execute(_CREATE_IDX_PLAN_RUNS_ACTIVE_SESSION)
        await self._conn.execute(_CREATE_IDX_PLAN_RUNS_SESSION_HISTORY)
        await self._conn.execute(_CREATE_IDX_PLAN_RUNS_REVISION)
        await self._conn.execute(_CREATE_IDX_PLAN_RUNS_DRIVER)
        await self._conn.execute(_CREATE_SESSION_GOALS)
        await self._conn.execute(_CREATE_IDX_SESSION_GOALS_ACTIVE_TASK)
        await self._conn.execute(_CREATE_IDX_SESSION_GOALS_STATUS)
        await self._conn.execute(_CREATE_GOAL_COMMAND_RECEIPTS)
        await self._conn.execute(_CREATE_IDX_GOAL_COMMAND_RECEIPTS_REQUEST)
        await self._conn.execute(_CREATE_IDX_GOAL_COMMAND_RECEIPTS_SESSION)
        await self._conn.execute(_CREATE_TRANSCRIPT)
        await self._conn.execute(_CREATE_IDX_TRANSCRIPT_SESSION)
        await self._conn.execute(_CREATE_IDX_TRANSCRIPT_KEY)
        await self._conn.execute(_CREATE_IDX_TRANSCRIPT_CURSOR)
        await self._conn.execute(_CREATE_COMPACTED_TRANSCRIPT)
        await self._conn.execute(_CREATE_IDX_COMPACTED_TRANSCRIPT_SESSION)
        await self._conn.execute(_CREATE_IDX_COMPACTED_TRANSCRIPT_KEY)
        await self._conn.execute(_CREATE_IDX_COMPACTED_TRANSCRIPT_CURSOR)
        await self._conn.execute(_CREATE_IDX_COMPACTED_TRANSCRIPT_COMPACTION)
        await self._conn.execute(_CREATE_SUMMARIES)
        await self._conn.execute(_CREATE_IDX_SUMMARIES)
        await self._conn.execute(_CREATE_CONTEXT_STATES)
        await self._conn.execute(_CREATE_IDX_CONTEXT_STATES_SESSION)
        await self._conn.execute(_CREATE_IDX_CONTEXT_STATES_KEY_VALID)
        await self._conn.execute(_CREATE_AGENT_TASKS)
        await self._conn.execute(_CREATE_IDX_AGENT_TASKS_SESSION_STATUS)
        await self._conn.execute(_CREATE_IDX_AGENT_TASKS_STATUS_UPDATED)
        await self._conn.execute(_CREATE_TURN_INGRESS_RECEIPTS)
        await self._conn.execute(_CREATE_IDX_TURN_INGRESS_REQUEST)
        await self._conn.execute(_CREATE_IDX_TURN_INGRESS_ACCEPTED_SESSION)
        await self._conn.execute(_CREATE_PENDING_CHAT_INPUTS)
        await self._conn.execute(_CREATE_IDX_PENDING_CHAT_INPUT_REQUEST)
        await self._conn.execute(_CREATE_IDX_PENDING_CHAT_INPUT_MESSAGE)
        await self._conn.execute(_CREATE_IDX_PENDING_CHAT_INPUT_SESSION_ORDER)
        await self._conn.execute(_CREATE_PENDING_CHAT_INPUT_CANCELLATIONS)
        await self._conn.execute(
            _CREATE_IDX_PENDING_CHAT_INPUT_CANCELLATIONS_SESSION
        )
        await self._conn.execute(_CREATE_PENDING_CHAT_INPUT_DISPATCH_RECEIPTS)
        await self._conn.execute(_CREATE_IDX_PENDING_CHAT_INPUT_DISPATCH_REQUEST)
        await self._conn.execute(_CREATE_IDX_PENDING_CHAT_INPUT_DISPATCH_MESSAGE)
        await self._conn.execute(_CREATE_IDX_PENDING_CHAT_INPUT_DISPATCH_SESSION)
        await self._conn.execute(_CREATE_META_CONTROL_INTENTS)
        await self._conn.execute(_CREATE_IDX_META_CONTROL_CORRELATION)
        await self._conn.execute(_CREATE_IDX_META_CONTROL_SESSION_STATUS)
        await self._conn.execute(_CREATE_META_LAUNCH_DRAFTS)
        await self._conn.execute(_CREATE_IDX_META_LAUNCH_DRAFT_REQUEST)
        await self._conn.execute(_CREATE_IDX_META_LAUNCH_DRAFT_SESSION_EXPIRY)
        await self._conn.execute(_CREATE_META_LAUNCH_DISCARD_TOMBSTONES)
        await self._conn.execute(_CREATE_IDX_META_LAUNCH_DISCARD_TOMBSTONES_EXPIRY)
        await self._conn.execute(_CREATE_MEMORY_DURABLE_RECEIPTS)
        await self._conn.execute(_CREATE_IDX_MEMORY_DURABLE_RECEIPTS_SESSION)
        await self._conn.execute(_CREATE_TELEMETRY_DAILY_USAGE)
        await self._conn.execute(_CREATE_USAGE_EVENTS)
        await self._conn.execute(_CREATE_USAGE_EVENT_ITEMS)
        await self._conn.execute(_CREATE_USAGE_ITEM_BILLING_RECEIPTS)
        await self._conn.execute(_CREATE_USAGE_BILLING_RECEIPT_STATE)
        await self._conn.execute(
            """
            INSERT OR IGNORE INTO usage_billing_receipt_state (
                singleton_id, tracking_started_at_ms, schema_version
            ) VALUES (1, ?, 1)
            """,
            (_now_ms(),),
        )
        await self._conn.execute(_CREATE_USAGE_LEDGER_STATE)
        await self._conn.execute(_CREATE_USAGE_LEGACY_BASELINES)
        await self._conn.execute(_CREATE_IDX_USAGE_EVENTS_COMPLETED)
        await self._conn.execute(_CREATE_IDX_USAGE_EVENTS_SESSION_COMPLETED)
        await self._conn.execute(_CREATE_IDX_USAGE_EVENTS_AGENT_COMPLETED)
        await self._conn.execute(_CREATE_IDX_USAGE_EVENTS_STATUS_COMPLETED)
        await self._conn.execute(_CREATE_IDX_USAGE_EVENTS_STATUS_STARTED)
        await self._conn.execute(_CREATE_IDX_USAGE_EVENT_ITEMS_MODEL)
        await self._conn.execute(_CREATE_IDX_USAGE_EVENT_ITEMS_PROVIDER)
        await self._conn.execute(_CREATE_IDX_USAGE_LEGACY_BASELINES_CAPTURED)
        # FTS5 full-text search index + auto-sync triggers
        await self._conn.execute(_CREATE_TRANSCRIPT_FTS)
        await self._conn.execute(_CREATE_FTS_TRIGGER_INSERT)
        await self._conn.execute(_CREATE_FTS_TRIGGER_DELETE)
        await self._conn.execute(_CREATE_FTS_TRIGGER_UPDATE)
        # Hard DB-level guarantee: epoch can never decrease via UPDATE.
        await self._conn.execute(_CREATE_EPOCH_ROLLBACK_TRIGGER)
        await self._conn.commit()
        # Migrate older databases — add the epoch column if missing.
        await self._migrate_epoch_column()
        await self._migrate_workspace_id_column()
        await self._migrate_collaboration_columns()
        await self._migrate_model_routing_columns()
        await self._migrate_derived_title_column()
        await self._migrate_transcript_reasoning_content_column()
        await self._migrate_transcript_turn_usage_column()
        await self._migrate_transcript_turn_context_column()
        await self._migrate_summary_metadata_columns()
        await self._migrate_memory_durable_receipt_coverage_columns()
        await self._conn.execute(_CREATE_IDX_MEMORY_DURABLE_RECEIPTS_COVERAGE)
        # Recency index for list_sessions / title search. Guarded on the column
        # because a very old (pre-updated_at) sessions table can survive here
        # without it — connect must not fail on those legacy databases.
        async with self._conn.execute("PRAGMA table_info(sessions)") as cur:
            session_columns = {row[1] for row in await cur.fetchall()}
        if "updated_at" in session_columns:
            await self._conn.execute(_CREATE_IDX_SESSIONS_UPDATED)
        # Launch drafts contain raw user prompts. Enforce their seven-day
        # retention at every process start even when nobody stages or lists a
        # new draft after the old rows expire.
        await self._purge_expired_meta_launch_drafts(
            self._conn,
            now_ms=_now_ms(),
            limit=_META_LAUNCH_DRAFT_GC_BATCH,
        )
        if "workspace_id" in session_columns:
            await self._conn.execute(_CREATE_IDX_SESSIONS_WORKSPACE)
        await self._conn.commit()
        required_recovery_columns = {
            "status",
            "updated_at",
            "ended_at",
            "runtime_ms",
            "started_at",
        }
        if required_recovery_columns <= session_columns:
            await self.mark_abandoned_agent_tasks(
                goal_pause_reason=goal_pause_reason,
            )

    async def prepare_usage_backfill_indexes(self) -> None:
        """Build optional historical-scan indexes after Gateway readiness.

        V021 and the fresh-install schema deliberately avoid indexing transcript
        history: creating an index scans the complete source table and must not
        delay an upgrade from becoming ready.  The post-ready backfill worker
        calls this method before paging.  File-backed databases use an
        independent connection so the shared interactive connection and its
        operation lock remain available to RPC reads.
        """

        async with self._usage_backfill_index_lock:
            if self._usage_backfill_indexes_ready:
                return
            statements = (
                _CREATE_IDX_TRANSCRIPT_USAGE_BACKFILL,
                _CREATE_IDX_COMPACTED_USAGE_BACKFILL,
                _CREATE_IDX_SESSIONS_ID_KEY,
            )
            if self._db_path == ":memory:":
                async with self._operation_lock:
                    self._raise_if_poisoned()
                    for statement in statements:
                        await self.conn.execute(statement)
                    await self.conn.commit()
            else:
                connection = await aiosqlite.connect(
                    self._db_path,
                    isolation_level=None,
                )
                try:
                    await connection.execute(
                        f"PRAGMA busy_timeout={int(_INTERACTIVE_BUSY_BUDGET_SECONDS * 1000)}"
                    )
                    for statement in statements:
                        await connection.execute(statement)
                finally:
                    await connection.close()
            self._usage_backfill_indexes_ready = True

    async def record_daily_usage(
        self,
        *,
        day: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        cache_write_tokens: int,
        updated_at: int,
    ) -> None:
        """Atomically add one completed interactive turn to a UTC-day bucket."""
        async with self._write_transaction("record_daily_usage") as conn:
            await conn.execute(
                """
                INSERT INTO telemetry_daily_usage (
                    day, conversation_turns, input_tokens, output_tokens,
                    cached_tokens, cache_write_tokens, updated_at, uploaded_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(day) DO UPDATE SET
                    conversation_turns = conversation_turns + 1,
                    input_tokens = input_tokens + excluded.input_tokens,
                    output_tokens = output_tokens + excluded.output_tokens,
                    cached_tokens = cached_tokens + excluded.cached_tokens,
                    cache_write_tokens = cache_write_tokens + excluded.cache_write_tokens,
                    updated_at = excluded.updated_at,
                    uploaded_at = NULL
                """,
                (
                    day,
                    input_tokens,
                    output_tokens,
                    cached_tokens,
                    cache_write_tokens,
                    updated_at,
                ),
            )

    @_serialized_read
    async def list_pending_daily_usage(self, *, before_day: str) -> list[dict[str, Any]]:
        """Return unsent completed UTC-day aggregates in chronological order."""
        async with self.conn.execute(
            """
            SELECT day, conversation_turns, input_tokens, output_tokens,
                   cached_tokens, cache_write_tokens, updated_at, uploaded_at
            FROM telemetry_daily_usage
            WHERE day < ? AND uploaded_at IS NULL
            ORDER BY day ASC
            """,
            (before_day,),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def mark_daily_usage_uploaded(
        self,
        *,
        day: str,
        uploaded_at: int,
        expected_conversation_turns: int,
    ) -> bool:
        """Mark a sent snapshot unless another turn changed it in flight."""
        async with self._write_transaction("mark_daily_usage_uploaded") as conn:
            cursor = await conn.execute(
                """
                UPDATE telemetry_daily_usage
                SET uploaded_at = ?
                WHERE day = ? AND conversation_turns = ?
                """,
                (uploaded_at, day, expected_conversation_turns),
            )
            updated = int(cursor.rowcount or 0) > 0
        return updated

    async def _migrate_epoch_column(self) -> None:
        """Idempotently add the epoch column to an existing sessions table.

        Uses PRAGMA table_info to detect whether the column is already present.
        If absent, ALTER TABLE adds it with DEFAULT 0, then any NULL rows
        (should not exist but guarded anyway) are set to 0.
        """
        assert self._conn is not None
        async with self._conn.execute("PRAGMA table_info(sessions)") as cur:
            columns = [row[1] for row in await cur.fetchall()]
        if "epoch" not in columns:
            await self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0"
            )
            await self._conn.commit()
        # Defensive: zero-out any NULL epoch rows left by a partial migration.
        async with self._conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE epoch IS NULL"
        ) as cur:
            row = await cur.fetchone()
        null_count = row[0] if row else 0
        if null_count > 0:
            await self._conn.execute(
                "UPDATE sessions SET epoch = 0 WHERE epoch IS NULL"
            )
            await self._conn.commit()

    async def _migrate_workspace_id_column(self) -> None:
        """Idempotently add the optional project-workspace session binding."""

        assert self._conn is not None
        async with self._conn.execute("PRAGMA table_info(sessions)") as cur:
            columns = {str(row[1]) for row in await cur.fetchall()}
        if "workspace_id" not in columns:
            await self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN workspace_id TEXT"
            )
            await self._conn.commit()

    async def _migrate_collaboration_columns(self) -> None:
        """Idempotently widen legacy sessions with durable Plan mode state."""

        assert self._conn is not None
        async with self._conn.execute("PRAGMA table_info(sessions)") as cur:
            columns = {row[1] for row in await cur.fetchall()}
        additions = {
            "collaboration_mode": (
                "ALTER TABLE sessions ADD COLUMN "
                "collaboration_mode TEXT NOT NULL DEFAULT 'default'"
            ),
            "collaboration_revision": (
                "ALTER TABLE sessions ADD COLUMN "
                "collaboration_revision INTEGER NOT NULL DEFAULT 0"
            ),
            "active_plan_revision_id": (
                "ALTER TABLE sessions ADD COLUMN active_plan_revision_id TEXT"
            ),
        }
        changed = False
        for column, sql in additions.items():
            if column not in columns:
                await self._conn.execute(sql)
                changed = True
        if changed:
            await self._conn.commit()
        await self._conn.execute(
            """
            UPDATE sessions
            SET collaboration_mode = 'default'
            WHERE collaboration_mode NOT IN ('default', 'plan')
               OR collaboration_mode IS NULL
            """
        )
        await self._conn.execute(
            """
            UPDATE sessions
            SET collaboration_revision = 0
            WHERE collaboration_revision IS NULL OR collaboration_revision < 0
            """
        )
        await self._conn.commit()

    async def _migrate_model_routing_columns(self) -> None:
        """Idempotently add durable per-session model-routing state."""

        assert self._conn is not None
        async with self._conn.execute("PRAGMA table_info(sessions)") as cur:
            columns = {str(row[1]) for row in await cur.fetchall()}
        additions = {
            "model_routing_mode": "ALTER TABLE sessions ADD COLUMN model_routing_mode TEXT",
            "model_routing_revision": (
                "ALTER TABLE sessions ADD COLUMN "
                "model_routing_revision INTEGER NOT NULL DEFAULT 0"
            ),
        }
        changed = False
        for column, sql in additions.items():
            if column not in columns:
                await self._conn.execute(sql)
                changed = True
        if changed:
            await self._conn.commit()

    async def _migrate_derived_title_column(self) -> None:
        """Idempotently add the derived_title column to an existing sessions table.

        Holds the LLM-generated session title. Sits between display_name (manual
        rename) and subject in the title precedence, so it never overrides a name
        the user set by hand. NULL is the natural default (no title generated yet).
        """
        assert self._conn is not None
        async with self._conn.execute("PRAGMA table_info(sessions)") as cur:
            columns = [row[1] for row in await cur.fetchall()]
        if "derived_title" not in columns:
            await self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN derived_title TEXT"
            )
            await self._conn.commit()

    async def _migrate_transcript_reasoning_content_column(self) -> None:
        """Idempotently add assistant reasoning replay storage to transcripts."""
        assert self._conn is not None
        async with self._conn.execute("PRAGMA table_info(transcript_entries)") as cur:
            columns = [row[1] for row in await cur.fetchall()]
        if "reasoning_content" not in columns:
            await self._conn.execute(
                "ALTER TABLE transcript_entries ADD COLUMN reasoning_content TEXT"
            )
            await self._conn.commit()

    async def _migrate_transcript_turn_usage_column(self) -> None:
        """Idempotently add per-turn usage metadata storage to transcripts."""
        assert self._conn is not None
        async with self._conn.execute("PRAGMA table_info(transcript_entries)") as cur:
            columns = [row[1] for row in await cur.fetchall()]
        if "turn_usage" not in columns:
            await self._conn.execute(
                "ALTER TABLE transcript_entries ADD COLUMN turn_usage TEXT"
            )
            await self._conn.commit()

    async def _migrate_transcript_turn_context_column(self) -> None:
        """Idempotently add causal turn identity to active and archived rows."""
        assert self._conn is not None
        for table in ("transcript_entries", "compacted_transcript_entries"):
            async with self._conn.execute(f"PRAGMA table_info({table})") as cur:
                columns = {row[1] for row in await cur.fetchall()}
            if "turn_context" not in columns:
                await self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN turn_context TEXT"
                )
        await self._conn.commit()

    async def _migrate_summary_metadata_columns(self) -> None:
        """Idempotently add structured compaction summary metadata columns."""
        assert self._conn is not None
        async with self._conn.execute("PRAGMA table_info(session_summaries)") as cur:
            columns = {row[1] for row in await cur.fetchall()}
        additions = {
            "compaction_id": "ALTER TABLE session_summaries ADD COLUMN compaction_id TEXT",
            "trigger_reason": "ALTER TABLE session_summaries ADD COLUMN trigger_reason TEXT",
            "summary_payload": "ALTER TABLE session_summaries ADD COLUMN summary_payload TEXT",
            "summary_format": (
                "ALTER TABLE session_summaries ADD COLUMN "
                "summary_format TEXT NOT NULL DEFAULT 'text'"
            ),
            "summary_source": (
                "ALTER TABLE session_summaries ADD COLUMN "
                "summary_source TEXT NOT NULL DEFAULT 'unknown'"
            ),
            "coverage_status": (
                "ALTER TABLE session_summaries ADD COLUMN "
                "coverage_status TEXT NOT NULL DEFAULT 'unknown'"
            ),
            "missing_obligations": (
                "ALTER TABLE session_summaries ADD COLUMN missing_obligations TEXT"
            ),
            "critical_carry_forward": (
                "ALTER TABLE session_summaries ADD COLUMN critical_carry_forward TEXT"
            ),
            "tokens_before": "ALTER TABLE session_summaries ADD COLUMN tokens_before INTEGER",
            "tokens_after": "ALTER TABLE session_summaries ADD COLUMN tokens_after INTEGER",
            "removed_count": (
                "ALTER TABLE session_summaries ADD COLUMN "
                "removed_count INTEGER NOT NULL DEFAULT 0"
            ),
            "kept_count": (
                "ALTER TABLE session_summaries ADD COLUMN kept_count INTEGER NOT NULL DEFAULT 0"
            ),
            "chunk_count": (
                "ALTER TABLE session_summaries ADD COLUMN chunk_count INTEGER NOT NULL DEFAULT 0"
            ),
            "flush_receipt_status": (
                "ALTER TABLE session_summaries ADD COLUMN "
                "flush_receipt_status TEXT NOT NULL DEFAULT 'unknown'"
            ),
        }
        changed = False
        for column, sql in additions.items():
            if column not in columns:
                await self._conn.execute(sql)
                changed = True
        if changed:
            await self._conn.commit()

    async def _migrate_memory_durable_receipt_coverage_columns(self) -> None:
        """Idempotently add deterministic checkpoint coverage metadata columns."""
        assert self._conn is not None
        async with self._conn.execute("PRAGMA table_info(memory_durable_receipts)") as cur:
            columns = {row[1] for row in await cur.fetchall()}
        additions = {
            "coverage_turn_id": (
                "ALTER TABLE memory_durable_receipts ADD COLUMN coverage_turn_id TEXT"
            ),
            "coverage_hash": (
                "ALTER TABLE memory_durable_receipts ADD COLUMN coverage_hash TEXT"
            ),
            "coverage_entry_count": (
                "ALTER TABLE memory_durable_receipts ADD COLUMN coverage_entry_count INTEGER"
            ),
        }
        changed = False
        for column, sql in additions.items():
            if column not in columns:
                await self._conn.execute(sql)
                changed = True
        if changed:
            await self._conn.commit()

    @property
    def conn(self) -> Any:
        if self._conn is None:
            raise RuntimeError("Storage not connected. Call connect() first.")
        return self._conn

    # ── Durable usage ledger ────────────────────────────────────────────────

    async def _get_usage_event_on_conn(
        self,
        conn: Any,
        *,
        event_id: str | None = None,
        execution_id: str | None = None,
        call_index: int | None = None,
    ) -> UsageEventRecord | None:
        if event_id is not None:
            sql = "SELECT * FROM usage_events WHERE event_id = ?"
            params: tuple[Any, ...] = (event_id,)
        elif execution_id is not None and call_index is not None:
            sql = "SELECT * FROM usage_events WHERE execution_id = ? AND call_index = ?"
            params = (execution_id, call_index)
        else:
            raise ValueError("an event id or execution identity is required")
        async with conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return None if row is None else _usage_event_from_row(row)

    async def _get_usage_items_on_conn(
        self,
        conn: Any,
        event_id: str,
    ) -> list[UsageEventItem]:
        async with conn.execute(
            "SELECT * FROM usage_event_items WHERE event_id = ? ORDER BY ordinal",
            (event_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_usage_item_from_row(row) for row in rows]

    async def _get_usage_billing_receipts_on_conn(
        self,
        conn: Any,
        event_id: str,
    ) -> list[UsageItemBillingReceipt]:
        async with conn.execute(
            """
            SELECT * FROM usage_item_billing_receipts
            WHERE event_id = ?
            ORDER BY ordinal
            """,
            (event_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_usage_billing_receipt_from_row(row) for row in rows]

    @staticmethod
    def _assert_usage_start_matches(
        persisted: UsageEventRecord,
        event: UsageEventStart,
    ) -> None:
        persisted_identity = (
            persisted.event_id,
            persisted.execution_id,
            persisted.call_index,
            persisted.session_id,
            persisted.agent_id,
            persisted.session_epoch,
            persisted.turn_id,
            persisted.agent_run_id,
            persisted.parent_turn_id,
            persisted.run_kind,
            persisted.started_at_ms,
            persisted.origin,
        )
        requested_identity = (
            event.event_id,
            event.execution_id,
            event.call_index,
            event.session_id,
            event.agent_id,
            event.session_epoch,
            event.turn_id,
            event.agent_run_id,
            event.parent_turn_id,
            event.run_kind,
            event.started_at_ms,
            event.origin,
        )
        if persisted_identity != requested_identity:
            raise UsageLedgerConflictError(
                "usage event identity was reused with different attribution"
            )

    @staticmethod
    def _assert_usage_completion_matches(
        persisted: UsageEventRecord,
        completion: UsageEventCompletion,
    ) -> None:
        expected_provider = completion.provider or persisted.provider
        expected_model = completion.model or persisted.model
        persisted_payload = (
            persisted.completed_at_ms,
            persisted.input_tokens,
            persisted.output_tokens,
            persisted.reasoning_tokens,
            persisted.cache_read_tokens,
            persisted.cache_write_tokens,
            persisted.total_tokens,
            persisted.cost_nanos,
            persisted.billed_cost_nanos,
            persisted.estimated_cost_nanos,
            persisted.cost_source,
            persisted.provider,
            persisted.model,
            persisted.estimate_basis,
            persisted.price_source,
            persisted.coverage_status,
            persisted.missing_cost_entries,
        )
        requested_payload = (
            completion.completed_at_ms,
            completion.input_tokens,
            completion.output_tokens,
            completion.reasoning_tokens,
            completion.cache_read_tokens,
            completion.cache_write_tokens,
            completion.total_tokens,
            completion.cost_nanos,
            completion.billed_cost_nanos,
            completion.estimated_cost_nanos,
            completion.cost_source,
            expected_provider,
            expected_model,
            completion.estimate_basis,
            completion.price_source,
            completion.coverage_status,
            completion.missing_cost_entries,
        )
        if persisted_payload != requested_payload:
            raise UsageLedgerConflictError(
                "usage event was finalized again with different accounting data"
            )

    @staticmethod
    def _usage_items_match_completion(
        items: Sequence[UsageEventItem],
        completion: UsageEventCompletion,
    ) -> bool:
        if not items:
            return True
        components = (
            ("input_tokens", completion.input_tokens),
            ("output_tokens", completion.output_tokens),
            ("reasoning_tokens", completion.reasoning_tokens),
            ("cache_read_tokens", completion.cache_read_tokens),
            ("cache_write_tokens", completion.cache_write_tokens),
            ("total_tokens", completion.total_tokens),
            ("cost_nanos", completion.cost_nanos),
            ("billed_cost_nanos", completion.billed_cost_nanos),
            ("estimated_cost_nanos", completion.estimated_cost_nanos),
        )
        return all(
            sum(getattr(item, field) for item in items) == expected
            for field, expected in components
        )

    @staticmethod
    def _validate_usage_billing_receipts(
        event_id: str,
        items: Sequence[UsageEventItem],
        receipts: Sequence[UsageItemBillingReceipt],
    ) -> None:
        items_by_ordinal = {item.ordinal: item for item in items}
        seen_ordinals: set[int] = set()
        for receipt in receipts:
            validate_usage_billing_receipt(receipt, event_id=event_id)
            if receipt.ordinal in seen_ordinals:
                raise ValueError("billing receipt ordinals must be unique per event")
            seen_ordinals.add(receipt.ordinal)
            item = items_by_ordinal.get(receipt.ordinal)
            if item is None:
                raise ValueError("billing receipt must reference a usage item")
            if receipt.status == "confirmed":
                if receipt.usd_equivalent_nanos != item.billed_cost_nanos:
                    raise ValueError(
                        "confirmed billing receipt USD equivalent must equal item billed cost"
                    )
                if item.estimated_cost_nanos != 0:
                    raise ValueError(
                        "confirmed billing receipt item must not include estimated cost"
                    )
                if item.cost_source != "provider_billed":
                    raise ValueError(
                        "confirmed billing receipt item must use provider_billed cost source"
                    )
            elif item.billed_cost_nanos != 0:
                raise ValueError("pending billing receipt item billed cost must be zero")
            elif item.cost_source == "provider_billed":
                raise ValueError(
                    "pending billing receipt item must not use provider_billed cost source"
                )

    async def _start_usage_event_on_conn(
        self,
        conn: Any,
        event: UsageEventStart,
    ) -> tuple[UsageEventRecord, bool]:
        insert_cursor = await conn.execute(
            """
            INSERT INTO usage_events (
                event_id, execution_id, call_index, turn_id, agent_run_id,
                parent_turn_id, session_id, session_epoch, agent_id, run_kind,
                provider, model, started_at_ms, status, coverage_status, origin
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'started', 'pending', ?)
            ON CONFLICT DO NOTHING
            """,
            (
                event.event_id,
                event.execution_id,
                event.call_index,
                event.turn_id,
                event.agent_run_id,
                event.parent_turn_id,
                event.session_id,
                event.session_epoch,
                event.agent_id,
                event.run_kind,
                event.provider,
                event.model,
                event.started_at_ms,
                event.origin,
            ),
        )
        by_event = await self._get_usage_event_on_conn(conn, event_id=event.event_id)
        by_execution = await self._get_usage_event_on_conn(
            conn,
            execution_id=event.execution_id,
            call_index=event.call_index,
        )
        if by_event is None or by_execution is None or by_event.event_id != by_execution.event_id:
            raise UsageLedgerConflictError(
                "usage event id and execution identity refer to different records"
            )
        self._assert_usage_start_matches(by_event, event)
        created = insert_cursor.rowcount == 1
        return by_event, created

    async def _resolve_live_usage_start_on_conn(
        self,
        conn: Any,
        event: UsageEventStart,
    ) -> UsageEventStart:
        """Fill default live attribution from the current session row.

        Exact event replays retain the originally persisted epoch even if the
        session has reset since the first reservation.
        """

        if event.origin != "live_provider":
            return event
        persisted = await self._get_usage_event_on_conn(conn, event_id=event.event_id)
        if persisted is None:
            persisted = await self._get_usage_event_on_conn(
                conn,
                execution_id=event.execution_id,
                call_index=event.call_index,
            )
        if persisted is not None:
            return replace(
                event,
                agent_id=persisted.agent_id,
                session_epoch=persisted.session_epoch,
            )
        async with conn.execute(
            """
            SELECT agent_id, epoch
            FROM sessions
            WHERE session_id = ?
            ORDER BY session_key
            LIMIT 1
            """,
            (event.session_id,),
        ) as cur:
            session_row = await cur.fetchone()
        if session_row is None:
            return event
        return replace(
            event,
            agent_id=str(session_row["agent_id"] or event.agent_id),
            session_epoch=max(0, int(session_row["epoch"] or 0)),
        )

    async def start_usage_event(self, event: UsageEventStart) -> UsageEventRecord:
        """Durably reserve a provider-call identity before the request is sent.

        Repeating the exact call is idempotent. Reusing either unique identity
        with different attribution raises ``UsageLedgerConflictError``.
        """

        validate_usage_event_start(event)
        async with self._write_transaction("start_usage_event") as conn:
            resolved_event = await self._resolve_live_usage_start_on_conn(conn, event)
            validate_usage_event_start(resolved_event)
            record, _created = await self._start_usage_event_on_conn(conn, resolved_event)
            return record

    async def _finalize_usage_event_on_conn(
        self,
        conn: Any,
        event_id: str,
        completion: UsageEventCompletion,
        items: Sequence[UsageEventItem],
        receipts: Sequence[UsageItemBillingReceipt],
    ) -> tuple[UsageEventRecord, bool]:
        persisted = await self._get_usage_event_on_conn(conn, event_id=event_id)
        if persisted is None:
            raise KeyError(f"usage event not found: {event_id}")
        if completion.completed_at_ms < persisted.started_at_ms:
            raise ValueError("completed_at_ms must not precede started_at_ms")

        seen_ordinals: set[int] = set()
        for item in items:
            validate_usage_item(item, event_id=event_id)
            if item.ordinal in seen_ordinals:
                raise ValueError("usage item ordinals must be unique per event")
            seen_ordinals.add(item.ordinal)
        if items and not self._usage_items_match_completion(items, completion):
            raise ValueError(
                "usage items must reconcile exactly with their event envelope"
            )
        self._validate_usage_billing_receipts(event_id, items, receipts)

        if persisted.status == "finalized":
            self._assert_usage_completion_matches(persisted, completion)
            persisted_items = await self._get_usage_items_on_conn(conn, event_id)
            if persisted_items != sorted(items, key=lambda item: item.ordinal):
                raise UsageLedgerConflictError(
                    "usage event was finalized again with different model items"
                )
            persisted_receipts = await self._get_usage_billing_receipts_on_conn(
                conn, event_id
            )
            if persisted_receipts != sorted(receipts, key=lambda receipt: receipt.ordinal):
                raise UsageLedgerConflictError(
                    "usage event was finalized again with different billing receipts"
                )
            return persisted, False

        provider = completion.provider or persisted.provider
        model = completion.model or persisted.model
        await conn.execute(
            """
            UPDATE usage_events
            SET completed_at_ms = ?, status = 'finalized', input_tokens = ?,
                output_tokens = ?, reasoning_tokens = ?, cache_read_tokens = ?,
                cache_write_tokens = ?, total_tokens = ?, cost_nanos = ?,
                billed_cost_nanos = ?, estimated_cost_nanos = ?, cost_source = ?,
                provider = ?, model = ?, estimate_basis = ?, price_source = ?,
                coverage_status = ?, missing_cost_entries = ?, unknown_reason = NULL
            WHERE event_id = ?
            """,
            (
                completion.completed_at_ms,
                completion.input_tokens,
                completion.output_tokens,
                completion.reasoning_tokens,
                completion.cache_read_tokens,
                completion.cache_write_tokens,
                completion.total_tokens,
                completion.cost_nanos,
                completion.billed_cost_nanos,
                completion.estimated_cost_nanos,
                completion.cost_source,
                provider,
                model,
                completion.estimate_basis,
                completion.price_source,
                completion.coverage_status,
                completion.missing_cost_entries,
                event_id,
            ),
        )
        await conn.execute("DELETE FROM usage_event_items WHERE event_id = ?", (event_id,))
        for item in sorted(items, key=lambda item: item.ordinal):
            await conn.execute(
                """
                INSERT INTO usage_event_items (
                    event_id, ordinal, provider, model, input_tokens, output_tokens,
                    reasoning_tokens, cache_read_tokens, cache_write_tokens,
                    total_tokens, cost_nanos, billed_cost_nanos,
                    estimated_cost_nanos, cost_source, estimate_basis, price_source,
                    schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.event_id,
                    item.ordinal,
                    item.provider,
                    item.model,
                    item.input_tokens,
                    item.output_tokens,
                    item.reasoning_tokens,
                    item.cache_read_tokens,
                    item.cache_write_tokens,
                    item.total_tokens,
                    item.cost_nanos,
                    item.billed_cost_nanos,
                    item.estimated_cost_nanos,
                    item.cost_source,
                    item.estimate_basis,
                    item.price_source,
                    item.schema_version,
                ),
            )
        for receipt in sorted(receipts, key=lambda receipt: receipt.ordinal):
            await conn.execute(
                """
                INSERT INTO usage_item_billing_receipts (
                    event_id, ordinal, currency, status, amount_nanos,
                    usd_equivalent_nanos, fx_native_per_usd_nanos, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.event_id,
                    receipt.ordinal,
                    receipt.currency,
                    receipt.status,
                    receipt.amount_nanos,
                    receipt.usd_equivalent_nanos,
                    receipt.fx_native_per_usd_nanos,
                    receipt.schema_version,
                ),
            )
        finalized = await self._get_usage_event_on_conn(conn, event_id=event_id)
        assert finalized is not None
        return finalized, True

    async def finalize_usage_event(
        self,
        event_id: str,
        completion: UsageEventCompletion,
        *,
        items: Sequence[UsageEventItem] = (),
        receipts: Sequence[UsageItemBillingReceipt] = (),
    ) -> UsageEventRecord:
        """Atomically finalize one event, its model items, and native receipts."""

        if not event_id:
            raise ValueError("event_id must not be empty")
        validate_usage_completion(completion)
        async with self._write_transaction("finalize_usage_event") as conn:
            record, _changed = await self._finalize_usage_event_on_conn(
                conn, event_id, completion, items, receipts
            )
            return record

    async def mark_usage_event_unknown(
        self,
        event_id: str,
        *,
        completed_at_ms: int,
        reason: str | None = None,
    ) -> UsageEventRecord:
        """Mark a started provider request as having no trustworthy usage receipt.

        A concurrent successful finalization wins and is never downgraded.
        ``reason`` must be a stable code, not a raw provider error message.
        """

        if not event_id:
            raise ValueError("event_id must not be empty")
        if completed_at_ms < 0:
            raise ValueError("completed_at_ms must be non-negative")
        stable_reason = normalize_usage_unknown_reason(reason)
        async with self._write_transaction("mark_usage_event_unknown") as conn:
            persisted = await self._get_usage_event_on_conn(conn, event_id=event_id)
            if persisted is None:
                raise KeyError(f"usage event not found: {event_id}")
            if persisted.status == "finalized":
                return persisted
            if persisted.status == "unknown":
                return persisted
            if completed_at_ms < persisted.started_at_ms:
                raise ValueError("completed_at_ms must not precede started_at_ms")
            await conn.execute(
                """
                UPDATE usage_events
                SET completed_at_ms = ?, status = 'unknown',
                    coverage_status = 'usage_unknown', missing_cost_entries = 1,
                    unknown_reason = ?
                WHERE event_id = ? AND status = 'started'
                """,
                (completed_at_ms, stable_reason, event_id),
            )
            record = await self._get_usage_event_on_conn(conn, event_id=event_id)
            assert record is not None
            return record

    async def recover_started_usage_events(
        self,
        *,
        completed_at_ms: int | None = None,
        reason: str = "process_restarted",
        started_before_ms: int | None = None,
    ) -> int:
        """Terminalize provider reservations left open by an earlier process.

        Boot should call this before accepting new turns. The optional strict
        ``started_before_ms`` cutoff lets tests or embedding hosts avoid touching
        requests reserved by another known-live writer.
        """

        recovered_at_ms = _now_ms() if completed_at_ms is None else completed_at_ms
        if recovered_at_ms < 0:
            raise ValueError("completed_at_ms must be non-negative")
        if started_before_ms is not None and started_before_ms < 0:
            raise ValueError("started_before_ms must be non-negative")
        stable_reason = normalize_usage_unknown_reason(reason)
        clauses = ["status = 'started'"]
        params: list[Any] = [recovered_at_ms, recovered_at_ms, stable_reason]
        if started_before_ms is not None:
            clauses.append("started_at_ms < ?")
            params.append(started_before_ms)
        async with self._write_transaction("recover_started_usage_events") as conn:
            cursor = await conn.execute(
                """
                UPDATE usage_events
                SET completed_at_ms = CASE
                        WHEN started_at_ms > ? THEN started_at_ms ELSE ?
                    END,
                    status = 'unknown', coverage_status = 'usage_unknown',
                    missing_cost_entries = 1, unknown_reason = ?
                WHERE """
                + " AND ".join(clauses),
                params,
            )
            return max(0, int(cursor.rowcount or 0))

    async def initialize_usage_ledger(
        self,
        now_ms: int | None = None,
    ) -> UsageLedgerState:
        """Atomically establish cutover and snapshot legacy totals with set SQL."""

        captured_at_ms = _now_ms() if now_ms is None else now_ms
        if captured_at_ms < 0:
            raise ValueError("now_ms must be non-negative")

        async with self._write_transaction("initialize_usage_ledger") as conn:
            existing = await self._get_usage_state_on_conn(conn)
            if existing is not None:
                await self._repair_post_cutover_usage_baselines_on_conn(
                    conn,
                    captured_at_ms=max(
                        captured_at_ms,
                        existing.ledger_started_at_ms + 1,
                    ),
                )
                return existing

            await conn.execute(
                """
                INSERT INTO usage_ledger_state (
                    singleton_id, ledger_started_at_ms, backfill_status, updated_at_ms
                ) VALUES (1, ?, 'pending', ?)
                """,
                (captured_at_ms, captured_at_ms),
            )
            # One bounded INSERT...SELECT replaces a Python row loop and keeps
            # the pre-live cutover transaction short even for large histories.
            # The registered deterministic functions sanitize corrupt legacy
            # values without aborting gateway startup.
            await conn.execute(
                """
                WITH normalized AS (
                    SELECT
                        session_key,
                        session_id,
                        usage_nonnegative_int(epoch) AS session_epoch,
                        COALESCE(NULLIF(agent_id, ''), 'main') AS agent_id,
                        usage_nonnegative_int(input_tokens) AS input_tokens,
                        usage_nonnegative_int(output_tokens) AS output_tokens,
                        usage_nonnegative_int(cache_read) AS cache_read_tokens,
                        usage_nonnegative_int(cache_write) AS cache_write_tokens,
                        usage_cost_total(
                            total_cost_usd,
                            billed_cost_usd,
                            estimated_cost_component_usd
                        ) AS cost_nanos,
                        usage_cost_billed(
                            total_cost_usd,
                            billed_cost_usd,
                            estimated_cost_component_usd
                        ) AS billed_cost_nanos,
                        usage_cost_estimated(
                            total_cost_usd,
                            billed_cost_usd,
                            estimated_cost_component_usd
                        ) AS estimated_cost_nanos,
                        COALESCE(NULLIF(cost_source, ''), 'none') AS cost_source,
                        usage_nonnegative_int(missing_cost_entries) AS missing_entries,
                        usage_invalid_int(epoch)
                            + usage_invalid_int(input_tokens)
                            + usage_invalid_int(output_tokens)
                            + usage_invalid_int(total_tokens)
                            + usage_invalid_int(cache_read)
                            + usage_invalid_int(cache_write)
                            + usage_invalid_int(missing_cost_entries)
                            + CASE WHEN usage_nonnegative_int(total_tokens)
                                != usage_nonnegative_int(input_tokens)
                                   + usage_nonnegative_int(output_tokens)
                              THEN 1 ELSE 0 END
                            + usage_cost_anomaly(
                                total_cost_usd,
                                billed_cost_usd,
                                estimated_cost_component_usd
                              ) AS row_anomalies
                    FROM sessions
                ), ranked AS (
                    SELECT
                        normalized.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY session_id, session_epoch
                            ORDER BY session_key
                        ) AS baseline_rank
                    FROM normalized
                )
                INSERT INTO usage_legacy_baselines (
                    session_id, session_epoch, agent_id, captured_at_ms,
                    input_tokens, output_tokens, total_tokens, cache_read_tokens,
                    cache_write_tokens, cost_nanos, billed_cost_nanos,
                    estimated_cost_nanos, cost_source, missing_cost_entries
                )
                SELECT
                    session_id,
                    session_epoch,
                    agent_id,
                    ?,
                    input_tokens,
                    output_tokens,
                    input_tokens + output_tokens,
                    cache_read_tokens,
                    cache_write_tokens,
                    cost_nanos,
                    billed_cost_nanos,
                    estimated_cost_nanos,
                    cost_source,
                    missing_entries + row_anomalies
                FROM ranked
                WHERE baseline_rank = 1
                """,
                (captured_at_ms,),
            )
            await conn.execute(
                """
                UPDATE usage_ledger_state
                SET anomaly_count =
                    COALESCE((
                        SELECT SUM(
                            usage_invalid_int(epoch)
                            + usage_invalid_int(input_tokens)
                            + usage_invalid_int(output_tokens)
                            + usage_invalid_int(total_tokens)
                            + usage_invalid_int(cache_read)
                            + usage_invalid_int(cache_write)
                            + usage_invalid_int(missing_cost_entries)
                            + CASE WHEN usage_nonnegative_int(total_tokens)
                                != usage_nonnegative_int(input_tokens)
                                   + usage_nonnegative_int(output_tokens)
                              THEN 1 ELSE 0 END
                            + usage_cost_anomaly(
                                total_cost_usd,
                                billed_cost_usd,
                                estimated_cost_component_usd
                              )
                        )
                        FROM sessions
                    ), 0)
                    + COALESCE((
                        SELECT SUM(duplicate_count - 1)
                        FROM (
                            SELECT COUNT(*) AS duplicate_count
                            FROM sessions
                            GROUP BY session_id, usage_nonnegative_int(epoch)
                            HAVING COUNT(*) > 1
                        )
                    ), 0)
                WHERE singleton_id = 1
                """
            )
            state = await self._get_usage_state_on_conn(conn)
            assert state is not None
            return state

    @staticmethod
    async def _repair_post_cutover_usage_baselines_on_conn(
        conn: Any,
        *,
        captured_at_ms: int,
        session_key: str | None = None,
    ) -> None:
        """Repair only generations whose ledger-only ancestry is provable.

        Cutover state and every then-current generation baseline are committed
        by one transaction. Consequently, a current ``(session_id, epoch)``
        missing from an existing cutover was created later and has no legacy
        usage, even when reset preserved an older session ``created_at`` value.
        Its first baseline is zero; for a later epoch, the baseline is the
        latest earlier baseline plus intervening live-provider ledger events.

        Mutable compatibility totals are intentionally ignored: normal Done
        turns may already be present there while cancelled turns may not be, so
        snapshotting or subtracting them is not authoritative.
        """

        await conn.execute(
            """
            WITH ranked_candidates AS (
                SELECT
                    s.session_key,
                    s.session_id,
                    usage_nonnegative_int(s.epoch) AS session_epoch,
                    COALESCE(NULLIF(s.agent_id, ''), 'main') AS agent_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY s.session_id, usage_nonnegative_int(s.epoch)
                        ORDER BY s.session_key
                    ) AS candidate_rank
                FROM sessions AS s
                JOIN usage_ledger_state AS state ON state.singleton_id = 1
                WHERE (? IS NULL OR s.session_key = ?)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM usage_legacy_baselines AS current_baseline
                      WHERE current_baseline.session_id = s.session_id
                        AND current_baseline.session_epoch =
                            usage_nonnegative_int(s.epoch)
                  )
            ), candidates AS (
                SELECT session_id, session_epoch, agent_id
                FROM ranked_candidates
                WHERE candidate_rank = 1
            ), anchor_epochs AS (
                SELECT
                    candidate.*,
                    MAX(baseline.session_epoch) AS anchor_epoch
                FROM candidates AS candidate
                LEFT JOIN usage_legacy_baselines AS baseline
                  ON baseline.session_id = candidate.session_id
                 AND baseline.session_epoch < candidate.session_epoch
                GROUP BY
                    candidate.session_id,
                    candidate.session_epoch,
                    candidate.agent_id
            ), anchored AS (
                SELECT
                    anchor.session_id,
                    anchor.session_epoch,
                    anchor.agent_id,
                    COALESCE(anchor.anchor_epoch, 0) AS ledger_from_epoch,
                    COALESCE(baseline.input_tokens, 0) AS base_input_tokens,
                    COALESCE(baseline.output_tokens, 0) AS base_output_tokens,
                    COALESCE(baseline.cache_read_tokens, 0) AS base_cache_read_tokens,
                    COALESCE(baseline.cache_write_tokens, 0) AS base_cache_write_tokens,
                    COALESCE(baseline.cost_nanos, 0) AS base_cost_nanos,
                    COALESCE(baseline.billed_cost_nanos, 0) AS base_billed_cost_nanos,
                    COALESCE(baseline.estimated_cost_nanos, 0)
                        AS base_estimated_cost_nanos,
                    COALESCE(baseline.cost_source, 'none') AS base_cost_source,
                    COALESCE(baseline.missing_cost_entries, 0)
                        AS base_missing_cost_entries
                FROM anchor_epochs AS anchor
                LEFT JOIN usage_legacy_baselines AS baseline
                  ON baseline.session_id = anchor.session_id
                 AND baseline.session_epoch = anchor.anchor_epoch
            ), rolled AS (
                SELECT
                    anchored.*,
                    COALESCE(SUM(CASE WHEN event.status = 'finalized'
                        THEN event.input_tokens ELSE 0 END), 0) AS live_input_tokens,
                    COALESCE(SUM(CASE WHEN event.status = 'finalized'
                        THEN event.output_tokens ELSE 0 END), 0) AS live_output_tokens,
                    COALESCE(SUM(CASE WHEN event.status = 'finalized'
                        THEN event.cache_read_tokens ELSE 0 END), 0)
                        AS live_cache_read_tokens,
                    COALESCE(SUM(CASE WHEN event.status = 'finalized'
                        THEN event.cache_write_tokens ELSE 0 END), 0)
                        AS live_cache_write_tokens,
                    COALESCE(SUM(CASE WHEN event.status = 'finalized'
                        THEN event.cost_nanos ELSE 0 END), 0) AS live_cost_nanos,
                    COALESCE(SUM(CASE WHEN event.status = 'finalized'
                        THEN event.billed_cost_nanos ELSE 0 END), 0)
                        AS live_billed_cost_nanos,
                    COALESCE(SUM(CASE WHEN event.status = 'finalized'
                        THEN event.estimated_cost_nanos ELSE 0 END), 0)
                        AS live_estimated_cost_nanos,
                    COALESCE(SUM(CASE
                        WHEN event.event_id IS NULL THEN 0
                        WHEN event.status = 'finalized' THEN event.missing_cost_entries
                        ELSE MAX(1, event.missing_cost_entries)
                    END), 0) AS live_missing_cost_entries,
                    COALESCE(SUM(CASE
                        WHEN event.status = 'finalized'
                         AND event.cost_source IN ('provider_billed', 'mixed')
                        THEN 1 ELSE 0 END), 0) AS live_provider_billed_entries,
                    COALESCE(SUM(CASE
                        WHEN event.status = 'finalized'
                         AND event.estimated_cost_nanos > 0
                        THEN 1 ELSE 0 END), 0) AS live_estimated_cost_entries
                FROM anchored
                LEFT JOIN usage_events AS event
                  ON event.session_id = anchored.session_id
                 AND event.session_epoch >= anchored.ledger_from_epoch
                 AND event.session_epoch < anchored.session_epoch
                 AND event.origin = 'live_provider'
                GROUP BY
                    anchored.session_id,
                    anchored.session_epoch,
                    anchored.agent_id,
                    anchored.ledger_from_epoch,
                    anchored.base_input_tokens,
                    anchored.base_output_tokens,
                    anchored.base_cache_read_tokens,
                    anchored.base_cache_write_tokens,
                    anchored.base_cost_nanos,
                    anchored.base_billed_cost_nanos,
                    anchored.base_estimated_cost_nanos,
                    anchored.base_cost_source,
                    anchored.base_missing_cost_entries
            ), classified AS (
                SELECT
                    rolled.*,
                    (
                        rolled.base_cost_source IN ('provider_billed', 'mixed')
                        OR rolled.base_billed_cost_nanos + rolled.live_billed_cost_nanos > 0
                        OR rolled.live_provider_billed_entries > 0
                    ) AS has_billed,
                    (
                        rolled.base_estimated_cost_nanos
                            + rolled.live_estimated_cost_nanos > 0
                        OR rolled.live_estimated_cost_entries > 0
                    ) AS has_estimate,
                    (
                        rolled.base_missing_cost_entries
                            + rolled.live_missing_cost_entries > 0
                    ) AS has_unavailable
                FROM rolled
            )
            INSERT OR IGNORE INTO usage_legacy_baselines (
                session_id, session_epoch, agent_id, captured_at_ms,
                input_tokens, output_tokens, total_tokens, cache_read_tokens,
                cache_write_tokens, cost_nanos, billed_cost_nanos,
                estimated_cost_nanos, cost_source, missing_cost_entries
            )
            SELECT
                session_id,
                session_epoch,
                agent_id,
                MAX(
                    ?,
                    (SELECT ledger_started_at_ms + 1
                     FROM usage_ledger_state WHERE singleton_id = 1)
                ),
                base_input_tokens + live_input_tokens,
                base_output_tokens + live_output_tokens,
                base_input_tokens + live_input_tokens
                    + base_output_tokens + live_output_tokens,
                base_cache_read_tokens + live_cache_read_tokens,
                base_cache_write_tokens + live_cache_write_tokens,
                base_cost_nanos + live_cost_nanos,
                base_billed_cost_nanos + live_billed_cost_nanos,
                base_estimated_cost_nanos + live_estimated_cost_nanos,
                CASE
                    WHEN has_billed + has_estimate + has_unavailable > 1 THEN 'mixed'
                    WHEN has_billed THEN 'provider_billed'
                    WHEN has_estimate THEN 'opensquilla_estimate'
                    WHEN has_unavailable THEN 'unavailable'
                    ELSE 'none'
                END,
                base_missing_cost_entries + live_missing_cost_entries
            FROM classified
            """,
            (session_key, session_key, captured_at_ms),
        )

    @staticmethod
    async def _ensure_usage_baseline_for_session_on_conn(
        conn: Any,
        *,
        session_key: str,
    ) -> None:
        """Snapshot a new durable session generation after ledger cutover.

        This helper is only called in the transaction that creates a generation,
        before its compatibility totals can contain that generation's live
        ledger events. Persisted missing generations are repaired separately
        only when their post-cutover ancestry is provable.
        """

        captured_at_ms = _now_ms()
        await conn.execute(
            """
            INSERT OR IGNORE INTO usage_legacy_baselines (
                session_id, session_epoch, agent_id, captured_at_ms,
                input_tokens, output_tokens, total_tokens, cache_read_tokens,
                cache_write_tokens, cost_nanos, billed_cost_nanos,
                estimated_cost_nanos, cost_source, missing_cost_entries
            )
            SELECT
                session_id,
                usage_nonnegative_int(epoch),
                COALESCE(NULLIF(agent_id, ''), 'main'),
                MAX(
                    ?,
                    (SELECT ledger_started_at_ms + 1
                     FROM usage_ledger_state WHERE singleton_id = 1)
                ),
                usage_nonnegative_int(input_tokens),
                usage_nonnegative_int(output_tokens),
                usage_nonnegative_int(input_tokens) + usage_nonnegative_int(output_tokens),
                usage_nonnegative_int(cache_read),
                usage_nonnegative_int(cache_write),
                usage_cost_total(
                    total_cost_usd, billed_cost_usd, estimated_cost_component_usd
                ),
                usage_cost_billed(
                    total_cost_usd, billed_cost_usd, estimated_cost_component_usd
                ),
                usage_cost_estimated(
                    total_cost_usd, billed_cost_usd, estimated_cost_component_usd
                ),
                COALESCE(NULLIF(cost_source, ''), 'none'),
                usage_nonnegative_int(missing_cost_entries)
                    + usage_invalid_int(epoch)
                    + usage_invalid_int(input_tokens)
                    + usage_invalid_int(output_tokens)
                    + usage_invalid_int(total_tokens)
                    + usage_invalid_int(cache_read)
                    + usage_invalid_int(cache_write)
                    + usage_invalid_int(missing_cost_entries)
                    + CASE WHEN usage_nonnegative_int(total_tokens)
                        != usage_nonnegative_int(input_tokens)
                           + usage_nonnegative_int(output_tokens)
                      THEN 1 ELSE 0 END
                    + usage_cost_anomaly(
                        total_cost_usd, billed_cost_usd, estimated_cost_component_usd
                      )
            FROM sessions
            WHERE session_key = ?
              AND EXISTS (SELECT 1 FROM usage_ledger_state WHERE singleton_id = 1)
            """,
            (captured_at_ms, session_key),
        )

    @_serialized_read
    async def get_usage_ledger_state(self) -> UsageLedgerState | None:
        async with self.conn.execute(
            "SELECT * FROM usage_ledger_state WHERE singleton_id = 1"
        ) as cur:
            row = await cur.fetchone()
        return None if row is None else _usage_state_from_row(row)

    @_serialized_read
    async def list_usage_legacy_baselines(self) -> list[UsageLegacyBaseline]:
        async with self.conn.execute(
            """
            SELECT * FROM usage_legacy_baselines
            ORDER BY captured_at_ms, session_id, session_epoch
            """
        ) as cur:
            rows = await cur.fetchall()
        return [_usage_baseline_from_row(row) for row in rows]

    @_serialized_read
    async def resolve_usage_session_keys(
        self,
        session_ids: Sequence[str],
    ) -> dict[str, str]:
        """Resolve only currently live session ids to navigable session keys."""

        unique_ids = list(dict.fromkeys(value for value in session_ids if value))
        resolved: dict[str, str] = {}
        for start in range(0, len(unique_ids), _SQLITE_VARIABLE_CHUNK_SIZE):
            chunk = unique_ids[start : start + _SQLITE_VARIABLE_CHUNK_SIZE]
            placeholders = ", ".join("?" for _ in chunk)
            async with self.conn.execute(
                "SELECT session_id, session_key FROM sessions "
                f"WHERE session_id IN ({placeholders}) "  # noqa: S608
                "ORDER BY session_id, session_key",
                chunk,
            ) as cur:
                rows = await cur.fetchall()
            for row in rows:
                resolved.setdefault(str(row["session_id"]), str(row["session_key"]))
        return resolved

    @_serialized_read
    async def query_usage_events(
        self,
        from_ms: int | None,
        to_ms: int | None,
        statuses: Sequence[UsageEventStatus] = ("finalized",),
        session_id: str | None = None,
    ) -> list[UsageEventRecord]:
        """Read terminal events whose completion time is in ``[from_ms, to_ms)``."""

        if from_ms is not None and from_ms < 0:
            raise ValueError("from_ms must be non-negative")
        if to_ms is not None and to_ms < 0:
            raise ValueError("to_ms must be non-negative")
        if from_ms is not None and to_ms is not None and from_ms > to_ms:
            raise ValueError("from_ms must not exceed to_ms")
        allowed_statuses = {"started", "finalized", "unknown"}
        if any(status not in allowed_statuses for status in statuses):
            raise ValueError("unsupported usage event status")
        if not statuses:
            return []
        clauses = [f"status IN ({', '.join('?' for _ in statuses)})"]
        params: list[Any] = list(statuses)
        if from_ms is not None:
            clauses.append("completed_at_ms >= ?")
            params.append(from_ms)
        if to_ms is not None:
            clauses.append("completed_at_ms < ?")
            params.append(to_ms)
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        # Keep the range/order traversal anchored on completion time.  Without
        # the hint SQLite can prefer the recovery-oriented status/started index
        # and materialize a temporary sort, which degrades as the ledger grows.
        range_index = (
            "idx_usage_events_session_completed"
            if session_id is not None
            else "idx_usage_events_completed"
        )
        sql = (
            f"SELECT * FROM usage_events INDEXED BY {range_index} WHERE "  # noqa: S608
            + " AND ".join(clauses)
            + " ORDER BY completed_at_ms, event_id"
        )
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_usage_event_from_row(row) for row in rows]

    @_serialized_read
    async def get_turn_usage_projection(
        self,
        *,
        session_id: str,
        session_epoch: int,
        turn_id: str,
    ) -> dict[str, Any] | None:
        """Project one turn's durable provider-call ledger into chat metadata.

        Finalized calls contribute measured usage. Started/unknown calls never
        fabricate token counts, but they do make the projection explicitly
        incomplete so cancellation cannot look fully accounted.
        """

        async with self.conn.execute(
            """
            SELECT
                COUNT(*) AS event_count,
                COALESCE(SUM(CASE WHEN status = 'finalized' THEN input_tokens ELSE 0 END), 0)
                    AS input_tokens,
                COALESCE(SUM(CASE WHEN status = 'finalized' THEN output_tokens ELSE 0 END), 0)
                    AS output_tokens,
                COALESCE(SUM(CASE WHEN status = 'finalized' THEN reasoning_tokens ELSE 0 END), 0)
                    AS reasoning_tokens,
                COALESCE(SUM(CASE WHEN status = 'finalized' THEN cache_read_tokens ELSE 0 END), 0)
                    AS cache_read_tokens,
                COALESCE(SUM(CASE WHEN status = 'finalized' THEN cache_write_tokens ELSE 0 END), 0)
                    AS cache_write_tokens,
                COALESCE(SUM(CASE WHEN status = 'finalized' THEN total_tokens ELSE 0 END), 0)
                    AS total_tokens,
                COALESCE(SUM(CASE WHEN status = 'finalized' THEN cost_nanos ELSE 0 END), 0)
                    AS cost_nanos,
                COALESCE(SUM(CASE WHEN status = 'finalized' THEN billed_cost_nanos ELSE 0 END), 0)
                    AS billed_cost_nanos,
                COALESCE(SUM(CASE WHEN status = 'finalized'
                    THEN estimated_cost_nanos ELSE 0 END), 0)
                    AS estimated_cost_nanos,
                COALESCE(SUM(CASE
                    WHEN status = 'finalized' THEN missing_cost_entries
                    ELSE MAX(1, missing_cost_entries)
                END), 0) AS missing_cost_entries,
                COALESCE(SUM(CASE WHEN status != 'finalized' THEN 1 ELSE 0 END), 0)
                    AS unknown_event_count,
                COALESCE(SUM(CASE
                    WHEN status = 'finalized' AND cost_source IN ('provider_billed', 'mixed')
                    THEN 1 ELSE 0 END), 0) AS provider_billed_entries,
                COALESCE(SUM(CASE
                    WHEN status = 'finalized' AND estimated_cost_nanos > 0
                    THEN 1 ELSE 0 END), 0) AS estimated_cost_entries,
                COALESCE(SUM(CASE
                    WHEN status = 'finalized' AND coverage_status != 'complete'
                    THEN 1 ELSE 0 END), 0) AS incomplete_finalized_count
            FROM usage_events
            WHERE session_id = ? AND session_epoch = ? AND turn_id = ?
              AND origin = 'live_provider'
            """,
            (session_id, session_epoch, turn_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None or int(row["event_count"] or 0) == 0:
            return None

        async with self.conn.execute(
            """
            SELECT provider, model
            FROM usage_events
            WHERE session_id = ? AND session_epoch = ? AND turn_id = ?
              AND origin = 'live_provider'
            ORDER BY call_index DESC, event_id DESC
            LIMIT 1
            """,
            (session_id, session_epoch, turn_id),
        ) as cur:
            identity = await cur.fetchone()

        billed_cost = nanos_to_usd(int(row["billed_cost_nanos"] or 0))
        estimated_cost = nanos_to_usd(int(row["estimated_cost_nanos"] or 0))
        missing_entries = max(0, int(row["missing_cost_entries"] or 0))
        unknown_events = max(0, int(row["unknown_event_count"] or 0))
        incomplete = max(0, int(row["incomplete_finalized_count"] or 0))
        cost_source = rollup_cost_source(
            billed_cost_usd=billed_cost,
            estimated_cost_component_usd=estimated_cost,
            missing_cost_entries=missing_entries,
            provider_billed_entries=max(0, int(row["provider_billed_entries"] or 0)),
            estimated_cost_entries=max(0, int(row["estimated_cost_entries"] or 0)),
        )
        coverage_status = "usage_unknown" if unknown_events or incomplete else "complete"
        return {
            "input_tokens": max(0, int(row["input_tokens"] or 0)),
            "output_tokens": max(0, int(row["output_tokens"] or 0)),
            "reasoning_tokens": max(0, int(row["reasoning_tokens"] or 0)),
            "cached_tokens": max(0, int(row["cache_read_tokens"] or 0)),
            "cache_write_tokens": max(0, int(row["cache_write_tokens"] or 0)),
            "total_tokens": max(0, int(row["total_tokens"] or 0)),
            "cost_usd": nanos_to_usd(int(row["cost_nanos"] or 0)),
            "billed_cost": billed_cost,
            "estimated_cost_component_usd": estimated_cost,
            "cost_source": cost_source,
            "missing_cost_entries": missing_entries,
            "coverage_status": coverage_status,
            "usage_unknown": coverage_status != "complete",
            "unknown_usage_events": unknown_events,
            "provider": str(identity["provider"] or "") if identity is not None else "",
            "model": str(identity["model"] or "") if identity is not None else "",
        }

    @_serialized_read
    async def get_turn_usage_projections(
        self,
        *,
        session_id: str,
        session_epoch: int,
        turn_ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        """Batch-project ledger usage for a bounded transcript page."""

        stable_turn_ids = list(dict.fromkeys(value for value in turn_ids if value))
        if not stable_turn_ids:
            return {}
        if len(stable_turn_ids) > _SQLITE_VARIABLE_CHUNK_SIZE:
            raise ValueError("too many turn ids for one usage projection page")
        placeholders = ", ".join("?" for _ in stable_turn_ids)
        async with self.conn.execute(
            f"""
            SELECT * FROM usage_events
            WHERE session_id = ? AND session_epoch = ?
              AND origin = 'live_provider'
              AND turn_id IN ({placeholders})
            ORDER BY turn_id, call_index, event_id
            """,  # noqa: S608 - placeholders are generated from a bounded list
            (session_id, session_epoch, *stable_turn_ids),
        ) as cur:
            rows = await cur.fetchall()

        grouped: dict[str, list[UsageEventRecord]] = {}
        for row in rows:
            event = _usage_event_from_row(row)
            if event.turn_id:
                grouped.setdefault(event.turn_id, []).append(event)

        projections: dict[str, dict[str, Any]] = {}
        for stable_turn_id, events in grouped.items():
            totals = {
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 0,
                "cost_nanos": 0,
                "billed_cost_nanos": 0,
                "estimated_cost_nanos": 0,
                "missing_cost_entries": 0,
            }
            unknown_events = 0
            incomplete = False
            provider_billed_entries = 0
            estimated_cost_entries = 0
            for event in events:
                if event.status != "finalized":
                    unknown_events += 1
                    totals["missing_cost_entries"] += max(
                        1, int(event.missing_cost_entries or 0)
                    )
                    continue
                totals["input_tokens"] += max(0, int(event.input_tokens or 0))
                totals["output_tokens"] += max(0, int(event.output_tokens or 0))
                totals["reasoning_tokens"] += max(0, int(event.reasoning_tokens or 0))
                totals["cached_tokens"] += max(0, int(event.cache_read_tokens or 0))
                totals["cache_write_tokens"] += max(
                    0, int(event.cache_write_tokens or 0)
                )
                totals["total_tokens"] += max(0, int(event.total_tokens or 0))
                totals["cost_nanos"] += max(0, int(event.cost_nanos or 0))
                totals["billed_cost_nanos"] += max(
                    0, int(event.billed_cost_nanos or 0)
                )
                totals["estimated_cost_nanos"] += max(
                    0, int(event.estimated_cost_nanos or 0)
                )
                totals["missing_cost_entries"] += max(
                    0, int(event.missing_cost_entries or 0)
                )
                provider_billed_entries += int(
                    event.cost_source in {"provider_billed", "mixed"}
                )
                estimated_cost_entries += int(event.estimated_cost_nanos > 0)
                incomplete = incomplete or event.coverage_status != "complete"

            billed_cost = nanos_to_usd(totals["billed_cost_nanos"])
            estimated_cost = nanos_to_usd(totals["estimated_cost_nanos"])
            coverage_status = (
                "usage_unknown" if unknown_events or incomplete else "complete"
            )
            latest = events[-1]
            projections[stable_turn_id] = {
                "input_tokens": totals["input_tokens"],
                "output_tokens": totals["output_tokens"],
                "reasoning_tokens": totals["reasoning_tokens"],
                "cached_tokens": totals["cached_tokens"],
                "cache_write_tokens": totals["cache_write_tokens"],
                "total_tokens": totals["total_tokens"],
                "cost_usd": nanos_to_usd(totals["cost_nanos"]),
                "billed_cost": billed_cost,
                "estimated_cost_component_usd": estimated_cost,
                "cost_source": rollup_cost_source(
                    billed_cost_usd=billed_cost,
                    estimated_cost_component_usd=estimated_cost,
                    missing_cost_entries=totals["missing_cost_entries"],
                    provider_billed_entries=provider_billed_entries,
                    estimated_cost_entries=estimated_cost_entries,
                ),
                "missing_cost_entries": totals["missing_cost_entries"],
                "coverage_status": coverage_status,
                "usage_unknown": coverage_status != "complete",
                "unknown_usage_events": unknown_events,
                "provider": latest.provider or "",
                "model": latest.model or "",
            }
        return projections

    @_serialized_read
    async def get_turn_ids_continuing_after_cursor(
        self,
        *,
        session_id: str,
        created_at: int,
        entry_id: int,
        turn_ids: Sequence[str],
    ) -> set[str]:
        """Return the turns whose assistant history continues past a page.

        Canonical history pages are contiguous keyset slices over
        ``(created_at, id)``, so a turn with no assistant row after the page's
        last cursor already holds its terminal row inside that page. Only rows
        beyond the cursor can move terminality onto a later page, and the
        newest page has none, so the common read is one empty index seek
        rather than a scan of the whole session.
        """

        stable_turn_ids = list(dict.fromkeys(value for value in turn_ids if value))
        if not stable_turn_ids:
            return set()
        if len(stable_turn_ids) > _SQLITE_VARIABLE_CHUNK_SIZE - 8:
            raise ValueError("too many turn ids for one usage continuation probe")
        requested_rows = ", ".join("(?)" for _ in stable_turn_ids)
        sql = f"""
            WITH requested(turn_id) AS (
                VALUES {requested_rows}
            ),
            continuing AS (
                SELECT json_extract(turn_context, '$.turn_id') AS turn_id
                FROM transcript_entries
                WHERE session_id = ?
                  AND (created_at > ? OR (created_at = ? AND id > ?))
                  AND role = 'assistant'
                  AND json_valid(turn_context)
                UNION ALL
                SELECT json_extract(turn_context, '$.turn_id') AS turn_id
                FROM compacted_transcript_entries
                WHERE session_id = ?
                  AND (created_at > ? OR (created_at = ? AND original_entry_id > ?))
                  AND role = 'assistant'
                  AND json_valid(turn_context)
            )
            SELECT DISTINCT turn_id
            FROM continuing
            WHERE turn_id IN (SELECT turn_id FROM requested)
        """  # noqa: S608 - placeholders are generated from a bounded list
        async with self.conn.execute(
            sql,
            (
                *stable_turn_ids,
                session_id,
                created_at,
                created_at,
                entry_id,
                session_id,
                created_at,
                created_at,
                entry_id,
            ),
        ) as cur:
            rows = await cur.fetchall()

        return {str(row["turn_id"] or "") for row in rows if row["turn_id"]}

    async def reconcile_session_usage_totals_from_ledger(
        self,
        *,
        session_key: str,
        expected_epoch: int,
    ) -> SessionNode | None:
        """Set compatibility session totals from the ledger, idempotently.

        The cutover baseline owns pre-ledger totals. Only live provider events
        are added, because backfilled transcript events describe usage already
        captured by that baseline.
        """

        stable_key = canonicalize_session_key(session_key)
        async with self._write_transaction("reconcile_session_usage_totals") as conn:
            async with conn.execute(
                "SELECT * FROM sessions WHERE session_key = ?",
                (stable_key,),
            ) as cur:
                session_row = await cur.fetchone()
            if session_row is None:
                return None
            actual_epoch = max(0, int(session_row["epoch"] or 0))
            if actual_epoch != expected_epoch:
                await self._raise_stale_epoch(
                    conn,
                    session_key=stable_key,
                    expected_epoch=expected_epoch,
                )
            session_id = str(session_row["session_id"])

            async with conn.execute(
                """
                SELECT * FROM usage_legacy_baselines
                WHERE session_id = ? AND session_epoch = ?
                """,
                (session_id, expected_epoch),
            ) as cur:
                baseline = await cur.fetchone()
            if baseline is None:
                await self._repair_post_cutover_usage_baselines_on_conn(
                    conn,
                    captured_at_ms=_now_ms(),
                    session_key=stable_key,
                )
                async with conn.execute(
                    """
                    SELECT * FROM usage_legacy_baselines
                    WHERE session_id = ? AND session_epoch = ?
                    """,
                    (session_id, expected_epoch),
                ) as cur:
                    baseline = await cur.fetchone()
            if baseline is None:
                # No cutover means this storage is not ledger-authoritative;
                # preserve the legacy DoneEvent rollup path.
                return None

            async with conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN status = 'finalized' THEN input_tokens ELSE 0 END), 0)
                        AS input_tokens,
                    COALESCE(SUM(CASE WHEN status = 'finalized' THEN output_tokens ELSE 0 END), 0)
                        AS output_tokens,
                    COALESCE(SUM(CASE WHEN status = 'finalized'
                        THEN cache_read_tokens ELSE 0 END), 0)
                        AS cache_read_tokens,
                    COALESCE(SUM(CASE WHEN status = 'finalized'
                        THEN cache_write_tokens ELSE 0 END), 0)
                        AS cache_write_tokens,
                    COALESCE(SUM(CASE WHEN status = 'finalized' THEN cost_nanos ELSE 0 END), 0)
                        AS cost_nanos,
                    COALESCE(SUM(CASE WHEN status = 'finalized'
                        THEN billed_cost_nanos ELSE 0 END), 0)
                        AS billed_cost_nanos,
                    COALESCE(SUM(CASE WHEN status = 'finalized'
                        THEN estimated_cost_nanos ELSE 0 END), 0)
                        AS estimated_cost_nanos,
                    COALESCE(SUM(CASE
                        WHEN status = 'finalized' THEN missing_cost_entries
                        ELSE MAX(1, missing_cost_entries)
                    END), 0) AS missing_cost_entries,
                    COALESCE(SUM(CASE
                        WHEN status = 'finalized' AND cost_source IN ('provider_billed', 'mixed')
                        THEN 1 ELSE 0 END), 0) AS provider_billed_entries,
                    COALESCE(SUM(CASE
                        WHEN status = 'finalized' AND estimated_cost_nanos > 0
                        THEN 1 ELSE 0 END), 0) AS estimated_cost_entries
                FROM usage_events
                WHERE session_id = ? AND session_epoch = ? AND origin = 'live_provider'
                """,
                (session_id, expected_epoch),
            ) as cur:
                live = await cur.fetchone()
            assert live is not None
            async with conn.execute(
                """
                SELECT provider, model
                FROM usage_events
                WHERE session_id = ? AND session_epoch = ?
                  AND origin = 'live_provider' AND status = 'finalized'
                ORDER BY completed_at_ms DESC, call_index DESC, event_id DESC
                LIMIT 1
                """,
                (session_id, expected_epoch),
            ) as cur:
                latest_identity = await cur.fetchone()

            input_tokens = max(0, int(baseline["input_tokens"] or 0)) + max(
                0, int(live["input_tokens"] or 0)
            )
            output_tokens = max(0, int(baseline["output_tokens"] or 0)) + max(
                0, int(live["output_tokens"] or 0)
            )
            cache_read = max(0, int(baseline["cache_read_tokens"] or 0)) + max(
                0, int(live["cache_read_tokens"] or 0)
            )
            cache_write = max(0, int(baseline["cache_write_tokens"] or 0)) + max(
                0, int(live["cache_write_tokens"] or 0)
            )
            cost_nanos = max(0, int(baseline["cost_nanos"] or 0)) + max(
                0, int(live["cost_nanos"] or 0)
            )
            billed_nanos = max(0, int(baseline["billed_cost_nanos"] or 0)) + max(
                0, int(live["billed_cost_nanos"] or 0)
            )
            estimated_nanos = max(0, int(baseline["estimated_cost_nanos"] or 0)) + max(
                0, int(live["estimated_cost_nanos"] or 0)
            )
            missing_entries = max(0, int(baseline["missing_cost_entries"] or 0)) + max(
                0, int(live["missing_cost_entries"] or 0)
            )
            baseline_source = str(baseline["cost_source"] or "none")
            cost_source = rollup_cost_source(
                billed_cost_usd=nanos_to_usd(billed_nanos),
                estimated_cost_component_usd=nanos_to_usd(estimated_nanos),
                missing_cost_entries=missing_entries,
                provider_billed_entries=(
                    int(baseline_source in {"provider_billed", "mixed"})
                    + max(0, int(live["provider_billed_entries"] or 0))
                ),
                estimated_cost_entries=(
                    int(int(baseline["estimated_cost_nanos"] or 0) > 0)
                    + max(0, int(live["estimated_cost_entries"] or 0))
                ),
            )
            await conn.execute(
                """
                UPDATE sessions
                SET input_tokens = ?, output_tokens = ?, total_tokens = ?,
                    total_tokens_fresh = 1, estimated_cost_usd = ?, total_cost_usd = ?,
                    billed_cost_usd = ?, estimated_cost_component_usd = ?,
                    cost_source = ?, missing_cost_entries = ?,
                    cache_read = ?, cache_write = ?,
                    model_override = COALESCE(?, model_override),
                    model_provider = COALESCE(?, model_provider)
                WHERE session_key = ? AND epoch = ?
                """,
                (
                    input_tokens,
                    output_tokens,
                    input_tokens + output_tokens,
                    nanos_to_usd(cost_nanos),
                    nanos_to_usd(cost_nanos),
                    nanos_to_usd(billed_nanos),
                    nanos_to_usd(estimated_nanos),
                    cost_source,
                    missing_entries,
                    cache_read,
                    cache_write,
                    (
                        str(latest_identity["model"])
                        if latest_identity is not None and latest_identity["model"]
                        else None
                    ),
                    (
                        str(latest_identity["provider"])
                        if latest_identity is not None and latest_identity["provider"]
                        else None
                    ),
                    stable_key,
                    expected_epoch,
                ),
            )
            async with conn.execute(
                "SELECT * FROM sessions WHERE session_key = ?",
                (stable_key,),
            ) as cur:
                updated = await cur.fetchone()
            assert updated is not None
            return SessionNode(**_deserialize_row(dict(updated)))

    @_serialized_read
    async def query_usage_event_items(
        self,
        event_ids: Sequence[str],
    ) -> list[UsageEventItem]:
        if not event_ids:
            return []
        unique_ids = list(dict.fromkeys(event_ids))
        items: list[UsageEventItem] = []
        for start in range(0, len(unique_ids), _SQLITE_VARIABLE_CHUNK_SIZE):
            chunk = unique_ids[start : start + _SQLITE_VARIABLE_CHUNK_SIZE]
            placeholders = ", ".join("?" for _ in chunk)
            async with self.conn.execute(
                "SELECT * FROM usage_event_items "
                f"WHERE event_id IN ({placeholders}) ORDER BY event_id, ordinal",  # noqa: S608
                chunk,
            ) as cur:
                rows = await cur.fetchall()
            items.extend(_usage_item_from_row(row) for row in rows)
        return items

    @_serialized_read
    async def query_usage_item_billing_receipts(
        self,
        event_ids: Sequence[str],
    ) -> list[UsageItemBillingReceipt]:
        """Return native receipts for the requested physical usage items."""

        if not event_ids:
            return []
        unique_ids = list(dict.fromkeys(event_ids))
        receipts: list[UsageItemBillingReceipt] = []
        for start in range(0, len(unique_ids), _SQLITE_VARIABLE_CHUNK_SIZE):
            chunk = unique_ids[start : start + _SQLITE_VARIABLE_CHUNK_SIZE]
            placeholders = ", ".join("?" for _ in chunk)
            async with self.conn.execute(
                "SELECT * FROM usage_item_billing_receipts "
                f"WHERE event_id IN ({placeholders}) ORDER BY event_id, ordinal",  # noqa: S608
                chunk,
            ) as cur:
                rows = await cur.fetchall()
            receipts.extend(_usage_billing_receipt_from_row(row) for row in rows)
        return receipts

    @_serialized_read
    async def get_usage_billing_receipt_state(
        self,
    ) -> UsageBillingReceiptState | None:
        """Return the native-receipt coverage cutover, if tracking is installed."""

        async with self.conn.execute(
            "SELECT * FROM usage_billing_receipt_state WHERE singleton_id = 1"
        ) as cur:
            row = await cur.fetchone()
        return None if row is None else _usage_billing_receipt_state_from_row(row)

    @_serialized_read
    async def get_usage_backfill_batch(
        self,
        *,
        before_ms: int,
        after: UsageBackfillCursor | None = None,
        limit: int = 500,
    ) -> UsageBackfillBatch:
        """Return canonical pre-cutover assistant rows in stable cursor order.

        Active and compacted copies are deduplicated by ``session_id`` and
        ``message_id``. Current session metadata is joined by ``session_id`` so
        the worker can reject inherited fork rows without a stable ``turn_id``.
        """

        if before_ms < 0:
            raise ValueError("before_ms must be non-negative")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        cursor_clause = ""
        cursor_params: list[Any] = []
        if after is not None:
            if after.created_at_ms < 0 or not after.session_id or not after.message_id:
                raise ValueError("backfill cursor fields must be valid")
            cursor_clause = (
                "AND (created_at, session_id, message_id) > (?, ?, ?)"
            )
            cursor_params.extend(
                (after.created_at_ms, after.session_id, after.message_id)
            )

        # Read at most one page from each indexed canonical source, then merge
        # and deduplicate in memory. This keeps every page O(log N + limit)
        # instead of rerunning ROW_NUMBER over the complete history.
        source_rows: list[tuple[int, dict[str, Any]]] = []
        source_full = False
        for priority, table in enumerate(
            ("transcript_entries", "compacted_transcript_entries")
        ):
            params = [before_ms, *cursor_params, limit + 1]
            sql = f"""
                SELECT session_id, message_id, created_at, turn_usage, turn_context
                FROM {table}
                WHERE role = 'assistant' AND turn_usage IS NOT NULL
                  AND created_at < ? {cursor_clause}
                ORDER BY created_at, session_id, message_id
                LIMIT ?
            """  # noqa: S608 - table is selected from fixed internal literals.
            async with self.conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
            source_full = source_full or len(rows) > limit
            source_rows.extend((priority, dict(row)) for row in rows)

        canonical: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
        for priority, row in source_rows:
            identity = (str(row["session_id"]), str(row["message_id"]))
            current = canonical.get(identity)
            if current is None or priority < current[0]:
                canonical[identity] = (priority, row)
        merged = sorted(
            canonical.values(),
            key=lambda value: (
                int(value[1]["created_at"]),
                str(value[1]["session_id"]),
                str(value[1]["message_id"]),
                value[0],
            ),
        )
        selected = [row for _priority, row in merged[:limit]]
        exhausted = not source_full and len(merged) <= limit

        metadata: dict[str, tuple[str, int, bool]] = {}
        session_ids = list(dict.fromkeys(str(row["session_id"]) for row in selected))
        for start in range(0, len(session_ids), _SQLITE_VARIABLE_CHUNK_SIZE):
            chunk = session_ids[start : start + _SQLITE_VARIABLE_CHUNK_SIZE]
            placeholders = ", ".join("?" for _ in chunk)
            async with self.conn.execute(
                "SELECT session_id, agent_id, epoch, forked_from_parent "
                "FROM sessions "
                f"WHERE session_id IN ({placeholders}) "  # noqa: S608
                "ORDER BY session_id, session_key",
                chunk,
            ) as cur:
                session_rows = await cur.fetchall()
            for row in session_rows:
                metadata.setdefault(
                    str(row["session_id"]),
                    (
                        str(row["agent_id"] or "main"),
                        max(0, int(row["epoch"] or 0)),
                        bool(row["forked_from_parent"]),
                    ),
                )

        entries = tuple(
            UsageBackfillEntry(
                cursor=UsageBackfillCursor(
                    created_at_ms=int(row["created_at"]),
                    session_id=str(row["session_id"]),
                    message_id=str(row["message_id"]),
                ),
                agent_id=metadata.get(str(row["session_id"]), ("main", 0, False))[0],
                session_epoch=metadata.get(
                    str(row["session_id"]), ("main", 0, False)
                )[1],
                forked_from_parent=metadata.get(
                    str(row["session_id"]), ("main", 0, False)
                )[2],
                turn_usage=_json_object_or_none(row["turn_usage"]),
                turn_context=_json_object_or_none(row["turn_context"]),
                session_metadata_missing=str(row["session_id"]) not in metadata,
            )
            for row in selected
        )
        return UsageBackfillBatch(
            entries=entries,
            next_cursor=entries[-1].cursor if entries else after,
            exhausted=exhausted,
        )

    async def update_usage_backfill_progress(
        self,
        *,
        status: UsageBackfillStatus,
        cursor: UsageBackfillCursor | None = None,
        backfilled_event_count_delta: int = 0,
        backfilled_cost_nanos_delta: int = 0,
        anomaly_count_delta: int = 0,
        last_error_code: str | None = None,
        now_ms: int | None = None,
    ) -> UsageLedgerState:
        """Update resumable worker state when no event batch is being committed."""

        allowed_statuses = {"pending", "running", "complete", "partial", "failed"}
        if status not in allowed_statuses:
            raise ValueError("unsupported usage backfill status")
        for label, value in (
            ("backfilled_event_count_delta", backfilled_event_count_delta),
            ("backfilled_cost_nanos_delta", backfilled_cost_nanos_delta),
            ("anomaly_count_delta", anomaly_count_delta),
        ):
            if value < 0:
                raise ValueError(f"{label} must be non-negative")
        updated_at_ms = _now_ms() if now_ms is None else now_ms
        if updated_at_ms < 0:
            raise ValueError("now_ms must be non-negative")
        if last_error_code is not None and (
            not last_error_code or len(last_error_code) > 128
        ):
            raise ValueError("last_error_code must be a stable code up to 128 characters")
        async with self._write_transaction("update_usage_backfill_progress") as conn:
            state = await self._get_usage_state_on_conn(conn)
            if state is None:
                raise RuntimeError("usage ledger must be initialized before backfill")
            self._validate_usage_backfill_cursor_advance(state, cursor)
            effective_cursor = cursor or self._cursor_from_usage_state(state)
            await conn.execute(
                """
                UPDATE usage_ledger_state
                SET backfill_status = ?, cursor_created_at_ms = ?,
                    cursor_session_id = ?, cursor_message_id = ?,
                    backfilled_event_count = backfilled_event_count + ?,
                    backfilled_cost_nanos = backfilled_cost_nanos + ?,
                    anomaly_count = anomaly_count + ?, last_error_code = ?,
                    updated_at_ms = ?
                WHERE singleton_id = 1
                """,
                (
                    status,
                    effective_cursor.created_at_ms if effective_cursor else None,
                    effective_cursor.session_id if effective_cursor else None,
                    effective_cursor.message_id if effective_cursor else None,
                    backfilled_event_count_delta,
                    backfilled_cost_nanos_delta,
                    anomaly_count_delta,
                    last_error_code,
                    updated_at_ms,
                ),
            )
            updated = await self._get_usage_state_on_conn(conn)
            assert updated is not None
            return updated

    async def _get_usage_state_on_conn(self, conn: Any) -> UsageLedgerState | None:
        async with conn.execute(
            "SELECT * FROM usage_ledger_state WHERE singleton_id = 1"
        ) as cur:
            row = await cur.fetchone()
        return None if row is None else _usage_state_from_row(row)

    @staticmethod
    def _cursor_from_usage_state(state: UsageLedgerState) -> UsageBackfillCursor | None:
        if (
            state.cursor_created_at_ms is None
            or state.cursor_session_id is None
            or state.cursor_message_id is None
        ):
            return None
        return UsageBackfillCursor(
            state.cursor_created_at_ms,
            state.cursor_session_id,
            state.cursor_message_id,
        )

    @classmethod
    def _validate_usage_backfill_cursor_advance(
        cls,
        state: UsageLedgerState,
        cursor: UsageBackfillCursor | None,
    ) -> None:
        if cursor is not None and (
            cursor.created_at_ms < 0 or not cursor.session_id or not cursor.message_id
        ):
            raise ValueError("backfill cursor fields must be valid")
        previous = cls._cursor_from_usage_state(state)
        if cursor is not None and previous is not None and cursor < previous:
            raise ValueError("backfill cursor must not move backwards")

    async def apply_usage_backfill_batch(
        self,
        writes: Sequence[UsageBackfillWrite],
        *,
        cursor: UsageBackfillCursor | None,
        exhausted: bool,
        anomaly_delta: int = 0,
        now_ms: int | None = None,
    ) -> UsageLedgerState:
        """Atomically persist historical events, their items, and worker cursor.

        Retrying an ambiguously committed batch does not increment state totals
        twice because exact finalized events are treated as idempotent replays.
        """

        if anomaly_delta < 0:
            raise ValueError("anomaly_delta must be non-negative")
        updated_at_ms = _now_ms() if now_ms is None else now_ms
        if updated_at_ms < 0:
            raise ValueError("now_ms must be non-negative")
        for write in writes:
            validate_usage_event_start(write.start)
            validate_usage_completion(write.completion)
            if write.start.origin != "backfilled_turn":
                raise ValueError("backfill events must use origin='backfilled_turn'")
            for item in write.items:
                validate_usage_item(item, event_id=write.start.event_id)

        async with self._write_transaction("apply_usage_backfill_batch") as conn:
            state = await self._get_usage_state_on_conn(conn)
            if state is None:
                raise RuntimeError("usage ledger must be initialized before backfill")
            self._validate_usage_backfill_cursor_advance(state, cursor)
            effective_cursor = cursor or self._cursor_from_usage_state(state)
            added_count = 0
            added_cost_nanos = 0
            implicit_anomalies = 0
            for write in writes:
                if write.completion.completed_at_ms >= state.ledger_started_at_ms:
                    raise ValueError("backfill events must complete before ledger cutover")
                if not self._usage_items_match_completion(
                    write.items,
                    write.completion,
                ):
                    implicit_anomalies += 1
                    continue
                existing = await self._get_usage_event_on_conn(
                    conn, event_id=write.start.event_id
                )
                if (
                    existing is not None
                    and existing.origin == "backfilled_turn"
                    and write.start.turn_id
                    and existing.turn_id == write.start.turn_id
                    and existing.execution_id == write.start.execution_id
                    and existing.call_index == write.start.call_index
                ):
                    if existing.status == "finalized":
                        try:
                            self._assert_usage_completion_matches(
                                existing, write.completion
                            )
                            existing_items = await self._get_usage_items_on_conn(
                                conn, existing.event_id
                            )
                            if existing_items != sorted(
                                write.items, key=lambda item: item.ordinal
                            ):
                                raise UsageLedgerConflictError(
                                    "fork copy has different model usage items"
                                )
                        except UsageLedgerConflictError:
                            implicit_anomalies += 1
                        # A proven inherited fork copy is attribution of the
                        # same physical spend, never another billable event.
                        continue
                await self._start_usage_event_on_conn(conn, write.start)
                _record, changed = await self._finalize_usage_event_on_conn(
                    conn,
                    write.start.event_id,
                    write.completion,
                    write.items,
                    (),
                )
                if changed:
                    added_count += 1
                    added_cost_nanos += write.completion.cost_nanos

            total_anomaly_delta = anomaly_delta + implicit_anomalies
            cumulative_anomalies = state.anomaly_count + total_anomaly_delta
            if exhausted:
                next_status = "partial" if cumulative_anomalies else "complete"
            else:
                next_status = "running"
            await conn.execute(
                """
                UPDATE usage_ledger_state
                SET backfill_status = ?, cursor_created_at_ms = ?,
                    cursor_session_id = ?, cursor_message_id = ?,
                    backfilled_event_count = backfilled_event_count + ?,
                    backfilled_cost_nanos = backfilled_cost_nanos + ?,
                    anomaly_count = anomaly_count + ?, last_error_code = NULL,
                    updated_at_ms = ?
                WHERE singleton_id = 1
                """,
                (
                    next_status,
                    effective_cursor.created_at_ms if effective_cursor else None,
                    effective_cursor.session_id if effective_cursor else None,
                    effective_cursor.message_id if effective_cursor else None,
                    added_count,
                    added_cost_nanos,
                    total_anomaly_delta,
                    updated_at_ms,
                ),
            )
            updated = await self._get_usage_state_on_conn(conn)
            assert updated is not None
            return updated

    # ── Session CRUD ────────────────────────────────────────────────────────

    async def create_or_restore_project_workspace(
        self,
        *,
        path: str,
        path_key: str,
        display_name: str,
        trusted_at: int | None,
        now_ms: int | None = None,
    ) -> ProjectWorkspace:
        now = _now_ms() if now_ms is None else int(now_ms)
        async with self._write_transaction("create_or_restore_project_workspace") as conn:
            return await self._create_or_restore_project_workspace_on_conn(
                conn,
                path=path,
                path_key=path_key,
                display_name=display_name,
                trusted_at=trusted_at,
                now_ms=now,
            )

    async def _create_or_restore_project_workspace_on_conn(
        self,
        conn: Any,
        *,
        path: str,
        path_key: str,
        display_name: str,
        trusted_at: int | None,
        now_ms: int,
    ) -> ProjectWorkspace:
        async with conn.execute(
            "SELECT * FROM project_workspaces WHERE path_key = ?",
            (path_key,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            position_at = await _next_project_workspace_order_value(
                conn,
                column="position_at",
                now_ms=now_ms,
            )
            workspace = ProjectWorkspace(
                path=path,
                path_key=path_key,
                display_name=display_name,
                created_at=now_ms,
                updated_at=now_ms,
                position_at=position_at,
                trusted_at=trusted_at,
            )
            data = workspace.model_dump()
            columns = list(data)
            await conn.execute(
                f"INSERT INTO project_workspaces ({', '.join(columns)}) "  # noqa: S608
                f"VALUES ({', '.join('?' for _ in columns)})",
                [_serialize(data[column]) for column in columns],
            )
            return workspace

        workspace = ProjectWorkspace(**dict(row))
        if workspace.removed_at is None:
            if workspace.trusted_at is None and trusted_at is not None:
                await conn.execute(
                    """
                    UPDATE project_workspaces
                    SET trusted_at = ?, updated_at = ?, path = ?
                    WHERE workspace_id = ?
                    """,
                    (trusted_at, now_ms, path, workspace.workspace_id),
                )
                workspace.trusted_at = trusted_at
                workspace.updated_at = now_ms
                workspace.path = path
            return workspace

        position_at = await _next_project_workspace_order_value(
            conn,
            column="position_at",
            now_ms=now_ms,
        )
        await conn.execute(
            """
            UPDATE project_workspaces
            SET removed_at = NULL, position_at = ?, updated_at = ?,
                trusted_at = COALESCE(?, trusted_at), path = ?
            WHERE workspace_id = ?
            """,
            (position_at, now_ms, trusted_at, path, workspace.workspace_id),
        )
        workspace.removed_at = None
        workspace.position_at = position_at
        workspace.updated_at = now_ms
        workspace.trusted_at = trusted_at or workspace.trusted_at
        workspace.path = path
        return workspace

    @_serialized_read
    async def get_project_workspace(self, workspace_id: str) -> ProjectWorkspace | None:
        async with self.conn.execute(
            "SELECT * FROM project_workspaces WHERE workspace_id = ?",
            (workspace_id,),
        ) as cur:
            row = await cur.fetchone()
        return ProjectWorkspace(**dict(row)) if row is not None else None

    @_serialized_read
    async def get_project_workspace_by_path_key(
        self,
        path_key: str,
    ) -> ProjectWorkspace | None:
        async with self.conn.execute(
            "SELECT * FROM project_workspaces WHERE path_key = ?",
            (path_key,),
        ) as cur:
            row = await cur.fetchone()
        return ProjectWorkspace(**dict(row)) if row is not None else None

    @_serialized_read
    async def list_project_workspaces(
        self,
        *,
        include_removed: bool = False,
    ) -> list[ProjectWorkspace]:
        where = "" if include_removed else "WHERE removed_at IS NULL"
        async with self.conn.execute(
            f"""
            SELECT * FROM project_workspaces
            {where}
            ORDER BY
                CASE WHEN pinned_at IS NULL THEN 1 ELSE 0 END ASC,
                pinned_at DESC,
                position_at DESC,
                created_at DESC,
                rowid DESC
            """  # noqa: S608 - fixed optional clause
        ) as cur:
            rows = await cur.fetchall()
        return [ProjectWorkspace(**dict(row)) for row in rows]

    async def update_project_workspace(
        self,
        workspace_id: str,
        *,
        display_name: str,
        now_ms: int | None = None,
    ) -> ProjectWorkspace:
        now = _now_ms() if now_ms is None else int(now_ms)
        async with self._write_transaction("update_project_workspace") as conn:
            cursor = await conn.execute(
                """
                UPDATE project_workspaces
                SET display_name = ?, updated_at = ?
                WHERE workspace_id = ?
                """,
                (display_name, now, workspace_id),
            )
            if int(cursor.rowcount or 0) == 0:
                raise KeyError(f"Project workspace not found: {workspace_id}")
            async with conn.execute(
                "SELECT * FROM project_workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        return ProjectWorkspace(**dict(row))

    async def set_project_workspace_pin(
        self,
        workspace_id: str,
        *,
        pinned: bool,
        now_ms: int | None = None,
    ) -> ProjectWorkspace:
        now = _now_ms() if now_ms is None else int(now_ms)
        async with self._write_transaction("set_project_workspace_pin") as conn:
            if pinned:
                pinned_at = await _next_project_workspace_order_value(
                    conn,
                    column="pinned_at",
                    now_ms=now,
                )
                cursor = await conn.execute(
                    """
                    UPDATE project_workspaces
                    SET pinned_at = ?, updated_at = ?
                    WHERE workspace_id = ? AND removed_at IS NULL
                    """,
                    (pinned_at, now, workspace_id),
                )
            else:
                position_at = await _next_project_workspace_order_value(
                    conn,
                    column="position_at",
                    now_ms=now,
                )
                cursor = await conn.execute(
                    """
                    UPDATE project_workspaces
                    SET pinned_at = NULL, position_at = ?, updated_at = ?
                    WHERE workspace_id = ? AND removed_at IS NULL
                    """,
                    (position_at, now, workspace_id),
                )
            if int(cursor.rowcount or 0) == 0:
                raise KeyError(f"Project workspace not found: {workspace_id}")
            async with conn.execute(
                "SELECT * FROM project_workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        return ProjectWorkspace(**dict(row))

    async def remove_project_workspace(
        self,
        workspace_id: str,
        *,
        now_ms: int | None = None,
    ) -> None:
        now = _now_ms() if now_ms is None else int(now_ms)
        async with self._write_transaction("remove_project_workspace") as conn:
            cursor = await conn.execute(
                """
                UPDATE project_workspaces
                SET removed_at = ?, pinned_at = NULL, updated_at = ?
                WHERE workspace_id = ? AND removed_at IS NULL
                """,
                (now, now, workspace_id),
            )
            if int(cursor.rowcount or 0) == 0:
                raise KeyError(f"Project workspace not found: {workspace_id}")

    async def bind_session_workspace(
        self,
        session_key: str,
        workspace_id: str | None,
    ) -> None:
        session_key = canonicalize_session_key(session_key)
        async with self._write_transaction("bind_session_workspace") as conn:
            cursor = await conn.execute(
                "UPDATE sessions SET workspace_id = ? WHERE session_key = ?",
                (workspace_id, session_key),
            )
            if int(cursor.rowcount or 0) == 0:
                raise KeyError(f"Session not found: {session_key}")

    @_serialized_read
    async def list_legacy_project_workspace_candidates(
        self,
        *,
        after_rowid: int = 0,
        limit: int = 500,
    ) -> list[tuple[int, str, str, dict[str, Any] | None]]:
        """Return one lightweight keyset page for legacy project adoption."""

        if limit <= 0:
            return []
        async with self.conn.execute(
            """
            SELECT rowid, session_key, agent_id, origin
            FROM sessions
            WHERE workspace_id IS NULL
              AND origin IS NOT NULL
              AND rowid > ?
            ORDER BY rowid
            LIMIT ?
            """,
            (max(0, int(after_rowid)), int(limit)),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            (
                int(row["rowid"]),
                str(row["session_key"]),
                str(row["agent_id"] or "main"),
                _json_object_or_none(row["origin"]),
            )
            for row in rows
        ]

    async def adopt_legacy_session_workspace(
        self,
        session_key: str,
        *,
        expected_agent_id: str,
        expected_origin: dict[str, Any],
        path: str,
        path_key: str,
        display_name: str,
        trusted_at: int | None,
        now_ms: int,
    ) -> ProjectWorkspace | None:
        """Atomically create and bind a still-current legacy session candidate."""

        session_key = canonicalize_session_key(session_key)
        async with self._write_transaction("adopt_legacy_session_workspace") as conn:
            async with conn.execute(
                """
                SELECT 1
                FROM sessions
                WHERE session_key = ?
                  AND workspace_id IS NULL
                  AND agent_id = ?
                  AND origin IS ?
                """,
                (
                    session_key,
                    normalize_agent_id(expected_agent_id),
                    _serialize(expected_origin),
                ),
            ) as cursor:
                if await cursor.fetchone() is None:
                    return None
            workspace = await self._create_or_restore_project_workspace_on_conn(
                conn,
                path=path,
                path_key=path_key,
                display_name=display_name,
                trusted_at=trusted_at,
                now_ms=now_ms,
            )
            cursor = await conn.execute(
                """
                UPDATE sessions
                SET workspace_id = ?
                WHERE session_key = ?
                  AND workspace_id IS NULL
                  AND agent_id = ?
                  AND origin IS ?
                """,
                (
                    workspace.workspace_id,
                    session_key,
                    normalize_agent_id(expected_agent_id),
                    _serialize(expected_origin),
                ),
            )
            if int(cursor.rowcount or 0) != 1:
                raise RuntimeError("legacy project candidate changed during transaction")
            return workspace

    @_serialized_read
    async def count_project_workspace_tasks(self, workspace_id: str) -> int:
        async with self.conn.execute(
            """
            SELECT COUNT(*)
            FROM sessions
            WHERE workspace_id = ? AND spawn_depth = 0
            """,
            (workspace_id,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0] if row is not None else 0)

    @_serialized_read
    async def list_project_workspace_session_keys(
        self,
        workspace_id: str,
    ) -> list[str]:
        async with self.conn.execute(
            """
            SELECT session_key
            FROM sessions
            WHERE workspace_id = ?
            ORDER BY created_at ASC, session_key ASC
            """,
            (workspace_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [str(row[0]) for row in rows]

    async def _delete_project_workspace_sessions(
        self,
        workspace_id: str,
        expected_session_keys: Sequence[str] | None,
    ) -> list[str]:
        deleted: list[SessionNode] = []
        async with self._write_transaction("delete_project_workspace_sessions") as conn:
            async with conn.execute(
                """
                SELECT removed_at
                FROM project_workspaces
                WHERE workspace_id = ?
                """,
                (workspace_id,),
            ) as cursor:
                workspace_row = await cursor.fetchone()
            if workspace_row is None or workspace_row["removed_at"] is not None:
                raise KeyError(f"Project workspace not found: {workspace_id}")

            async with conn.execute(
                """
                SELECT *
                FROM sessions
                WHERE workspace_id = ?
                ORDER BY created_at ASC, session_key ASC
                """,
                (workspace_id,),
            ) as cursor:
                deleted = [
                    SessionNode(**_deserialize_row(dict(row)))
                    for row in await cursor.fetchall()
                ]
            deleted_keys = [session.session_key for session in deleted]
            if (
                expected_session_keys is not None
                and deleted_keys != list(expected_session_keys)
            ):
                raise ProjectSessionSnapshotMismatchError(
                    "Project session snapshot changed before deletion"
                )

            for session in deleted:
                await self._delete_session_rows(conn, session)

        for session in deleted:
            try:
                await self._cleanup_deleted_session(session)
            except Exception:  # noqa: BLE001 - the database commit is authoritative.
                log.warning(
                    "project_workspace.session_cleanup_failed "
                    "workspace_id=%s session_key=%s",
                    workspace_id,
                    session.session_key,
                    exc_info=True,
                )
        return [session.session_key for session in deleted]

    async def delete_project_workspace_sessions(
        self,
        workspace_id: str,
        *,
        expected_session_keys: Sequence[str] | None = None,
    ) -> list[str]:
        """Atomically delete one project's history and exhaust post-commit cleanup.

        The database transaction and every cleanup attempt run in a child task
        shielded from caller cancellation. Cancellation is propagated only
        after that operation settles, so a committed delete cannot strand
        session material merely because its RPC transport disappeared.
        """

        operation = asyncio.create_task(
            self._delete_project_workspace_sessions(
                workspace_id,
                expected_session_keys,
            )
        )
        cancellation: asyncio.CancelledError | None = None
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
        if cancellation is not None:
            # The operation outcome is now known and all cleanup attempts have
            # settled. Retrieve it to avoid an unobserved child exception;
            # cancellation remains authoritative for the interrupted caller.
            with contextlib.suppress(BaseException):
                operation.result()
            raise cancellation
        return operation.result()

    async def upsert_session(
        self,
        node: SessionNode,
        *,
        expected_session_id: str | None = None,
    ) -> None:
        """Insert or update a session, optionally fencing an existing generation.

        ``expected_session_id`` is for delayed mutations of an already-read
        session. When supplied, a missing row or a different session id raises
        ``KeyError`` inside the write transaction, before the UPSERT can recreate
        a deleted row or overwrite a same-key replacement. Omitting it preserves
        the create/repair behavior of the legacy UPSERT.
        """

        node.session_key = canonicalize_session_key(node.session_key)
        node.agent_id = normalize_agent_id(node.agent_id)
        data = node.model_dump()
        cols = list(data.keys())
        placeholders = ", ".join("?" for _ in cols)
        update_columns = []
        for c in cols:
            if c == "session_key" or c in _SESSION_DEDICATED_WRITER_COLUMNS:
                continue
            if c == "epoch":
                # Hard guarantee: epoch can only increase, never roll back.
                update_columns.append("epoch = MAX(sessions.epoch, excluded.epoch)")
            else:
                update_columns.append(f"{c}=excluded.{c}")
        updates = ", ".join(update_columns)
        values = [_serialize(data[c]) for c in cols]
        sql = (
            f"INSERT INTO sessions ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(session_key) DO UPDATE SET {updates}"
        )
        async with self._write_transaction("upsert_session") as conn:
            async with conn.execute(
                """
                SELECT session_id, epoch, model_routing_mode, model_routing_revision
                FROM sessions WHERE session_key = ?
                """,
                (node.session_key,),
            ) as cursor:
                previous_identity = await cursor.fetchone()
            if previous_identity is not None:
                # Keep the caller-visible node coherent with the dedicated
                # writer fields that this general-purpose UPSERT preserves.
                node.model_routing_mode = previous_identity["model_routing_mode"]
                node.model_routing_revision = max(
                    0,
                    int(previous_identity["model_routing_revision"] or 0),
                )
            if expected_session_id is not None:
                if node.session_id != expected_session_id:
                    raise KeyError(
                        f"Session generation changed: {node.session_key}"
                    )
                async with conn.execute(
                    "SELECT session_id FROM sessions WHERE session_key = ?",
                    (node.session_key,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None or str(row["session_id"]) != expected_session_id:
                    raise KeyError(
                        f"Session generation changed: {node.session_key}"
                    )
            await conn.execute(sql, values)
            if previous_identity is None or (
                str(previous_identity["session_id"]) != node.session_id
                or int(previous_identity["epoch"] or 0) != int(node.epoch or 0)
            ):
                await self._ensure_usage_baseline_for_session_on_conn(
                    conn,
                    session_key=node.session_key,
                )

    @_serialized_read
    async def get_session(self, session_key: str) -> SessionNode | None:
        session_key = canonicalize_session_key(session_key)
        async with self.conn.execute(
            "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return SessionNode(**_deserialize_row(dict(row)))

    async def compare_and_set_session_origin(
        self,
        *,
        expected_session: SessionNode,
        expected_origin: dict[str, Any] | None,
        origin: dict[str, Any] | None,
        workspace_guard: ProjectWorkspaceGuard | None,
    ) -> SessionNode | None:
        """Replace one origin only while identity, binding, and origin still match."""

        session_key = canonicalize_session_key(expected_session.session_key)
        async with self._write_transaction("compare_and_set_session_origin") as conn:
            await _verify_project_workspace_guard(
                conn,
                session_node=expected_session,
                entry_session_key=session_key,
                workspace_guard=workspace_guard,
            )
            async with conn.execute(
                """
                UPDATE sessions
                SET origin = ?, updated_at = ?
                WHERE session_key = ?
                  AND session_id = ?
                  AND epoch = ?
                  AND workspace_id IS ?
                  AND origin IS ?
                """,
                (
                    _serialize(origin),
                    _now_ms(),
                    session_key,
                    expected_session.session_id,
                    int(expected_session.epoch or 0),
                    expected_session.workspace_id,
                    _serialize(expected_origin),
                ),
            ) as cursor:
                updated = cursor.rowcount or 0
            if updated != 1:
                return None
            async with conn.execute(
                "SELECT * FROM sessions WHERE session_key = ?",
                (session_key,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            return SessionNode(**_deserialize_row(dict(row)))

    @_serialized_read
    async def list_sessions(
        self,
        agent_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        spawned_by: str | None = None,
        guest_owner_id: str | None = None,
    ) -> list[SessionNode]:
        clauses: list[str] = []
        params: list[Any] = []
        if agent_id is not None:
            clauses.append("sessions.agent_id = ?")
            params.append(normalize_agent_id(agent_id))
        if status is not None:
            clauses.append("sessions.status = ?")
            params.append(status)
        if spawned_by is not None:
            clauses.append("sessions.spawned_by = ?")
            params.append(canonicalize_session_key(spawned_by))
        if guest_owner_id is not None:
            owner_id = str(guest_owner_id).strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", owner_id):
                return []
            clauses.append(
                "sessions.session_key GLOB ? "
                "AND (length(sessions.session_key) - "
                "length(replace(sessions.session_key, ':', ''))) = 5"
            )
            params.append(f"agent:?*:webchat:guest:{owner_id}:?*")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT sessions.*
            FROM sessions
            LEFT JOIN (
                SELECT
                    session_key,
                    MAX(
                        max(
                            max(COALESCE(updated_at, 0), COALESCE(started_at, 0)),
                            COALESCE(created_at, 0)
                        )
                    ) AS active_at
                FROM agent_tasks
                WHERE status IN (?, ?)
                GROUP BY session_key
            ) active_tasks ON active_tasks.session_key = sessions.session_key
            {where}
            ORDER BY
                max(sessions.updated_at, COALESCE(active_tasks.active_at, 0)) DESC,
                sessions.updated_at DESC,
                sessions.session_key DESC
            LIMIT ? OFFSET ?
        """
        query_params = [
            AgentTaskStatus.QUEUED.value,
            AgentTaskStatus.RUNNING.value,
            *params,
            limit,
            offset,
        ]
        async with self.conn.execute(sql, query_params) as cur:
            rows = await cur.fetchall()
        return [SessionNode(**_deserialize_row(dict(r))) for r in rows]

    @_serialized_read
    async def list_sessions_page(
        self,
        *,
        limit: int,
        cursor: SessionListCursor | None = None,
        guest_owner_id: str | None = None,
    ) -> SessionListPage:
        """Return a stable keyset page in the same order as ``list_sessions``."""

        page_limit = max(1, int(limit))
        clauses: list[str] = []
        params: list[Any] = []
        if guest_owner_id is not None:
            owner_id = str(guest_owner_id).strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", owner_id):
                return SessionListPage(sessions=[], next_cursor=None, has_more=False)
            clauses.append(
                "sessions.session_key GLOB ? "
                "AND (length(sessions.session_key) - "
                "length(replace(sessions.session_key, ':', ''))) = 5"
            )
            params.append(f"agent:?*:webchat:guest:{owner_id}:?*")
        source_where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        cursor_where = ""
        cursor_params: list[Any] = []
        if cursor is not None:
            cursor_where = """
                WHERE list_activity_at < ?
                   OR (list_activity_at = ? AND updated_at < ?)
                   OR (
                       list_activity_at = ?
                       AND updated_at = ?
                       AND session_key < ?
                   )
            """
            cursor_params = [
                cursor.activity_at,
                cursor.activity_at,
                cursor.updated_at,
                cursor.activity_at,
                cursor.updated_at,
                cursor.session_key,
            ]

        sql = f"""
            WITH active_tasks AS (
                SELECT
                    session_key,
                    MAX(
                        max(
                            max(COALESCE(updated_at, 0), COALESCE(started_at, 0)),
                            COALESCE(created_at, 0)
                        )
                    ) AS active_at
                FROM agent_tasks
                WHERE status IN (?, ?)
                GROUP BY session_key
            ), ordered_sessions AS (
                SELECT
                    sessions.*,
                    max(
                        sessions.updated_at,
                        COALESCE(active_tasks.active_at, 0)
                    ) AS list_activity_at
                FROM sessions
                LEFT JOIN active_tasks
                    ON active_tasks.session_key = sessions.session_key
                {source_where}
            )
            SELECT *
            FROM ordered_sessions
            {cursor_where}
            ORDER BY list_activity_at DESC, updated_at DESC, session_key DESC
            LIMIT ?
        """
        query_params = [
            AgentTaskStatus.QUEUED.value,
            AgentTaskStatus.RUNNING.value,
            *params,
            *cursor_params,
            page_limit + 1,
        ]
        async with self.conn.execute(sql, query_params) as cur:
            rows = await cur.fetchall()

        has_more = len(rows) > page_limit
        page_rows = rows[:page_limit]
        sessions: list[SessionNode] = []
        positions: list[SessionListCursor] = []
        for row in page_rows:
            data = dict(row)
            activity_at = int(data.pop("list_activity_at"))
            session = SessionNode(**_deserialize_row(data))
            sessions.append(session)
            positions.append(
                SessionListCursor(
                    activity_at=activity_at,
                    updated_at=int(session.updated_at),
                    session_key=session.session_key,
                )
            )
        next_cursor = positions[-1] if has_more and positions else None
        return SessionListPage(
            sessions=sessions,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    async def _delete_session_rows(
        self,
        conn: aiosqlite.Connection,
        session: SessionNode,
    ) -> None:
        for table in (
            "transcript_entries",
            "compacted_transcript_entries",
            "session_summaries",
        ):
            await conn.execute(
                f"DELETE FROM {table} WHERE session_id = ?",  # noqa: S608 - fixed literals
                (session.session_id,),
            )
        # A reset rotates session_id but deliberately retains invalid context
        # rows from older epochs. The stable session key owns every one.
        await conn.execute(
            "DELETE FROM session_context_states WHERE session_key = ?",
            (session.session_key,),
        )
        for table in ("router_decisions", "turn_errors"):
            async with conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                (table,),
            ) as cursor:
                exists = await cursor.fetchone() is not None
            if exists:
                await conn.execute(
                    f"DELETE FROM {table} WHERE session_key = ?",  # noqa: S608 - fixed literals
                    (session.session_key,),
                )
        for table in ("agent_tasks", "memory_durable_receipts"):
            await conn.execute(
                f"DELETE FROM {table} WHERE session_key = ?",  # noqa: S608 - fixed literals
                (session.session_key,),
            )
        await conn.execute(
            "DELETE FROM turn_ingress_receipts WHERE accepted_session_key = ?",
            (session.session_key,),
        )
        await conn.execute(
            "DELETE FROM pending_chat_inputs WHERE session_key = ?",
            (session.session_key,),
        )
        await conn.execute(
            "DELETE FROM pending_chat_input_cancellations WHERE session_key = ?",
            (session.session_key,),
        )
        await conn.execute(
            "DELETE FROM pending_chat_input_dispatch_receipts WHERE session_key = ?",
            (session.session_key,),
        )
        await conn.execute(
            "DELETE FROM plan_runs WHERE session_key = ?",
            (session.session_key,),
        )
        await conn.execute(
            "DELETE FROM plan_revisions WHERE source_session_key = ?",
            (session.session_key,),
        )
        await conn.execute(
            "DELETE FROM sessions WHERE session_key = ?",
            (session.session_key,),
        )

    async def _cleanup_deleted_session(self, session: SessionNode) -> None:
        # Cascade the on-disk session material (transcript media + workspace
        # attachment copies). DB-only deletion otherwise leaks both stores until
        # the transcript disk budget hard-fails. Best-effort via the registered
        # process-global hook; never fails the delete.
        from opensquilla.session.material_cleanup import run_session_material_cleanup

        await run_session_material_cleanup(session.session_id, session.session_key)

        # G4 cleanup: cascade meta-skill audit rows for this session. The
        # sessions table is created lazily at runtime (not via yoyo), so
        # there is no SQL FK to rely on — explicit purge is required.
        if self._meta_run_writer is not None:
            try:
                # The writer commits synchronously (busy_timeout=5000); keep the
                # delete off the event loop like every other writer call site.
                await asyncio.to_thread(
                    self._meta_run_writer.purge_for_session,
                    session.session_key,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("session_delete.purge_meta_runs_failed: %s", exc)

    async def delete_session(self, session_key: str) -> None:
        session_key = canonicalize_session_key(session_key)
        session: SessionNode | None = None
        async with self._write_transaction("delete_session") as conn:
            # Controls and drafts can exist on a provisional key before the
            # first accepted turn creates a sessions row. Fence those request
            # identities even when there is no session left to delete.
            await self._tombstone_meta_launches_for_boundary(
                conn,
                session_key=session_key,
                now_ms=_now_ms(),
                intent_statuses=("staged", "accepted"),
            )
            await conn.execute(
                "DELETE FROM meta_launch_drafts WHERE session_key = ?",
                (session_key,),
            )
            await conn.execute(
                "DELETE FROM pending_chat_inputs WHERE session_key = ?",
                (session_key,),
            )
            await conn.execute(
                "DELETE FROM pending_chat_input_cancellations WHERE session_key = ?",
                (session_key,),
            )
            await conn.execute(
                "DELETE FROM pending_chat_input_dispatch_receipts WHERE session_key = ?",
                (session_key,),
            )
            await conn.execute(
                "DELETE FROM meta_control_intents WHERE session_key = ?",
                (session_key,),
            )
            async with conn.execute(
                "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                session = SessionNode(**_deserialize_row(dict(row)))
                await self._delete_session_rows(conn, session)

        _clear_pending_meta_launch_boundary(session_key)
        if session is None:
            return
        await self._cleanup_deleted_session(session)

    async def prune_stale_session_records(self, before_ms: int) -> list[SessionNode]:
        """Delete and return the exact stale session generations committed."""

        deleted: list[SessionNode] = []
        async with self._write_transaction("prune_stale_sessions") as conn:
            async with conn.execute(
                "SELECT * FROM sessions WHERE updated_at < ?",
                (before_ms,),
            ) as cur:
                rows = await cur.fetchall()
            for row in rows:
                session = SessionNode(**_deserialize_row(dict(row)))
                await self._delete_session_rows(conn, session)
                deleted.append(session)
        for session in deleted:
            await self._cleanup_deleted_session(session)
        return deleted

    async def prune_stale_sessions(self, before_ms: int) -> int:
        """Delete sessions not updated since before_ms epoch ms. Returns count deleted."""

        return len(await self.prune_stale_session_records(before_ms))

    @_serialized_read
    async def count_sessions(self, guest_owner_id: str | None = None) -> int:
        where = ""
        params: tuple[str, ...] = ()
        if guest_owner_id is not None:
            owner_id = str(guest_owner_id).strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", owner_id):
                return 0
            where = (
                "WHERE session_key GLOB ? "
                "AND (length(session_key) - length(replace(session_key, ':', ''))) = 5"
            )
            params = (f"agent:?*:webchat:guest:{owner_id}:?*",)
        async with self.conn.execute(
            f"SELECT COUNT(*) FROM sessions {where}",  # noqa: S608 - fixed clause
            params,
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def increment_epoch(self, session_key: str) -> int:
        """Atomically increment the epoch counter for a session.

        Returns the new epoch value. Raises KeyError if the session is not found.
        """
        session_key = canonicalize_session_key(session_key)
        async with self._write_transaction("increment_epoch") as conn:
            await conn.execute(
                "UPDATE sessions SET epoch = epoch + 1 WHERE session_key = ?",
                (session_key,),
            )
            await conn.execute(
                "DELETE FROM session_goals WHERE session_key = ?",
                (session_key,),
            )
            async with conn.execute(
                "SELECT epoch FROM sessions WHERE session_key = ?", (session_key,)
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                raise KeyError(f"Session not found: {session_key}")
            await self._ensure_usage_baseline_for_session_on_conn(
                conn,
                session_key=session_key,
            )
            return int(row[0])

    async def advance_reset_epoch(self, session_key: str) -> int:
        """Fence a same-key reset and invalidate its unaccepted MetaSkill controls.

        The epoch transition and staged-control deletion share one transaction,
        so another client retaining an old hidden control can never observe the
        new epoch while its pre-reset authorization is still consumable. Recent
        accepted browser coordinates are also fenced against stale outbox
        retries; their intent rows remain immutable history.
        """

        session_key = canonicalize_session_key(session_key)
        async with self._write_transaction("advance_reset_epoch") as conn:
            await conn.execute(
                "UPDATE sessions SET epoch = epoch + 1 WHERE session_key = ?",
                (session_key,),
            )
            async with conn.execute(
                "SELECT epoch FROM sessions WHERE session_key = ?", (session_key,)
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                raise KeyError(f"Session not found: {session_key}")
            await self._ensure_usage_baseline_for_session_on_conn(
                conn,
                session_key=session_key,
            )
            await self._tombstone_meta_launches_for_boundary(
                conn,
                session_key=session_key,
                now_ms=_now_ms(),
                intent_statuses=("staged", "accepted"),
            )
            await conn.execute(
                "DELETE FROM meta_control_intents WHERE session_key = ? AND status = 'staged'",
                (session_key,),
            )
            await conn.execute(
                "DELETE FROM meta_launch_drafts WHERE session_key = ?",
                (session_key,),
            )
            new_epoch = int(row[0])
        _clear_pending_meta_launch_boundary(session_key)
        return new_epoch

    @_serialized_read
    async def get_epoch(self, session_key: str) -> int:
        """Return current epoch for a session (0 if not found)."""
        session_key = canonicalize_session_key(session_key)
        async with self.conn.execute(
            "SELECT epoch FROM sessions WHERE session_key = ?", (session_key,)
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row is not None else 0

    # ── Backend-owned runtime preferences ──────────────────────────────────

    @_serialized_read
    async def get_runtime_preference(self, key: str) -> str | None:
        """Return one persisted runtime preference, if configured."""
        normalized_key = key.strip() if isinstance(key, str) else ""
        if not normalized_key:
            raise ValueError("runtime preference key must not be empty")
        async with self.conn.execute(
            """
            SELECT preference_value
            FROM runtime_preferences
            WHERE preference_key = ?
            """,
            (normalized_key,),
        ) as cur:
            row = await cur.fetchone()
        return str(row[0]) if row is not None else None

    async def set_runtime_preference(self, key: str, value: str) -> str:
        """Persist one runtime preference and return its confirmed value."""
        normalized_key = key.strip() if isinstance(key, str) else ""
        normalized_value = value.strip() if isinstance(value, str) else ""
        if not normalized_key:
            raise ValueError("runtime preference key must not be empty")
        if not normalized_value:
            raise ValueError("runtime preference value must not be empty")
        async with self._write_transaction("set_runtime_preference") as conn:
            await conn.execute(
                """
                INSERT INTO runtime_preferences (
                    preference_key,
                    preference_value,
                    updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(preference_key) DO UPDATE SET
                    preference_value = excluded.preference_value,
                    updated_at = excluded.updated_at
                """,
                (normalized_key, normalized_value, _now_ms()),
            )
        return normalized_value

    # ── Per-session model routing ──────────────────────────────────────────

    @staticmethod
    def _normalize_model_routing_mode(value: str) -> str:
        mode = value.strip().lower() if isinstance(value, str) else ""
        if mode not in {"direct", "router", "ensemble"}:
            raise ValueError("model routing mode must be direct, router, or ensemble")
        return mode

    @_serialized_read
    async def _read_model_routing_state(self, session_key: str) -> Any:
        async with self.conn.execute(
            """
            SELECT model_routing_mode, model_routing_revision
            FROM sessions WHERE session_key = ?
            """,
            (session_key,),
        ) as cur:
            return await cur.fetchone()

    async def resolve_model_routing_mode(
        self,
        session_key: str,
        fallback_mode: str,
    ) -> dict[str, Any]:
        """Read one session's mode, atomically materializing legacy NULL rows.

        A NULL only represents a pre-feature (or not-yet-materialized) row.  It
        is never returned to an execution caller: the fallback is persisted
        under the write transaction so subsequent global changes cannot alter
        the session's routing policy.
        """

        session_key = canonicalize_session_key(session_key)
        fallback = self._normalize_model_routing_mode(fallback_mode)
        row = await self._read_model_routing_state(session_key)
        if row is None:
            raise KeyError(f"Session not found: {session_key}")
        raw_mode = row["model_routing_mode"]
        revision = max(0, int(row["model_routing_revision"] or 0))
        if raw_mode is not None:
            return {
                "mode": self._normalize_model_routing_mode(str(raw_mode)),
                "revision": revision,
                "source": "session",
                "initialized": False,
            }

        # Only legacy NULL rows require writer ownership. Re-read after taking
        # that ownership because another process may have materialized the row
        # between the read-only fast path and BEGIN IMMEDIATE.
        async with self._write_transaction("resolve_model_routing_mode") as conn:
            async with conn.execute(
                """
                SELECT model_routing_mode, model_routing_revision
                FROM sessions WHERE session_key = ?
                """,
                (session_key,),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                raise KeyError(f"Session not found: {session_key}")
            raw_mode = row["model_routing_mode"]
            revision = max(0, int(row["model_routing_revision"] or 0))
            if raw_mode is not None:
                return {
                    "mode": self._normalize_model_routing_mode(str(raw_mode)),
                    "revision": revision,
                    "source": "session",
                    "initialized": False,
                }
            async with conn.execute(
                """
                UPDATE sessions
                SET model_routing_mode = ?,
                    model_routing_revision = model_routing_revision + 1
                WHERE session_key = ? AND model_routing_mode IS NULL
                """,
                (fallback, session_key),
            ) as cur:
                changed = cur.rowcount or 0
            if changed != 1:
                # Another writer materialized the row after our read.  Read
                # its authoritative choice rather than overwriting it.
                async with conn.execute(
                    """
                    SELECT model_routing_mode, model_routing_revision
                    FROM sessions WHERE session_key = ?
                    """,
                    (session_key,),
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    raise KeyError(f"Session not found: {session_key}")
                return {
                    "mode": self._normalize_model_routing_mode(
                        str(row["model_routing_mode"])
                    ),
                    "revision": max(0, int(row["model_routing_revision"] or 0)),
                    "source": "session",
                    "initialized": False,
                }
            return {
                "mode": fallback,
                "revision": revision + 1,
                "source": "legacy_initialized",
                "initialized": True,
            }

    async def set_model_routing_mode(
        self,
        session_key: str,
        mode: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Compare-and-set one persisted session routing mode."""

        session_key = canonicalize_session_key(session_key)
        normalized_mode = self._normalize_model_routing_mode(mode)
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ValueError("expected_revision must be a non-negative integer")
        async with self._write_transaction("set_model_routing_mode") as conn:
            async with conn.execute(
                """
                SELECT model_routing_mode, model_routing_revision
                FROM sessions WHERE session_key = ?
                """,
                (session_key,),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                raise KeyError(f"Session not found: {session_key}")
            current_mode = row["model_routing_mode"]
            current_revision = max(0, int(row["model_routing_revision"] or 0))
            if current_mode == normalized_mode:
                # A lost acknowledgement may retry the same durable choice
                # with the caller's older generation. Treat that as idempotent
                # rather than forcing a needless UI reload.
                return {
                    "mode": normalized_mode,
                    "revision": current_revision,
                    "source": "session",
                    "initialized": False,
                }
            if expected_revision is not None and current_revision != expected_revision:
                raise SessionRoutingConflictError(
                    "model routing changed before the mode update"
                )
            async with conn.execute(
                """
                UPDATE sessions
                SET model_routing_mode = ?,
                    model_routing_revision = model_routing_revision + 1,
                    updated_at = ?
                WHERE session_key = ? AND model_routing_revision = ?
                """,
                (normalized_mode, _now_ms(), session_key, current_revision),
            ) as cur:
                changed = cur.rowcount or 0
            if changed != 1:
                raise SessionRoutingConflictError(
                    "model routing changed before the mode update"
                )
            return {
                "mode": normalized_mode,
                "revision": current_revision + 1,
                "source": "session",
                "initialized": current_mode is None,
            }

    # ── Collaboration plans ────────────────────────────────────────────────

    async def set_collaboration_mode(
        self,
        session_key: str,
        mode: str | CollaborationMode,
        *,
        expected_revision: int,
    ) -> SessionNode:
        """Compare-and-set the user-controlled collaboration mode."""

        session_key = canonicalize_session_key(session_key)
        try:
            normalized_mode = CollaborationMode(mode).value
        except ValueError as exc:
            raise PlanValidationError(f"unsupported collaboration mode: {mode}") from exc
        if expected_revision < 0:
            raise PlanValidationError("expected_revision must be non-negative")

        async with self._write_transaction("set_collaboration_mode") as conn:
            async with conn.execute(
                "SELECT * FROM sessions WHERE session_key = ?",
                (session_key,),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                raise KeyError(f"Session not found: {session_key}")
            current = SessionNode(**_deserialize_row(dict(row)))
            if current.collaboration_revision != expected_revision:
                raise PlanConflictError(
                    "collaboration state changed before the mode update"
                )
            if current.collaboration_mode == normalized_mode:
                return current
            updated_at = _now_ms()
            async with conn.execute(
                """
                UPDATE sessions
                SET collaboration_mode = ?,
                    collaboration_revision = collaboration_revision + 1,
                    updated_at = ?
                WHERE session_key = ? AND collaboration_revision = ?
                """,
                (normalized_mode, updated_at, session_key, expected_revision),
            ) as cur:
                changed = cur.rowcount or 0
            if changed == 0:
                raise PlanConflictError(
                    "collaboration state changed before the mode update"
                )
            async with conn.execute(
                "SELECT * FROM sessions WHERE session_key = ?",
                (session_key,),
            ) as cur:
                updated_row = await cur.fetchone()
            assert updated_row is not None
            return SessionNode(**_deserialize_row(dict(updated_row)))

    @staticmethod
    async def _select_plan_revision_on_conn(
        conn: Any,
        revision_id: str,
    ) -> PlanRevisionRecord | None:
        async with conn.execute(
            "SELECT * FROM plan_revisions WHERE revision_id = ?",
            (revision_id,),
        ) as cur:
            row = await cur.fetchone()
        return (
            None
            if row is None
            else PlanRevisionRecord(**_deserialize_row(dict(row)))
        )

    @classmethod
    async def _find_idempotent_plan_revision_on_conn(
        cls,
        conn: Any,
        revision: PlanRevisionRecord,
    ) -> PlanRevisionRecord | None:
        existing = await cls._select_plan_revision_on_conn(
            conn,
            revision.revision_id,
        )
        if existing is None and revision.source_message_id:
            async with conn.execute(
                """
                SELECT *
                FROM plan_revisions
                WHERE source_session_id = ? AND source_message_id = ?
                """,
                (revision.source_session_id, revision.source_message_id),
            ) as cur:
                row = await cur.fetchone()
            if row is not None:
                existing = PlanRevisionRecord(**_deserialize_row(dict(row)))
        if existing is None:
            return None
        if (
            existing.content_hash != revision.content_hash
            or existing.parent_revision_id != revision.parent_revision_id
            or existing.source_session_key != revision.source_session_key
            or existing.source_session_id != revision.source_session_id
            or existing.source_epoch != revision.source_epoch
        ):
            raise PlanConflictError(
                "revision identity was already used for different plan content"
            )
        return existing

    @classmethod
    async def _create_plan_revision_on_conn(
        cls,
        conn: Any,
        revision: PlanRevisionRecord,
        *,
        expected_parent_revision_id: str | None,
        transcript_entry: TranscriptEntry | None = None,
        expected_epoch: int | None = None,
        updated_at: int | None = None,
        token_delta: int = 0,
        mark_total_tokens_stale: bool = False,
    ) -> PlanRevisionRecord:
        existing = await cls._find_idempotent_plan_revision_on_conn(conn, revision)
        if existing is not None:
            return existing

        async with conn.execute(
            """
            SELECT session_id, epoch, active_plan_revision_id
            FROM sessions
            WHERE session_key = ?
            """,
            (revision.source_session_key,),
        ) as cur:
            session_row = await cur.fetchone()
        if session_row is None:
            raise KeyError(f"Session not found: {revision.source_session_key}")
        if (
            str(session_row["session_id"]) != revision.source_session_id
            or int(session_row["epoch"]) != revision.source_epoch
        ):
            await cls._raise_stale_epoch(
                conn,
                session_key=revision.source_session_key,
                expected_epoch=revision.source_epoch,
            )

        active_parent = session_row["active_plan_revision_id"]
        if active_parent != expected_parent_revision_id:
            raise PlanConflictError(
                "active plan revision changed before the revision was committed"
            )
        if revision.parent_revision_id != expected_parent_revision_id:
            raise PlanValidationError(
                "revision parent must match expected_parent_revision_id"
            )
        active_placeholders = ", ".join("?" for _ in PLAN_RUN_ACTIVE_STATUSES)
        async with conn.execute(
            f"""
            SELECT run_id, status
            FROM plan_runs
            WHERE session_key = ? AND status IN ({active_placeholders})
            LIMIT 1
            """,  # noqa: S608 - placeholder count is from a fixed constant
            [
                revision.source_session_key,
                *sorted(PLAN_RUN_ACTIVE_STATUSES),
            ],
        ) as cur:
            active_run_row = await cur.fetchone()
        if (
            active_run_row is not None
            and str(active_run_row["status"])
            in {PlanRunStatus.QUEUED.value, PlanRunStatus.RUNNING.value}
        ):
            raise PlanRunConflictError(
                "cannot replace a plan while its implementation task is active"
            )

        if expected_parent_revision_id is None:
            if revision.generation != 1:
                raise PlanValidationError("an initial plan revision must use generation 1")
        else:
            parent = await cls._select_plan_revision_on_conn(
                conn,
                expected_parent_revision_id,
            )
            if parent is None:
                raise PlanConflictError("parent plan revision no longer exists")
            if revision.plan_id != parent.plan_id:
                raise PlanValidationError("a replan must preserve plan_id")
            if revision.generation != parent.generation + 1:
                raise PlanValidationError(
                    "a replan generation must immediately follow its parent"
                )

        if transcript_entry is not None:
            if (
                transcript_entry.session_key != revision.source_session_key
                or transcript_entry.session_id != revision.source_session_id
            ):
                raise PlanValidationError(
                    "plan revision and transcript entry must target the same session"
                )
            if transcript_entry.role != "assistant":
                raise PlanValidationError(
                    "a durable plan revision must be attached to an assistant entry"
                )
            if (
                revision.source_message_id is not None
                and revision.source_message_id != transcript_entry.message_id
            ):
                raise PlanValidationError(
                    "source_message_id must match the assistant transcript entry"
                )
            await cls._insert_transcript_entry(
                conn,
                transcript_entry,
                expected_epoch=expected_epoch,
            )

        data = revision.model_dump()
        columns = list(data)
        placeholders = ", ".join("?" for _ in columns)
        await conn.execute(
            f"INSERT INTO plan_revisions ({', '.join(columns)}) "
            f"VALUES ({placeholders})",
            [_serialize(data[column]) for column in columns],
        )
        if expected_parent_revision_id is None:
            parent_clause = "active_plan_revision_id IS NULL"
            parent_params: list[Any] = []
        else:
            parent_clause = "active_plan_revision_id = ?"
            parent_params = [expected_parent_revision_id]
        async with conn.execute(
            f"""
            UPDATE sessions
            SET active_plan_revision_id = ?,
                collaboration_revision = collaboration_revision + 1,
                updated_at = MAX(updated_at, ?),
                total_tokens = total_tokens + ?,
                total_tokens_fresh = CASE
                    WHEN ? THEN 0
                    ELSE total_tokens_fresh
                END
            WHERE session_key = ? AND session_id = ? AND epoch = ?
              AND {parent_clause}
            """,  # noqa: S608 - parent clause is selected from fixed literals above
            [
                revision.revision_id,
                revision.created_at if updated_at is None else updated_at,
                token_delta,
                int(mark_total_tokens_stale),
                revision.source_session_key,
                revision.source_session_id,
                revision.source_epoch,
                *parent_params,
            ],
        ) as cur:
            activated = cur.rowcount or 0
        if activated == 0:
            raise PlanConflictError(
                "active plan revision changed before the revision was committed"
            )
        if active_run_row is not None:
            timestamp = revision.created_at if updated_at is None else updated_at
            await conn.execute(
                """
                UPDATE plan_runs
                SET status = 'superseded',
                    state_revision = state_revision + 1,
                    active_task_id = NULL,
                    terminal_reason = 'superseded_by_new_revision',
                    updated_at = ?,
                    finished_at = ?
                WHERE run_id = ?
                """,
                (timestamp, timestamp, str(active_run_row["run_id"])),
            )
        return revision

    async def create_plan_revision(
        self,
        revision: PlanRevisionRecord,
        *,
        expected_parent_revision_id: str | None,
    ) -> PlanRevisionRecord:
        """Persist and activate one immutable structured plan revision."""

        prepared = prepare_plan_revision(revision)
        prepared.source_session_key = canonicalize_session_key(
            prepared.source_session_key
        )
        async with self._write_transaction("create_plan_revision") as conn:
            return await self._create_plan_revision_on_conn(
                conn,
                prepared,
                expected_parent_revision_id=expected_parent_revision_id,
            )

    async def append_plan_revision(
        self,
        entry: TranscriptEntry,
        revision: PlanRevisionRecord,
        *,
        expected_epoch: int,
        expected_parent_revision_id: str | None,
        updated_at: int | None = None,
        token_delta: int | None = None,
        mark_total_tokens_stale: bool | None = None,
    ) -> PlanRevisionRecord:
        """Atomically append an assistant entry and activate its plan revision."""

        prepared = prepare_plan_revision(revision)
        prepared.source_session_key = canonicalize_session_key(
            prepared.source_session_key
        )
        entry.session_key = canonicalize_session_key(entry.session_key)
        effective_token_delta = (
            (
                entry.token_count
                if entry.token_count is not None and entry.turn_usage is None
                else 0
            )
            if token_delta is None
            else token_delta
        )
        effective_mark_stale = (
            bool(effective_token_delta)
            if mark_total_tokens_stale is None
            else mark_total_tokens_stale
        )
        async with self._write_transaction("append_plan_revision") as conn:
            return await self._create_plan_revision_on_conn(
                conn,
                prepared,
                expected_parent_revision_id=expected_parent_revision_id,
                transcript_entry=entry,
                expected_epoch=expected_epoch,
                updated_at=updated_at,
                token_delta=effective_token_delta,
                mark_total_tokens_stale=effective_mark_stale,
            )

    @_serialized_read
    async def get_plan_revision(
        self,
        revision_id: str,
    ) -> PlanRevisionRecord | None:
        return await self._select_plan_revision_on_conn(self.conn, revision_id)

    @_serialized_read
    async def get_current_plan_revision(
        self,
        session_key: str,
    ) -> PlanRevisionRecord | None:
        """Return the current user-visible Plan revision, never Goal internals."""

        session_key = canonicalize_session_key(session_key)
        async with self.conn.execute(
            """
            SELECT plan_revisions.*
            FROM sessions
            JOIN plan_revisions
              ON plan_revisions.revision_id = sessions.active_plan_revision_id
            WHERE sessions.session_key = ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM plan_runs
                  WHERE plan_runs.plan_revision_id = plan_revisions.revision_id
                    AND plan_runs.driver_kind = 'goal'
              )
            """,
            (session_key,),
        ) as cur:
            row = await cur.fetchone()
        return (
            None
            if row is None
            else PlanRevisionRecord(**_deserialize_row(dict(row)))
        )

    @_serialized_read
    async def list_plan_revisions(
        self,
        *,
        session_key: str | None = None,
        plan_id: str | None = None,
        limit: int = 100,
    ) -> list[PlanRevisionRecord]:
        if session_key is None and plan_id is None:
            raise ValueError("session_key or plan_id is required")
        if limit < 1:
            raise ValueError("limit must be positive")
        clauses: list[str] = []
        parameters: list[Any] = []
        if session_key is not None:
            clauses.append("source_session_key = ?")
            parameters.append(canonicalize_session_key(session_key))
        if plan_id is not None:
            clauses.append("plan_id = ?")
            parameters.append(plan_id)
        parameters.append(limit)
        async with self.conn.execute(
            f"""
            SELECT *
            FROM plan_revisions
            WHERE {" AND ".join(clauses)}
            ORDER BY generation DESC, created_at DESC
            LIMIT ?
            """,  # noqa: S608 - clauses contain fixed literals only
            parameters,
        ) as cur:
            rows = await cur.fetchall()
        return [
            PlanRevisionRecord(**_deserialize_row(dict(row)))
            for row in rows
        ]

    @staticmethod
    async def _select_plan_run_on_conn(
        conn: Any,
        run_id: str,
    ) -> PlanRunRecord | None:
        async with conn.execute(
            "SELECT * FROM plan_runs WHERE run_id = ?",
            (run_id,),
        ) as cur:
            row = await cur.fetchone()
        return None if row is None else PlanRunRecord(**_deserialize_row(dict(row)))

    @classmethod
    async def _start_plan_run_on_conn(
        cls,
        conn: Any,
        run: PlanRunRecord,
    ) -> PlanRunRecord:
        existing = await cls._select_plan_run_on_conn(conn, run.run_id)
        if existing is not None:
            if (
                existing.session_key != run.session_key
                or existing.session_id != run.session_id
                or existing.session_epoch != run.session_epoch
                or existing.plan_revision_id != run.plan_revision_id
            ):
                raise PlanRunConflictError(
                    "run_id was already used for a different plan run"
                )
            if (
                existing.driver_kind != run.driver_kind
                or existing.driver_id != run.driver_id
            ):
                raise PlanRunConflictError(
                    "plan run is owned by a different execution driver"
                )
            incoming_task_id = str(run.active_task_id or "").strip()
            if not incoming_task_id:
                return existing
            if existing.status in {
                PlanRunStatus.PAUSED.value,
                PlanRunStatus.BLOCKED.value,
            }:
                if run.state_revision != existing.state_revision:
                    raise PlanRunConflictError(
                        "the paused plan run changed before it was queued"
                    )
                timestamp = _now_ms()
                async with conn.execute(
                    """
                    UPDATE plan_runs
                    SET status = 'queued',
                        state_revision = state_revision + 1,
                        active_task_id = ?,
                        pause_reason = NULL,
                        terminal_reason = NULL,
                        updated_at = ?
                    WHERE run_id = ? AND state_revision = ?
                    """,
                    (
                        incoming_task_id,
                        timestamp,
                        run.run_id,
                        existing.state_revision,
                    ),
                ) as cur:
                    changed = cur.rowcount or 0
                if changed == 0:
                    raise PlanRunConflictError(
                        "the paused plan run changed before it was queued"
                    )
                resumed = await cls._select_plan_run_on_conn(conn, run.run_id)
                assert resumed is not None
                return resumed
            if (
                existing.status == PlanRunStatus.QUEUED.value
                and existing.active_task_id == incoming_task_id
            ):
                return existing
            raise PlanRunConflictError(
                f"cannot attach a task to a {existing.status} plan run"
            )

        async with conn.execute(
            """
            SELECT session_id, epoch, active_plan_revision_id
            FROM sessions
            WHERE session_key = ?
            """,
            (run.session_key,),
        ) as cur:
            session_row = await cur.fetchone()
        if session_row is None:
            raise KeyError(f"Session not found: {run.session_key}")
        if (
            str(session_row["session_id"]) != run.session_id
            or int(session_row["epoch"]) != run.session_epoch
        ):
            await cls._raise_stale_epoch(
                conn,
                session_key=run.session_key,
                expected_epoch=run.session_epoch,
            )
        if session_row["active_plan_revision_id"] != run.plan_revision_id:
            raise PlanConflictError("only the current plan revision can be implemented")

        revision = await cls._select_plan_revision_on_conn(
            conn,
            run.plan_revision_id,
        )
        if revision is None:
            raise PlanConflictError("plan revision no longer exists")
        prepared = prepare_plan_run(run, revision=revision)

        active_placeholders = ", ".join("?" for _ in PLAN_RUN_ACTIVE_STATUSES)
        async with conn.execute(
            f"""
            SELECT *
            FROM plan_runs
            WHERE session_key = ? AND status IN ({active_placeholders})
            ORDER BY created_at DESC
            LIMIT 1
            """,  # noqa: S608 - placeholder count is derived from a fixed constant
            [run.session_key, *sorted(PLAN_RUN_ACTIVE_STATUSES)],
        ) as cur:
            active_row = await cur.fetchone()
        superseded_run_id = (
            str(active_row["run_id"]) if active_row is not None else None
        )
        if (
            prepared.supersedes_run_id is not None
            and prepared.supersedes_run_id != superseded_run_id
        ):
            raise PlanRunConflictError("the active run changed before implementation")
        async with conn.execute(
            """
            SELECT MAX(created_at) AS max_created_at
            FROM plan_runs
            WHERE session_key = ?
            """,
            (run.session_key,),
        ) as cur:
            newest_row = await cur.fetchone()
        newest_created_at = (
            int(newest_row["max_created_at"])
            if newest_row is not None and newest_row["max_created_at"] is not None
            else -1
        )
        # Distinct runs need a server-authoritative total order. Wall-clock
        # milliseconds alone can collide, causing clients to discard a newly
        # queued run as stale. Keep creation time monotonic per session while
        # preserving externally supplied timestamps that are already newer.
        timestamp = max(prepared.created_at, _now_ms(), newest_created_at + 1)
        if active_row is not None and str(active_row["status"]) in {
            PlanRunStatus.QUEUED.value,
            PlanRunStatus.RUNNING.value,
        }:
            raise PlanRunConflictError(
                "an implementation task is already queued or running"
            )
        if (
            active_row is not None
            and (
                str(active_row["driver_kind"]) != prepared.driver_kind
                or (
                    (str(active_row["driver_id"] or "") or None)
                    != prepared.driver_id
                )
            )
        ):
            raise PlanRunConflictError(
                "the active plan run is owned by a different execution driver"
            )
        if (
            active_row is not None
            and str(active_row["driver_kind"]) == "goal"
        ):
            raise PlanRunConflictError(
                "an active Goal plan run must be resumed by its existing run_id"
            )
        if superseded_run_id is not None:
            await conn.execute(
                """
                UPDATE plan_runs
                SET status = 'superseded',
                    state_revision = state_revision + 1,
                    active_task_id = NULL,
                    terminal_reason = 'superseded_by_new_run',
                    updated_at = ?,
                    finished_at = ?
                WHERE run_id = ?
                """,
                (timestamp, timestamp, superseded_run_id),
            )
        prepared = prepared.model_copy(
            update={
                "supersedes_run_id": superseded_run_id,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        data = prepared.model_dump()
        columns = list(data)
        placeholders = ", ".join("?" for _ in columns)
        await conn.execute(
            f"INSERT INTO plan_runs ({', '.join(columns)}) VALUES ({placeholders})",
            [_serialize(data[column]) for column in columns],
        )
        return prepared

    async def start_plan_run(self, run: PlanRunRecord) -> PlanRunRecord:
        """Start a queued run and atomically supersede any prior active run."""

        run.session_key = canonicalize_session_key(run.session_key)
        async with self._write_transaction("start_plan_run") as conn:
            return await self._start_plan_run_on_conn(conn, run)

    @_serialized_read
    async def get_plan_run(self, run_id: str) -> PlanRunRecord | None:
        return await self._select_plan_run_on_conn(self.conn, run_id)

    @_serialized_read
    async def get_latest_plan_run_for_revision(
        self,
        plan_revision_id: str,
    ) -> PlanRunRecord | None:
        """Return the newest execution overlay attached to a plan revision."""

        async with self.conn.execute(
            """
            SELECT *
            FROM plan_runs
            WHERE plan_revision_id = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (plan_revision_id,),
        ) as cur:
            row = await cur.fetchone()
        return None if row is None else PlanRunRecord(**_deserialize_row(dict(row)))

    @_serialized_read
    async def get_active_plan_run(
        self,
        session_key: str,
    ) -> PlanRunRecord | None:
        session_key = canonicalize_session_key(session_key)
        placeholders = ", ".join("?" for _ in PLAN_RUN_ACTIVE_STATUSES)
        async with self.conn.execute(
            f"""
            SELECT *
            FROM plan_runs
            WHERE session_key = ? AND status IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT 1
            """,  # noqa: S608 - placeholder count is derived from a fixed constant
            [session_key, *sorted(PLAN_RUN_ACTIVE_STATUSES)],
        ) as cur:
            row = await cur.fetchone()
        return None if row is None else PlanRunRecord(**_deserialize_row(dict(row)))

    async def supersede_active_plan_runs(
        self,
        session_key: str,
        *,
        reason: str,
        updated_at: int | None = None,
    ) -> int:
        """Terminate every active execution overlay for a session boundary."""

        session_key = canonicalize_session_key(session_key)
        timestamp = _now_ms() if updated_at is None else updated_at
        placeholders = ", ".join("?" for _ in PLAN_RUN_ACTIVE_STATUSES)
        async with self._write_transaction("supersede_active_plan_runs") as conn:
            async with conn.execute(
                f"""
                UPDATE plan_runs
                SET status = 'superseded',
                    state_revision = state_revision + 1,
                    active_task_id = NULL,
                    terminal_reason = ?,
                    updated_at = ?,
                    finished_at = ?
                WHERE session_key = ? AND status IN ({placeholders})
                """,  # noqa: S608 - placeholder count is from a fixed constant
                [
                    reason,
                    timestamp,
                    timestamp,
                    session_key,
                    *sorted(PLAN_RUN_ACTIVE_STATUSES),
                ],
            ) as cur:
                return int(cur.rowcount or 0)

    @classmethod
    async def _load_plan_run_for_cas(
        cls,
        conn: Any,
        *,
        run_id: str,
        expected_state_revision: int,
    ) -> PlanRunRecord:
        run = await cls._select_plan_run_on_conn(conn, run_id)
        if run is None:
            raise KeyError(f"Plan run not found: {run_id}")
        if run.state_revision != expected_state_revision:
            raise PlanRunConflictError("plan run state changed before the update")
        async with conn.execute(
            "SELECT session_id, epoch FROM sessions WHERE session_key = ?",
            (run.session_key,),
        ) as cur:
            session_row = await cur.fetchone()
        if (
            session_row is None
            or str(session_row["session_id"]) != run.session_id
            or int(session_row["epoch"]) != run.session_epoch
        ):
            raise PlanRunConflictError("plan run belongs to a stale session epoch")
        return run

    async def mark_plan_run_running(
        self,
        run_id: str,
        *,
        expected_state_revision: int,
        active_task_id: str,
    ) -> PlanRunRecord:
        """Claim an active run for a task and enter its execution phase."""

        if not active_task_id:
            raise PlanValidationError("active_task_id is required")
        async with self._write_transaction("mark_plan_run_running") as conn:
            run = await self._load_plan_run_for_cas(
                conn,
                run_id=run_id,
                expected_state_revision=expected_state_revision,
            )
            async with conn.execute(
                """
                SELECT active_plan_revision_id
                FROM sessions
                WHERE session_key = ? AND session_id = ? AND epoch = ?
                """,
                (run.session_key, run.session_id, run.session_epoch),
            ) as cur:
                session_row = await cur.fetchone()
            if (
                session_row is None
                or session_row["active_plan_revision_id"] != run.plan_revision_id
            ):
                timestamp = _now_ms()
                await conn.execute(
                    """
                    UPDATE plan_runs
                    SET status = 'superseded',
                        state_revision = state_revision + 1,
                        active_task_id = NULL,
                        terminal_reason = 'stale_plan_revision',
                        updated_at = ?,
                        finished_at = ?
                    WHERE run_id = ? AND state_revision = ?
                    """,
                    (timestamp, timestamp, run_id, expected_state_revision),
                )
                updated = await self._select_plan_run_on_conn(conn, run_id)
                assert updated is not None
                return updated
            if run.status not in {
                PlanRunStatus.QUEUED.value,
                PlanRunStatus.PAUSED.value,
                PlanRunStatus.BLOCKED.value,
            }:
                raise PlanRunConflictError(
                    f"cannot mark a {run.status} plan run as running"
                )
            if run.active_task_id is not None and run.active_task_id != active_task_id:
                raise PlanRunConflictError("plan run is owned by another task")
            states = [dict(state) for state in run.step_states]
            current_step_id = run.current_step_id
            if current_step_id is None:
                current_step_id = next(
                    (
                        str(state["step_id"])
                        for state in states
                        if state.get("status") not in {"completed", "skipped"}
                    ),
                    None,
                )
            delivery_ready = bool(states) and all(
                state.get("status") in {"completed", "skipped"}
                for state in states
            )
            if current_step_id is None and not delivery_ready:
                raise PlanRunConflictError("plan run has no resumable execution step")
            if current_step_id is not None:
                for state in states:
                    if state.get("step_id") == current_step_id:
                        state["status"] = "in_progress"
                        state.pop("reason", None)
                        break
            timestamp = _now_ms()
            async with conn.execute(
                """
                UPDATE plan_runs
                SET status = 'running',
                    step_states = ?,
                    current_step_id = ?,
                    state_revision = state_revision + 1,
                    active_task_id = ?,
                    pause_reason = NULL,
                    terminal_reason = NULL,
                    started_at = COALESCE(started_at, ?),
                    updated_at = ?
                WHERE run_id = ? AND state_revision = ?
                """,
                (
                    _serialize(states),
                    current_step_id,
                    active_task_id,
                    timestamp,
                    timestamp,
                    run_id,
                    expected_state_revision,
                ),
            ) as cur:
                changed = cur.rowcount or 0
            if changed == 0:
                raise PlanRunConflictError("plan run state changed before the update")
            updated = await self._select_plan_run_on_conn(conn, run_id)
            assert updated is not None
            return updated

    async def checkpoint_plan_run(
        self,
        run_id: str,
        *,
        expected_state_revision: int,
        step_id: str,
        step_status: str,
        next_step_id: str | None = None,
        expected_active_task_id: str | None = None,
        reason: str | None = None,
    ) -> PlanRunRecord:
        """Compare-and-set one step checkpoint and derive the run lifecycle."""

        async with self._write_transaction("checkpoint_plan_run") as conn:
            run = await self._load_plan_run_for_cas(
                conn,
                run_id=run_id,
                expected_state_revision=expected_state_revision,
            )
            if run.status != PlanRunStatus.RUNNING.value:
                raise PlanRunConflictError(
                    f"cannot checkpoint a {run.status} plan run"
                )
            if (
                expected_active_task_id is not None
                and run.active_task_id != expected_active_task_id
            ):
                raise PlanRunConflictError("plan run is owned by another task")
            if run.current_step_id != step_id:
                raise PlanRunConflictError(
                    "only the current plan step may be checkpointed"
                )
            states, current_step_id, status = checkpoint_plan_step_states(
                run.step_states,
                step_id=step_id,
                step_status=step_status,
                next_step_id=next_step_id,
                reason=reason,
            )
            timestamp = _now_ms()
            blocked = status == PlanRunStatus.BLOCKED.value
            async with conn.execute(
                """
                UPDATE plan_runs
                SET status = ?,
                    step_states = ?,
                    current_step_id = ?,
                    state_revision = state_revision + 1,
                    active_task_id = ?,
                    pause_reason = ?,
                    terminal_reason = ?,
                    updated_at = ?,
                    finished_at = ?
                WHERE run_id = ? AND state_revision = ?
                """,
                (
                    status,
                    _serialize(states),
                    current_step_id,
                    None if blocked else run.active_task_id,
                    reason if blocked else None,
                    None,
                    timestamp,
                    None,
                    run_id,
                    expected_state_revision,
                ),
            ) as cur:
                changed = cur.rowcount or 0
            if changed == 0:
                raise PlanRunConflictError("plan run state changed before the update")
            updated = await self._select_plan_run_on_conn(conn, run_id)
            assert updated is not None
            return updated

    async def complete_plan_run(
        self,
        run_id: str,
        *,
        expected_state_revision: int,
        expected_active_task_id: str,
    ) -> PlanRunRecord:
        """Finalize a fully checkpointed run after its owning task succeeds."""

        if not expected_active_task_id:
            raise PlanValidationError("expected_active_task_id is required")
        async with self._write_transaction("complete_plan_run") as conn:
            run = await self._load_plan_run_for_cas(
                conn,
                run_id=run_id,
                expected_state_revision=expected_state_revision,
            )
            if run.status != PlanRunStatus.RUNNING.value:
                raise PlanRunConflictError(
                    f"cannot complete a {run.status} plan run"
                )
            if run.active_task_id != expected_active_task_id:
                raise PlanRunConflictError("plan run is owned by another task")
            if run.current_step_id is not None:
                raise PlanRunConflictError(
                    "plan run cannot complete before its final checkpoint"
                )
            if not run.step_states or any(
                state.get("status") not in {"completed", "skipped"}
                for state in run.step_states
            ):
                raise PlanRunConflictError(
                    "plan run cannot complete with unfinished steps"
                )

            timestamp = _now_ms()
            async with conn.execute(
                """
                UPDATE plan_runs
                SET status = 'completed',
                    state_revision = state_revision + 1,
                    active_task_id = NULL,
                    pause_reason = NULL,
                    terminal_reason = NULL,
                    updated_at = ?,
                    finished_at = ?
                WHERE run_id = ?
                  AND state_revision = ?
                  AND status = 'running'
                  AND current_step_id IS NULL
                  AND active_task_id = ?
                """,
                (
                    timestamp,
                    timestamp,
                    run_id,
                    expected_state_revision,
                    expected_active_task_id,
                ),
            ) as cur:
                changed = cur.rowcount or 0
            if changed == 0:
                raise PlanRunConflictError("plan run state changed before the update")
            updated = await self._select_plan_run_on_conn(conn, run_id)
            assert updated is not None
            return updated

    async def reopen_completed_plan_run(
        self,
        run_id: str,
        *,
        expected_state_revision: int,
        reason: str,
    ) -> PlanRunRecord:
        """Reopen a completed run at its first step as paused.

        Recovery-only transition for goal-driven runs whose generic settle
        path completed the run before the goal continuation driver could
        terminalize it: the goal ledger row is left stranded as "running"
        while the driver refuses to operate on a terminal run. Reopening at
        the first step restores the resumable ``goal_turn_finished`` anchor
        so the driver/recovery can parse the last turn's marker and apply the
        correct terminal outcome.
        """

        reason = reason.strip()
        if not reason:
            raise PlanValidationError("reopen reason is required")
        async with self._write_transaction("reopen_completed_plan_run") as conn:
            run = await self._load_plan_run_for_cas(
                conn,
                run_id=run_id,
                expected_state_revision=expected_state_revision,
            )
            if run.status != PlanRunStatus.COMPLETED.value:
                raise PlanRunConflictError(
                    f"cannot reopen a {run.status} plan run"
                )
            if not run.step_states:
                raise PlanRunConflictError("plan run has no steps to reopen")
            states = [dict(state) for state in run.step_states]
            states[0]["status"] = "in_progress"
            states[0].pop("reason", None)
            timestamp = _now_ms()
            async with conn.execute(
                """
                UPDATE plan_runs
                SET status = 'paused',
                    step_states = ?,
                    current_step_id = ?,
                    state_revision = state_revision + 1,
                    active_task_id = NULL,
                    pause_reason = ?,
                    terminal_reason = NULL,
                    finished_at = NULL,
                    updated_at = ?
                WHERE run_id = ? AND state_revision = ?
                """,
                (
                    _serialize(states),
                    str(states[0].get("step_id") or ""),
                    reason,
                    timestamp,
                    run_id,
                    expected_state_revision,
                ),
            ) as cur:
                changed = cur.rowcount or 0
            if changed == 0:
                raise PlanRunConflictError("plan run state changed before the update")
            updated = await self._select_plan_run_on_conn(conn, run_id)
            assert updated is not None
            return updated

    async def pause_plan_run(
        self,
        run_id: str,
        *,
        expected_state_revision: int,
        reason: str,
        expected_active_task_id: str | None = None,
        expected_driver_kind: str | None = None,
        expected_driver_id: str | None = None,
    ) -> PlanRunRecord:
        """Release a manual run at a turn boundary without advancing progress."""

        reason = reason.strip()
        if not reason:
            raise PlanValidationError("pause reason is required")
        async with self._write_transaction("pause_plan_run") as conn:
            run = await self._load_plan_run_for_cas(
                conn,
                run_id=run_id,
                expected_state_revision=expected_state_revision,
            )
            if run.status not in {
                PlanRunStatus.QUEUED.value,
                PlanRunStatus.RUNNING.value,
                PlanRunStatus.BLOCKED.value,
            }:
                raise PlanRunConflictError(f"cannot pause a {run.status} plan run")
            if (
                expected_active_task_id is not None
                and run.active_task_id != expected_active_task_id
            ):
                raise PlanRunConflictError("plan run is owned by another task")
            if (
                expected_driver_kind is not None
                and run.driver_kind != expected_driver_kind
            ):
                raise PlanRunConflictError(
                    "plan run is owned by a different execution driver"
                )
            if (
                expected_driver_id is not None
                and run.driver_id != expected_driver_id
            ):
                raise PlanRunConflictError(
                    "plan run is owned by a different execution driver"
                )
            timestamp = _now_ms()
            async with conn.execute(
                """
                UPDATE plan_runs
                SET status = 'paused',
                    state_revision = state_revision + 1,
                    active_task_id = NULL,
                    pause_reason = ?,
                    updated_at = ?
                WHERE run_id = ? AND state_revision = ?
                """,
                (reason, timestamp, run_id, expected_state_revision),
            ) as cur:
                changed = cur.rowcount or 0
            if changed == 0:
                raise PlanRunConflictError("plan run state changed before the update")
            updated = await self._select_plan_run_on_conn(conn, run_id)
            assert updated is not None
            return updated

    async def cancel_plan_run(
        self,
        run_id: str,
        *,
        expected_state_revision: int,
        reason: str,
        expected_active_task_id: str | None = None,
    ) -> PlanRunRecord:
        """Cancel an active run with a durable terminal reason."""

        reason = reason.strip()
        if not reason:
            raise PlanValidationError("cancel reason is required")
        async with self._write_transaction("cancel_plan_run") as conn:
            run = await self._load_plan_run_for_cas(
                conn,
                run_id=run_id,
                expected_state_revision=expected_state_revision,
            )
            if run.status not in PLAN_RUN_ACTIVE_STATUSES:
                raise PlanRunConflictError(f"cannot cancel a {run.status} plan run")
            if (
                expected_active_task_id is not None
                and run.active_task_id != expected_active_task_id
            ):
                raise PlanRunConflictError("plan run is owned by another task")
            timestamp = _now_ms()
            async with conn.execute(
                """
                UPDATE plan_runs
                SET status = 'cancelled',
                    state_revision = state_revision + 1,
                    active_task_id = NULL,
                    terminal_reason = ?,
                    updated_at = ?,
                    finished_at = ?
                WHERE run_id = ? AND state_revision = ?
                """,
                (reason, timestamp, timestamp, run_id, expected_state_revision),
            ) as cur:
                changed = cur.rowcount or 0
            if changed == 0:
                raise PlanRunConflictError("plan run state changed before the update")
            updated = await self._select_plan_run_on_conn(conn, run_id)
            assert updated is not None
            return updated

    # ── Goal run ledger CRUD ────────────────────────────────────────────────

    # Goal writes below are deliberately named transitions.  There is no
    # open-ended field update API and no timestamp-based compare-and-set.

    @staticmethod
    def _goal_from_row(row: Any | None) -> GoalRecord | None:
        if row is None:
            return None
        return GoalRecord(**_deserialize_row(dict(row)))

    @classmethod
    async def _select_goal_on_conn(
        cls,
        conn: Any,
        *,
        session_key: str | None = None,
        goal_id: str | None = None,
    ) -> GoalRecord | None:
        if (session_key is None) == (goal_id is None):
            raise ValueError("select Goal by exactly one identity")
        params: tuple[Any, ...]
        if session_key is not None:
            query = "SELECT * FROM session_goals WHERE session_key = ?"
            params = (session_key,)
        else:
            query = "SELECT * FROM session_goals WHERE goal_id = ?"
            params = (goal_id,)
        async with conn.execute(query, params) as cur:
            return cls._goal_from_row(await cur.fetchone())

    @staticmethod
    async def _select_goal_command_receipt_on_conn(
        conn: Any,
        command: GoalCommandRequest,
    ) -> GoalCommandReceiptRecord | None:
        async with conn.execute(
            """
            SELECT * FROM goal_command_receipts
            WHERE source_scope = ?
              AND request_session_key = ?
              AND client_request_id = ?
            """,
            (
                command.source_scope,
                command.request_session_key,
                command.client_request_id,
            ),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return GoalCommandReceiptRecord(**_deserialize_row(dict(row)))

    @classmethod
    async def _replay_goal_command_on_conn(
        cls,
        conn: Any,
        command: GoalCommandRequest,
    ) -> GoalCommandResult | None:
        receipt = await cls._select_goal_command_receipt_on_conn(conn, command)
        if receipt is None:
            return None
        if (
            receipt.action != command.action
            or receipt.request_fingerprint != command.request_fingerprint
        ):
            raise GoalConflictError(
                "IDEMPOTENCY_CONFLICT",
                "clientRequestId was already used for a different Goal command",
            )
        goal = await cls._select_goal_on_conn(
            conn,
            session_key=command.request_session_key,
        )
        return GoalCommandResult(
            response=dict(receipt.response_json),
            goal=goal,
            replayed=True,
        )

    @staticmethod
    async def _insert_goal_command_receipt_on_conn(
        conn: Any,
        *,
        command: GoalCommandRequest,
        accepted_session_id: str,
        accepted_session_epoch: int,
        response: dict[str, Any],
    ) -> GoalCommandReceiptRecord:
        receipt = GoalCommandReceiptRecord(
            source_scope=command.source_scope,
            request_session_key=command.request_session_key,
            client_request_id=command.client_request_id,
            action=command.action,
            request_fingerprint=command.request_fingerprint,
            accepted_session_id=accepted_session_id,
            accepted_session_epoch=accepted_session_epoch,
            response_json=response,
        )
        data = receipt.model_dump()
        columns = list(data)
        placeholders = ", ".join("?" for _ in columns)
        await conn.execute(
            f"INSERT INTO goal_command_receipts ({', '.join(columns)}) "
            f"VALUES ({placeholders})",
            [_serialize(data[column]) for column in columns],
        )
        return receipt

    @staticmethod
    async def _insert_goal_on_conn(conn: Any, goal: GoalRecord) -> None:
        data = goal.model_dump()
        columns = list(data)
        placeholders = ", ".join("?" for _ in columns)
        await conn.execute(
            f"INSERT INTO session_goals ({', '.join(columns)}) "
            f"VALUES ({placeholders})",
            [_serialize(data[column]) for column in columns],
        )

    @staticmethod
    def _prepare_goal_command(
        command: GoalCommandRequest,
        *,
        action: str,
        session_key: str,
    ) -> GoalCommandRequest:
        command.validate()
        canonical_key = canonicalize_session_key(session_key)
        if command.action != action:
            raise GoalValidationError(
                f"expected Goal action {action}, got {command.action}",
                code="INVALID_GOAL_COMMAND",
            )
        if canonicalize_session_key(command.request_session_key) != canonical_key:
            raise GoalValidationError(
                "Goal command session key does not match its target",
                code="INVALID_GOAL_COMMAND",
            )
        return replace(command, request_session_key=canonical_key)

    @staticmethod
    def _goal_mutation_response(
        *,
        command: GoalCommandRequest,
        goal: GoalRecord | None,
        session_id: str,
        epoch: int,
        task_id: str | None = None,
        user_message_id: str | None = None,
        previous_goal_id: str | None = None,
        execution_state: str | None = None,
    ) -> dict[str, Any]:
        return {
            "accepted": True,
            "clientRequestId": command.client_request_id,
            "sessionKey": command.request_session_key,
            "sessionId": session_id,
            "epoch": epoch,
            "taskId": task_id,
            "userMessageId": user_message_id,
            "previousGoalId": previous_goal_id,
            "goal": (
                goal_snapshot(goal, execution_state=execution_state)
                if goal is not None
                else None
            ),
        }

    @staticmethod
    async def _goal_execution_state_on_conn(
        conn: Any,
        goal: GoalRecord,
    ) -> str:
        if goal.active_task_id is None:
            return "idle"
        async with conn.execute(
            "SELECT status FROM agent_tasks WHERE task_id = ?",
            (goal.active_task_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is not None and str(row["status"]) == AgentTaskStatus.QUEUED.value:
            return "queued"
        return "working"

    @_serialized_read
    async def get_goal(self, session_key: str) -> GoalRecord | None:
        return await self._select_goal_on_conn(
            self.conn,
            session_key=canonicalize_session_key(session_key),
        )

    @_serialized_read
    async def get_goal_by_id(self, goal_id: str) -> GoalRecord | None:
        return await self._select_goal_on_conn(self.conn, goal_id=goal_id)

    @_serialized_read
    async def get_goal_command_receipt(
        self,
        command: GoalCommandRequest,
    ) -> GoalCommandResult | None:
        command = self._prepare_goal_command(
            command,
            action=command.action,
            session_key=command.request_session_key,
        )
        return await self._replay_goal_command_on_conn(self.conn, command)

    @classmethod
    async def _require_expected_goal_on_conn(
        cls,
        conn: Any,
        *,
        session_key: str,
        expected: ExpectedGoal,
    ) -> GoalRecord:
        goal = await cls._select_goal_on_conn(conn, session_key=session_key)
        if goal is None:
            raise GoalConflictError("GOAL_NOT_FOUND", "No Goal exists for this session")
        if (
            goal.session_id != expected.session_id
            or goal.session_epoch != expected.epoch
        ):
            raise GoalConflictError(
                "SESSION_GENERATION_CHANGED",
                "The session generation changed before the Goal command",
                current=goal,
            )
        if (
            goal.goal_id != expected.goal_id
            or goal.state_revision != expected.state_revision
        ):
            raise GoalConflictError(
                "STALE_GOAL",
                "The Goal changed before the command",
                current=goal,
            )
        return goal

    @staticmethod
    async def _require_default_goal_mode_on_conn(
        conn: Any,
        *,
        goal: GoalRecord,
    ) -> None:
        async with conn.execute(
            """
            SELECT collaboration_mode
            FROM sessions
            WHERE session_key = ? AND session_id = ? AND epoch = ?
            """,
            (goal.session_key, goal.session_id, goal.session_epoch),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise GoalConflictError(
                "SESSION_GENERATION_CHANGED",
                "The session generation changed before Goal admission",
                current=goal,
            )
        if str(row["collaboration_mode"]) != CollaborationMode.DEFAULT.value:
            raise GoalConflictError(
                "PLAN_MODE_ACTIVE",
                "Goal execution cannot start while Plan mode is active",
                current=goal,
            )
        async with conn.execute(
            """
            SELECT 1 FROM plan_runs
            WHERE session_key = ?
              AND driver_kind = 'manual'
              AND status IN ('queued', 'running', 'paused', 'blocked')
            LIMIT 1
            """,
            (goal.session_key,),
        ) as cur:
            if await cur.fetchone() is not None:
                raise GoalConflictError(
                    "PLAN_RUN_ACTIVE",
                    "A manual Plan run is active for this session",
                    current=goal,
                )

    @staticmethod
    async def _require_idle_goal_session_on_conn(
        conn: Any,
        *,
        session_key: str,
        exclude_task_id: str | None = None,
    ) -> None:
        params: list[Any] = [
            session_key,
            AgentTaskStatus.QUEUED.value,
            AgentTaskStatus.RUNNING.value,
        ]
        task_clause = ""
        if exclude_task_id is not None:
            task_clause = " AND task_id != ?"
            params.append(exclude_task_id)
        async with conn.execute(
            "SELECT task_id, status FROM agent_tasks "
            "WHERE session_key = ? AND status IN (?, ?)"
            + task_clause
            + " ORDER BY created_at ASC, rowid ASC LIMIT 1",
            params,
        ) as cur:
            busy = await cur.fetchone()
        if busy is not None:
            raise GoalConflictError(
                "GOAL_BUSY",
                "The session already has a queued or running task",
            )

    # ── AgentTask ledger CRUD ───────────────────────────────────────────────

    async def edit_goal(
        self,
        *,
        session_key: str,
        expected: ExpectedGoal,
        objective: str,
        command: GoalCommandRequest,
        adoption_task_id: str | None = None,
        now_ms: int | None = None,
    ) -> GoalCommandResult:
        session_key = canonicalize_session_key(session_key)
        command = self._prepare_goal_command(
            command,
            action="edit",
            session_key=session_key,
        )
        objective = normalize_goal_objective(objective)
        timestamp = _now_ms() if now_ms is None else now_ms
        async with self._write_transaction("edit_goal") as conn:
            replay = await self._replay_goal_command_on_conn(conn, command)
            if replay is not None:
                return replay
            goal = await self._require_expected_goal_on_conn(
                conn,
                session_key=session_key,
                expected=expected,
            )
            if (
                goal.status == GoalStatus.COMPLETE.value
                and goal.active_task_id is not None
            ):
                raise GoalConflictError(
                    "GOAL_BUSY",
                    "The completed Goal is still settling its terminal task",
                    current=goal,
                )
            async with conn.execute(
                """
                UPDATE session_goals
                SET objective = ?,
                    objective_revision = objective_revision + 1,
                    progress_json = NULL,
                    progress_revision = progress_revision + 1,
                    state_revision = state_revision + 1,
                    status = CASE
                        WHEN status = 'complete' THEN 'active' ELSE status
                    END,
                    terminal_task_id = NULL,
                    window_turns_started = CASE
                        WHEN status = 'complete' THEN 0 ELSE window_turns_started
                    END,
                    window_active_time_ms = CASE
                        WHEN status = 'complete' THEN 0 ELSE window_active_time_ms
                    END,
                    pause_reason = CASE
                        WHEN status = 'complete' THEN NULL ELSE pause_reason
                    END,
                    blocked_reason = NULL,
                    terminal_reason = CASE
                        WHEN status IN ('blocked', 'complete') THEN NULL
                        ELSE terminal_reason
                    END,
                    updated_at_ms = ?,
                    finished_at_ms = CASE
                        WHEN status = 'complete' THEN NULL ELSE finished_at_ms
                    END
                WHERE session_key = ? AND goal_id = ? AND state_revision = ?
                """,
                (
                    objective,
                    timestamp,
                    session_key,
                    expected.goal_id,
                    expected.state_revision,
                ),
            ) as cur:
                if (cur.rowcount or 0) != 1:
                    raise GoalConflictError(
                        "STALE_GOAL",
                        "The Goal changed before it could be edited",
                    )
            updated = await self._select_goal_on_conn(conn, session_key=session_key)
            assert updated is not None
            if (
                adoption_task_id is not None
                and updated.active_task_id == adoption_task_id
            ):
                async with conn.execute(
                    "SELECT * FROM agent_tasks WHERE task_id = ?",
                    (adoption_task_id,),
                ) as task_cur:
                    task_row = await task_cur.fetchone()
                if task_row is not None:
                    task = AgentTaskRecord(**_deserialize_row(dict(task_row)))
                    details = dict(task.details or {})
                    accepted_context = effective_goal_turn_context(details)
                    if (
                        task.session_key == session_key
                        and task.status
                        in {AgentTaskStatus.QUEUED, AgentTaskStatus.RUNNING}
                        and accepted_context is not None
                        and accepted_context.session_id == updated.session_id
                        and accepted_context.epoch == updated.session_epoch
                        and accepted_context.goal_id == updated.goal_id
                        and accepted_context.task_id == adoption_task_id
                    ):
                        next_context = GoalTurnContext(
                            session_id=updated.session_id,
                            epoch=updated.session_epoch,
                            goal_id=updated.goal_id,
                            objective_revision=updated.objective_revision,
                            objective_snapshot=updated.objective,
                            task_id=adoption_task_id,
                            continuation_seq=accepted_context.continuation_seq,
                            automatic=accepted_context.automatic,
                        )
                        pending_update = GoalObjectiveUpdate(
                            context=next_context,
                            state_revision=updated.state_revision,
                            accepted_at_ms=timestamp,
                        )
                        details[GOAL_OBJECTIVE_UPDATE_DETAIL_KEY] = (
                            pending_update.as_task_detail()
                        )
                        await conn.execute(
                            """
                            UPDATE agent_tasks
                            SET details = ?, updated_at = ?
                            WHERE task_id = ? AND status IN (?, ?)
                            """,
                            (
                                _serialize(details),
                                timestamp,
                                adoption_task_id,
                                AgentTaskStatus.QUEUED.value,
                                AgentTaskStatus.RUNNING.value,
                            ),
                        )
            response = self._goal_mutation_response(
                command=command,
                goal=updated,
                session_id=updated.session_id,
                epoch=updated.session_epoch,
                execution_state=await self._goal_execution_state_on_conn(
                    conn,
                    updated,
                ),
            )
            await self._insert_goal_command_receipt_on_conn(
                conn,
                command=command,
                accepted_session_id=updated.session_id,
                accepted_session_epoch=updated.session_epoch,
                response=response,
            )
            return GoalCommandResult(response=response, goal=updated, replayed=False)

    async def claim_goal_objective_update(
        self,
        update: GoalObjectiveUpdate,
        *,
        now_ms: int | None = None,
    ) -> GoalObjectiveUpdate | None:
        """Claim a pending objective edit at an ordinary Agent safe boundary."""

        context = update.context
        timestamp = _now_ms() if now_ms is None else now_ms
        async with self._write_transaction("claim_goal_objective_update") as conn:
            goal = await self._select_goal_on_conn(conn, goal_id=context.goal_id)
            if (
                goal is None
                or goal.session_id != context.session_id
                or goal.session_epoch != context.epoch
                or goal.objective_revision != context.objective_revision
                or goal.objective != context.objective_snapshot
                or goal.active_task_id != context.task_id
                or goal.status
                not in {GoalStatus.ACTIVE.value, GoalStatus.PAUSED.value}
            ):
                return None
            async with conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id = ?",
                (context.task_id,),
            ) as task_cur:
                task_row = await task_cur.fetchone()
            if task_row is None:
                return None
            task = AgentTaskRecord(**_deserialize_row(dict(task_row)))
            details = dict(task.details or {})
            pending = GoalObjectiveUpdate.from_task_detail(
                details.get(GOAL_OBJECTIVE_UPDATE_DETAIL_KEY)
            )
            if (
                task.status != AgentTaskStatus.RUNNING
                or task.session_key != goal.session_key
                or pending is None
                or pending.context != context
                or pending.state_revision != update.state_revision
                or pending.status not in {"pending", "claimed"}
            ):
                return None
            claimed = GoalObjectiveUpdate(
                context=context,
                state_revision=pending.state_revision,
                accepted_at_ms=pending.accepted_at_ms,
                status="claimed",
            )
            details[GOAL_OBJECTIVE_UPDATE_DETAIL_KEY] = claimed.as_task_detail()
            await conn.execute(
                "UPDATE agent_tasks SET details = ?, updated_at = ? WHERE task_id = ?",
                (_serialize(details), timestamp, context.task_id),
            )
            return claimed

    async def apply_goal_objective_update(
        self,
        update: GoalObjectiveUpdate,
        *,
        iteration: int,
        model_call_id: str,
        now_ms: int | None = None,
    ) -> GoalObjectiveUpdate | None:
        """Promote a claimed edit to effective Goal tool authority."""

        context = update.context
        timestamp = _now_ms() if now_ms is None else now_ms
        async with self._write_transaction("apply_goal_objective_update") as conn:
            goal = await self._select_goal_on_conn(conn, goal_id=context.goal_id)
            if (
                goal is None
                or goal.session_id != context.session_id
                or goal.session_epoch != context.epoch
                or goal.objective_revision != context.objective_revision
                or goal.objective != context.objective_snapshot
                or goal.active_task_id != context.task_id
                or goal.status
                not in {GoalStatus.ACTIVE.value, GoalStatus.PAUSED.value}
            ):
                return None
            async with conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id = ?",
                (context.task_id,),
            ) as task_cur:
                task_row = await task_cur.fetchone()
            if task_row is None:
                return None
            task = AgentTaskRecord(**_deserialize_row(dict(task_row)))
            details = dict(task.details or {})
            claimed = GoalObjectiveUpdate.from_task_detail(
                details.get(GOAL_OBJECTIVE_UPDATE_DETAIL_KEY)
            )
            if (
                task.status != AgentTaskStatus.RUNNING
                or task.session_key != goal.session_key
                or claimed is None
                or claimed.context != context
                or claimed.state_revision != update.state_revision
                or claimed.status != "claimed"
            ):
                return None
            applied = GoalObjectiveUpdate(
                context=context,
                state_revision=claimed.state_revision,
                accepted_at_ms=claimed.accepted_at_ms,
                status="applied",
            )
            applied_detail = applied.as_task_detail()
            applied_detail["appliedIteration"] = iteration
            applied_detail["modelCallId"] = model_call_id
            applied_detail["appliedAtMs"] = timestamp
            details[GOAL_EFFECTIVE_CONTEXT_DETAIL_KEY] = context.as_task_detail()
            details[GOAL_OBJECTIVE_UPDATE_DETAIL_KEY] = applied_detail
            await conn.execute(
                "UPDATE agent_tasks SET details = ?, updated_at = ? WHERE task_id = ?",
                (_serialize(details), timestamp, context.task_id),
            )
            return applied

    async def pause_goal(
        self,
        *,
        session_key: str,
        expected: ExpectedGoal,
        command: GoalCommandRequest,
        reason: str = "user",
        now_ms: int | None = None,
    ) -> GoalCommandResult:
        session_key = canonicalize_session_key(session_key)
        command = self._prepare_goal_command(
            command,
            action="pause",
            session_key=session_key,
        )
        reason = normalize_goal_reason(reason) or "user"
        timestamp = _now_ms() if now_ms is None else now_ms
        async with self._write_transaction("pause_goal") as conn:
            replay = await self._replay_goal_command_on_conn(conn, command)
            if replay is not None:
                return replay
            goal = await self._require_expected_goal_on_conn(
                conn,
                session_key=session_key,
                expected=expected,
            )
            if goal.status != GoalStatus.ACTIVE.value:
                raise GoalConflictError(
                    "GOAL_NOT_RESUMABLE",
                    "Only an active Goal can be paused",
                    current=goal,
                )
            await conn.execute(
                """
                UPDATE session_goals
                SET status = 'paused',
                    state_revision = state_revision + 1,
                    terminal_task_id = NULL,
                    pause_reason = ?,
                    terminal_reason = NULL,
                    updated_at_ms = ?,
                    finished_at_ms = NULL
                WHERE session_key = ? AND goal_id = ? AND state_revision = ?
                """,
                (
                    reason,
                    timestamp,
                    session_key,
                    expected.goal_id,
                    expected.state_revision,
                ),
            )
            updated = await self._select_goal_on_conn(conn, session_key=session_key)
            assert updated is not None
            response = self._goal_mutation_response(
                command=command,
                goal=updated,
                session_id=updated.session_id,
                epoch=updated.session_epoch,
                execution_state=await self._goal_execution_state_on_conn(
                    conn,
                    updated,
                ),
            )
            await self._insert_goal_command_receipt_on_conn(
                conn,
                command=command,
                accepted_session_id=updated.session_id,
                accepted_session_epoch=updated.session_epoch,
                response=response,
            )
            return GoalCommandResult(response=response, goal=updated, replayed=False)

    async def resume_goal(
        self,
        *,
        session_key: str,
        expected: ExpectedGoal,
        command: GoalCommandRequest,
        now_ms: int | None = None,
    ) -> GoalCommandResult:
        session_key = canonicalize_session_key(session_key)
        command = self._prepare_goal_command(
            command,
            action="resume",
            session_key=session_key,
        )
        timestamp = _now_ms() if now_ms is None else now_ms
        async with self._write_transaction("resume_goal") as conn:
            replay = await self._replay_goal_command_on_conn(conn, command)
            if replay is not None:
                return replay
            goal = await self._require_expected_goal_on_conn(
                conn,
                session_key=session_key,
                expected=expected,
            )
            if goal.status not in {
                GoalStatus.PAUSED.value,
                GoalStatus.BLOCKED.value,
                GoalStatus.USAGE_LIMITED.value,
            }:
                raise GoalConflictError(
                    "GOAL_NOT_RESUMABLE",
                    "This Goal is not resumable",
                    current=goal,
                )
            await conn.execute(
                """
                UPDATE session_goals
                SET status = 'active',
                    state_revision = state_revision + 1,
                    terminal_task_id = NULL,
                    window_turns_started = 0,
                    window_active_time_ms = 0,
                    pause_reason = NULL,
                    terminal_reason = NULL,
                    updated_at_ms = ?,
                    finished_at_ms = NULL
                WHERE session_key = ? AND goal_id = ? AND state_revision = ?
                """,
                (timestamp, session_key, expected.goal_id, expected.state_revision),
            )
            updated = await self._select_goal_on_conn(conn, session_key=session_key)
            assert updated is not None
            response = self._goal_mutation_response(
                command=command,
                goal=updated,
                session_id=updated.session_id,
                epoch=updated.session_epoch,
                execution_state=await self._goal_execution_state_on_conn(
                    conn,
                    updated,
                ),
            )
            await self._insert_goal_command_receipt_on_conn(
                conn,
                command=command,
                accepted_session_id=updated.session_id,
                accepted_session_epoch=updated.session_epoch,
                response=response,
            )
            return GoalCommandResult(response=response, goal=updated, replayed=False)

    async def clear_goal(
        self,
        *,
        session_key: str,
        expected: ExpectedGoal,
        command: GoalCommandRequest,
    ) -> GoalCommandResult:
        session_key = canonicalize_session_key(session_key)
        command = self._prepare_goal_command(
            command,
            action="clear",
            session_key=session_key,
        )
        async with self._write_transaction("clear_goal") as conn:
            replay = await self._replay_goal_command_on_conn(conn, command)
            if replay is not None:
                return replay
            goal = await self._require_expected_goal_on_conn(
                conn,
                session_key=session_key,
                expected=expected,
            )
            if goal.active_task_id is not None:
                async with conn.execute(
                    "SELECT details FROM agent_tasks WHERE task_id = ?",
                    (goal.active_task_id,),
                ) as task_cur:
                    task_row = await task_cur.fetchone()
                if task_row is not None:
                    details_raw = _deserialize_row(
                        {"details": task_row["details"]}
                    ).get("details")
                    details = (
                        dict(details_raw) if isinstance(details_raw, dict) else {}
                    )
                    objective_update = GoalObjectiveUpdate.from_task_detail(
                        details.get(GOAL_OBJECTIVE_UPDATE_DETAIL_KEY)
                    )
                    # Clear linearizes at the durable Goal row. A pending
                    # update can no longer be claimed, while a claim already
                    # handed to the Agent may remain in that task's assembled
                    # prompt. Mark both pending and claimed states revoked so
                    # a late provider start cannot promote either one to Goal
                    # tool authority. Applied task evidence is intentionally
                    # retained; deleting the Goal row still fences every later
                    # progress or terminal write from the surviving task.
                    if (
                        objective_update is not None
                        and objective_update.status != "applied"
                    ):
                        revoked = GoalObjectiveUpdate(
                            context=objective_update.context,
                            state_revision=objective_update.state_revision,
                            accepted_at_ms=objective_update.accepted_at_ms,
                            status="revoked",
                        )
                        details[GOAL_OBJECTIVE_UPDATE_DETAIL_KEY] = (
                            revoked.as_task_detail()
                        )
                        await conn.execute(
                            "UPDATE agent_tasks SET details = ? WHERE task_id = ?",
                            (_serialize(details), goal.active_task_id),
                        )
            async with conn.execute(
                """
                DELETE FROM session_goals
                WHERE session_key = ? AND goal_id = ? AND state_revision = ?
                """,
                (session_key, expected.goal_id, expected.state_revision),
            ) as cur:
                if (cur.rowcount or 0) != 1:
                    raise GoalConflictError(
                        "STALE_GOAL",
                        "The Goal changed before it could be cleared",
                    )
            response = self._goal_mutation_response(
                command=command,
                goal=None,
                previous_goal_id=goal.goal_id,
                session_id=goal.session_id,
                epoch=goal.session_epoch,
            )
            await self._insert_goal_command_receipt_on_conn(
                conn,
                command=command,
                accepted_session_id=goal.session_id,
                accepted_session_epoch=goal.session_epoch,
                response=response,
            )
            return GoalCommandResult(response=response, goal=None, replayed=False)

    async def accept_goal_continuation(
        self,
        *,
        expected: ExpectedGoal,
        expected_continuation_seq: int,
        task_record: AgentTaskRecord,
        max_turns: int = 50,
        runtime_budget_seconds: int = 3_600,
        workspace_guard: ProjectWorkspaceGuard | None = None,
        now_ms: int | None = None,
    ) -> GoalTaskAcceptance | GoalGuardrailPause:
        """Atomically bind and persist the next automatic Goal AgentTask."""

        if task_record.status != AgentTaskStatus.QUEUED:
            raise GoalValidationError(
                "A Goal continuation task must start queued",
                code="INVALID_GOAL_COMMAND",
            )
        if isinstance(max_turns, bool) or not 1 <= max_turns <= 500:
            raise GoalValidationError(
                "max_turns must be between 1 and 500",
                code="INVALID_GOAL_GUARDRAIL",
            )
        if (
            isinstance(runtime_budget_seconds, bool)
            or not 60 <= runtime_budget_seconds <= 86_400
        ):
            raise GoalValidationError(
                "runtime_budget_seconds must be between 60 and 86400",
                code="INVALID_GOAL_GUARDRAIL",
            )
        task_record.session_key = canonicalize_session_key(task_record.session_key)
        timestamp = _now_ms() if now_ms is None else now_ms
        async with self._write_transaction("accept_goal_continuation") as conn:
            goal = await self._require_expected_goal_on_conn(
                conn,
                session_key=task_record.session_key,
                expected=expected,
            )
            if goal.status != GoalStatus.ACTIVE.value:
                raise GoalConflictError(
                    "GOAL_NOT_RESUMABLE",
                    "Only an active Goal can continue",
                    current=goal,
                )
            if goal.active_task_id is not None:
                raise GoalConflictError(
                    "GOAL_BUSY",
                    "The Goal already owns a task",
                    current=goal,
                )
            if goal.continuation_seq != expected_continuation_seq:
                raise GoalConflictError(
                    "STALE_GOAL",
                    "The Goal continuation sequence changed",
                    current=goal,
                )
            await self._require_default_goal_mode_on_conn(conn, goal=goal)
            async with conn.execute(
                """
                SELECT collaboration_revision FROM sessions
                WHERE session_key = ? AND session_id = ? AND epoch = ?
                """,
                (goal.session_key, goal.session_id, goal.session_epoch),
            ) as collaboration_cur:
                collaboration_row = await collaboration_cur.fetchone()
            if collaboration_row is None:
                raise GoalConflictError(
                    "SESSION_GENERATION_CHANGED",
                    "The Goal session generation no longer exists",
                    current=goal,
                )
            await _verify_project_workspace_guard(
                conn,
                session_node=None,
                entry_session_key=task_record.session_key,
                workspace_guard=workspace_guard,
            )
            await self._require_idle_goal_session_on_conn(
                conn,
                session_key=task_record.session_key,
            )
            guardrail_reason: str | None = None
            if goal.window_turns_started >= max_turns:
                guardrail_reason = "turn_limit"
            elif goal.window_active_time_ms >= runtime_budget_seconds * 1000:
                guardrail_reason = "runtime_limit"
            if guardrail_reason is not None:
                async with conn.execute(
                    """
                    UPDATE session_goals
                    SET status = 'paused',
                        state_revision = state_revision + 1,
                        pause_reason = ?,
                        terminal_reason = ?,
                        updated_at_ms = ?
                    WHERE session_key = ?
                      AND goal_id = ?
                      AND state_revision = ?
                      AND active_task_id IS NULL
                      AND status = 'active'
                    """,
                    (
                        guardrail_reason,
                        guardrail_reason,
                        timestamp,
                        goal.session_key,
                        goal.goal_id,
                        goal.state_revision,
                    ),
                ) as cur:
                    if (cur.rowcount or 0) != 1:
                        raise GoalConflictError(
                            "STALE_GOAL",
                            "The Goal changed before guardrail evaluation",
                        )
                paused = await self._select_goal_on_conn(
                    conn,
                    session_key=goal.session_key,
                )
                assert paused is not None
                return GoalGuardrailPause(goal=paused, reason=guardrail_reason)
            next_seq = goal.continuation_seq + 1
            expected_task_id = automatic_goal_task_id(
                goal.goal_id,
                goal.objective_revision,
                next_seq,
            )
            if task_record.task_id != expected_task_id:
                raise GoalValidationError(
                    "Automatic Goal task id does not match its continuation fence",
                    code="INVALID_GOAL_COMMAND",
                )
            context = GoalTurnContext(
                session_id=goal.session_id,
                epoch=goal.session_epoch,
                goal_id=goal.goal_id,
                objective_revision=goal.objective_revision,
                objective_snapshot=goal.objective,
                task_id=task_record.task_id,
                continuation_seq=next_seq,
                automatic=True,
            )
            details = dict(task_record.details or {})
            details.pop("goal_candidate", None)
            details["goal_context"] = context.as_task_detail()
            metadata_raw = details.get("metadata")
            metadata = dict(metadata_raw) if isinstance(metadata_raw, dict) else {}
            metadata["required_collaboration_mode"] = "default"
            metadata["required_collaboration_revision"] = int(
                collaboration_row["collaboration_revision"]
            )
            details["metadata"] = metadata
            task_record.details = details
            await self._insert_agent_task(conn, task_record)
            async with conn.execute(
                """
                UPDATE session_goals
                SET active_task_id = ?,
                    terminal_task_id = NULL,
                    continuation_seq = ?,
                    turns_started = turns_started + 1,
                    window_turns_started = window_turns_started + 1,
                    state_revision = state_revision + 1,
                    updated_at_ms = ?
                WHERE session_key = ?
                  AND goal_id = ?
                  AND state_revision = ?
                  AND active_task_id IS NULL
                  AND status = 'active'
                """,
                (
                    task_record.task_id,
                    next_seq,
                    timestamp,
                    goal.session_key,
                    goal.goal_id,
                    goal.state_revision,
                ),
            ) as cur:
                if (cur.rowcount or 0) != 1:
                    raise GoalConflictError(
                        "STALE_GOAL",
                        "The Goal changed before continuation acceptance",
                    )
            updated = await self._select_goal_on_conn(
                conn,
                session_key=goal.session_key,
            )
            assert updated is not None
            return GoalTaskAcceptance(goal=updated, context=context)

    async def claim_goal_for_queued_task(
        self,
        *,
        candidate: GoalClaimCandidate,
        task_id: str,
        frozen_collaboration_mode: str,
        now_ms: int | None = None,
    ) -> GoalTaskAcceptance | None:
        """Best-effort claim of the still-current Goal at task activation."""

        if frozen_collaboration_mode != CollaborationMode.DEFAULT.value:
            return None
        timestamp = _now_ms() if now_ms is None else now_ms
        async with self._write_transaction("claim_goal_for_queued_task") as conn:
            async with conn.execute(
                """
                SELECT session_key, details FROM agent_tasks
                WHERE task_id = ? AND status = ?
                """,
                (task_id, AgentTaskStatus.QUEUED.value),
            ) as cur:
                task_row = await cur.fetchone()
            if task_row is None:
                return None
            session_key = str(task_row["session_key"])
            task_details_raw = _deserialize_row(
                {"details": task_row["details"]}
            ).get("details")
            task_details = (
                dict(task_details_raw)
                if isinstance(task_details_raw, dict)
                else {}
            )
            # The in-memory activation hook only carries an advisory copy.  The
            # durable queued task is the authority for whether this explicit
            # user turn was ever admitted as a candidate for this Goal.  Never
            # let a stale/forged callback attach an ordinary queued task to a
            # Goal it did not carry at acceptance time.
            if (
                GoalClaimCandidate.from_task_detail(
                    task_details.get("goal_candidate")
                )
                != candidate
            ):
                return None
            goal = await self._select_goal_on_conn(conn, session_key=session_key)
            if (
                goal is None
                or goal.session_id != candidate.session_id
                or goal.session_epoch != candidate.epoch
                or goal.goal_id != candidate.goal_id
                or goal.status != GoalStatus.ACTIVE.value
                or goal.active_task_id is not None
            ):
                return None
            try:
                await self._require_default_goal_mode_on_conn(conn, goal=goal)
            except GoalConflictError as exc:
                if exc.code in {
                    "SESSION_GENERATION_CHANGED",
                    "PLAN_MODE_ACTIVE",
                    "PLAN_RUN_ACTIVE",
                }:
                    return None
                raise
            async with conn.execute(
                """
                SELECT collaboration_revision FROM sessions
                WHERE session_key = ? AND session_id = ? AND epoch = ?
                """,
                (session_key, goal.session_id, goal.session_epoch),
            ) as collaboration_cur:
                collaboration_row = await collaboration_cur.fetchone()
            if collaboration_row is None:
                return None
            async with conn.execute(
                """
                SELECT 1 FROM agent_tasks
                WHERE session_key = ? AND task_id != ? AND status = ?
                LIMIT 1
                """,
                (session_key, task_id, AgentTaskStatus.RUNNING.value),
            ) as cur:
                if await cur.fetchone() is not None:
                    return None
            context = goal_turn_context(goal, task_id=task_id, automatic=False)
            details = task_details
            details.pop("goal_candidate", None)
            details["goal_context"] = context.as_task_detail()
            metadata_raw = details.get("metadata")
            metadata = dict(metadata_raw) if isinstance(metadata_raw, dict) else {}
            metadata["required_collaboration_mode"] = "default"
            metadata["required_collaboration_revision"] = int(
                collaboration_row["collaboration_revision"]
            )
            details["metadata"] = metadata
            await conn.execute(
                "UPDATE agent_tasks SET details = ?, updated_at = ? WHERE task_id = ?",
                (_serialize(details), timestamp, task_id),
            )
            async with conn.execute(
                """
                UPDATE session_goals
                SET active_task_id = ?,
                    terminal_task_id = NULL,
                    turns_started = turns_started + 1,
                    window_turns_started = window_turns_started + 1,
                    state_revision = state_revision + 1,
                    updated_at_ms = ?
                WHERE session_key = ? AND goal_id = ? AND active_task_id IS NULL
                """,
                (task_id, timestamp, session_key, goal.goal_id),
            ) as cur:
                if (cur.rowcount or 0) != 1:
                    raise GoalConflictError(
                        "STALE_GOAL",
                        "The Goal changed during queued-task claim",
                    )
            updated = await self._select_goal_on_conn(conn, session_key=session_key)
            assert updated is not None
            return GoalTaskAcceptance(goal=updated, context=context)

    @staticmethod
    async def _require_persisted_goal_context_on_conn(
        conn: Any,
        *,
        context: GoalTurnContext,
        expected_session_key: str,
        current: GoalRecord,
    ) -> AgentTaskRecord:
        """Require the exact frozen Goal context stored on the owning task.

        A task id plus mutable Goal-row fields is not enough evidence for a
        tool write: delayed callbacks must also match the immutable context
        accepted with the AgentTask.  This makes ``objective_snapshot`` and the
        remaining generation scalars real storage fences rather than values
        trusted only because the current caller supplied them.
        """

        async with conn.execute(
            "SELECT * FROM agent_tasks WHERE task_id = ?",
            (context.task_id,),
        ) as cur:
            task_row = await cur.fetchone()
        if task_row is None:
            raise GoalConflictError(
                "STALE_GOAL",
                "The owning Goal task no longer exists",
                current=current,
            )
        task = AgentTaskRecord(**_deserialize_row(dict(task_row)))
        task_details = task.details if isinstance(task.details, dict) else {}
        if (
            task.session_key != expected_session_key
            or effective_goal_turn_context(task_details) != context
        ):
            raise GoalConflictError(
                "STALE_GOAL",
                "The task does not carry this Goal generation",
                current=current,
            )
        return task

    async def commit_goal_terminal(
        self,
        context: GoalTurnContext,
        *,
        status: str,
        blocked_reason: str | None = None,
        now_ms: int | None = None,
    ) -> GoalRecord:
        """Durably commit an owning task's structured complete/blocked result."""

        if status not in {GoalStatus.COMPLETE.value, GoalStatus.BLOCKED.value}:
            raise GoalValidationError(
                "update_goal status must be complete or blocked",
                code="INVALID_GOAL_STATUS",
            )
        reason = normalize_goal_reason(blocked_reason)
        if status == GoalStatus.COMPLETE.value and reason is not None:
            raise GoalValidationError(
                "blocked reason is only valid for blocked Goals",
                code="INVALID_GOAL_REASON",
            )
        timestamp = _now_ms() if now_ms is None else now_ms
        async with self._write_transaction("commit_goal_terminal") as conn:
            goal = await self._select_goal_on_conn(conn, goal_id=context.goal_id)
            if goal is None:
                raise GoalConflictError("GOAL_NOT_FOUND", "The Goal no longer exists")
            if (
                goal.session_id != context.session_id
                or goal.session_epoch != context.epoch
                or goal.objective_revision != context.objective_revision
            ):
                raise GoalConflictError(
                    "STALE_GOAL",
                    "The Goal objective changed before terminal commit",
                    current=goal,
                )
            await self._require_persisted_goal_context_on_conn(
                conn,
                context=context,
                expected_session_key=goal.session_key,
                current=goal,
            )
            if goal.status in {GoalStatus.COMPLETE.value, GoalStatus.BLOCKED.value}:
                if goal.status == status and goal.terminal_task_id == context.task_id:
                    return goal
                raise GoalConflictError(
                    "STALE_GOAL",
                    "The Goal already has a different terminal result",
                    current=goal,
                )
            if (
                goal.status not in {GoalStatus.ACTIVE.value, GoalStatus.PAUSED.value}
                or goal.active_task_id != context.task_id
            ):
                raise GoalConflictError(
                    "STALE_GOAL",
                    "The task no longer owns this Goal",
                    current=goal,
                )
            await conn.execute(
                """
                UPDATE session_goals
                SET status = ?,
                    state_revision = state_revision + 1,
                    terminal_task_id = ?,
                    blocked_reason = ?,
                    pause_reason = NULL,
                    terminal_reason = ?,
                    updated_at_ms = ?,
                    finished_at_ms = ?
                WHERE goal_id = ?
                  AND objective_revision = ?
                  AND active_task_id = ?
                """,
                (
                    status,
                    context.task_id,
                    reason if status == GoalStatus.BLOCKED.value else None,
                    "model_blocked"
                    if status == GoalStatus.BLOCKED.value
                    else "model_complete",
                    timestamp,
                    timestamp if status == GoalStatus.COMPLETE.value else None,
                    context.goal_id,
                    context.objective_revision,
                    context.task_id,
                ),
            )
            updated = await self._select_goal_on_conn(conn, goal_id=context.goal_id)
            assert updated is not None
            return updated

    async def update_goal_progress(
        self,
        context: GoalTurnContext,
        *,
        explanation: object | None,
        steps: object,
        now_ms: int | None = None,
    ) -> GoalRecord:
        """Replace progress for the exact owning Goal objective."""

        progress = normalize_goal_progress(explanation=explanation, steps=steps)
        timestamp = _now_ms() if now_ms is None else now_ms
        async with self._write_transaction("update_goal_progress") as conn:
            goal = await self._select_goal_on_conn(conn, goal_id=context.goal_id)
            if goal is None:
                raise GoalConflictError("GOAL_NOT_FOUND", "The Goal no longer exists")
            if (
                goal.session_id != context.session_id
                or goal.session_epoch != context.epoch
                or goal.objective_revision != context.objective_revision
                or goal.active_task_id != context.task_id
                or goal.status
                not in {GoalStatus.ACTIVE.value, GoalStatus.PAUSED.value}
            ):
                raise GoalConflictError(
                    "STALE_GOAL",
                    "The task no longer owns this Goal objective",
                    current=goal,
                )
            await self._require_persisted_goal_context_on_conn(
                conn,
                context=context,
                expected_session_key=goal.session_key,
                current=goal,
            )
            await conn.execute(
                """
                UPDATE session_goals
                SET progress_json = ?,
                    progress_revision = progress_revision + 1,
                    updated_at_ms = ?
                WHERE goal_id = ?
                  AND objective_revision = ?
                  AND active_task_id = ?
                """,
                (
                    json.dumps(
                        progress,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    timestamp,
                    context.goal_id,
                    context.objective_revision,
                    context.task_id,
                ),
            )
            updated = await self._select_goal_on_conn(conn, goal_id=context.goal_id)
            assert updated is not None
            return updated

    @staticmethod
    async def _turn_usage_totals_on_conn(
        conn: Any,
        *,
        context: GoalTurnContext,
    ) -> dict[str, int]:
        async with conn.execute(
            """
            SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                   COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                   COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens
            FROM usage_events
            WHERE turn_id = ? AND session_id = ? AND session_epoch = ?
              AND status = 'finalized'
            """,
            (context.task_id, context.session_id, context.epoch),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        return {
            name: max(0, int(row[name] or 0))
            for name in (
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "total_tokens",
            )
        }

    @_serialized_read
    async def get_turn_usage_totals(
        self,
        context: GoalTurnContext,
    ) -> dict[str, int]:
        return await self._turn_usage_totals_on_conn(self.conn, context=context)

    async def settle_goal_task(
        self,
        context: GoalTurnContext,
        *,
        max_turns: int,
        runtime_budget_seconds: int,
        usage_limited: bool = False,
        successor_expected: bool = False,
        process_restart: bool = False,
        now_ms: int | None = None,
    ) -> GoalRecord | None:
        """Settle one authoritative terminal task exactly once by owner CAS."""

        if isinstance(max_turns, bool) or not 1 <= max_turns <= 500:
            raise GoalValidationError(
                "max_turns must be between 1 and 500",
                code="INVALID_GOAL_GUARDRAIL",
            )
        if (
            isinstance(runtime_budget_seconds, bool)
            or not 60 <= runtime_budget_seconds <= 86_400
        ):
            raise GoalValidationError(
                "runtime_budget_seconds must be between 60 and 86400",
                code="INVALID_GOAL_GUARDRAIL",
            )
        timestamp = _now_ms() if now_ms is None else now_ms
        async with self._write_transaction("settle_goal_task") as conn:
            async with conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id = ?",
                (context.task_id,),
            ) as cur:
                task_row = await cur.fetchone()
            if task_row is None:
                raise GoalConflictError(
                    "GOAL_BUSY",
                    "The authoritative Goal task is unavailable",
                )
            task = AgentTaskRecord(**_deserialize_row(dict(task_row)))
            if task.status in {AgentTaskStatus.QUEUED, AgentTaskStatus.RUNNING}:
                raise GoalConflictError(
                    "GOAL_BUSY",
                    "The Goal task has not reached a durable terminal state",
                )
            task_details = task.details if isinstance(task.details, dict) else {}
            persisted_context = effective_goal_turn_context(task_details)
            if persisted_context != context:
                raise GoalConflictError(
                    "STALE_GOAL",
                    "The terminal task does not carry this Goal generation",
                )
            goal = await self._select_goal_on_conn(conn, goal_id=context.goal_id)
            if (
                goal is None
                or goal.session_id != context.session_id
                or goal.session_epoch != context.epoch
                or task.session_key != goal.session_key
                or goal.active_task_id != context.task_id
            ):
                return None

            usage = await self._turn_usage_totals_on_conn(conn, context=context)
            duration_ms = 0
            if task.started_at is not None and task.finished_at is not None:
                duration_ms = max(0, task.finished_at - task.started_at)
            active_time_after = goal.active_time_ms + duration_ms
            window_active_after = goal.window_active_time_ms + duration_ms

            status = goal.status
            pause_reason = goal.pause_reason
            blocked_reason = goal.blocked_reason
            terminal_reason = goal.terminal_reason
            finished_at_ms = goal.finished_at_ms
            terminal_task_id = (
                goal.terminal_task_id
                if goal.status in {
                    GoalStatus.COMPLETE.value,
                    GoalStatus.BLOCKED.value,
                }
                and goal.terminal_task_id == context.task_id
                else None
            )
            same_objective = goal.objective_revision == context.objective_revision
            # A blocked Goal retains its old blocker internally across Resume
            # so the first resumed prompt can explain what was previously in
            # the way.  Consume that historical value only after the task has
            # really entered RUNNING; queued cancellation/activation failure
            # never reached a provider and must leave it for the next Resume.
            if (
                same_objective
                and task.started_at is not None
                and status in {GoalStatus.ACTIVE.value, GoalStatus.PAUSED.value}
            ):
                blocked_reason = None
            # System/user pauses are authoritative.  The still-owning task may
            # subsequently commit a structured complete/blocked result, but a
            # plain terminal callback must not erase lease_revoked,
            # process_restart, user pause, or another already-durable pause.
            # Terminal classification and guardrails apply only while the Goal
            # remains active.
            if status == GoalStatus.ACTIVE.value:
                if usage_limited:
                    status = GoalStatus.USAGE_LIMITED.value
                    pause_reason = "usage_limited"
                    blocked_reason = None
                    terminal_reason = "usage_limited"
                    finished_at_ms = None
                elif task.status == AgentTaskStatus.SUCCEEDED:
                    if not same_objective:
                        # A successful owner that did not consume the latest
                        # objective simply releases ownership. The ordinary
                        # idle gate will continue the revised Goal without
                        # applying old-objective guardrails to it.
                        pass
                    elif goal.window_turns_started >= max_turns:
                        status = GoalStatus.PAUSED.value
                        pause_reason = "turn_limit"
                        terminal_reason = "turn_limit"
                    elif window_active_after >= runtime_budget_seconds * 1000:
                        status = GoalStatus.PAUSED.value
                        pause_reason = "runtime_limit"
                        terminal_reason = "runtime_limit"
                elif task.status in {AgentTaskStatus.FAILED, AgentTaskStatus.TIMEOUT}:
                    if task.error_class == "goal_checkpoint_required":
                        # Artifact delivery already succeeded, so a missing
                        # bookkeeping checkpoint is resumable orchestration
                        # state rather than an objective-level blocker.
                        status = GoalStatus.PAUSED.value
                        pause_reason = "goal_checkpoint_required"
                        blocked_reason = None
                        terminal_reason = "goal_checkpoint_required"
                    else:
                        status = GoalStatus.BLOCKED.value
                        pause_reason = None
                        blocked_reason = normalize_goal_reason(
                            task.error_class
                            or task.terminal_reason
                            or "turn_error"
                        )
                        terminal_reason = "turn_error"
                        finished_at_ms = None
                elif task.status == AgentTaskStatus.CANCELLED:
                    if successor_expected:
                        status = GoalStatus.ACTIVE.value
                        pause_reason = None
                    elif process_restart:
                        status = GoalStatus.PAUSED.value
                        pause_reason = "process_restart"
                        terminal_reason = "process_restart"
                    else:
                        status = GoalStatus.PAUSED.value
                        pause_reason = "user_cancelled"
                        terminal_reason = "user_cancelled"
                elif task.status == AgentTaskStatus.ABANDONED:
                    status = GoalStatus.PAUSED.value
                    pause_reason = "process_restart"
                    terminal_reason = "process_restart"

            await conn.execute(
                """
                UPDATE session_goals
                SET status = ?,
                    state_revision = state_revision + 1,
                    active_task_id = NULL,
                    terminal_task_id = ?,
                    turns_settled = turns_settled + 1,
                    active_time_ms = ?,
                    window_active_time_ms = ?,
                    input_tokens = input_tokens + ?,
                    output_tokens = output_tokens + ?,
                    reasoning_tokens = reasoning_tokens + ?,
                    cache_read_tokens = cache_read_tokens + ?,
                    cache_write_tokens = cache_write_tokens + ?,
                    total_tokens = total_tokens + ?,
                    pause_reason = ?,
                    blocked_reason = ?,
                    terminal_reason = ?,
                    finished_at_ms = ?,
                    updated_at_ms = ?
                WHERE goal_id = ? AND active_task_id = ?
                """,
                (
                    status,
                    terminal_task_id,
                    active_time_after,
                    window_active_after,
                    usage["input_tokens"],
                    usage["output_tokens"],
                    usage["reasoning_tokens"],
                    usage["cache_read_tokens"],
                    usage["cache_write_tokens"],
                    usage["total_tokens"],
                    pause_reason,
                    blocked_reason,
                    terminal_reason,
                    finished_at_ms,
                    timestamp,
                    context.goal_id,
                    context.task_id,
                ),
            )
            return await self._select_goal_on_conn(conn, goal_id=context.goal_id)

    async def _compensate_goal_task(
        self,
        context: GoalTurnContext,
        *,
        reason: str,
        now_ms: int | None,
    ) -> GoalRecord | None:
        timestamp = _now_ms() if now_ms is None else now_ms
        async with self._write_transaction(f"compensate_goal_{reason}") as conn:
            async with conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id = ?",
                (context.task_id,),
            ) as task_cur:
                task_row = await task_cur.fetchone()
            if task_row is None:
                return None
            task = AgentTaskRecord(**_deserialize_row(dict(task_row)))
            task_details = task.details if isinstance(task.details, dict) else {}
            if effective_goal_turn_context(task_details) != context:
                raise GoalConflictError(
                    "STALE_GOAL",
                    "The compensated task does not carry this Goal generation",
                )
            usage = await self._turn_usage_totals_on_conn(conn, context=context)
            duration_ms = 0
            if task.started_at is not None:
                duration_ms = max(
                    0,
                    int(task.finished_at if task.finished_at is not None else timestamp)
                    - task.started_at,
                )
            await conn.execute(
                """
                UPDATE agent_tasks
                SET status = ?, terminal_reason = ?, updated_at = ?,
                    finished_at = COALESCE(finished_at, ?)
                WHERE task_id = ? AND status IN (?, ?)
                """,
                (
                    AgentTaskStatus.ABANDONED.value,
                    reason,
                    timestamp,
                    timestamp,
                    context.task_id,
                    AgentTaskStatus.QUEUED.value,
                    AgentTaskStatus.RUNNING.value,
                ),
            )
            goal = await self._select_goal_on_conn(conn, goal_id=context.goal_id)
            if (
                goal is None
                or goal.session_id != context.session_id
                or goal.session_epoch != context.epoch
                or goal.active_task_id != context.task_id
            ):
                return None
            preserve_status = goal.status in {
                GoalStatus.COMPLETE.value,
                GoalStatus.BLOCKED.value,
            }
            terminal_task_id = (
                goal.terminal_task_id
                if preserve_status and goal.terminal_task_id == context.task_id
                else None
            )
            await conn.execute(
                """
                UPDATE session_goals
                SET status = ?, state_revision = state_revision + 1,
                    active_task_id = NULL,
                    terminal_task_id = ?,
                    turns_settled = turns_settled + 1,
                    active_time_ms = active_time_ms + ?,
                    window_active_time_ms = window_active_time_ms + ?,
                    input_tokens = input_tokens + ?,
                    output_tokens = output_tokens + ?,
                    reasoning_tokens = reasoning_tokens + ?,
                    cache_read_tokens = cache_read_tokens + ?,
                    cache_write_tokens = cache_write_tokens + ?,
                    total_tokens = total_tokens + ?,
                    pause_reason = ?,
                    terminal_reason = CASE
                        WHEN status IN ('complete', 'blocked') THEN terminal_reason
                        ELSE ?
                    END,
                    blocked_reason = CASE
                        WHEN status IN ('active', 'paused') AND ? THEN NULL
                        ELSE blocked_reason
                    END,
                    updated_at_ms = ?
                WHERE goal_id = ? AND active_task_id = ?
                """,
                (
                    goal.status if preserve_status else GoalStatus.PAUSED.value,
                    terminal_task_id,
                    duration_ms,
                    duration_ms,
                    usage["input_tokens"],
                    usage["output_tokens"],
                    usage["reasoning_tokens"],
                    usage["cache_read_tokens"],
                    usage["cache_write_tokens"],
                    usage["total_tokens"],
                    goal.pause_reason if preserve_status else reason,
                    reason,
                    int(
                        task.started_at is not None
                        and goal.objective_revision == context.objective_revision
                    ),
                    timestamp,
                    context.goal_id,
                    context.task_id,
                ),
            )
            return await self._select_goal_on_conn(conn, goal_id=context.goal_id)

    async def compensate_goal_activation_failure(
        self,
        context: GoalTurnContext,
        *,
        reason: str = "activation_failed",
        now_ms: int | None = None,
    ) -> GoalRecord | None:
        if reason not in {
            "activation_failed",
            "feature_disabled",
            "lease_revoked",
            "process_restart",
        }:
            raise GoalValidationError(
                "Invalid Goal activation compensation reason",
                code="INVALID_GOAL_COMMAND",
            )
        return await self._compensate_goal_task(
            context,
            reason=reason,
            now_ms=now_ms,
        )

    async def compensate_terminal_persistence_failure(
        self,
        context: GoalTurnContext,
        *,
        now_ms: int | None = None,
    ) -> GoalRecord | None:
        return await self._compensate_goal_task(
            context,
            reason="persistence_error",
            now_ms=now_ms,
        )

    async def pause_goal_for_system(
        self,
        *,
        session_key: str,
        goal_id: str,
        expected_state_revision: int,
        reason: str,
        now_ms: int | None = None,
    ) -> GoalRecord | None:
        """Pause an active Goal for a trusted lifecycle boundary.

        The owning task, if any, is deliberately preserved so it may deliver a
        structured terminal result.  Disconnect and kill-switch callers do not
        mint user command receipts.
        """

        session_key = canonicalize_session_key(session_key)
        reason = normalize_goal_reason(reason) or "system"
        timestamp = _now_ms() if now_ms is None else now_ms
        async with self._write_transaction("pause_goal_for_system") as conn:
            current = await self._select_goal_on_conn(conn, session_key=session_key)
            if current is None:
                return None
            if (
                current.goal_id != goal_id
                or current.state_revision != expected_state_revision
            ):
                return None
            if current.status != GoalStatus.ACTIVE.value:
                return None
            async with conn.execute(
                """
                UPDATE session_goals
                SET status = 'paused', state_revision = state_revision + 1,
                    terminal_task_id = NULL,
                    pause_reason = ?, terminal_reason = ?, updated_at_ms = ?,
                    finished_at_ms = NULL
                WHERE session_key = ? AND goal_id = ? AND state_revision = ?
                """,
                (
                    reason,
                    reason,
                    timestamp,
                    session_key,
                    goal_id,
                    expected_state_revision,
                ),
            ) as cur:
                if (cur.rowcount or 0) != 1:
                    return None
            updated = await self._select_goal_on_conn(conn, session_key=session_key)
            assert updated is not None
            return updated

    @staticmethod
    async def _insert_agent_task(conn: Any, task: AgentTaskRecord) -> None:
        data = task.model_dump()
        cols = list(data.keys())
        placeholders = ", ".join("?" for _ in cols)
        values = [_serialize(data[col]) for col in cols]
        await conn.execute(
            f"INSERT INTO agent_tasks ({', '.join(cols)}) VALUES ({placeholders})",
            values,
        )

    async def create_agent_task(self, task: AgentTaskRecord) -> AgentTaskRecord:
        task.session_key = canonicalize_session_key(task.session_key)
        task.agent_id = normalize_agent_id(task.agent_id)
        async with self._write_transaction("create_agent_task") as conn:
            await self._insert_agent_task(conn, task)
        return task

    @_serialized_read
    async def get_agent_task(self, task_id: str) -> AgentTaskRecord | None:
        async with self.conn.execute(
            "SELECT * FROM agent_tasks WHERE task_id = ?",
            (task_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return AgentTaskRecord(**_deserialize_row(dict(row)))

    @_serialized_read
    async def get_agent_tasks_by_ids(
        self,
        task_ids: Iterable[str],
    ) -> list[AgentTaskRecord]:
        """Fetch exact task identities without a page-order or age limit."""

        ids = list(dict.fromkeys(str(task_id) for task_id in task_ids if task_id))
        if not ids:
            return []
        rows_by_id: dict[str, AgentTaskRecord] = {}
        for index in range(0, len(ids), _SQLITE_VARIABLE_CHUNK_SIZE):
            chunk = ids[index : index + _SQLITE_VARIABLE_CHUNK_SIZE]
            placeholders = ", ".join("?" for _ in chunk)
            async with self.conn.execute(
                f"SELECT * FROM agent_tasks WHERE task_id IN ({placeholders})",
                chunk,
            ) as cur:
                rows = await cur.fetchall()
            for row in rows:
                task = AgentTaskRecord(**_deserialize_row(dict(row)))
                rows_by_id[task.task_id] = task
        return [rows_by_id[task_id] for task_id in ids if task_id in rows_by_id]

    async def update_agent_task(self, task_id: str, **fields: Any) -> AgentTaskRecord:
        if not fields:
            existing = await self.get_agent_task(task_id)
            if existing is None:
                raise KeyError(f"Agent task not found: {task_id}")
            return existing

        allowed = set(AgentTaskRecord.model_fields) - {"task_id", "created_at"}
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise ValueError(f"Unknown agent task fields: {', '.join(unknown)}")
        fields.setdefault("updated_at", _now_ms())
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = [_serialize(value) for value in fields.values()]
        values.append(task_id)
        async with self._write_transaction("update_agent_task") as conn:
            await conn.execute(
                f"UPDATE agent_tasks SET {assignments} WHERE task_id = ?",
                values,
            )
            async with conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id = ?", (task_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                raise KeyError(f"Agent task not found: {task_id}")
            updated = AgentTaskRecord(**_deserialize_row(dict(row)))
        return updated

    @_serialized_read
    async def list_agent_tasks(
        self,
        session_key: str | None = None,
        status: str | AgentTaskStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AgentTaskRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if session_key is not None:
            clauses.append("session_key = ?")
            params.append(canonicalize_session_key(session_key))
        if status is not None:
            clauses.append("status = ?")
            params.append(str(status))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params += [limit, offset]
        sql = (
            f"SELECT * FROM agent_tasks {where} "
            "ORDER BY created_at ASC, rowid ASC LIMIT ? OFFSET ?"
        )
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [AgentTaskRecord(**_deserialize_row(dict(row))) for row in rows]

    @_serialized_read
    async def has_queued_goal_successor(
        self,
        *,
        session_key: str,
        context: GoalTurnContext,
    ) -> bool:
        """Return whether any queued task can inherit this exact Goal generation.

        This is intentionally unbounded: queue length is configuration-driven,
        so a correctness decision cannot depend on an arbitrary hydration page.
        """

        async with self.conn.execute(
            """
            SELECT details FROM agent_tasks
            WHERE session_key = ? AND status = ?
            ORDER BY created_at ASC, rowid ASC
            """,
            (
                canonicalize_session_key(session_key),
                AgentTaskStatus.QUEUED.value,
            ),
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            details_raw = _deserialize_row({"details": row["details"]}).get("details")
            details = details_raw if isinstance(details_raw, dict) else {}
            candidate = GoalClaimCandidate.from_task_detail(details.get("goal_candidate"))
            successor_context = GoalTurnContext.from_task_detail(
                details.get("goal_context")
            )
            for successor in (candidate, successor_context):
                if (
                    successor is not None
                    and successor.session_id == context.session_id
                    and successor.epoch == context.epoch
                    and successor.goal_id == context.goal_id
                ):
                    return True
        return False

    @_serialized_read
    async def list_recent_agent_tasks(
        self,
        session_key: str,
        limit: int = 100,
    ) -> list[AgentTaskRecord]:
        """Return the newest task state needed by interactive hydration."""

        if limit <= 0:
            return []
        async with self.conn.execute(
            """
            SELECT *
            FROM agent_tasks
            WHERE session_key = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?
            """,
            (canonicalize_session_key(session_key), limit),
        ) as cur:
            rows = await cur.fetchall()
        return [AgentTaskRecord(**_deserialize_row(dict(row))) for row in rows]

    async def upsert_memory_durable_receipt(
        self,
        receipt: MemoryDurableReceipt,
        *,
        expected_session_id: str | None = None,
    ) -> MemoryDurableReceipt:
        """Upsert a receipt, optionally requiring its live session generation.

        ``expected_session_id`` is checked in the same write transaction as the
        receipt UPSERT. A missing or replaced session raises ``KeyError``.
        Omitting it intentionally retains synthetic repair and legacy behavior.
        """

        receipt.session_key = canonicalize_session_key(receipt.session_key)
        receipt.updated_at = _now_ms()
        data = receipt.model_dump()
        cols = list(data.keys())
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(
            f"{col}=excluded.{col}"
            for col in cols
            if col not in {"receipt_id", "idempotency_key", "created_at"}
        )
        values = [_serialize(data[col]) for col in cols]
        async with self._write_transaction("upsert_memory_durable_receipt") as conn:
            if expected_session_id is not None:
                if receipt.session_id != expected_session_id:
                    raise KeyError(
                        f"Session generation changed: {receipt.session_key}"
                    )
                async with conn.execute(
                    "SELECT session_id FROM sessions WHERE session_key = ?",
                    (receipt.session_key,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None or str(row["session_id"]) != expected_session_id:
                    raise KeyError(
                        f"Session generation changed: {receipt.session_key}"
                    )
            await conn.execute(
                f"""
                INSERT INTO memory_durable_receipts ({", ".join(cols)})
                VALUES ({placeholders})
                ON CONFLICT(idempotency_key) DO UPDATE SET {updates}
                """,
                values,
            )
            async with conn.execute(
                """
                SELECT * FROM memory_durable_receipts
                WHERE session_key = ? AND idempotency_key = ?
                ORDER BY created_at ASC, rowid ASC
                LIMIT 1
                """,
                (receipt.session_key, receipt.idempotency_key),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                raise RuntimeError("Upserted memory durable receipt was not readable")
            stored = MemoryDurableReceipt(**_deserialize_row(dict(row)))
        return stored

    @_serialized_read
    async def list_memory_durable_receipts(
        self,
        session_key: str | None = None,
        session_id: str | None = None,
        scope: str | None = None,
        status: str | None = None,
        coverage_turn_id: str | None = None,
        coverage_hash: str | None = None,
        coverage_entry_count: int | None = None,
        idempotency_key: str | None = None,
        limit: int = 100,
    ) -> list[MemoryDurableReceipt]:
        clauses: list[str] = []
        params: list[Any] = []
        if session_key is not None:
            clauses.append("session_key = ?")
            params.append(canonicalize_session_key(session_key))
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if coverage_turn_id is not None:
            clauses.append("coverage_turn_id = ?")
            params.append(coverage_turn_id)
        if coverage_hash is not None:
            clauses.append("coverage_hash = ?")
            params.append(coverage_hash)
        if coverage_entry_count is not None:
            clauses.append("coverage_entry_count = ?")
            params.append(coverage_entry_count)
        if idempotency_key is not None:
            clauses.append("idempotency_key = ?")
            params.append(idempotency_key)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        async with self.conn.execute(
            f"""
            SELECT * FROM memory_durable_receipts
            {where}
            ORDER BY created_at ASC, rowid ASC
            LIMIT ?
            """,
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [MemoryDurableReceipt(**_deserialize_row(dict(row))) for row in rows]

    @_serialized_read
    async def list_memory_repair_receipts(
        self,
        *,
        statuses: tuple[str, ...],
        limit: int,
        due_before_ms: int | None = None,
        path: str | None = None,
        session_key_prefix: str | None = None,
    ) -> list[MemoryDurableReceipt]:
        """List repair candidates without bypassing the shared operation gate."""

        if limit <= 0 or not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        clauses = [f"status IN ({placeholders})"]
        params: list[Any] = [*statuses]
        if due_before_ms is not None:
            clauses.append("(next_retry_at_ms IS NULL OR next_retry_at_ms <= ?)")
            params.append(due_before_ms)
        if path is not None:
            clauses.append("(source_path = ? OR target_path = ?)")
            params.extend((path, path))
        if session_key_prefix is not None:
            clauses.append("substr(session_key, 1, ?) = ?")
            params.extend((len(session_key_prefix), session_key_prefix))
        params.append(limit)
        async with self.conn.execute(
            f"""
            SELECT * FROM memory_durable_receipts
            WHERE {' AND '.join(clauses)}
            ORDER BY
                next_retry_at_ms IS NOT NULL ASC,
                next_retry_at_ms ASC,
                created_at ASC,
                rowid ASC
            LIMIT ?
            """,
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [MemoryDurableReceipt(**_deserialize_row(dict(row))) for row in rows]

    @_serialized_read
    async def list_recent_memory_durable_receipts(
        self,
        *,
        limit: int,
        session_key_prefix: str | None = None,
    ) -> list[MemoryDurableReceipt]:
        """Return the newest durable receipts under the storage read gate."""

        if limit <= 0:
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if session_key_prefix is not None:
            clauses.append("substr(session_key, 1, ?) = ?")
            params.extend((len(session_key_prefix), session_key_prefix))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        async with self.conn.execute(
            f"""
            SELECT * FROM memory_durable_receipts
            {where}
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?
            """,
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [MemoryDurableReceipt(**_deserialize_row(dict(row))) for row in rows]

    @_serialized_read
    async def memory_durable_receipt_exists_for_path(
        self,
        path: str,
        *,
        session_key_prefix: str | None = None,
    ) -> bool:
        """Check source/target path identity without exposing the raw connection."""

        clauses = ["(source_path = ? OR target_path = ?)"]
        params: list[Any] = [path, path]
        if session_key_prefix is not None:
            clauses.append("substr(session_key, 1, ?) = ?")
            params.extend((len(session_key_prefix), session_key_prefix))
        async with self.conn.execute(
            f"""
            SELECT 1 FROM memory_durable_receipts
            WHERE {' AND '.join(clauses)}
            LIMIT 1
            """,
            params,
        ) as cur:
            return await cur.fetchone() is not None

    async def claim_memory_repair_receipt(
        self,
        receipt_id: str,
        *,
        eligible_statuses: tuple[str, ...],
        claimed_status: str,
        now_ms: int,
    ) -> MemoryDurableReceipt | None:
        """Atomically claim one due repair receipt and return the claimed row."""

        if not eligible_statuses:
            return None
        placeholders = ", ".join("?" for _ in eligible_statuses)
        async with self._write_transaction("claim_memory_repair_receipt") as conn:
            async with conn.execute(
                f"""
                UPDATE memory_durable_receipts
                SET status = ?, updated_at = ?
                WHERE receipt_id = ?
                  AND status IN ({placeholders})
                  AND (next_retry_at_ms IS NULL OR next_retry_at_ms <= ?)
                """,
                (
                    claimed_status,
                    now_ms,
                    receipt_id,
                    *eligible_statuses,
                    now_ms,
                ),
            ) as cur:
                claimed = cur.rowcount or 0
            if claimed != 1:
                return None
            async with conn.execute(
                "SELECT * FROM memory_durable_receipts WHERE receipt_id = ?",
                (receipt_id,),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                raise RuntimeError("Claimed memory repair receipt was not readable")
            return MemoryDurableReceipt(**_deserialize_row(dict(row)))

    async def recover_stale_memory_repair_claims(
        self,
        *,
        running_status: str,
        pending_status: str,
        stale_before_ms: int,
        next_retry_at_ms: int,
        updated_at_ms: int,
        reason: str,
    ) -> int:
        """Move stale repair claims back to pending in one explicit transaction."""

        async with self._write_transaction("recover_stale_memory_repair_claims") as conn:
            async with conn.execute(
                """
                UPDATE memory_durable_receipts
                SET status = ?,
                    reason = ?,
                    next_retry_at_ms = ?,
                    updated_at = ?
                WHERE status = ?
                  AND updated_at <= ?
                """,
                (
                    pending_status,
                    reason,
                    next_retry_at_ms,
                    updated_at_ms,
                    running_status,
                    stale_before_ms,
                ),
            ) as cur:
                return int(cur.rowcount or 0)

    async def update_memory_durable_receipt(
        self,
        receipt_id: str,
        **fields: Any,
    ) -> MemoryDurableReceipt:
        allowed = set(MemoryDurableReceipt.model_fields) - {"receipt_id", "created_at"}
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise ValueError(
                f"Unknown memory durable receipt fields: {', '.join(unknown)}"
            )
        if "session_key" in fields:
            fields["session_key"] = canonicalize_session_key(fields["session_key"])
        fields.setdefault("updated_at", _now_ms())
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = [_serialize(value) for value in fields.values()]
        values.append(receipt_id)
        async with self._write_transaction("update_memory_durable_receipt") as conn:
            await conn.execute(
                f"UPDATE memory_durable_receipts SET {assignments} WHERE receipt_id = ?",
                values,
            )
            async with conn.execute(
                "SELECT * FROM memory_durable_receipts WHERE receipt_id = ?",
                (receipt_id,),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                raise KeyError(f"Memory durable receipt not found: {receipt_id}")
            updated = MemoryDurableReceipt(**_deserialize_row(dict(row)))
        return updated

    @_serialized_read
    async def list_agent_tasks_for_sessions(
        self,
        session_keys: list[str],
        limit_per_session: int = 100,
    ) -> dict[str, list[AgentTaskRecord]]:
        keys = list(dict.fromkeys(canonicalize_session_key(key) for key in session_keys))
        grouped: dict[str, list[AgentTaskRecord]] = {key: [] for key in keys}
        if not keys or limit_per_session <= 0:
            return grouped

        for index in range(0, len(keys), _SQLITE_VARIABLE_CHUNK_SIZE):
            chunk = keys[index : index + _SQLITE_VARIABLE_CHUNK_SIZE]
            placeholders = ", ".join("?" for _ in chunk)
            # Session-list/subagent summaries never inspect task details. Keep
            # durable channel outbox content out of this high-fanout batch read;
            # exact replay still uses get_agent_task(), which selects all fields.
            summary_columns = ", ".join(
                name for name in AgentTaskRecord.model_fields if name != "details"
            )
            sql = (
                f"SELECT {summary_columns} FROM agent_tasks "
                f"WHERE session_key IN ({placeholders}) "
                "ORDER BY session_key ASC, created_at DESC, rowid DESC"
            )
            async with self.conn.execute(sql, chunk) as cur:
                rows = await cur.fetchall()

            for row in rows:
                task = AgentTaskRecord(**_deserialize_row(dict(row)))
                bucket = grouped.setdefault(task.session_key, [])
                if len(bucket) < limit_per_session:
                    bucket.append(task)
        return grouped

    async def mark_abandoned_agent_tasks(
        self,
        now_ms: int | None = None,
        *,
        goal_pause_reason: str = "process_restart",
    ) -> int:
        """Mark non-terminal tasks abandoned and pause Goals at startup.

        ``goal_pause_reason`` lets the Gateway distinguish an ordinary process
        restart from startup with Goal execution disabled.  The default keeps
        existing callers compatible; only the two startup classifications are
        accepted so this recovery API cannot become an open-ended Goal update.
        """

        if goal_pause_reason not in {"process_restart", "feature_disabled"}:
            raise ValueError(
                "goal_pause_reason must be process_restart or feature_disabled"
            )
        ts = now_ms or _now_ms()
        plan_run_reconciliation = {
            "cancelled": 0,
            "completed": 0,
            "paused_terminal_owner": 0,
            "paused_orphan_owner": 0,
        }
        terminal_session_statuses = (
            SessionStatus.DONE,
            SessionStatus.FAILED,
            SessionStatus.KILLED,
            SessionStatus.TIMEOUT,
        )
        async with self._write_transaction("mark_abandoned_agent_tasks") as conn:
            async with conn.execute(
                """
                SELECT task_id, details
                FROM agent_tasks
                WHERE status IN (?, ?)
                   OR (status = ? AND terminal_reason = ?)
                """,
                (
                    AgentTaskStatus.QUEUED,
                    AgentTaskStatus.RUNNING,
                    AgentTaskStatus.ABANDONED,
                    "process_restart",
                ),
            ) as task_cur:
                restart_task_rows = await task_cur.fetchall()
            # Capture Goal owners before queued/running tasks are rewritten.
            # ``updated_at`` is the last durable running heartbeat, so it is a
            # safe recovery cutoff that excludes Gateway downtime when a task
            # has no terminal ``finished_at`` yet.
            async with conn.execute(
                """
                SELECT goal.session_key AS goal_session_key,
                       goal.session_id AS goal_session_id,
                       goal.session_epoch AS goal_session_epoch,
                       goal.goal_id AS goal_id,
                       goal.active_task_id AS active_task_id,
                       task.session_key AS task_session_key,
                       task.started_at AS task_started_at,
                       task.finished_at AS task_finished_at,
                       task.updated_at AS task_updated_at,
                       task.details AS task_details
                FROM session_goals AS goal
                JOIN agent_tasks AS task
                  ON task.task_id = goal.active_task_id
                WHERE goal.active_task_id IS NOT NULL
                """
            ) as goal_owner_cur:
                goal_owner_rows = await goal_owner_cur.fetchall()
            async with conn.execute(
                """
                SELECT DISTINCT session_key
                FROM agent_tasks
                WHERE status IN (?, ?)
                   OR (status = ? AND terminal_reason = ?)
                ORDER BY session_key ASC
                """,
                (
                    AgentTaskStatus.QUEUED,
                    AgentTaskStatus.RUNNING,
                    AgentTaskStatus.ABANDONED,
                    "process_restart",
                ),
            ) as restart_cur:
                self._restart_abandoned_session_keys = tuple(
                    str(row[0]) for row in await restart_cur.fetchall()
                )

            async with conn.execute(
                """
                SELECT DISTINCT agent_tasks.session_key
                FROM agent_tasks
                JOIN sessions ON sessions.session_key = agent_tasks.session_key
                WHERE sessions.status NOT IN (?, ?, ?, ?)
                  AND (
                    agent_tasks.status IN (?, ?)
                    OR (
                        agent_tasks.status = ?
                        AND agent_tasks.terminal_reason = ?
                    )
                  )
                """,
                (
                    *terminal_session_statuses,
                    AgentTaskStatus.QUEUED,
                    AgentTaskStatus.RUNNING,
                    AgentTaskStatus.ABANDONED,
                    "process_restart",
                ),
            ) as session_cur:
                session_keys = [str(row[0]) for row in await session_cur.fetchall()]

            cur = await conn.execute(
                """
                UPDATE agent_tasks
                SET status = ?,
                    updated_at = ?,
                    finished_at = COALESCE(finished_at, ?),
                    terminal_reason = CASE
                        WHEN status = ? AND EXISTS (
                            SELECT 1 FROM meta_control_intents AS intent
                            WHERE intent.accepted_task_id = agent_tasks.task_id
                              AND intent.status = 'accepted'
                        ) AND EXISTS (
                            SELECT 1 FROM sessions AS owner
                            WHERE owner.session_key = agent_tasks.session_key
                              AND owner.status NOT IN (?, ?, ?, ?)
                        ) THEN 'meta_control_restart_before_start'
                        ELSE COALESCE(terminal_reason, ?)
                    END
                WHERE status IN (?, ?)
                """,
                (
                    AgentTaskStatus.ABANDONED,
                    ts,
                    ts,
                    AgentTaskStatus.QUEUED,
                    *terminal_session_statuses,
                    "process_restart",
                    AgentTaskStatus.QUEUED,
                    AgentTaskStatus.RUNNING,
                ),
            )
            count = int(cur.rowcount if cur.rowcount is not None else 0)
            for task_row in restart_task_rows:
                details_raw = _deserialize_row({"details": task_row["details"]}).get(
                    "details"
                )
                details = dict(details_raw) if isinstance(details_raw, dict) else {}
                details["turn_outcome"] = {
                    "kind": "interrupted",
                    "reason": "process_restart",
                    "error_class": "process_restart",
                    "retryable": True,
                }
                await conn.execute(
                    """
                    UPDATE agent_tasks
                    SET details = ?
                    WHERE task_id = ?
                      AND status = ?
                      AND terminal_reason = ?
                    """,
                    (
                        _serialize(details),
                        task_row["task_id"],
                        AgentTaskStatus.ABANDONED,
                        "process_restart",
                    ),
                )
            for owner_row in goal_owner_rows:
                details_raw = _deserialize_row(
                    {"details": owner_row["task_details"]}
                ).get("details")
                details = dict(details_raw) if isinstance(details_raw, dict) else {}
                context = effective_goal_turn_context(details)
                if (
                    context is None
                    or context.task_id != str(owner_row["active_task_id"])
                    or context.goal_id != str(owner_row["goal_id"])
                    or context.session_id != str(owner_row["goal_session_id"])
                    or context.epoch != int(owner_row["goal_session_epoch"])
                    or str(owner_row["task_session_key"])
                    != str(owner_row["goal_session_key"])
                ):
                    # Fail closed: stale/corrupt ownership is released by the
                    # fallback update below, but is never attributed to Goal
                    # accounting without its frozen generation evidence.
                    continue

                usage = await self._turn_usage_totals_on_conn(conn, context=context)
                started_at_raw = owner_row["task_started_at"]
                finished_at_raw = owner_row["task_finished_at"]
                last_running_at_raw = owner_row["task_updated_at"]
                duration_ms = 0
                if started_at_raw is not None:
                    started_at = int(started_at_raw)
                    running_cutoff = int(
                        finished_at_raw
                        if finished_at_raw is not None
                        else last_running_at_raw
                    )
                    duration_ms = max(0, running_cutoff - started_at)

                async with conn.execute(
                    """
                    UPDATE session_goals
                    SET status = CASE
                            WHEN status = 'active' THEN 'paused'
                            ELSE status
                        END,
                        state_revision = state_revision + 1,
                        active_task_id = NULL,
                        turns_settled = turns_settled + 1,
                        active_time_ms = active_time_ms + ?,
                        window_active_time_ms = window_active_time_ms + ?,
                        input_tokens = input_tokens + ?,
                        output_tokens = output_tokens + ?,
                        reasoning_tokens = reasoning_tokens + ?,
                        cache_read_tokens = cache_read_tokens + ?,
                        cache_write_tokens = cache_write_tokens + ?,
                        total_tokens = total_tokens + ?,
                        pause_reason = CASE
                            WHEN status = 'active' THEN ?
                            ELSE pause_reason
                        END,
                        terminal_reason = CASE
                            WHEN status = 'active' THEN ?
                            ELSE terminal_reason
                        END,
                        blocked_reason = CASE
                            WHEN status IN ('active', 'paused') AND ? THEN NULL
                            ELSE blocked_reason
                        END,
                        updated_at_ms = ?
                    WHERE goal_id = ? AND active_task_id = ?
                    """,
                    (
                        duration_ms,
                        duration_ms,
                        usage["input_tokens"],
                        usage["output_tokens"],
                        usage["reasoning_tokens"],
                        usage["cache_read_tokens"],
                        usage["cache_write_tokens"],
                        usage["total_tokens"],
                        goal_pause_reason,
                        goal_pause_reason,
                        int(started_at_raw is not None),
                        ts,
                        context.goal_id,
                        context.task_id,
                    ),
                ) as goal_cur:
                    if (goal_cur.rowcount or 0) != 1:
                        log.warning(
                            "goal.restart_settlement_cas_miss goal_id=%s task_id=%s",
                            context.goal_id,
                            context.task_id,
                        )
            # A Goal execution lease is process-local.  Restart therefore
            # atomically pauses every active Goal (including an idle one) and
            # releases any persisted owner.  Other unfinished/terminal states
            # keep their semantic status while stale ownership is cleared.
            await conn.execute(
                """
                UPDATE session_goals
                SET status = CASE WHEN status = 'active' THEN 'paused' ELSE status END,
                    state_revision = state_revision + 1,
                    active_task_id = NULL,
                    pause_reason = CASE
                        WHEN status = 'active' THEN ?
                        ELSE pause_reason
                    END,
                    terminal_reason = CASE
                        WHEN status = 'active' THEN ?
                        ELSE terminal_reason
                    END,
                    updated_at_ms = ?
                WHERE status = 'active' OR active_task_id IS NOT NULL
                """,
                (goal_pause_reason, goal_pause_reason, ts),
            )
            # A persisted PlanRun and its AgentTask form one ownership lease.
            # Process restart abandons the in-memory task, so release that lease
            # in the same recovery transaction. Preserve the run/driver and its
            # step overlay: a later task can resume either the current step or
            # the delivery-only phase after the final checkpoint.
            await conn.execute(
                """
                UPDATE plan_runs
                SET status = 'paused',
                    state_revision = state_revision + 1,
                    active_task_id = NULL,
                    pause_reason = 'process_restart',
                    terminal_reason = 'process_restart',
                    updated_at = ?,
                    finished_at = NULL
                WHERE status IN ('queued', 'running')
                  AND active_task_id IN (
                      SELECT task_id
                      FROM agent_tasks
                      WHERE status = ?
                        AND terminal_reason = 'process_restart'
                  )
                """,
                (
                    ts,
                    AgentTaskStatus.ABANDONED,
                ),
            )
            # A prior process can also crash after the AgentTask reaches a
            # terminal state but before TaskRuntime settles its attached
            # PlanRun. Reconcile that narrow window here, outside the task
            # terminalization hot path. Runs paused above for process restart
            # are deliberately excluded, preserving their resumable state.
            async with conn.execute(
                """
                SELECT plan_runs.run_id,
                       plan_runs.status,
                       plan_runs.state_revision,
                       plan_runs.driver_kind,
                       plan_runs.current_step_id,
                       plan_runs.step_states,
                       plan_runs.active_task_id,
                       agent_tasks.status AS owner_status
                FROM plan_runs
                LEFT JOIN agent_tasks
                  ON agent_tasks.task_id = plan_runs.active_task_id
                WHERE plan_runs.status IN ('queued', 'running')
                ORDER BY plan_runs.created_at ASC, plan_runs.run_id ASC
                """
            ) as plan_run_cur:
                unsettled_plan_runs = await plan_run_cur.fetchall()
            for row in unsettled_plan_runs:
                run_id = str(row["run_id"])
                run_status = str(row["status"])
                state_revision = int(row["state_revision"])
                active_task_id_raw = row["active_task_id"]
                active_task_id = (
                    None
                    if active_task_id_raw is None
                    else str(active_task_id_raw)
                )
                owner_status_raw = row["owner_status"]
                owner_status = (
                    None if owner_status_raw is None else str(owner_status_raw)
                )
                if owner_status is None:
                    await conn.execute(
                        """
                        UPDATE plan_runs
                        SET status = 'paused',
                            state_revision = state_revision + 1,
                            active_task_id = NULL,
                            pause_reason = 'orphaned_plan_run_owner',
                            terminal_reason = 'orphaned_plan_run_owner',
                            updated_at = ?,
                            finished_at = NULL
                        WHERE run_id = ?
                          AND state_revision = ?
                          AND status = ?
                          AND active_task_id IS ?
                        """,
                        (
                            ts,
                            run_id,
                            state_revision,
                            run_status,
                            active_task_id,
                        ),
                    )
                    plan_run_reconciliation["paused_orphan_owner"] += 1
                    continue
                assert active_task_id is not None
                if owner_status in {
                    AgentTaskStatus.QUEUED.value,
                    AgentTaskStatus.RUNNING.value,
                }:
                    continue
                if run_status == PlanRunStatus.QUEUED.value:
                    await conn.execute(
                        """
                        UPDATE plan_runs
                        SET status = 'cancelled',
                            state_revision = state_revision + 1,
                            active_task_id = NULL,
                            pause_reason = NULL,
                            terminal_reason = 'implementation_turn_ended_before_start',
                            updated_at = ?,
                            finished_at = ?
                        WHERE run_id = ?
                          AND state_revision = ?
                          AND status = 'queued'
                          AND active_task_id = ?
                        """,
                        (ts, ts, run_id, state_revision, active_task_id),
                    )
                    plan_run_reconciliation["cancelled"] += 1
                    continue

                step_states_raw = _deserialize_row(
                    {"step_states": row["step_states"]}
                ).get("step_states")
                step_states = (
                    step_states_raw if isinstance(step_states_raw, list) else []
                )
                delivery_ready = (
                    row["current_step_id"] is None
                    and bool(step_states)
                    and all(
                        isinstance(state, dict)
                        and str(state.get("status") or "")
                        in {"completed", "skipped"}
                        for state in step_states
                    )
                )
                if (
                    owner_status == AgentTaskStatus.SUCCEEDED.value
                    and delivery_ready
                ):
                    await conn.execute(
                        """
                        UPDATE plan_runs
                        SET status = 'completed',
                            state_revision = state_revision + 1,
                            active_task_id = NULL,
                            pause_reason = NULL,
                            terminal_reason = NULL,
                            updated_at = ?,
                            finished_at = ?
                        WHERE run_id = ?
                          AND state_revision = ?
                          AND status = 'running'
                          AND current_step_id IS NULL
                          AND active_task_id = ?
                        """,
                        (ts, ts, run_id, state_revision, active_task_id),
                    )
                    plan_run_reconciliation["completed"] += 1
                    continue

                driver_kind = str(row["driver_kind"] or "manual")
                reason = (
                    (
                        "manual_turn_finished"
                        if driver_kind == "manual"
                        else "goal_turn_finished"
                    )
                    if owner_status == AgentTaskStatus.SUCCEEDED.value
                    else f"{driver_kind}_turn_{owner_status}"
                )
                await conn.execute(
                    """
                    UPDATE plan_runs
                    SET status = 'paused',
                        state_revision = state_revision + 1,
                        active_task_id = NULL,
                        pause_reason = ?,
                        updated_at = ?,
                        finished_at = NULL
                    WHERE run_id = ?
                      AND state_revision = ?
                      AND status = 'running'
                      AND active_task_id = ?
                    """,
                    (reason, ts, run_id, state_revision, active_task_id),
                )
                plan_run_reconciliation["paused_terminal_owner"] += 1
            for index in range(0, len(session_keys), _SQLITE_VARIABLE_CHUNK_SIZE):
                chunk = session_keys[index : index + _SQLITE_VARIABLE_CHUNK_SIZE]
                placeholders = ", ".join("?" for _ in chunk)
                await conn.execute(
                f"""
                UPDATE sessions
                SET status = ?,
                    updated_at = ?,
                    ended_at = COALESCE(ended_at, ?),
                    runtime_ms = CASE
                        WHEN runtime_ms IS NOT NULL THEN runtime_ms
                        WHEN started_at IS NULL THEN NULL
                        WHEN ? >= started_at THEN ? - started_at
                        ELSE 0
                    END
                WHERE session_key IN ({placeholders})
                  AND status NOT IN (?, ?, ?, ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_tasks AS recoverable
                      WHERE recoverable.session_key = sessions.session_key
                        AND recoverable.status = ?
                        AND recoverable.terminal_reason = 'meta_control_restart_before_start'
                  )
                """,
                (
                    SessionStatus.FAILED,
                    ts,
                    ts,
                    ts,
                    ts,
                    *chunk,
                    *terminal_session_statuses,
                    AgentTaskStatus.ABANDONED,
                ),
            )
        if any(plan_run_reconciliation.values()):
            log.info(
                "plan_run.startup_reconciliation",
                extra=plan_run_reconciliation,
            )
        return count

    async def claim_recoverable_meta_control_tasks(
        self,
        *,
        limit: int = 64,
    ) -> list[RecoverableMetaControlTask]:
        """Claim accepted control tasks proven not to have started before restart.

        Running tasks are deliberately excluded: provider side effects may have
        occurred before the crash, so replaying them automatically would be
        unsafe. A claimed queued task is returned with its original transcript
        row and task identity; another crash marks it recoverable again.
        """

        bounded_limit = max(1, min(int(limit), 256))
        recovered: list[RecoverableMetaControlTask] = []
        now_ms = _now_ms()
        terminal_session_statuses = (
            SessionStatus.DONE,
            SessionStatus.FAILED,
            SessionStatus.KILLED,
            SessionStatus.TIMEOUT,
        )
        async with self._write_transaction("claim_meta_control_recovery") as conn:
            async def quarantine_invalid(task_id: str) -> None:
                await conn.execute(
                    """
                    UPDATE agent_tasks
                    SET terminal_reason = ?, updated_at = ?,
                        error_class = 'MetaControlRecoveryInvalid',
                        error_message = 'Durable MetaSkill control recovery data is invalid.'
                    WHERE task_id = ? AND status = ?
                      AND terminal_reason = 'meta_control_restart_before_start'
                    """,
                    (
                        _META_CONTROL_RECOVERY_INVALID_REASON,
                        now_ms,
                        task_id,
                        AgentTaskStatus.ABANDONED,
                    ),
                )

            # Invalid rows must not permanently head-of-line block later valid
            # controls when callers use a small limit. Every selected row is
            # either claimed or quarantined before the next bounded read.
            while len(recovered) < bounded_limit:
                remaining = bounded_limit - len(recovered)
                async with conn.execute(
                    """
                    SELECT task.*
                    FROM agent_tasks AS task
                    JOIN meta_control_intents AS intent
                      ON intent.accepted_task_id = task.task_id
                    JOIN sessions AS owner
                      ON owner.session_key = task.session_key
                    WHERE task.status = ?
                      AND task.terminal_reason = 'meta_control_restart_before_start'
                      AND intent.status = 'accepted'
                      AND owner.status NOT IN (?, ?, ?, ?)
                    ORDER BY task.created_at ASC, task.task_id ASC
                    LIMIT ?
                    """,
                    (
                        AgentTaskStatus.ABANDONED,
                        *terminal_session_statuses,
                        remaining,
                    ),
                ) as cur:
                    task_rows = await cur.fetchall()
                if not task_rows:
                    break

                for raw_task in task_rows:
                    task = AgentTaskRecord(**_deserialize_row(dict(raw_task)))
                    details = task.details if isinstance(task.details, dict) else {}
                    metadata = details.get("metadata")
                    message_id = details.get("persisted_user_message_id")
                    if not isinstance(metadata, dict) or not isinstance(message_id, str):
                        await quarantine_invalid(task.task_id)
                        continue
                    control = metadata.get("meta_control")
                    if not isinstance(control, dict):
                        await quarantine_invalid(task.task_id)
                        continue
                    async with conn.execute(
                        """
                        SELECT * FROM transcript_entries
                        WHERE session_key = ? AND message_id = ? AND role = 'user'
                        """,
                        (task.session_key, message_id),
                    ) as entry_cur:
                        entry_row = await entry_cur.fetchone()
                    if entry_row is None:
                        await quarantine_invalid(task.task_id)
                        continue
                    entry = TranscriptEntry(**_deserialize_row(dict(entry_row)))
                    if (
                        not isinstance(entry.turn_context, dict)
                        or entry.turn_context.get("meta_control") != control
                    ):
                        await quarantine_invalid(task.task_id)
                        continue
                    async with conn.execute(
                        """
                        UPDATE agent_tasks
                        SET status = ?, updated_at = ?, finished_at = NULL,
                            terminal_reason = NULL, error_class = NULL, error_message = NULL
                        WHERE task_id = ? AND status = ?
                          AND terminal_reason = 'meta_control_restart_before_start'
                        """,
                        (
                            AgentTaskStatus.QUEUED,
                            now_ms,
                            task.task_id,
                            AgentTaskStatus.ABANDONED,
                        ),
                    ) as update_cur:
                        if int(update_cur.rowcount or 0) != 1:
                            continue
                    task.status = AgentTaskStatus.QUEUED
                    task.updated_at = now_ms
                    task.finished_at = None
                    task.terminal_reason = None
                    task.error_class = None
                    task.error_message = None
                    recovered.append(RecoverableMetaControlTask(task=task, entry=entry))

            recovered_keys = sorted({item.task.session_key for item in recovered})
            for session_key in recovered_keys:
                await conn.execute(
                    """
                    UPDATE sessions
                    SET status = ?, updated_at = ?, ended_at = NULL, runtime_ms = NULL
                    WHERE session_key = ?
                      AND status NOT IN (?, ?, ?, ?)
                    """,
                    (
                        SessionStatus.RUNNING,
                        now_ms,
                        session_key,
                        *terminal_session_statuses,
                    ),
                )
        return recovered

    # ── Transcript CRUD ──────────────────────────────────────────────────────

    @staticmethod
    async def _raise_stale_epoch(
        conn: Any,
        *,
        session_key: str,
        expected_epoch: int,
    ) -> None:
        async with conn.execute(
            "SELECT epoch FROM sessions WHERE session_key = ?",
            (session_key,),
        ) as cur:
            row = await cur.fetchone()
        actual = int(row[0]) if row is not None else None
        raise StaleEpochError(
            f"Epoch mismatch for {session_key}: expected {expected_epoch}, got {actual}"
        )

    @classmethod
    async def _insert_transcript_entry(
        cls,
        conn: Any,
        entry: TranscriptEntry,
        *,
        expected_epoch: int | None,
    ) -> None:
        data = entry.model_dump(exclude={"id"})
        cols = list(data.keys())
        placeholders = ", ".join("?" for _ in cols)
        values = [_serialize(data[c]) for c in cols]

        if expected_epoch is None:
            await conn.execute(
                f"INSERT INTO transcript_entries ({', '.join(cols)}) "
                f"VALUES ({placeholders})",
                values,
            )
            return

        insert_sql = (
            f"INSERT INTO transcript_entries ({', '.join(cols)}) "
            f"SELECT {placeholders} "
            "WHERE EXISTS ("
            "  SELECT 1 FROM sessions "
            "  WHERE session_key = ? AND epoch = ?"
            ")"
        )
        async with conn.execute(
            insert_sql,
            values + [entry.session_key, expected_epoch],
        ) as cur:
            inserted = cur.rowcount or 0
        if inserted == 0:
            await cls._raise_stale_epoch(
                conn,
                session_key=entry.session_key,
                expected_epoch=expected_epoch,
            )

    async def append_transcript_entry(
        self, entry: TranscriptEntry, *, expected_epoch: int | None = None
    ) -> None:
        entry.session_key = canonicalize_session_key(entry.session_key)
        async with self._write_transaction("append_transcript_entry") as conn:
            await self._insert_transcript_entry(
                conn,
                entry,
                expected_epoch=expected_epoch,
            )

    @classmethod
    async def _upsert_transcript_entry(
        cls,
        conn: Any,
        entry: TranscriptEntry,
        *,
        expected_epoch: int | None,
    ) -> bool:
        """Materialize or replace one row identified by session + message id.

        ``message_id`` is not made a database-wide unique key: it is a
        caller-supplied identity scoped to the current session.  The enclosing
        write transaction serializes the select/update-or-insert decision, and
        the epoch guard remains identical to ordinary transcript writes.
        """

        if expected_epoch is not None:
            async with conn.execute(
                "SELECT epoch FROM sessions WHERE session_key = ?",
                (entry.session_key,),
            ) as cur:
                session_row = await cur.fetchone()
            if session_row is None or int(session_row[0]) != expected_epoch:
                await cls._raise_stale_epoch(
                    conn,
                    session_key=entry.session_key,
                    expected_epoch=expected_epoch,
                )

        async with conn.execute(
            """
            SELECT id, created_at
            FROM transcript_entries
            WHERE session_id = ? AND message_id = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (entry.session_id, entry.message_id),
        ) as cur:
            existing = await cur.fetchone()

        if existing is None:
            await cls._insert_transcript_entry(
                conn,
                entry,
                expected_epoch=expected_epoch,
            )
            return True

        entry.id = int(existing[0])
        entry.created_at = int(existing[1])
        data = entry.model_dump(exclude={"id", "created_at"})
        assignments = [f"{column} = ?" for column in data]
        values = [_serialize(data[column]) for column in data]
        values.append(entry.id)
        await conn.execute(
            f"UPDATE transcript_entries SET {', '.join(assignments)} WHERE id = ?",
            values,
        )
        return False

    async def upsert_transcript_entry_and_touch(
        self,
        entry: TranscriptEntry,
        *,
        expected_epoch: int,
        updated_at: int,
        token_delta: int = 0,
        mark_total_tokens_stale: bool = False,
    ) -> bool:
        """Upsert caller-identified content and touch its session once.

        Returns ``True`` only when a new row was materialized.  Replaying the
        same finalization therefore cannot add another transcript row or count
        its token delta a second time.
        """

        entry.session_key = canonicalize_session_key(entry.session_key)
        async with self._write_transaction("upsert_transcript_entry_and_touch") as conn:
            inserted = await self._upsert_transcript_entry(
                conn,
                entry,
                expected_epoch=expected_epoch,
            )
            effective_token_delta = token_delta if inserted else 0
            async with conn.execute(
                """
                UPDATE sessions
                SET updated_at = ?,
                    total_tokens = total_tokens + ?,
                    total_tokens_fresh = CASE WHEN ? THEN 0 ELSE total_tokens_fresh END
                WHERE session_key = ? AND epoch = ?
                """,
                (
                    updated_at,
                    effective_token_delta,
                    int(mark_total_tokens_stale and inserted),
                    entry.session_key,
                    expected_epoch,
                ),
            ) as cur:
                touched = cur.rowcount or 0
            if touched == 0:
                await self._raise_stale_epoch(
                    conn,
                    session_key=entry.session_key,
                    expected_epoch=expected_epoch,
                )
        return inserted

    async def append_transcript_entry_and_touch(
        self,
        entry: TranscriptEntry,
        *,
        expected_epoch: int,
        updated_at: int,
        token_delta: int = 0,
        mark_total_tokens_stale: bool = False,
    ) -> None:
        """Append one entry and narrowly touch its session in one transaction."""

        entry.session_key = canonicalize_session_key(entry.session_key)
        async with self._write_transaction("append_transcript_entry_and_touch") as conn:
            await self._insert_transcript_entry(
                conn,
                entry,
                expected_epoch=expected_epoch,
            )
            async with conn.execute(
                """
                UPDATE sessions
                SET updated_at = ?,
                    total_tokens = total_tokens + ?,
                    total_tokens_fresh = CASE WHEN ? THEN 0 ELSE total_tokens_fresh END
                WHERE session_key = ? AND epoch = ?
                """,
                (
                    updated_at,
                    token_delta,
                    int(mark_total_tokens_stale),
                    entry.session_key,
                    expected_epoch,
                ),
            ) as cur:
                touched = cur.rowcount or 0
            if touched == 0:
                await self._raise_stale_epoch(
                    conn,
                    session_key=entry.session_key,
                    expected_epoch=expected_epoch,
                )

    @staticmethod
    async def _select_canonical_transcript(
        conn: Any,
        session_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[TranscriptEntry]:
        """Read compacted archive rows plus the active tail on one connection."""

        limit_val = limit if limit is not None else -1
        sql = """
            SELECT
                original_entry_id AS id,
                session_id,
                session_key,
                message_id,
                role,
                content,
                tool_calls,
                tool_call_id,
                reasoning_content,
                turn_usage,
                turn_context,
                created_at,
                token_count,
                provenance_kind,
                provenance_origin_session_id,
                provenance_source_session_key,
                provenance_source_channel,
                provenance_source_tool,
                schema_version
            FROM compacted_transcript_entries
            WHERE session_id = ?
            UNION ALL
            SELECT
                id,
                session_id,
                session_key,
                message_id,
                role,
                content,
                tool_calls,
                tool_call_id,
                reasoning_content,
                turn_usage,
                turn_context,
                created_at,
                token_count,
                provenance_kind,
                provenance_origin_session_id,
                provenance_source_session_key,
                provenance_source_channel,
                provenance_source_tool,
                schema_version
            FROM transcript_entries
            WHERE session_id = ?
            ORDER BY created_at ASC, id ASC
            LIMIT ? OFFSET ?
        """
        async with conn.execute(
            sql,
            (session_id, session_id, limit_val, offset),
        ) as cur:
            rows = await cur.fetchall()
        return [TranscriptEntry(**_deserialize_row(dict(row))) for row in rows]

    @staticmethod
    async def _select_all_summaries(
        conn: Any,
        session_id: str,
    ) -> list[SessionSummary]:
        """Read all summaries on an existing operation/transaction connection."""

        async with conn.execute(
            "SELECT * FROM session_summaries WHERE session_id = ? "
            "ORDER BY compaction_index ASC",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [SessionSummary(**_deserialize_row(dict(row))) for row in rows]

    @staticmethod
    async def _select_turn_ingress_receipt(
        conn: Any,
        *,
        source_scope: str,
        request_session_key: str,
        client_request_id: str,
    ) -> tuple[
        TurnIngressReceipt,
        AgentTaskStatus | None,
        bool,
        dict[str, Any],
    ] | None:
        async with conn.execute(
            """
            SELECT receipt.*, task.status AS accepted_task_status,
                   task.details AS accepted_task_details
            FROM turn_ingress_receipts AS receipt
            LEFT JOIN agent_tasks AS task ON task.task_id = receipt.task_id
            WHERE receipt.source_scope = ?
              AND receipt.request_session_key = ?
              AND receipt.client_request_id = ?
            """,
            (source_scope, request_session_key, client_request_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        raw = dict(row)
        task_status_raw = raw.pop("accepted_task_status", None)
        task_details_raw = raw.pop("accepted_task_details", None)
        task_status = (
            AgentTaskStatus(task_status_raw) if task_status_raw is not None else None
        )
        task_details: dict[str, Any] = {}
        if isinstance(task_details_raw, str):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                parsed = json.loads(task_details_raw)
                if isinstance(parsed, dict):
                    task_details = parsed
        receipt = TurnIngressReceipt(**_deserialize_row(raw))
        return (
            receipt,
            task_status,
            bool(task_details.get("fresh_user_session", False)),
            task_details,
        )

    @_serialized_read
    async def get_turn_ingress_receipt(
        self,
        *,
        source_scope: str,
        request_session_key: str,
        client_request_id: str,
    ) -> TurnAcceptanceResult | None:
        """Look up an accepted request before re-running destructive ingest work."""

        selected = await self._select_turn_ingress_receipt(
            self.conn,
            source_scope=source_scope,
            request_session_key=canonicalize_session_key(request_session_key),
            client_request_id=client_request_id,
        )
        if selected is None:
            return None
        receipt, task_status, fresh_user_session, task_details = selected
        return TurnAcceptanceResult(
            receipt=receipt,
            replayed=True,
            fresh_user_session=fresh_user_session,
            task_status=task_status,
            goal_context=GoalTurnContext.from_task_detail(
                task_details.get("goal_context")
            ),
            goal_candidate=GoalClaimCandidate.from_task_detail(
                task_details.get("goal_candidate")
            ),
        )

    async def replay_turn_ingress_receipt(
        self,
        *,
        source_scope: str,
        request_session_key: str,
        client_request_id: str,
    ) -> TurnAcceptanceResult | None:
        """Replay accepted ingress and consume an obsolete Meta launch draft.

        Older builds could commit the ingress receipt before removing the
        browser-recovery draft. Keep the fast replay lookup transactional with
        that repair so the RPC shortcut preserves the same cleanup contract as
        :meth:`accept_turn`.
        """

        canonical_session_key = canonicalize_session_key(request_session_key)
        # Keep the common miss path read-only. Besides avoiding unnecessary
        # writer contention, this preserves the ingress admission contract: a
        # locked database is reported by the later atomic accept, where the RPC
        # layer can classify whether any write was accepted.
        previous = await self.get_turn_ingress_receipt(
            source_scope=source_scope,
            request_session_key=canonical_session_key,
            client_request_id=client_request_id,
        )
        if previous is None:
            return None
        async with self._write_transaction("replay_turn_ingress_receipt") as conn:
            selected = await self._select_turn_ingress_receipt(
                conn,
                source_scope=source_scope,
                request_session_key=canonical_session_key,
                client_request_id=client_request_id,
            )
            if selected is None:
                return None
            receipt, task_status, fresh_user_session, task_details = selected
            await conn.execute(
                """
                DELETE FROM meta_launch_drafts
                WHERE session_key = ? AND client_request_id = ?
                """,
                (canonical_session_key, client_request_id),
            )
            return TurnAcceptanceResult(
                receipt=receipt,
                replayed=True,
                fresh_user_session=fresh_user_session,
                task_status=task_status,
                goal_context=GoalTurnContext.from_task_detail(
                    task_details.get("goal_context")
                ),
                goal_candidate=GoalClaimCandidate.from_task_detail(
                    task_details.get("goal_candidate")
                ),
            )

    @staticmethod
    def _pending_chat_input_from_row(row: Any) -> PendingChatInput:
        raw = dict(row)
        payload_raw = raw.pop("payload_json", None)
        try:
            payload = json.loads(payload_raw) if isinstance(payload_raw, str) else None
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("pending chat input contains invalid payload JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("pending chat input payload must be an object")
        return PendingChatInput(payload=payload, **raw)

    @staticmethod
    async def _select_pending_chat_input(
        conn: Any,
        *,
        pending_input_id: str,
    ) -> PendingChatInput | None:
        async with conn.execute(
            "SELECT * FROM pending_chat_inputs WHERE pending_input_id = ?",
            (pending_input_id,),
        ) as cur:
            row = await cur.fetchone()
        return (
            SessionStorage._pending_chat_input_from_row(row)
            if row is not None
            else None
        )

    @staticmethod
    async def _select_pending_chat_input_cancellation(
        conn: Any,
        *,
        pending_input_id: str,
    ) -> tuple[str, int] | None:
        async with conn.execute(
            """
            SELECT session_key, cancelled_at
            FROM pending_chat_input_cancellations
            WHERE pending_input_id = ?
            """,
            (pending_input_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return str(row["session_key"]), int(row["cancelled_at"])

    @staticmethod
    async def _select_pending_chat_input_dispatch_receipt(
        conn: Any,
        *,
        pending_input_id: str,
    ) -> PendingChatInputDispatchReceipt | None:
        async with conn.execute(
            """
            SELECT * FROM pending_chat_input_dispatch_receipts
            WHERE pending_input_id = ?
            """,
            (pending_input_id,),
        ) as cur:
            row = await cur.fetchone()
        return (
            PendingChatInputDispatchReceipt(**_deserialize_row(dict(row)))
            if row is not None
            else None
        )

    @staticmethod
    async def _find_pending_chat_input_dispatch_receipts(
        conn: Any,
        *,
        pending_input_id: str,
        session_key: str,
        source_scope: str,
        client_request_id: str,
        client_message_id: str,
    ) -> list[PendingChatInputDispatchReceipt]:
        async with conn.execute(
            """
            SELECT * FROM pending_chat_input_dispatch_receipts
            WHERE pending_input_id = ?
               OR (
                    session_key = ?
                AND source_scope = ?
                AND client_request_id = ?
               )
               OR (session_key = ? AND client_message_id = ?)
            ORDER BY accepted_at ASC, pending_input_id ASC
            """,
            (
                pending_input_id,
                session_key,
                source_scope,
                client_request_id,
                session_key,
                client_message_id,
            ),
        ) as cur:
            rows = await cur.fetchall()
        return [
            PendingChatInputDispatchReceipt(**_deserialize_row(dict(row)))
            for row in rows
        ]

    @staticmethod
    async def _insert_pending_chat_input_dispatch_receipt(
        conn: Any,
        *,
        pending_input_id: str,
        session_key: str,
        source_scope: str,
        client_request_id: str,
        client_message_id: str,
        request_fingerprint: str,
        accepted_at: int,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO pending_chat_input_dispatch_receipts (
                pending_input_id, session_key, source_scope,
                client_request_id, client_message_id, request_fingerprint, accepted_at,
                schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                pending_input_id,
                session_key,
                source_scope,
                client_request_id,
                client_message_id,
                request_fingerprint,
                accepted_at,
            ),
        )

    async def enqueue_pending_chat_input(
        self,
        *,
        pending_input_id: str,
        session_key: str,
        source_scope: str,
        client_request_id: str,
        client_message_id: str,
        request_fingerprint: str,
        payload: dict[str, Any],
        position: int | None = None,
    ) -> tuple[PendingChatInput, bool]:
        """Stage one follow-up, returning ``(row, replayed)``.

        Capacity and all three stable identities are checked under the same
        write lock.  An ambiguous enqueue can therefore retry byte-for-byte;
        the retry either returns the original row or raises a hard conflict.
        """

        pending_input_id = pending_input_id.strip()
        session_key = canonicalize_session_key(session_key)
        source_scope = source_scope.strip()
        client_request_id = client_request_id.strip()
        client_message_id = client_message_id.strip()
        request_fingerprint = request_fingerprint.strip()
        identifiers = {
            "pending_input_id": pending_input_id,
            "session_key": session_key,
            "source_scope": source_scope,
            "client_request_id": client_request_id,
            "client_message_id": client_message_id,
            "request_fingerprint": request_fingerprint,
        }
        if any(not value for value in identifiers.values()):
            raise ValueError("pending input identities must be non-empty")
        for name in ("pending_input_id", "client_request_id", "client_message_id"):
            if len(identifiers[name]) > 256:
                raise ValueError(f"{name} must not exceed 256 characters")
        if len(session_key) > 512 or len(source_scope) > 256:
            raise ValueError("pending input session/source identity is too long")
        if not isinstance(payload, dict):
            raise ValueError("pending input payload must be an object")
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        now = _now_ms()
        async with self._write_transaction("enqueue_pending_chat_input") as conn:
            dispatch_receipts = (
                await self._find_pending_chat_input_dispatch_receipts(
                    conn,
                    pending_input_id=pending_input_id,
                    session_key=session_key,
                    source_scope=source_scope,
                    client_request_id=client_request_id,
                    client_message_id=client_message_id,
                )
            )
            if dispatch_receipts:
                exact = (
                    len(dispatch_receipts) == 1
                    and dispatch_receipts[0].pending_input_id == pending_input_id
                    and dispatch_receipts[0].session_key == session_key
                    and dispatch_receipts[0].source_scope == source_scope
                    and dispatch_receipts[0].client_request_id
                    == client_request_id
                    and dispatch_receipts[0].client_message_id
                    == client_message_id
                    and dispatch_receipts[0].request_fingerprint
                    == request_fingerprint
                )
                if exact:
                    raise PendingChatInputAlreadyDispatchedError(
                        "pending input was already dispatched"
                    )
                raise PendingChatInputConflictError(
                    "pending input dispatch identity was already used"
                )
            cancellation = await self._select_pending_chat_input_cancellation(
                conn,
                pending_input_id=pending_input_id,
            )
            if cancellation is not None:
                cancelled_session_key, _cancelled_at = cancellation
                if cancelled_session_key != session_key:
                    raise PendingChatInputConflictError(
                        "pending input cancellation belongs to a different session"
                    )
                raise PendingChatInputCancelledError(
                    "pending input was durably cancelled"
                )
            async with conn.execute(
                """
                SELECT * FROM pending_chat_inputs
                WHERE pending_input_id = ?
                   OR (session_key = ? AND client_request_id = ?)
                   OR (session_key = ? AND client_message_id = ?)
                ORDER BY created_at ASC
                """,
                (
                    pending_input_id,
                    session_key,
                    client_request_id,
                    session_key,
                    client_message_id,
                ),
            ) as cur:
                matches = await cur.fetchall()
            if matches:
                rows = [self._pending_chat_input_from_row(row) for row in matches]
                first = rows[0]
                exact = (
                    len(rows) == 1
                    and first.pending_input_id == pending_input_id
                    and first.session_key == session_key
                    and first.source_scope == source_scope
                    and first.client_request_id == client_request_id
                    and first.client_message_id == client_message_id
                    and first.request_fingerprint == request_fingerprint
                    and first.payload == payload
                )
                if exact:
                    return first, True
                raise PendingChatInputConflictError(
                    "pending input identity was already used for a different payload"
                )

            async with conn.execute(
                "SELECT COUNT(*) FROM pending_chat_inputs WHERE session_key = ?",
                (session_key,),
            ) as cur:
                count_row = await cur.fetchone()
            if int(count_row[0] if count_row is not None else 0) >= MAX_PENDING_CHAT_INPUTS:
                raise PendingChatInputCapacityError(
                    f"session already has {MAX_PENDING_CHAT_INPUTS} pending inputs"
                )
            if position is None:
                async with conn.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 "
                    "FROM pending_chat_inputs WHERE session_key = ?",
                    (session_key,),
                ) as cur:
                    position_row = await cur.fetchone()
                position = int(position_row[0] if position_row is not None else 0)
            if isinstance(position, bool) or not isinstance(position, int) or position < 0:
                raise ValueError("position must be a non-negative integer")
            await conn.execute(
                """
                INSERT INTO pending_chat_inputs (
                    pending_input_id, session_key, source_scope,
                    client_request_id, client_message_id, request_fingerprint,
                    payload_json, position, state_revision, created_at, updated_at,
                    schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 1)
                """,
                (
                    pending_input_id,
                    session_key,
                    source_scope,
                    client_request_id,
                    client_message_id,
                    request_fingerprint,
                    payload_json,
                    position,
                    now,
                    now,
                ),
            )
            row = await self._select_pending_chat_input(
                conn,
                pending_input_id=pending_input_id,
            )
            assert row is not None
            return row, False

    @_serialized_read
    async def get_pending_chat_input(
        self,
        pending_input_id: str,
    ) -> PendingChatInput | None:
        return await self._select_pending_chat_input(
            self.conn,
            pending_input_id=pending_input_id.strip(),
        )

    @_serialized_read
    async def get_pending_chat_input_dispatch_receipt(
        self,
        pending_input_id: str,
    ) -> PendingChatInputDispatchReceipt | None:
        return await self._select_pending_chat_input_dispatch_receipt(
            self.conn,
            pending_input_id=pending_input_id.strip(),
        )

    async def consume_replayed_pending_chat_input(
        self,
        *,
        pending_input_id: str,
        session_key: str,
        source_scope: str,
        client_request_id: str,
        client_message_id: str,
        request_fingerprint: str,
        expected_revision: int,
    ) -> bool:
        """Remove a stale staged row after its turn receipt already committed.

        Older or racing clients may retry enqueue after another tab committed
        dispatch but before observing its acknowledgement.  The accepted turn
        remains exactly once through ``turn_ingress_receipts``; this repair
        consumes the otherwise permanent queue ghost only after all durable
        request and message identities match the dispatch receipt.
        """

        pending_input_id = pending_input_id.strip()
        session_key = canonicalize_session_key(session_key)
        source_scope = source_scope.strip()
        client_request_id = client_request_id.strip()
        client_message_id = client_message_id.strip()
        request_fingerprint = request_fingerprint.strip()
        if any(
            not value
            for value in (
                pending_input_id,
                session_key,
                source_scope,
                client_request_id,
                client_message_id,
                request_fingerprint,
            )
        ):
            raise ValueError("pending dispatch replay identities must be non-empty")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError("expected_revision must be a positive integer")

        async with self._write_transaction(
            "consume_replayed_pending_chat_input"
        ) as conn:
            receipts = await self._find_pending_chat_input_dispatch_receipts(
                conn,
                pending_input_id=pending_input_id,
                session_key=session_key,
                source_scope=source_scope,
                client_request_id=client_request_id,
                client_message_id=client_message_id,
            )
            if len(receipts) != 1:
                raise PendingChatInputConflictError(
                    "pending input dispatch receipt is missing or ambiguous"
                )
            receipt = receipts[0]
            if (
                receipt.session_key != session_key
                or receipt.source_scope != source_scope
                or receipt.client_request_id != client_request_id
                or receipt.client_message_id != client_message_id
                or receipt.request_fingerprint != request_fingerprint
            ):
                raise PendingChatInputConflictError(
                    "pending input does not match its accepted dispatch receipt"
                )

            pending = await self._select_pending_chat_input(
                conn,
                pending_input_id=pending_input_id,
            )
            if pending is None:
                return False
            if (
                pending.session_key != session_key
                or pending.source_scope != source_scope
                or pending.client_request_id != client_request_id
                or pending.client_message_id != client_message_id
                or pending.request_fingerprint != request_fingerprint
                or pending.state_revision != expected_revision
            ):
                raise PendingChatInputConflictError(
                    "pending input changed before dispatch replay cleanup"
                )
            async with conn.execute(
                """
                DELETE FROM pending_chat_inputs
                WHERE pending_input_id = ?
                  AND session_key = ?
                  AND source_scope = ?
                  AND client_request_id = ?
                  AND client_message_id = ?
                  AND request_fingerprint = ?
                  AND state_revision = ?
                """,
                (
                    pending_input_id,
                    session_key,
                    source_scope,
                    client_request_id,
                    client_message_id,
                    request_fingerprint,
                    expected_revision,
                ),
            ) as cur:
                consumed = int(cur.rowcount or 0)
            if consumed != 1:
                raise PendingChatInputConflictError(
                    "pending input changed before dispatch replay cleanup"
                )
            return True

    @_serialized_read
    async def list_pending_chat_inputs(self, session_key: str) -> list[PendingChatInput]:
        session_key = canonicalize_session_key(session_key)
        async with self.conn.execute(
            """
            SELECT * FROM pending_chat_inputs
            WHERE session_key = ?
            ORDER BY position ASC, created_at ASC, pending_input_id ASC
            """,
            (session_key,),
        ) as cur:
            rows = await cur.fetchall()
        return [self._pending_chat_input_from_row(row) for row in rows]

    async def update_pending_chat_input(
        self,
        pending_input_id: str,
        *,
        session_key: str,
        expected_revision: int,
        position: int,
    ) -> PendingChatInput:
        """Move one row using a monotonic compare-and-set revision."""

        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError("expected_revision must be a positive integer")
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise ValueError("position must be a non-negative integer")
        pending_input_id = pending_input_id.strip()
        session_key = canonicalize_session_key(session_key)
        async with self._write_transaction("update_pending_chat_input") as conn:
            async with conn.execute(
                """
                UPDATE pending_chat_inputs
                SET position = ?, state_revision = state_revision + 1, updated_at = ?
                WHERE pending_input_id = ? AND session_key = ? AND state_revision = ?
                """,
                (position, _now_ms(), pending_input_id, session_key, expected_revision),
            ) as cur:
                changed = int(cur.rowcount or 0)
            if changed != 1:
                existing = await self._select_pending_chat_input(
                    conn,
                    pending_input_id=pending_input_id,
                )
                if existing is None:
                    raise PendingChatInputNotFoundError(pending_input_id)
                if existing.session_key != session_key:
                    raise PendingChatInputConflictError(
                        "pending input belongs to a different session"
                    )
                raise PendingChatInputConflictError(
                    "pending input revision changed before update"
                )
            row = await self._select_pending_chat_input(
                conn,
                pending_input_id=pending_input_id,
            )
            assert row is not None
            return row

    async def reorder_pending_chat_inputs(
        self,
        *,
        session_key: str,
        expected_revisions: list[tuple[str, int]],
    ) -> list[PendingChatInput]:
        """Replace one session's complete pending order atomically.

        The caller supplies every currently staged row in the desired order.
        Comparing the complete identity set and every state revision prevents a
        concurrent enqueue, cancel, dispatch, or peer reorder from producing a
        partially-applied order.
        """

        session_key = canonicalize_session_key(session_key)
        if not 2 <= len(expected_revisions) <= MAX_PENDING_CHAT_INPUTS:
            raise ValueError(
                f"expected_revisions must contain 2-{MAX_PENDING_CHAT_INPUTS} rows"
            )
        pending_ids: list[str] = []
        revisions: dict[str, int] = {}
        for raw_pending_id, revision in expected_revisions:
            pending_input_id = raw_pending_id.strip()
            if not pending_input_id or len(pending_input_id) > 256:
                raise ValueError("pending_input_id must be a non-empty bounded string")
            if pending_input_id in revisions:
                raise ValueError("pending_input_id values must be unique")
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
            ):
                raise ValueError("expected revision must be a positive integer")
            pending_ids.append(pending_input_id)
            revisions[pending_input_id] = revision

        async with self._write_transaction("reorder_pending_chat_inputs") as conn:
            async with conn.execute(
                """
                SELECT * FROM pending_chat_inputs
                WHERE session_key = ?
                ORDER BY position ASC, created_at ASC, pending_input_id ASC
                """,
                (session_key,),
            ) as cur:
                current_rows = await cur.fetchall()
            current = [self._pending_chat_input_from_row(row) for row in current_rows]
            if len(current) != len(pending_ids):
                raise PendingChatInputConflictError(
                    "pending input set changed before reorder"
                )
            current_by_id = {row.pending_input_id: row for row in current}
            if set(current_by_id) != set(pending_ids):
                raise PendingChatInputConflictError(
                    "pending input set changed before reorder"
                )
            for pending_input_id, expected_revision in revisions.items():
                if current_by_id[pending_input_id].state_revision != expected_revision:
                    raise PendingChatInputConflictError(
                        "pending input revision changed before reorder"
                    )

            updated_at = _now_ms()
            for position, pending_input_id in enumerate(pending_ids):
                async with conn.execute(
                    """
                    UPDATE pending_chat_inputs
                    SET position = ?, state_revision = state_revision + 1,
                        updated_at = ?
                    WHERE pending_input_id = ? AND session_key = ?
                      AND state_revision = ?
                    """,
                    (
                        position,
                        updated_at,
                        pending_input_id,
                        session_key,
                        revisions[pending_input_id],
                    ),
                ) as cur:
                    if int(cur.rowcount or 0) != 1:
                        raise PendingChatInputConflictError(
                            "pending input changed during reorder"
                        )

            async with conn.execute(
                """
                SELECT * FROM pending_chat_inputs
                WHERE session_key = ?
                ORDER BY position ASC, created_at ASC, pending_input_id ASC
                """,
                (session_key,),
            ) as cur:
                reordered_rows = await cur.fetchall()
            return [self._pending_chat_input_from_row(row) for row in reordered_rows]

    async def cancel_pending_chat_input(
        self,
        pending_input_id: str,
        *,
        session_key: str,
        expected_revision: int | None = None,
    ) -> bool:
        """Cancel one staged row; missing rows are an idempotent success."""

        pending_input_id = pending_input_id.strip()
        session_key = canonicalize_session_key(session_key)
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError("expected_revision must be a positive integer")
        async with self._write_transaction("cancel_pending_chat_input") as conn:
            existing = await self._select_pending_chat_input(
                conn,
                pending_input_id=pending_input_id,
            )
            if existing is None:
                dispatch_receipt = (
                    await self._select_pending_chat_input_dispatch_receipt(
                        conn,
                        pending_input_id=pending_input_id,
                    )
                )
                if dispatch_receipt is not None:
                    if dispatch_receipt.session_key != session_key:
                        raise PendingChatInputConflictError(
                            "pending input dispatch belongs to a different session"
                        )
                    return False
                cancellation = await self._select_pending_chat_input_cancellation(
                    conn,
                    pending_input_id=pending_input_id,
                )
                if cancellation is not None:
                    if cancellation[0] != session_key:
                        raise PendingChatInputConflictError(
                            "pending input cancellation belongs to a different session"
                        )
                    return False
                await conn.execute(
                    """
                    INSERT INTO pending_chat_input_cancellations (
                        pending_input_id, session_key, cancelled_at, schema_version
                    ) VALUES (?, ?, ?, 1)
                    """,
                    (pending_input_id, session_key, _now_ms()),
                )
                return False
            if existing.session_key != session_key:
                raise PendingChatInputConflictError(
                    "pending input belongs to a different session"
                )
            if (
                expected_revision is not None
                and existing.state_revision != expected_revision
            ):
                raise PendingChatInputConflictError(
                    "pending input revision changed before cancellation"
                )
            dispatch_receipt = await self._select_pending_chat_input_dispatch_receipt(
                conn,
                pending_input_id=pending_input_id,
            )
            if dispatch_receipt is not None:
                if dispatch_receipt.session_key != session_key:
                    raise PendingChatInputConflictError(
                        "pending input dispatch belongs to a different session"
                    )
            else:
                cancellation = await self._select_pending_chat_input_cancellation(
                    conn,
                    pending_input_id=pending_input_id,
                )
                if cancellation is not None and cancellation[0] != session_key:
                    raise PendingChatInputConflictError(
                        "pending input cancellation belongs to a different session"
                    )
                if cancellation is None:
                    await conn.execute(
                        """
                        INSERT INTO pending_chat_input_cancellations (
                            pending_input_id, session_key, cancelled_at, schema_version
                        ) VALUES (?, ?, ?, 1)
                        """,
                        (pending_input_id, session_key, _now_ms()),
                    )
            await conn.execute(
                "DELETE FROM pending_chat_inputs WHERE pending_input_id = ?",
                (pending_input_id,),
            )
            return True

    @staticmethod
    async def _select_meta_control_intent(
        conn: Any,
        *,
        session_key: str,
        control_kind: str,
        correlation_id: str,
    ) -> MetaControlIntent | None:
        async with conn.execute(
            """
            SELECT * FROM meta_control_intents
            WHERE session_key = ? AND control_kind = ? AND correlation_id = ?
            """,
            (session_key, control_kind, correlation_id),
        ) as cur:
            row = await cur.fetchone()
        return MetaControlIntent(**_deserialize_row(dict(row))) if row is not None else None

    async def stage_meta_control_intent(
        self,
        *,
        session_key: str,
        control_kind: str,
        correlation_id: str,
        meta_skill_name: str,
        replay_run_id: str | None = None,
        replay_mode: str | None = None,
    ) -> tuple[MetaControlIntent, str]:
        """Durably stage one manual launch or committed replay authorization.

        Repeating the same coordinates is idempotent even after acceptance.
        Reusing their correlation identity for a different skill/run/mode is a
        hard conflict. Staged rows have a 30-day recovery window, far longer
        than the browser outbox, so long turns and restarts remain safe without
        allowing abandoned pre-send authorizations to grow forever.
        """

        session_key = canonicalize_session_key(session_key)
        control_kind = control_kind.strip()
        correlation_id = correlation_id.strip()
        meta_skill_name = meta_skill_name.strip()
        replay_run_id = replay_run_id.strip() if isinstance(replay_run_id, str) else None
        replay_mode = replay_mode.strip() if isinstance(replay_mode, str) else None
        if not session_key or not correlation_id or not meta_skill_name:
            raise ValueError("meta control session, correlation, and skill are required")
        if control_kind not in {"manual", "replay"}:
            raise ValueError("meta control kind must be manual or replay")
        if len(correlation_id) > 272:
            raise ValueError("meta control correlation exceeds 272 characters")
        if control_kind == "manual":
            if not correlation_id.startswith("request:"):
                raise ValueError("manual meta control requires a request correlation")
            if replay_run_id is not None or replay_mode is not None:
                raise ValueError("manual meta control cannot carry replay coordinates")
            session_key, request_id = normalize_meta_launch_coordinates(
                session_key,
                correlation_id.removeprefix("request:"),
            )
            correlation_id = f"request:{request_id}"
        elif (
            not correlation_id.startswith("nonce:")
            or replay_run_id is None
            or replay_mode not in {"failed-step", "partial-context"}
        ):
            raise ValueError("replay meta control requires nonce, run, and live mode")

        async with self._write_transaction("stage_meta_control_intent") as conn:
            return await self._stage_meta_control_intent_in_transaction(
                conn,
                session_key=session_key,
                control_kind=control_kind,
                correlation_id=correlation_id,
                meta_skill_name=meta_skill_name,
                replay_run_id=replay_run_id,
                replay_mode=replay_mode,
            )

    async def _stage_meta_control_intent_in_transaction(
        self,
        conn: Any,
        *,
        session_key: str,
        control_kind: str,
        correlation_id: str,
        meta_skill_name: str,
        replay_run_id: str | None,
        replay_mode: str | None,
    ) -> tuple[MetaControlIntent, str]:
        """Insert or replay a validated control on an existing write transaction."""

        now_ms = _now_ms()
        cutoff_ms = now_ms - _META_CONTROL_STAGED_RETENTION_MS
        await conn.execute(
            """
            DELETE FROM meta_control_intents
            WHERE intent_id IN (
                SELECT intent_id FROM meta_control_intents
                WHERE status = 'staged' AND created_at < ?
                ORDER BY created_at ASC, intent_id ASC
                LIMIT ?
            )
            """,
            (cutoff_ms, _META_CONTROL_STAGED_GC_BATCH),
        )
        if control_kind == "manual":
            request_id = correlation_id.removeprefix("request:")
            await conn.execute(
                """
                DELETE FROM meta_launch_discard_tombstones
                WHERE session_key = ? AND client_request_id = ? AND expires_at <= ?
                """,
                (session_key, request_id, now_ms),
            )
            async with conn.execute(
                """
                SELECT 1 FROM meta_launch_discard_tombstones
                WHERE session_key = ? AND client_request_id = ? AND expires_at > ?
                """,
                (session_key, request_id, now_ms),
            ) as cur:
                discarded = await cur.fetchone()
            if discarded is not None:
                raise MetaLaunchDraftDiscardedError(
                    "meta launch draft identity was explicitly discarded"
                )
        existing = await self._select_meta_control_intent(
            conn,
            session_key=session_key,
            control_kind=control_kind,
            correlation_id=correlation_id,
        )
        if existing is not None:
            if (
                existing.meta_skill_name != meta_skill_name
                or existing.replay_run_id != replay_run_id
                or existing.replay_mode != replay_mode
            ):
                raise MetaControlIntentConflictError(
                    "meta control identity was already used for another launch"
                )
            return existing, "replayed"

        if control_kind == "manual":
            await self._ensure_meta_launch_coordinate_capacity(
                conn,
                now_ms=now_ms,
                session_key=session_key,
                client_request_id=correlation_id.removeprefix("request:"),
            )

        intent = MetaControlIntent(
            session_key=session_key,
            control_kind=control_kind,
            correlation_id=correlation_id,
            meta_skill_name=meta_skill_name,
            replay_run_id=replay_run_id,
            replay_mode=replay_mode,
        )
        data = intent.model_dump()
        columns = list(data)
        await conn.execute(
            f"INSERT INTO meta_control_intents ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            [_serialize(data[column]) for column in columns],
        )
        return intent, "stamped"

    @_serialized_read
    async def get_meta_control_intent(
        self,
        *,
        session_key: str,
        control_kind: str,
        correlation_id: str,
    ) -> MetaControlIntent | None:
        """Return the exact durable control authorization, if one exists."""

        return await self._select_meta_control_intent(
            self.conn,
            session_key=canonicalize_session_key(session_key),
            control_kind=control_kind,
            correlation_id=correlation_id,
        )

    @staticmethod
    async def _select_meta_launch_draft(
        conn: Any,
        *,
        session_key: str,
        client_request_id: str,
    ) -> MetaLaunchDraft | None:
        async with conn.execute(
            """
            SELECT * FROM meta_launch_drafts
            WHERE session_key = ? AND client_request_id = ?
            """,
            (session_key, client_request_id),
        ) as cur:
            row = await cur.fetchone()
        return MetaLaunchDraft(**_deserialize_row(dict(row))) if row is not None else None

    async def _select_live_meta_launch_coordinates(
        self,
        conn: Any,
        *,
        now_ms: int,
        session_key: str | None = None,
        include_drafts: bool = True,
        include_tombstones: bool = True,
        include_staged: bool = True,
        include_accepted: bool = True,
        exclude_intent_id: str | None = None,
    ) -> set[tuple[str, str]]:
        """Return the bounded live launch identities represented across ledgers.

        One browser request may briefly exist as both a raw draft and a staged
        control. Capacity is therefore defined over exact coordinates, not row
        counts. Accepted controls contribute only the newest browser-outbox
        window per session; older accepted history is not a live resend source.
        """

        canonical_session = canonicalize_session_key(session_key) if session_key else ""
        coordinates: set[tuple[str, str]] = set()

        def append_coordinate(raw_session: object, raw_request: object) -> None:
            try:
                coordinate = normalize_meta_launch_coordinates(
                    raw_session,
                    raw_request,
                )
            except ValueError:
                # Older ledgers may contain identifiers that current ingress no
                # longer accepts. They cannot be replayed through the bounded RPC.
                return
            if canonical_session and coordinate[0] != canonical_session:
                return
            coordinates.add(coordinate)

        async def append_rows(sql: str, params: tuple[Any, ...]) -> None:
            async with conn.execute(sql, params) as cur:
                for row in await cur.fetchall():
                    append_coordinate(row[0], row[1])

        session_clause = " AND session_key = ?" if canonical_session else ""
        session_params: tuple[Any, ...] = (canonical_session,) if canonical_session else ()
        if include_drafts:
            await append_rows(
                "SELECT session_key, client_request_id FROM meta_launch_drafts "
                f"WHERE expires_at > ?{session_clause}",
                (now_ms, *session_params),
            )
        if include_tombstones:
            await append_rows(
                "SELECT session_key, client_request_id "
                "FROM meta_launch_discard_tombstones "
                f"WHERE expires_at > ?{session_clause}",
                (now_ms, *session_params),
            )
        if include_staged:
            exclude_clause = " AND intent_id <> ?" if exclude_intent_id else ""
            exclude_params: tuple[Any, ...] = (
                (exclude_intent_id,) if exclude_intent_id else ()
            )
            await append_rows(
                "SELECT session_key, substr(correlation_id, 9) "
                "FROM meta_control_intents "
                "WHERE control_kind = 'manual' AND status = 'staged' "
                "AND correlation_id LIKE 'request:%' AND created_at > ?"
                f"{session_clause}{exclude_clause}",
                (
                    now_ms - _META_CONTROL_STAGED_RETENTION_MS,
                    *session_params,
                    *exclude_params,
                ),
            )
        if include_accepted:
            exclude_clause = " AND intent_id <> ?" if exclude_intent_id else ""
            exclude_params = (exclude_intent_id,) if exclude_intent_id else ()
            if canonical_session:
                accepted_sql = (
                    "SELECT session_key, substr(correlation_id, 9) "
                    "FROM meta_control_intents "
                    "WHERE control_kind = 'manual' AND status = 'accepted' "
                    "AND correlation_id LIKE 'request:%' AND updated_at > ?"
                    f"{session_clause}{exclude_clause} "
                    "ORDER BY updated_at DESC, intent_id DESC LIMIT ?"
                )
                accepted_params = (
                    now_ms - _META_LAUNCH_DRAFT_RETENTION_MS,
                    *session_params,
                    *exclude_params,
                    _META_LAUNCH_ACCEPTED_PER_SESSION_LIMIT,
                )
            else:
                # Rank in SQLite so the selector never materializes the full
                # seven-day acceptance history in Python. The outer allowance
                # covers every coordinate already selected plus one decisive
                # row beyond the global capacity bound, even when ledgers
                # temporarily overlap during an atomic hand-off.
                accepted_sql = (
                    "SELECT session_key, client_request_id FROM ("
                    "SELECT session_key, substr(correlation_id, 9) AS client_request_id, "
                    "updated_at, intent_id, "
                    "ROW_NUMBER() OVER (PARTITION BY session_key "
                    "ORDER BY updated_at DESC, intent_id DESC) AS accepted_rank "
                    "FROM meta_control_intents "
                    "WHERE control_kind = 'manual' AND status = 'accepted' "
                    "AND correlation_id LIKE 'request:%' AND updated_at > ?"
                    f"{exclude_clause}"
                    ") WHERE accepted_rank <= ? "
                    "ORDER BY session_key ASC, updated_at DESC, intent_id DESC LIMIT ?"
                )
                accepted_params = (
                    now_ms - _META_LAUNCH_DRAFT_RETENTION_MS,
                    *exclude_params,
                    _META_LAUNCH_ACCEPTED_PER_SESSION_LIMIT,
                    _META_LAUNCH_DISCARD_GLOBAL_LIMIT + len(coordinates) + 1,
                )
            async with conn.execute(accepted_sql, accepted_params) as cur:
                accepted_counts: dict[str, int] = {}
                for row in await cur.fetchall():
                    row_session = canonicalize_session_key(str(row[0]))
                    seen = accepted_counts.get(row_session, 0)
                    if seen >= _META_LAUNCH_ACCEPTED_PER_SESSION_LIMIT:
                        continue
                    accepted_counts[row_session] = seen + 1
                    append_coordinate(row[0], row[1])

        return coordinates

    async def _ensure_meta_launch_coordinate_capacity(
        self,
        conn: Any,
        *,
        now_ms: int,
        session_key: str,
        client_request_id: str,
    ) -> None:
        """Reserve one exact live coordinate without exceeding either bound."""

        coordinate = normalize_meta_launch_coordinates(session_key, client_request_id)
        live = await self._select_live_meta_launch_coordinates(conn, now_ms=now_ms)
        if coordinate in live:
            return
        if sum(1 for item in live if item[0] == coordinate[0]) >= (
            _META_LAUNCH_DISCARD_PER_SESSION_LIMIT
        ):
            raise MetaLaunchDraftCapacityError(
                "MetaSkill cancellation retention is full for this session"
            )
        if len(live) >= _META_LAUNCH_DISCARD_GLOBAL_LIMIT:
            raise MetaLaunchDraftCapacityError("MetaSkill cancellation retention is full")

    @_serialized_read
    async def is_meta_launch_discarded(
        self,
        *,
        session_key: str,
        client_request_id: str,
    ) -> bool:
        """Return whether a live terminal marker fences this request identity."""

        session_key, client_request_id = normalize_meta_launch_coordinates(
            session_key,
            client_request_id,
        )
        async with self.conn.execute(
            """
            SELECT 1 FROM meta_launch_discard_tombstones
            WHERE session_key = ? AND client_request_id = ? AND expires_at > ?
            """,
            (session_key, client_request_id, _now_ms()),
        ) as cur:
            return await cur.fetchone() is not None

    @staticmethod
    async def _purge_expired_meta_launch_drafts(
        conn: Any,
        *,
        now_ms: int,
        limit: int,
    ) -> int:
        """Delete bounded pages of expired raw prompts and cancellation markers."""

        bounded_limit = max(1, min(int(limit), _META_LAUNCH_DRAFT_GLOBAL_LIMIT))
        await conn.execute(
            """
            DELETE FROM meta_launch_discard_tombstones
            WHERE rowid IN (
                SELECT rowid
                FROM meta_launch_discard_tombstones
                WHERE expires_at <= ?
                ORDER BY expires_at ASC, created_at ASC
                LIMIT ?
            )
            """,
            (now_ms, bounded_limit),
        )
        async with conn.execute(
            """
            SELECT draft_id, session_key, client_request_id
            FROM meta_launch_drafts
            WHERE expires_at <= ?
            ORDER BY expires_at ASC, draft_id ASC
            LIMIT ?
            """,
            (now_ms, bounded_limit),
        ) as cur:
            expired = await cur.fetchall()
        if not expired:
            return 0

        # Revoke the one-shot authorization before deleting the only row that
        # correlates it to the expiring raw request.
        for row in expired:
            await conn.execute(
                """
                DELETE FROM meta_control_intents
                WHERE session_key = ? AND control_kind = 'manual'
                  AND correlation_id = ? AND status = 'staged'
                """,
                (str(row["session_key"]), f"request:{row['client_request_id']}"),
            )
        placeholders = ", ".join("?" for _ in expired)
        await conn.execute(
            f"DELETE FROM meta_launch_drafts WHERE draft_id IN ({placeholders})",
            [str(row["draft_id"]) for row in expired],
        )
        return len(expired)

    async def _tombstone_meta_launches_for_boundary(
        self,
        conn: Any,
        *,
        session_key: str,
        now_ms: int,
        intent_statuses: tuple[str, ...],
        exclude_intent_id: str | None = None,
        exclude_client_request_id: str | None = None,
    ) -> int:
        """Fence stale MetaSkill identities while erasing their raw content."""

        statuses = tuple(
            status for status in intent_statuses if status in {"staged", "accepted"}
        )
        if not statuses:
            return 0
        await self._purge_expired_meta_launch_drafts(
            conn,
            now_ms=now_ms,
            limit=_META_LAUNCH_DRAFT_GC_BATCH,
        )
        coordinates = await self._select_live_meta_launch_coordinates(
            conn,
            now_ms=now_ms,
            session_key=session_key,
            include_tombstones=False,
            include_staged="staged" in statuses,
            include_accepted="accepted" in statuses,
            exclude_intent_id=exclude_intent_id,
        )
        request_ids = {
            request_id
            for _coordinate_session, request_id in coordinates
            if request_id != exclude_client_request_id
        }

        for request_id in sorted(request_ids):
            await conn.execute(
                """
                INSERT INTO meta_launch_discard_tombstones (
                    session_key, client_request_id, created_at, expires_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(session_key, client_request_id) DO UPDATE SET
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                WHERE meta_launch_discard_tombstones.expires_at <= excluded.created_at
                """,
                (
                    session_key,
                    request_id,
                    now_ms,
                    now_ms + _META_LAUNCH_DRAFT_RETENTION_MS,
                ),
            )
        return len(request_ids)

    async def stage_meta_launch_draft(
        self,
        *,
        session_key: str,
        client_request_id: str,
        meta_skill_name: str,
        launch_text: str,
    ) -> tuple[MetaLaunchDraft, str]:
        """Retain one exact manual request until its hidden turn is accepted.

        The outbox is deliberately small and short-lived because ``launch_text``
        is user content.  Stable request identities are immutable, making
        retries safe after an RPC response loss or application restart.
        """

        session_key, client_request_id = normalize_meta_launch_coordinates(
            session_key,
            client_request_id,
        )
        meta_skill_name = meta_skill_name.strip()
        if (
            not meta_skill_name
            or len(meta_skill_name) > 256
            or any(character.isspace() for character in meta_skill_name)
        ):
            raise ValueError("meta launch draft skill is invalid")
        if not isinstance(launch_text, str) or not launch_text or len(launch_text) > 128_000:
            raise ValueError("meta launch draft content is invalid")
        prefix = f"/meta {meta_skill_name}"
        suffix = launch_text[len(prefix) :] if launch_text.startswith(prefix) else ""
        if launch_text != prefix:
            if not suffix.startswith(" --") or (len(suffix) > 3 and not suffix[3].isspace()):
                raise ValueError("meta launch draft does not match its skill")

        now_ms = _now_ms()
        async with self._write_transaction("stage_meta_launch_draft") as conn:
            await self._purge_expired_meta_launch_drafts(
                conn,
                now_ms=now_ms,
                limit=_META_LAUNCH_DRAFT_GC_BATCH,
            )
            # The bounded global GC page may not include this coordinate when
            # many markers expire together. Remove its own expired marker so a
            # legitimate reuse after the retention window is deterministic.
            await conn.execute(
                """
                DELETE FROM meta_launch_discard_tombstones
                WHERE session_key = ? AND client_request_id = ? AND expires_at <= ?
                """,
                (session_key, client_request_id, now_ms),
            )
            async with conn.execute(
                """
                SELECT 1
                FROM meta_launch_discard_tombstones
                WHERE session_key = ? AND client_request_id = ? AND expires_at > ?
                """,
                (session_key, client_request_id, now_ms),
            ) as cur:
                discarded = await cur.fetchone()
            if discarded is not None:
                raise MetaLaunchDraftDiscardedError(
                    "meta launch draft identity was explicitly discarded"
                )
            existing = await self._select_meta_launch_draft(
                conn,
                session_key=session_key,
                client_request_id=client_request_id,
            )
            if existing is not None:
                if (
                    existing.meta_skill_name != meta_skill_name
                    or existing.launch_text != launch_text
                ):
                    raise MetaLaunchDraftConflictError(
                        "meta launch draft identity was already used for another request"
                    )
                return existing, "replayed"

            async with conn.execute(
                "SELECT COUNT(*) FROM meta_launch_drafts WHERE session_key = ?",
                (session_key,),
            ) as cur:
                per_session_row = await cur.fetchone()
            per_session_count = int(per_session_row[0] if per_session_row else 0)
            if per_session_count >= _META_LAUNCH_DRAFT_PER_SESSION_LIMIT:
                raise MetaLaunchDraftCapacityError("MetaSkill draft outbox is full")
            async with conn.execute("SELECT COUNT(*) FROM meta_launch_drafts") as cur:
                global_row = await cur.fetchone()
            global_drafts = int(global_row[0] if global_row else 0)
            if global_drafts >= _META_LAUNCH_DRAFT_GLOBAL_LIMIT:
                raise MetaLaunchDraftCapacityError("MetaSkill draft outbox is full")
            await self._ensure_meta_launch_coordinate_capacity(
                conn,
                now_ms=now_ms,
                session_key=session_key,
                client_request_id=client_request_id,
            )

            draft = MetaLaunchDraft(
                session_key=session_key,
                client_request_id=client_request_id,
                meta_skill_name=meta_skill_name,
                launch_text=launch_text,
                created_at=now_ms,
                updated_at=now_ms,
                expires_at=now_ms + _META_LAUNCH_DRAFT_RETENTION_MS,
            )
            data = draft.model_dump()
            columns = list(data)
            await conn.execute(
                f"INSERT INTO meta_launch_drafts ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                [_serialize(data[column]) for column in columns],
            )
            return draft, "stamped"

    async def promote_meta_launch_draft(
        self,
        *,
        session_key: str,
        client_request_id: str,
        meta_skill_name: str,
        launch_text: str,
    ) -> tuple[MetaControlIntent, str]:
        """Atomically verify a live draft and stage its manual authorization.

        Readiness checks intentionally run outside SQLite. This compare-and-set
        closes the later boundary: if another tab discarded or expiry removed
        the raw request while readiness was running, no consumable control is
        created and the caller receives a retry-safe failure.
        """

        session_key, client_request_id = normalize_meta_launch_coordinates(
            session_key,
            client_request_id,
        )
        meta_skill_name = meta_skill_name.strip()
        if not meta_skill_name or not launch_text:
            raise ValueError("meta launch draft promotion coordinates are required")

        now_ms = _now_ms()
        async with self._write_transaction("promote_meta_launch_draft") as conn:
            await self._purge_expired_meta_launch_drafts(
                conn,
                now_ms=now_ms,
                limit=_META_LAUNCH_DRAFT_GC_BATCH,
            )
            async with conn.execute(
                """
                SELECT 1 FROM meta_launch_discard_tombstones
                WHERE session_key = ? AND client_request_id = ? AND expires_at > ?
                """,
                (session_key, client_request_id, now_ms),
            ) as cur:
                discarded = await cur.fetchone()
            if discarded is not None:
                raise MetaLaunchDraftDiscardedError(
                    "meta launch draft identity was explicitly discarded"
                )
            draft = await self._select_meta_launch_draft(
                conn,
                session_key=session_key,
                client_request_id=client_request_id,
            )
            if draft is None:
                raise MetaLaunchDraftUnavailableError(
                    "meta launch draft was discarded or expired"
                )
            if (
                draft.meta_skill_name != meta_skill_name
                or draft.launch_text != launch_text
            ):
                raise MetaLaunchDraftConflictError(
                    "meta launch draft identity was already used for another request"
                )
            return await self._stage_meta_control_intent_in_transaction(
                conn,
                session_key=session_key,
                control_kind="manual",
                correlation_id=f"request:{client_request_id}",
                meta_skill_name=meta_skill_name,
                replay_run_id=None,
                replay_mode=None,
            )

    async def list_meta_launch_drafts(
        self,
        *,
        session_key: str | None = None,
        agent_id: str | None = None,
        provisional_only: bool = False,
        limit: int = _META_LAUNCH_DRAFT_PER_SESSION_LIMIT,
    ) -> list[MetaLaunchDraft]:
        """List live drafts for one session or one agent without consuming them."""

        canonical_session = canonicalize_session_key(session_key) if session_key else ""
        normalized_agent = normalize_agent_id(agent_id) if agent_id else ""
        if not canonical_session and not normalized_agent:
            raise ValueError("meta launch draft session or agent is required")
        bounded_limit = max(1, min(int(limit), _META_LAUNCH_DRAFT_PER_SESSION_LIMIT))
        now_ms = _now_ms()
        async with self._write_transaction("list_meta_launch_drafts") as conn:
            await self._purge_expired_meta_launch_drafts(
                conn,
                now_ms=now_ms,
                limit=_META_LAUNCH_DRAFT_GC_BATCH,
            )
            if canonical_session:
                sql = (
                    "SELECT * FROM meta_launch_drafts "
                    "WHERE session_key = ? AND expires_at > ? "
                    "ORDER BY created_at ASC, draft_id ASC LIMIT ?"
                )
                params: tuple[Any, ...] = (canonical_session, now_ms, bounded_limit)
            else:
                session_prefix = f"agent:{normalized_agent}:"
                provisional_clause = (
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM sessions "
                    "WHERE sessions.session_key = meta_launch_drafts.session_key"
                    ") "
                    if provisional_only
                    else ""
                )
                sql = (
                    "SELECT * FROM meta_launch_drafts "
                    "WHERE substr(session_key, 1, length(?)) = ? AND expires_at > ? "
                    f"{provisional_clause}"
                    "ORDER BY created_at ASC, draft_id ASC LIMIT ?"
                )
                params = (session_prefix, session_prefix, now_ms, bounded_limit)
            async with conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
        drafts = [MetaLaunchDraft(**_deserialize_row(dict(row))) for row in rows]
        if normalized_agent:
            # Keep the parser authoritative if session-key formats expand; the
            # SQL prefix is only the bounded query accelerator.
            drafts = [
                draft
                for draft in drafts
                if parse_agent_id(draft.session_key) == normalized_agent
            ]
        return drafts

    async def discard_meta_launch_draft(
        self,
        *,
        session_key: str,
        client_request_id: str,
    ) -> bool:
        """Make an explicit user discard terminal for a bounded retention window."""

        try:
            session_key, client_request_id = normalize_meta_launch_coordinates(
                session_key,
                client_request_id,
            )
        except ValueError:
            return False
        now_ms = _now_ms()
        async with self._write_transaction("discard_meta_launch_draft") as conn:
            intent = await self._select_meta_control_intent(
                conn,
                session_key=session_key,
                control_kind="manual",
                correlation_id=f"request:{client_request_id}",
            )
            # Acceptance is the irreversible boundary: the task may already be
            # invoking a paid provider even if the browser lost the send
            # response. Never report that request as cancelled or let the UI
            # restore it as a newly sendable composer draft.
            if intent is not None and intent.status == "accepted":
                return False
            await self._purge_expired_meta_launch_drafts(
                conn,
                now_ms=now_ms,
                limit=_META_LAUNCH_DRAFT_GC_BATCH,
            )
            async with conn.execute(
                """
                SELECT 1 FROM meta_launch_discard_tombstones
                WHERE session_key = ? AND client_request_id = ? AND expires_at > ?
                """,
                (session_key, client_request_id, now_ms),
            ) as cur:
                existing_tombstone = await cur.fetchone()
            if existing_tombstone is None:
                await self._ensure_meta_launch_coordinate_capacity(
                    conn,
                    now_ms=now_ms,
                    session_key=session_key,
                    client_request_id=client_request_id,
                )
            # Keep only the request coordinates, never the raw launch text or
            # skill name. Repeated response-loss retries do not extend the
            # finite retention window established by the first discard.
            await conn.execute(
                """
                INSERT INTO meta_launch_discard_tombstones (
                    session_key, client_request_id, created_at, expires_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(session_key, client_request_id) DO UPDATE SET
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                WHERE meta_launch_discard_tombstones.expires_at <= excluded.created_at
                """,
                (
                    session_key,
                    client_request_id,
                    now_ms,
                    now_ms + _META_LAUNCH_DRAFT_RETENTION_MS,
                ),
            )
            await conn.execute(
                "DELETE FROM meta_launch_drafts WHERE session_key = ? AND client_request_id = ?",
                (session_key, client_request_id),
            )
            await conn.execute(
                """
                DELETE FROM meta_control_intents
                WHERE session_key = ? AND control_kind = 'manual'
                  AND correlation_id = ? AND status = 'staged'
                """,
                (session_key, f"request:{client_request_id}"),
            )
            # Idempotent cancellation: a retry after a committed discard
            # response loss must confirm the same terminal intent instead of
            # resurrecting a launch in the browser.
            return True

    @staticmethod
    async def _delete_reset_history(conn: Any, session_id: str) -> None:
        """Delete reset-owned history on an existing write transaction."""

        for table in (
            "transcript_entries",
            "compacted_transcript_entries",
            "session_summaries",
        ):
            await conn.execute(
                f"DELETE FROM {table} WHERE session_id = ?",  # noqa: S608
                (session_id,),
            )

    async def reset_session(
        self,
        node: SessionNode,
        *,
        expected_session_id: str,
        expected_epoch: int,
        archive_writer: Callable[[ResetArchiveSnapshot], Awaitable[None]],
    ) -> None:
        """Archive and rotate one session identity in a single transaction."""

        node.session_key = canonicalize_session_key(node.session_key)
        node.agent_id = normalize_agent_id(node.agent_id)
        if not expected_session_id:
            raise ValueError("expected_session_id is required")
        if node.session_id == expected_session_id:
            raise ValueError("reset session id must change")
        if int(node.epoch or 0) != expected_epoch + 1:
            raise ValueError("reset epoch must advance exactly once")

        async with self._write_transaction("reset_session") as conn:
            async with conn.execute(
                """
                SELECT *
                FROM sessions
                WHERE session_key = ? AND session_id = ? AND epoch = ?
                """,
                (node.session_key, expected_session_id, expected_epoch),
            ) as cur:
                previous_row = await cur.fetchone()
            if previous_row is None:
                await self._raise_stale_epoch(
                    conn,
                    session_key=node.session_key,
                    expected_epoch=expected_epoch,
                )
            assert previous_row is not None
            previous_node = SessionNode(**_deserialize_row(dict(previous_row)))
            node.model_routing_mode = previous_node.model_routing_mode
            node.model_routing_revision = previous_node.model_routing_revision
            session_data = node.model_dump()
            snapshot = ResetArchiveSnapshot(
                node=previous_node,
                entries=tuple(
                    await self._select_canonical_transcript(
                        conn,
                        expected_session_id,
                    )
                ),
                summaries=tuple(
                    await self._select_all_summaries(
                        conn,
                        expected_session_id,
                    )
                ),
            )
            await archive_writer(snapshot)

            assignments = [
                f"{column} = ?"
                for column in session_data
                if column != "session_key"
                and column not in _SESSION_DEDICATED_WRITER_COLUMNS
            ]
            values = [
                _serialize(value)
                for column, value in session_data.items()
                if column != "session_key"
                and column not in _SESSION_DEDICATED_WRITER_COLUMNS
            ]
            async with conn.execute(
                f"UPDATE sessions SET {', '.join(assignments)} "
                "WHERE session_key = ? AND session_id = ? AND epoch = ?",
                [
                    *values,
                    node.session_key,
                    expected_session_id,
                    expected_epoch,
                ],
            ) as cur:
                rotated = cur.rowcount or 0
            if rotated == 0:
                await self._raise_stale_epoch(
                    conn,
                    session_key=node.session_key,
                    expected_epoch=expected_epoch,
                )
            await self._ensure_usage_baseline_for_session_on_conn(
                conn,
                session_key=node.session_key,
            )

            await self._delete_reset_history(conn, expected_session_id)
            # Reset rotates the Goal generation boundary.  Command receipts
            # remain attached to the stable session key for safe replay.
            await conn.execute(
                "DELETE FROM session_goals WHERE session_key = ?",
                (node.session_key,),
            )
            await conn.execute(
                """
                UPDATE session_context_states
                SET valid = 0, invalid_reason = 'session_reset'
                WHERE session_key = ? AND valid = 1
                """,
                (node.session_key,),
            )
            await self._tombstone_meta_launches_for_boundary(
                conn,
                session_key=node.session_key,
                now_ms=_now_ms(),
                intent_statuses=("staged", "accepted"),
            )
            await conn.execute(
                "DELETE FROM meta_control_intents "
                "WHERE session_key = ? AND status = 'staged'",
                (node.session_key,),
            )
            await conn.execute(
                "DELETE FROM meta_launch_drafts WHERE session_key = ?",
                (node.session_key,),
            )
            timestamp = _now_ms()
            active_placeholders = ", ".join(
                "?" for _ in PLAN_RUN_ACTIVE_STATUSES
            )
            await conn.execute(
                f"""
                UPDATE plan_runs
                SET status = 'superseded',
                    state_revision = state_revision + 1,
                    active_task_id = NULL,
                    terminal_reason = 'session_reset',
                    updated_at = ?,
                    finished_at = ?
                WHERE session_key = ? AND status IN ({active_placeholders})
                """,  # noqa: S608 - placeholder count is from a fixed constant
                [
                    timestamp,
                    timestamp,
                    node.session_key,
                    *sorted(PLAN_RUN_ACTIVE_STATUSES),
                ],
            )

        _clear_pending_meta_launch_boundary(node.session_key)

    async def accept_turn(
        self,
        entry: TranscriptEntry,
        *,
        expected_epoch: int,
        updated_at: int,
        task_record: AgentTaskRecord | None,
        receipt_task_id: str | None = None,
        source_scope: str,
        request_session_key: str,
        client_request_id: str,
        request_fingerprint: str,
        session_node: SessionNode | None = None,
        reset_from_session_id: str | None = None,
        reset_archive_writer: Callable[[ResetArchiveSnapshot], Awaitable[None]] | None = None,
        initial_transcript_entries: tuple[TranscriptEntry, ...] = (),
        session_updates: dict[str, Any] | None = None,
        plan_revision: PlanRevisionRecord | None = None,
        plan_run: PlanRunRecord | None = None,
        merge_into_task: bool = False,
        meta_control_intent_id: str | None = None,
        workspace_guard: ProjectWorkspaceGuard | None = None,
        expected_collaboration_revision: int | None = None,
        expected_active_plan_revision_id: str | None = None,
        require_idle_for_current_plan_implementation: bool = False,
        goal_mutation: (
            StartGoalMutation | ClaimGoalMutation | ClaimCurrentGoalMutation | None
        ) = None,
        pending_input_id: str | None = None,
        pending_input_fingerprint: str | None = None,
        pending_input_revision: int | None = None,
    ) -> TurnAcceptanceResult:
        """Commit one user message, optional task, and request receipt atomically.

        Repeating the same scoped client request returns the original receipt.
        Reusing its id for a different payload is rejected before any write.
        ``receipt_task_id`` associates an accepted input with an existing task
        without inserting or mutating that task. This is used by same-turn
        input, whose durable receipt must retain the target turn identity.
        """

        source_scope = source_scope.strip()
        client_request_id = client_request_id.strip()
        if not source_scope:
            raise ValueError("source_scope is required")
        if not client_request_id:
            raise ValueError("client_request_id is required")
        if not request_fingerprint:
            raise ValueError("request_fingerprint is required")
        if (
            expected_collaboration_revision is not None
            and (
                isinstance(expected_collaboration_revision, bool)
                or expected_collaboration_revision < 0
            )
        ):
            raise ValueError(
                "expected_collaboration_revision must be a non-negative integer"
            )
        if expected_active_plan_revision_id is not None:
            expected_active_plan_revision_id = expected_active_plan_revision_id.strip()
            if not expected_active_plan_revision_id:
                raise ValueError("expected_active_plan_revision_id must not be blank")
        if require_idle_for_current_plan_implementation and plan_run is None:
            raise ValueError(
                "idle Plan implementation admission requires an accepted plan run"
            )
        if goal_mutation is not None and plan_run is not None:
            raise ValueError("Goal turns cannot start or claim a Plan run")
        if goal_mutation is not None and meta_control_intent_id is not None:
            raise ValueError("Goal turns cannot consume a MetaSkill control intent")
        pending_guard_values = (
            pending_input_id,
            pending_input_fingerprint,
            pending_input_revision,
        )
        if any(value is not None for value in pending_guard_values) and not all(
            value is not None for value in pending_guard_values
        ):
            raise ValueError("pending input dispatch guard must be complete")
        if pending_input_id is not None:
            pending_input_id = pending_input_id.strip()
            pending_input_fingerprint = str(pending_input_fingerprint).strip()
            if not pending_input_id or not pending_input_fingerprint:
                raise ValueError("pending input dispatch identity must not be blank")
            if request_fingerprint != pending_input_fingerprint:
                raise PendingChatInputConflictError(
                    "pending input fingerprint must match the accepted turn fingerprint"
                )
            if (
                isinstance(pending_input_revision, bool)
                or not isinstance(pending_input_revision, int)
                or pending_input_revision < 1
            ):
                raise ValueError("pending input revision must be a positive integer")

        request_session_key = canonicalize_session_key(request_session_key)
        entry.session_key = canonicalize_session_key(entry.session_key)
        if task_record is not None:
            task_record.session_key = canonicalize_session_key(task_record.session_key)
            task_record.agent_id = normalize_agent_id(task_record.agent_id)
            if task_record.session_key != entry.session_key:
                raise ValueError("task and transcript session keys must match")
        if isinstance(goal_mutation, StartGoalMutation):
            if task_record is None or merge_into_task:
                raise ValueError("Goal set requires one newly accepted runtime task")
            goal_mutation.goal.session_key = canonicalize_session_key(
                goal_mutation.goal.session_key
            )
            # Storage owns the atomic transcript/Goal binding. Callers created
            # before the anchor field existed may omit it, but no caller may
            # bind a Goal to a different transcript row.
            if goal_mutation.goal.source_user_message_id is None:
                goal_mutation.goal.source_user_message_id = entry.message_id
            goal_mutation.goal.objective = normalize_goal_objective(
                goal_mutation.goal.objective
            )
            command = self._prepare_goal_command(
                goal_mutation.command,
                action="set",
                session_key=entry.session_key,
            )
            goal_mutation = replace(goal_mutation, command=command)
            if (
                goal_mutation.goal.session_key != entry.session_key
                or goal_mutation.goal.session_id != entry.session_id
                or goal_mutation.goal.session_epoch != expected_epoch
                or goal_mutation.goal.active_task_id != task_record.task_id
                or goal_mutation.goal.source_user_message_id != entry.message_id
                or command.source_scope != source_scope.strip()
                or command.client_request_id != client_request_id.strip()
                or command.request_fingerprint != request_fingerprint
            ):
                raise ValueError(
                    "Goal set, transcript, task and idempotency identities must match"
                )
        elif isinstance(
            goal_mutation,
            (ClaimGoalMutation, ClaimCurrentGoalMutation),
        ) and task_record is None:
            raise ValueError("Goal claim requires an accepted runtime task")
        if receipt_task_id is not None:
            receipt_task_id = receipt_task_id.strip()
            if not receipt_task_id:
                raise ValueError("receipt_task_id must not be blank")
            if task_record is not None and task_record.task_id != receipt_task_id:
                raise ValueError("receipt_task_id must match task_record.task_id")
        if session_node is not None:
            session_node.session_key = canonicalize_session_key(session_node.session_key)
            session_node.agent_id = normalize_agent_id(session_node.agent_id)
            if session_node.session_key != entry.session_key:
                raise ValueError("prepared session and transcript session keys must match")
            if session_node.session_id != entry.session_id:
                raise ValueError("prepared session and transcript session ids must match")
        elif reset_from_session_id is not None:
            raise ValueError("reset_from_session_id requires session_node")
        if initial_transcript_entries and session_node is None:
            raise ValueError("initial transcript entries require session_node")
        if merge_into_task and task_record is None:
            raise ValueError("task collection requires task_record")
        if reset_archive_writer is not None and reset_from_session_id is None:
            raise ValueError("reset_archive_writer requires reset_from_session_id")
        if merge_into_task and session_node is not None:
            raise ValueError("task collection cannot create, reset, or fork a session")
        if merge_into_task and meta_control_intent_id is not None:
            raise ValueError("a MetaSkill control turn cannot merge into another task")
        if plan_run is not None:
            if merge_into_task:
                raise ValueError("a plan implementation turn cannot merge into a task")
            if task_record is None:
                raise ValueError("an accepted plan run requires a runtime task")
            plan_run.session_key = canonicalize_session_key(plan_run.session_key)
            if (
                plan_run.session_key != entry.session_key
                or plan_run.session_id != entry.session_id
                or plan_run.session_epoch != expected_epoch
            ):
                raise ValueError(
                    "plan run and transcript entry must target the same session epoch"
                )
            if plan_run.active_task_id != task_record.task_id:
                raise ValueError(
                    "accepted plan run must be bound to the accepted runtime task"
                )
        if plan_revision is not None:
            if plan_run is None:
                raise ValueError("an accepted plan revision requires a plan run")
            plan_revision.source_session_key = canonicalize_session_key(
                plan_revision.source_session_key
            )
            if (
                plan_revision.source_session_key != entry.session_key
                or plan_revision.source_session_id != entry.session_id
                or plan_revision.source_epoch != expected_epoch
                or plan_revision.revision_id != plan_run.plan_revision_id
            ):
                raise ValueError(
                    "accepted plan revision must own the same session epoch and plan run"
                )
        allowed_session_updates = {
            "last_channel",
            "last_to",
            "last_account_id",
            "last_thread_id",
            "delivery_context",
            "origin",
            "collaboration_mode",
            "active_plan_revision_id",
        }
        session_updates = dict(session_updates or {})
        unknown_session_updates = sorted(set(session_updates) - allowed_session_updates)
        if unknown_session_updates:
            raise ValueError(
                "Unsupported atomic session updates: "
                + ", ".join(unknown_session_updates)
            )
        collaboration_mode_update = session_updates.pop(
            "collaboration_mode",
            None,
        )
        if collaboration_mode_update is not None:
            try:
                collaboration_mode_update = CollaborationMode(
                    collaboration_mode_update
                ).value
            except ValueError as exc:
                raise ValueError(
                    f"Unsupported collaboration mode: {collaboration_mode_update}"
                ) from exc
        active_plan_revision_update = session_updates.pop(
            "active_plan_revision_id",
            None,
        )
        if active_plan_revision_update is not None and (
            plan_run is None
            or plan_run.plan_revision_id != active_plan_revision_update
        ):
            raise ValueError(
                "active_plan_revision_id may only select the accepted plan run"
            )

        pending_dispatch_client_message_id: str | None = None
        async with self._write_transaction("accept_turn") as conn:
            selected = await self._select_turn_ingress_receipt(
                conn,
                source_scope=source_scope,
                request_session_key=request_session_key,
                client_request_id=client_request_id,
            )
            if selected is not None:
                receipt, task_status, fresh_user_session, task_details = selected
                if receipt.request_fingerprint != request_fingerprint:
                    if isinstance(goal_mutation, StartGoalMutation):
                        raise GoalConflictError(
                            "IDEMPOTENCY_CONFLICT",
                            "clientRequestId was already used for a different Goal command",
                        )
                    raise TurnIngressConflictError(
                        "client_request_id was already used for a different turn"
                    )
                goal_command_result: GoalCommandResult | None = None
                # Repair an outbox left by an older build that committed the
                # receipt before learning to consume drafts atomically.
                await conn.execute(
                    """
                    DELETE FROM meta_launch_drafts
                    WHERE session_key = ? AND client_request_id = ?
                    """,
                    (request_session_key, client_request_id),
                )
                if pending_input_id is not None:
                    dispatch_receipt = (
                        await self._select_pending_chat_input_dispatch_receipt(
                            conn,
                            pending_input_id=pending_input_id,
                        )
                    )
                    if dispatch_receipt is None or (
                        dispatch_receipt.session_key != request_session_key
                        or dispatch_receipt.source_scope != source_scope
                        or dispatch_receipt.client_request_id != client_request_id
                        or dispatch_receipt.request_fingerprint
                        != pending_input_fingerprint
                    ):
                        raise PendingChatInputConflictError(
                            "pending input is not bound to the accepted turn receipt"
                        )
                    pending = await self._select_pending_chat_input(
                        conn,
                        pending_input_id=pending_input_id,
                    )
                    if pending is not None:
                        if (
                            pending.session_key != request_session_key
                            or pending.source_scope != source_scope
                            or pending.client_request_id != client_request_id
                            or pending.client_message_id
                            != dispatch_receipt.client_message_id
                            or pending.request_fingerprint
                            != pending_input_fingerprint
                            or pending.state_revision != pending_input_revision
                        ):
                            raise PendingChatInputConflictError(
                                "pending input does not match the accepted turn receipt"
                            )
                        await conn.execute(
                            "DELETE FROM pending_chat_inputs WHERE pending_input_id = ?",
                            (pending_input_id,),
                        )
                if isinstance(goal_mutation, StartGoalMutation):
                    goal_command_result = await self._replay_goal_command_on_conn(
                        conn,
                        goal_mutation.command,
                    )
                    if goal_command_result is None:
                        raise RuntimeError(
                            "Goal turn receipt exists without its atomic command receipt"
                        )
                return TurnAcceptanceResult(
                    receipt=receipt,
                    replayed=True,
                    fresh_user_session=fresh_user_session,
                    task_status=task_status,
                    goal=(
                        goal_command_result.goal
                        if goal_command_result is not None
                        else None
                    ),
                    goal_command_response=(
                        goal_command_result.response
                        if goal_command_result is not None
                        else None
                    ),
                    goal_context=GoalTurnContext.from_task_detail(
                        task_details.get("goal_context")
                    ),
                    goal_candidate=GoalClaimCandidate.from_task_detail(
                        task_details.get("goal_candidate")
                    ),
                )

            if pending_input_id is not None:
                pending = await self._select_pending_chat_input(
                    conn,
                    pending_input_id=pending_input_id,
                )
                if pending is None:
                    raise PendingChatInputNotFoundError(pending_input_id)
                if (
                    pending.session_key != request_session_key
                    or pending.source_scope != source_scope
                    or pending.client_request_id != client_request_id
                    or pending.request_fingerprint != pending_input_fingerprint
                ):
                    raise PendingChatInputConflictError(
                        "pending input dispatch identity changed before acceptance"
                    )
                if pending.state_revision != pending_input_revision:
                    raise PendingChatInputConflictError(
                        "pending input revision changed before acceptance"
                    )
                turn_context = entry.turn_context if isinstance(entry.turn_context, dict) else {}
                turn_client_message_id = turn_context.get("client_message_id")
                if (
                    isinstance(turn_client_message_id, str)
                    and turn_client_message_id
                    and turn_client_message_id != pending.client_message_id
                ):
                    raise PendingChatInputConflictError(
                        "pending input message identity changed before acceptance"
                    )
                pending_dispatch_client_message_id = pending.client_message_id

            if meta_control_intent_id is not None:
                if task_record is None:
                    raise ValueError(
                        "a MetaSkill control turn requires a runtime task"
                    )
                async with conn.execute(
                    "SELECT * FROM meta_control_intents WHERE intent_id = ?",
                    (meta_control_intent_id,),
                ) as cur:
                    control_row = await cur.fetchone()
                if control_row is None:
                    raise MetaControlIntentConflictError(
                        "MetaSkill control authorization is missing"
                    )
                control = MetaControlIntent(**_deserialize_row(dict(control_row)))
                if control.session_key != request_session_key:
                    raise MetaControlIntentConflictError(
                        "MetaSkill control authorization belongs to another session"
                    )
                if control.status != "staged":
                    raise MetaControlIntentConflictError(
                        "MetaSkill control authorization was already accepted"
                    )
                embedded = (
                    entry.turn_context.get("meta_control")
                    if isinstance(entry.turn_context, dict)
                    else None
                )
                expected_embedded: dict[str, Any] = {
                    "version": 1,
                    "intent_id": control.intent_id,
                    "kind": control.control_kind,
                    "name": control.meta_skill_name,
                    "correlation_id": control.correlation_id,
                }
                if control.control_kind == "replay":
                    expected_embedded.update({
                        "run_id": control.replay_run_id,
                        "mode": control.replay_mode,
                    })
                if embedded != expected_embedded:
                    raise MetaControlIntentConflictError(
                        "MetaSkill control payload does not match its authorization"
                    )
                task_metadata = (task_record.details or {}).get("metadata")
                if (
                    not isinstance(task_metadata, dict)
                    or task_metadata.get("meta_control") != expected_embedded
                ):
                    raise MetaControlIntentConflictError(
                        "MetaSkill control task lost its authorized payload"
                    )

            if isinstance(goal_mutation, StartGoalMutation):
                command_replay = await self._replay_goal_command_on_conn(
                    conn,
                    goal_mutation.command,
                )
                if command_replay is not None:
                    raise RuntimeError(
                        "Goal command receipt exists without its atomic turn receipt"
                    )

            # Existing-session Plan operations require compare-and-set guards
            # before *any* transcript, session, task, receipt, or PlanRun write.
            # BEGIN IMMEDIATE makes this the cross-connection final arbiter;
            # the in-memory broker check is only a richer preflight response.
            if session_node is None and (
                expected_collaboration_revision is not None
                or expected_active_plan_revision_id is not None
                or require_idle_for_current_plan_implementation
            ):
                async with conn.execute(
                    """
                    SELECT session_id, epoch, collaboration_revision,
                           active_plan_revision_id
                    FROM sessions
                    WHERE session_key = ?
                    """,
                    (entry.session_key,),
                ) as cur:
                    guarded_session_row = await cur.fetchone()
                if guarded_session_row is None:
                    raise KeyError(f"Session not found: {entry.session_key}")
                if (
                    str(guarded_session_row["session_id"]) != entry.session_id
                    or int(guarded_session_row["epoch"]) != expected_epoch
                ):
                    await self._raise_stale_epoch(
                        conn,
                        session_key=entry.session_key,
                        expected_epoch=expected_epoch,
                    )
                if (
                    expected_collaboration_revision is not None
                    and int(guarded_session_row["collaboration_revision"])
                    != expected_collaboration_revision
                ):
                    raise PlanConflictError(
                        "collaboration state changed before turn acceptance"
                    )
                if (
                    expected_active_plan_revision_id is not None
                    and guarded_session_row["active_plan_revision_id"]
                    != expected_active_plan_revision_id
                ):
                    raise PlanConflictError(
                        "active plan revision changed before turn acceptance"
                    )
                if require_idle_for_current_plan_implementation:
                    async with conn.execute(
                        """
                        SELECT task_id, status
                        FROM agent_tasks
                        WHERE session_key = ? AND status IN (?, ?)
                        ORDER BY CASE status WHEN ? THEN 0 ELSE 1 END,
                                 created_at ASC, rowid ASC
                        LIMIT 1
                        """,
                        (
                            entry.session_key,
                            AgentTaskStatus.QUEUED,
                            AgentTaskStatus.RUNNING,
                            AgentTaskStatus.RUNNING,
                        ),
                    ) as cur:
                        busy_task = await cur.fetchone()
                    if busy_task is not None:
                        raise PlanImplementationSessionBusyError(
                            task_id=str(busy_task["task_id"]),
                            task_status=str(busy_task["status"]),
                        )

            await _verify_project_workspace_guard(
                conn,
                session_node=session_node,
                entry_session_key=entry.session_key,
                workspace_guard=workspace_guard,
            )

            reset_archive_snapshot: ResetArchiveSnapshot | None = None
            accepted_goal: GoalRecord | None = None
            accepted_goal_context: GoalTurnContext | None = None
            accepted_goal_candidate: GoalClaimCandidate | None = None
            accepted_goal_command_response: dict[str, Any] | None = None
            accepted_goal_previous_id: str | None = None
            if session_node is not None:
                session_data = session_node.model_dump()
                if reset_from_session_id is None:
                    session_cols = list(session_data.keys())
                    session_placeholders = ", ".join("?" for _ in session_cols)
                    await conn.execute(
                        f"INSERT INTO sessions ({', '.join(session_cols)}) "
                        f"VALUES ({session_placeholders})",
                        [_serialize(session_data[col]) for col in session_cols],
                    )
                    await self._ensure_usage_baseline_for_session_on_conn(
                        conn,
                        session_key=session_node.session_key,
                    )
                else:
                    previous_epoch = max(0, expected_epoch - 1)
                    async with conn.execute(
                        """
                        SELECT *
                        FROM sessions
                        WHERE session_key = ? AND session_id = ? AND epoch = ?
                        """,
                        (
                            session_node.session_key,
                            reset_from_session_id,
                            previous_epoch,
                        ),
                    ) as cur:
                        previous_row = await cur.fetchone()
                    if previous_row is None:
                        await self._raise_stale_epoch(
                            conn,
                            session_key=session_node.session_key,
                            expected_epoch=previous_epoch,
                        )
                    assert previous_row is not None
                    previous_node = SessionNode(
                        **_deserialize_row(dict(previous_row))
                    )
                    session_node.model_routing_mode = previous_node.model_routing_mode
                    session_node.model_routing_revision = (
                        previous_node.model_routing_revision
                    )
                    reset_archive_snapshot = ResetArchiveSnapshot(
                        node=previous_node,
                        entries=tuple(
                            await self._select_canonical_transcript(
                                conn,
                                reset_from_session_id,
                            )
                        ),
                        summaries=tuple(
                            await self._select_all_summaries(
                                conn,
                                reset_from_session_id,
                            )
                        ),
                    )
                    if reset_archive_writer is not None:
                        await reset_archive_writer(reset_archive_snapshot)
                        reset_archive_snapshot = None
                    assignments = [
                        f"{column} = ?"
                        for column in session_data
                        if column != "session_key"
                        and column not in _SESSION_DEDICATED_WRITER_COLUMNS
                    ]
                    values = [
                        _serialize(value)
                        for column, value in session_data.items()
                        if column != "session_key"
                        and column not in _SESSION_DEDICATED_WRITER_COLUMNS
                    ]
                    async with conn.execute(
                        f"UPDATE sessions SET {', '.join(assignments)} "
                        "WHERE session_key = ? AND session_id = ? AND epoch = ?",
                        [
                            *values,
                            session_node.session_key,
                            reset_from_session_id,
                            previous_epoch,
                        ],
                    ) as cur:
                        rotated = cur.rowcount or 0
                    if rotated == 0:
                        await self._raise_stale_epoch(
                            conn,
                            session_key=session_node.session_key,
                            expected_epoch=previous_epoch,
                        )
                    await self._ensure_usage_baseline_for_session_on_conn(
                        conn,
                        session_key=session_node.session_key,
                    )
                    await self._delete_reset_history(conn, reset_from_session_id)
                    await conn.execute(
                        "DELETE FROM session_goals WHERE session_key = ?",
                        (session_node.session_key,),
                    )
                    await conn.execute(
                        """
                        UPDATE session_context_states
                        SET valid = 0, invalid_reason = 'session_reset'
                        WHERE session_key = ? AND valid = 1
                        """,
                        (session_node.session_key,),
                    )
                    await self._tombstone_meta_launches_for_boundary(
                        conn,
                        session_key=session_node.session_key,
                        now_ms=_now_ms(),
                        intent_statuses=("staged", "accepted"),
                        exclude_intent_id=meta_control_intent_id,
                        exclude_client_request_id=(
                            client_request_id
                            if meta_control_intent_id is not None
                            and request_session_key == session_node.session_key
                            else None
                        ),
                    )
                    if meta_control_intent_id is None:
                        await conn.execute(
                            """
                            DELETE FROM meta_control_intents
                            WHERE session_key = ? AND status = 'staged'
                            """,
                            (session_node.session_key,),
                        )
                    else:
                        # The currently accepted hidden control belongs to this
                        # atomic reset turn. Preserve only that validated row;
                        # every other staged authorization belongs to the old
                        # session identity and must be invalidated.
                        await conn.execute(
                            """
                            DELETE FROM meta_control_intents
                            WHERE session_key = ? AND status = 'staged'
                              AND intent_id <> ?
                            """,
                            (session_node.session_key, meta_control_intent_id),
                        )
                    # A reset discards every unaccepted request owned by the old
                    # session identity. If this transaction is itself accepting
                    # one of them, rollback restores it on any later failure.
                    await conn.execute(
                        "DELETE FROM meta_launch_drafts WHERE session_key = ?",
                        (session_node.session_key,),
                    )

                    timestamp = _now_ms()
                    active_placeholders = ", ".join(
                        "?" for _ in PLAN_RUN_ACTIVE_STATUSES
                    )
                    await conn.execute(
                        f"""
                        UPDATE plan_runs
                        SET status = 'superseded',
                            state_revision = state_revision + 1,
                            active_task_id = NULL,
                            terminal_reason = 'session_reset',
                            updated_at = ?,
                            finished_at = ?
                        WHERE session_key = ? AND status IN ({active_placeholders})
                        """,  # noqa: S608 - placeholder count is from a fixed constant
                        [
                            timestamp,
                            timestamp,
                            session_node.session_key,
                            *sorted(PLAN_RUN_ACTIVE_STATUSES),
                        ],
                    )

            if isinstance(goal_mutation, StartGoalMutation):
                assert task_record is not None
                goal = goal_mutation.goal
                if (
                    goal.status != GoalStatus.ACTIVE.value
                    or goal.state_revision != 1
                    or goal.objective_revision != 1
                    or goal.progress_revision != 0
                    or goal.continuation_seq != 0
                    or goal.active_task_id != task_record.task_id
                    or goal.source_user_message_id != entry.message_id
                    or goal.terminal_task_id is not None
                    or goal.turns_started != 1
                    or goal.turns_settled != 0
                    or goal.window_turns_started != 1
                ):
                    raise GoalValidationError(
                        "Goal set requires a fresh Goal bound to its first task",
                        code="INVALID_GOAL_COMMAND",
                    )
                current = await self._select_goal_on_conn(
                    conn,
                    session_key=entry.session_key,
                )
                if current is not None:
                    if current.status in GOAL_UNFINISHED_STATUSES:
                        raise GoalConflictError(
                            "GOAL_ACTIVE",
                            "An unfinished Goal already exists for this session",
                            current=current,
                        )
                    if current.active_task_id is not None:
                        raise GoalConflictError(
                            "GOAL_BUSY",
                            "The completed Goal still owns an unsettled task",
                            current=current,
                        )
                await self._require_default_goal_mode_on_conn(conn, goal=goal)
                await self._require_idle_goal_session_on_conn(
                    conn,
                    session_key=entry.session_key,
                )
                if current is not None:
                    accepted_goal_previous_id = current.goal_id
                    await conn.execute(
                        "DELETE FROM session_goals WHERE session_key = ?",
                        (entry.session_key,),
                    )
                accepted_goal_context = goal_turn_context(
                    goal,
                    task_id=task_record.task_id,
                    automatic=False,
                )
                task_details = dict(task_record.details or {})
                task_details.pop("goal_candidate", None)
                task_details["goal_context"] = accepted_goal_context.as_task_detail()
                task_record.details = task_details
                await self._insert_goal_on_conn(conn, goal)
                accepted_goal = goal

            elif isinstance(
                goal_mutation,
                (ClaimGoalMutation, ClaimCurrentGoalMutation),
            ):
                assert task_record is not None
                current = await self._select_goal_on_conn(
                    conn,
                    session_key=entry.session_key,
                )
                if isinstance(goal_mutation, ClaimCurrentGoalMutation):
                    matching_active = (
                        current is not None
                        and current.session_id == entry.session_id
                        and current.session_epoch == expected_epoch
                        and current.status == GoalStatus.ACTIVE.value
                    )
                    candidate = (
                        GoalClaimCandidate(
                            session_id=current.session_id,
                            epoch=current.session_epoch,
                            goal_id=current.goal_id,
                        )
                        if matching_active and current is not None
                        else None
                    )
                else:
                    candidate = goal_mutation.candidate
                    matching_active = (
                        current is not None
                        and current.session_id == candidate.session_id
                        and current.session_epoch == candidate.epoch
                        and current.goal_id == candidate.goal_id
                        and current.status == GoalStatus.ACTIVE.value
                    )
                if matching_active:
                    assert current is not None and candidate is not None
                    async with conn.execute(
                        """
                        SELECT collaboration_mode FROM sessions
                        WHERE session_key = ? AND session_id = ? AND epoch = ?
                        """,
                        (entry.session_key, entry.session_id, expected_epoch),
                    ) as mode_cur:
                        mode_row = await mode_cur.fetchone()
                    mode_is_default = (
                        mode_row is not None
                        and str(mode_row["collaboration_mode"])
                        == CollaborationMode.DEFAULT.value
                    )
                    async with conn.execute(
                        """
                        SELECT 1 FROM agent_tasks
                        WHERE session_key = ? AND status IN (?, ?)
                        LIMIT 1
                        """,
                        (
                            entry.session_key,
                            AgentTaskStatus.QUEUED.value,
                            AgentTaskStatus.RUNNING.value,
                        ),
                    ) as busy_cur:
                        has_existing_task = await busy_cur.fetchone() is not None
                    async with conn.execute(
                        """
                        SELECT 1 FROM plan_runs
                        WHERE session_key = ?
                          AND driver_kind = 'manual'
                          AND status IN ('queued', 'running', 'paused', 'blocked')
                        LIMIT 1
                        """,
                        (entry.session_key,),
                    ) as plan_cur:
                        has_manual_plan_run = await plan_cur.fetchone() is not None
                    can_claim_now = (
                        mode_is_default
                        and current.active_task_id is None
                        and not has_existing_task
                        and not has_manual_plan_run
                        and not merge_into_task
                    )
                    task_details = dict(task_record.details or {})
                    if can_claim_now:
                        accepted_goal_context = goal_turn_context(
                            current,
                            task_id=task_record.task_id,
                            automatic=False,
                        )
                        task_details.pop("goal_candidate", None)
                        task_details["goal_context"] = (
                            accepted_goal_context.as_task_detail()
                        )
                        await conn.execute(
                            """
                            UPDATE session_goals
                            SET active_task_id = ?,
                                terminal_task_id = NULL,
                                turns_started = turns_started + 1,
                                window_turns_started = window_turns_started + 1,
                                state_revision = state_revision + 1,
                                updated_at_ms = ?
                            WHERE session_key = ? AND goal_id = ?
                              AND active_task_id IS NULL AND status = 'active'
                            """,
                            (
                                task_record.task_id,
                                updated_at,
                                entry.session_key,
                                current.goal_id,
                            ),
                        )
                        accepted_goal = await self._select_goal_on_conn(
                            conn,
                            session_key=entry.session_key,
                        )
                    else:
                        task_details["goal_candidate"] = candidate.as_task_detail()
                        accepted_goal_candidate = candidate
                    task_record.details = task_details
                else:
                    # The candidate is advisory.  A generation/status mismatch
                    # turns this into an ordinary user task, including when it
                    # is collected into a queued task that carried an older
                    # candidate in memory or durable details.
                    task_details = dict(task_record.details or {})
                    task_details.pop("goal_candidate", None)
                    task_record.details = task_details

            if plan_revision is not None:
                await self._create_plan_revision_on_conn(
                    conn,
                    prepare_plan_revision(plan_revision),
                    expected_parent_revision_id=None,
                )

            for initial_entry in initial_transcript_entries:
                initial_entry.session_key = canonicalize_session_key(
                    initial_entry.session_key
                )
                if (
                    initial_entry.session_key != entry.session_key
                    or initial_entry.session_id != entry.session_id
                ):
                    raise ValueError(
                        "initial transcript entries must target the accepted session"
                    )
                await self._insert_transcript_entry(
                    conn,
                    initial_entry,
                    expected_epoch=expected_epoch,
                )

            async with conn.execute(
                "SELECT 1 FROM transcript_entries WHERE session_id = ? LIMIT 1",
                (entry.session_id,),
            ) as cur:
                fresh_user_session = await cur.fetchone() is None

            await self._insert_transcript_entry(
                conn,
                entry,
                expected_epoch=expected_epoch,
            )
            async with conn.execute(
                """
                SELECT collaboration_mode, collaboration_revision,
                       active_plan_revision_id
                FROM sessions
                WHERE session_key = ? AND session_id = ? AND epoch = ?
                """,
                (entry.session_key, entry.session_id, expected_epoch),
            ) as cur:
                collaboration_row = await cur.fetchone()
            if collaboration_row is None:
                await self._raise_stale_epoch(
                    conn,
                    session_key=entry.session_key,
                    expected_epoch=expected_epoch,
                )
            assert collaboration_row is not None
            mode_changed = (
                collaboration_mode_update is not None
                and collaboration_mode_update != collaboration_row["collaboration_mode"]
            )
            active_revision_changed = (
                active_plan_revision_update is not None
                and active_plan_revision_update
                != collaboration_row["active_plan_revision_id"]
            )

            touch_fields = {"updated_at": updated_at, **session_updates}
            touch_assignments = [f"{name} = ?" for name in touch_fields]
            touch_values = [_serialize(value) for value in touch_fields.values()]
            collaboration_changed = mode_changed or active_revision_changed
            if mode_changed:
                touch_assignments.append("collaboration_mode = ?")
                touch_values.append(collaboration_mode_update)
            if active_revision_changed:
                touch_assignments.append("active_plan_revision_id = ?")
                touch_values.append(active_plan_revision_update)
            if collaboration_changed:
                touch_assignments.append(
                    "collaboration_revision = collaboration_revision + 1"
                )
            session_where = "WHERE session_key = ? AND session_id = ? AND epoch = ?"
            session_where_values: list[Any] = [
                entry.session_key,
                entry.session_id,
                expected_epoch,
            ]
            if expected_collaboration_revision is not None:
                session_where += " AND collaboration_revision = ?"
                session_where_values.append(expected_collaboration_revision)
            if expected_active_plan_revision_id is not None:
                session_where += " AND active_plan_revision_id = ?"
                session_where_values.append(expected_active_plan_revision_id)
            async with conn.execute(
                f"UPDATE sessions SET {', '.join(touch_assignments)} "  # noqa: S608
                f"{session_where}",  # noqa: S608
                [*touch_values, *session_where_values],
            ) as cur:
                touched = cur.rowcount or 0
            if touched == 0:
                if (
                    expected_collaboration_revision is not None
                    or expected_active_plan_revision_id is not None
                ):
                    raise PlanConflictError(
                        "plan collaboration state changed before turn acceptance"
                    )
                await self._raise_stale_epoch(
                    conn,
                    session_key=entry.session_key,
                    expected_epoch=expected_epoch,
                )

            if plan_run is not None:
                await self._start_plan_run_on_conn(conn, plan_run)

            async with conn.execute(
                """
                SELECT collaboration_mode, collaboration_revision,
                       active_plan_revision_id
                FROM sessions
                WHERE session_key = ? AND session_id = ? AND epoch = ?
                """,
                (entry.session_key, entry.session_id, expected_epoch),
            ) as cur:
                accepted_collaboration_row = await cur.fetchone()
            assert accepted_collaboration_row is not None

            if task_record is not None and isinstance(task_record.details, dict):
                task_details = dict(task_record.details)
                task_metadata_raw = task_details.get("metadata")
                task_metadata = (
                    dict(task_metadata_raw)
                    if isinstance(task_metadata_raw, dict)
                    else {}
                )
                if accepted_goal_context is not None:
                    task_metadata["required_collaboration_mode"] = "default"
                    task_metadata["required_collaboration_revision"] = int(
                        accepted_collaboration_row["collaboration_revision"]
                    )
                    task_details["metadata"] = task_metadata
                    task_record.details = task_details
                elif task_metadata.get("required_collaboration_mode") in {
                    "default",
                    "plan",
                }:
                    task_metadata["required_collaboration_revision"] = int(
                        accepted_collaboration_row["collaboration_revision"]
                    )
                    task_details["metadata"] = task_metadata
                    task_record.details = task_details

            if task_record is not None:
                incoming_details = dict(task_record.details or {})
                if merge_into_task:
                    async with conn.execute(
                        """
                        SELECT details
                        FROM agent_tasks
                        WHERE task_id = ? AND session_key = ? AND status = ?
                        """,
                        (
                            task_record.task_id,
                            task_record.session_key,
                            AgentTaskStatus.QUEUED.value,
                        ),
                    ) as cur:
                        existing_row = await cur.fetchone()
                    if existing_row is None:
                        raise TaskCollectionUnavailableError(
                            "The target task is no longer queued for collection"
                        )
                    deserialized = _deserialize_row({"details": existing_row["details"]})
                    existing_details_raw = deserialized.get("details")
                    existing_details = (
                        dict(existing_details_raw)
                        if isinstance(existing_details_raw, dict)
                        else {}
                    )
                    details = {**existing_details, **incoming_details}
                    if (
                        isinstance(
                            goal_mutation,
                            (ClaimGoalMutation, ClaimCurrentGoalMutation),
                        )
                        and accepted_goal_context is None
                        and accepted_goal_candidate is None
                    ):
                        details.pop("goal_candidate", None)
                    # A queued task that already owns a frozen Goal context
                    # remains that same Goal turn when later user input is
                    # collected into it.  The current-Goal marker may have
                    # produced an advisory candidate for the incoming input,
                    # but durable task details must never carry both forms.
                    if GoalTurnContext.from_task_detail(
                        details.get("goal_context")
                    ) is not None:
                        details.pop("goal_candidate", None)
                    message_ids = _ordered_detail_message_ids(
                        existing_details.get("persisted_user_message_id"),
                        existing_details.get("persisted_user_message_ids"),
                        incoming_details.get("persisted_user_message_id"),
                        incoming_details.get("persisted_user_message_ids"),
                        entry.message_id,
                    )
                    existing_count = existing_details.get("message_count")
                    incoming_count = incoming_details.get("message_count")
                    existing_count = (
                        existing_count
                        if isinstance(existing_count, int) and existing_count > 0
                        else 0
                    )
                    incoming_count = (
                        incoming_count
                        if isinstance(incoming_count, int) and incoming_count > 0
                        else 0
                    )
                    details["persisted_user_message_id"] = (
                        message_ids[0] if message_ids else entry.message_id
                    )
                    details["persisted_user_message_ids"] = message_ids
                    details["message_count"] = max(
                        1,
                        incoming_count,
                        existing_count + 1,
                    )
                    details["fresh_user_session"] = existing_details.get(
                        "fresh_user_session",
                        fresh_user_session,
                    )
                    task_record.details = details
                    async with conn.execute(
                        """
                        UPDATE agent_tasks
                        SET details = ?, updated_at = ?
                        WHERE task_id = ? AND session_key = ? AND status = ?
                        """,
                        (
                            _serialize(details),
                            task_record.updated_at,
                            task_record.task_id,
                            task_record.session_key,
                            AgentTaskStatus.QUEUED.value,
                        ),
                    ) as cur:
                        merged = cur.rowcount or 0
                    if merged == 0:
                        raise TaskCollectionUnavailableError(
                            "The target task is no longer queued for collection"
                        )
                else:
                    message_ids = _ordered_detail_message_ids(
                        entry.message_id,
                        incoming_details.get("persisted_user_message_id"),
                        incoming_details.get("persisted_user_message_ids"),
                    )
                    incoming_count = incoming_details.get("message_count")
                    details = dict(incoming_details)
                    details["persisted_user_message_id"] = entry.message_id
                    details["persisted_user_message_ids"] = message_ids
                    details["message_count"] = (
                        incoming_count
                        if isinstance(incoming_count, int) and incoming_count > 0
                        else 1
                    )
                    details["fresh_user_session"] = fresh_user_session
                    task_record.details = details
                    await self._insert_agent_task(conn, task_record)

                authoritative_task_details = dict(task_record.details or {})
                accepted_goal_context = (
                    accepted_goal_context
                    or GoalTurnContext.from_task_detail(
                        authoritative_task_details.get("goal_context")
                    )
                )
                if accepted_goal_context is None:
                    accepted_goal_candidate = GoalClaimCandidate.from_task_detail(
                        authoritative_task_details.get("goal_candidate")
                    )
                else:
                    accepted_goal_candidate = None

            if isinstance(goal_mutation, StartGoalMutation):
                assert accepted_goal is not None
                assert task_record is not None
                accepted_goal_command_response = self._goal_mutation_response(
                    command=goal_mutation.command,
                    goal=accepted_goal,
                    session_id=accepted_goal.session_id,
                    epoch=accepted_goal.session_epoch,
                    task_id=task_record.task_id,
                    user_message_id=entry.message_id,
                    previous_goal_id=accepted_goal_previous_id,
                    execution_state="queued",
                )
                await self._insert_goal_command_receipt_on_conn(
                    conn,
                    command=goal_mutation.command,
                    accepted_session_id=accepted_goal.session_id,
                    accepted_session_epoch=accepted_goal.session_epoch,
                    response=accepted_goal_command_response,
                )

            receipt = TurnIngressReceipt(
                source_scope=source_scope,
                request_session_key=request_session_key,
                client_request_id=client_request_id,
                request_fingerprint=request_fingerprint,
                accepted_session_key=entry.session_key,
                session_id=entry.session_id,
                message_id=entry.message_id,
                task_id=(
                    task_record.task_id
                    if task_record is not None
                    else receipt_task_id
                ),
            )
            data = receipt.model_dump()
            cols = list(data.keys())
            placeholders = ", ".join("?" for _ in cols)
            await conn.execute(
                f"INSERT INTO turn_ingress_receipts ({', '.join(cols)}) "
                f"VALUES ({placeholders})",
                [_serialize(data[col]) for col in cols],
            )
            if pending_input_id is not None:
                if pending_dispatch_client_message_id is None:
                    raise AssertionError(
                        "validated pending input lost its client message identity"
                    )
                try:
                    await self._insert_pending_chat_input_dispatch_receipt(
                        conn,
                        pending_input_id=pending_input_id,
                        session_key=request_session_key,
                        source_scope=source_scope,
                        client_request_id=client_request_id,
                        client_message_id=pending_dispatch_client_message_id,
                        request_fingerprint=str(pending_input_fingerprint),
                        accepted_at=receipt.accepted_at,
                    )
                except sqlite3.IntegrityError as exc:
                    raise PendingChatInputConflictError(
                        "pending input dispatch identity was already consumed"
                    ) from exc
                async with conn.execute(
                    """
                    DELETE FROM pending_chat_inputs
                    WHERE pending_input_id = ?
                      AND session_key = ?
                      AND source_scope = ?
                      AND client_request_id = ?
                      AND request_fingerprint = ?
                      AND state_revision = ?
                    """,
                    (
                        pending_input_id,
                        request_session_key,
                        source_scope,
                        client_request_id,
                        pending_input_fingerprint,
                        pending_input_revision,
                    ),
                ) as cur:
                    consumed = int(cur.rowcount or 0)
                if consumed != 1:
                    raise PendingChatInputConflictError(
                        "pending input changed before atomic dispatch"
                    )
            await conn.execute(
                """
                DELETE FROM meta_launch_drafts
                WHERE session_key = ? AND client_request_id = ?
                """,
                (request_session_key, client_request_id),
            )
            if meta_control_intent_id is not None:
                if task_record is None:
                    raise AssertionError(
                        "validated MetaSkill control turn lost its runtime task"
                    )
                async with conn.execute(
                    """
                    UPDATE meta_control_intents
                    SET status = 'accepted', accepted_source_scope = ?,
                        accepted_request_session_key = ?, accepted_client_request_id = ?,
                        accepted_request_fingerprint = ?, accepted_message_id = ?,
                        accepted_task_id = ?, updated_at = ?
                    WHERE intent_id = ? AND status = 'staged'
                    """,
                    (
                        source_scope,
                        request_session_key,
                        client_request_id,
                        request_fingerprint,
                        entry.message_id,
                        task_record.task_id,
                        updated_at,
                        meta_control_intent_id,
                    ),
                ) as cur:
                    if int(cur.rowcount or 0) != 1:
                        raise MetaControlIntentConflictError(
                            "MetaSkill control authorization changed during acceptance"
                        )
            acceptance_result = TurnAcceptanceResult(
                receipt=receipt,
                replayed=False,
                fresh_user_session=fresh_user_session,
                task_status=task_record.status if task_record is not None else None,
                reset_archive_snapshot=reset_archive_snapshot,
                collaboration_mode=str(
                    accepted_collaboration_row["collaboration_mode"]
                ),
                collaboration_revision=int(
                    accepted_collaboration_row["collaboration_revision"]
                ),
                active_plan_revision_id=(
                    str(accepted_collaboration_row["active_plan_revision_id"])
                    if accepted_collaboration_row["active_plan_revision_id"] is not None
                    else None
                ),
                goal=accepted_goal,
                goal_context=accepted_goal_context,
                goal_candidate=accepted_goal_candidate,
                goal_command_response=accepted_goal_command_response,
            )
        if reset_from_session_id is not None:
            _clear_pending_meta_launch_boundary(
                entry.session_key,
                preserve_client_request_id=client_request_id,
                preserve_message=entry.content,
            )
        return acceptance_result

    @_serialized_read
    async def get_transcript(
        self, session_id: str, limit: int | None = None, offset: int = 0
    ) -> list[TranscriptEntry]:
        # SQLite requires LIMIT before OFFSET; use -1 for unlimited
        limit_val = limit if limit is not None else -1
        sql = (
            "SELECT * FROM transcript_entries WHERE session_id = ? "
            "ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?"
        )
        async with self.conn.execute(sql, (session_id, limit_val, offset)) as cur:
            rows = await cur.fetchall()
        return [TranscriptEntry(**_deserialize_row(dict(r))) for r in rows]

    @_serialized_read
    async def get_canonical_transcript(
        self, session_id: str, limit: int | None = None, offset: int = 0
    ) -> list[TranscriptEntry]:
        """Return archived compacted rows plus the active transcript tail.

        Provider replay intentionally keeps using get_transcript(). This API is
        for recovery, diagnostics, and future provider-view construction where
        the raw transcript needs to survive destructive compaction rewrites.
        """
        return await self._select_canonical_transcript(
            self.conn,
            session_id,
            limit=limit,
            offset=offset,
        )

    @_serialized_read
    async def get_canonical_transcript_entry(
        self,
        session_id: str,
        message_id: str,
    ) -> TranscriptEntry | None:
        """Look up one message across compacted history and the active tail."""

        sql = """
            SELECT
                original_entry_id AS id,
                session_id,
                session_key,
                message_id,
                role,
                content,
                tool_calls,
                tool_call_id,
                reasoning_content,
                turn_usage,
                turn_context,
                created_at,
                token_count,
                provenance_kind,
                provenance_origin_session_id,
                provenance_source_session_key,
                provenance_source_channel,
                provenance_source_tool,
                schema_version
            FROM compacted_transcript_entries
            WHERE session_id = ? AND message_id = ?
            UNION ALL
            SELECT
                id,
                session_id,
                session_key,
                message_id,
                role,
                content,
                tool_calls,
                tool_call_id,
                reasoning_content,
                turn_usage,
                turn_context,
                created_at,
                token_count,
                provenance_kind,
                provenance_origin_session_id,
                provenance_source_session_key,
                provenance_source_channel,
                provenance_source_tool,
                schema_version
            FROM transcript_entries
            WHERE session_id = ? AND message_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        """
        async with self.conn.execute(
            sql,
            (session_id, message_id, session_id, message_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return TranscriptEntry(**_deserialize_row(dict(row)))

    @_serialized_read
    async def list_stranded_steer_inputs(self) -> list[StrandedSteerInput]:
        """Return pending steer rows whose exact target task can no longer run.

        The receipt-to-task association is the authority here. Turn-context
        text alone is not enough: old clients may have written similar
        metadata without crossing the atomic ``sessions.steer.v2`` admission
        boundary.
        """

        terminal_statuses = tuple(status.value for status in (
            AgentTaskStatus.SUCCEEDED,
            AgentTaskStatus.FAILED,
            AgentTaskStatus.CANCELLED,
            AgentTaskStatus.TIMEOUT,
            AgentTaskStatus.ABANDONED,
        ))
        placeholders = ", ".join("?" for _ in terminal_statuses)
        async with self.conn.execute(
            f"""
            SELECT receipt.*
            FROM turn_ingress_receipts AS receipt
            JOIN agent_tasks AS task ON task.task_id = receipt.task_id
            WHERE task.status IN ({placeholders})
            ORDER BY receipt.accepted_at ASC, receipt.receipt_id ASC
            """,  # noqa: S608 - placeholders are derived from a fixed enum tuple
            terminal_statuses,
        ) as cur:
            receipt_rows = await cur.fetchall()

        task_cache: dict[str, AgentTaskRecord] = {}
        stranded: list[StrandedSteerInput] = []
        entry_sql = """
            SELECT
                original_entry_id AS id,
                session_id,
                session_key,
                message_id,
                role,
                content,
                tool_calls,
                tool_call_id,
                reasoning_content,
                turn_usage,
                turn_context,
                created_at,
                token_count,
                provenance_kind,
                provenance_origin_session_id,
                provenance_source_session_key,
                provenance_source_channel,
                provenance_source_tool,
                schema_version
            FROM compacted_transcript_entries
            WHERE session_id = ? AND message_id = ?
            UNION ALL
            SELECT
                id,
                session_id,
                session_key,
                message_id,
                role,
                content,
                tool_calls,
                tool_call_id,
                reasoning_content,
                turn_usage,
                turn_context,
                created_at,
                token_count,
                provenance_kind,
                provenance_origin_session_id,
                provenance_source_session_key,
                provenance_source_channel,
                provenance_source_tool,
                schema_version
            FROM transcript_entries
            WHERE session_id = ? AND message_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        """
        for raw_receipt in receipt_rows:
            receipt = TurnIngressReceipt(**_deserialize_row(dict(raw_receipt)))
            target_task_id = receipt.task_id
            if target_task_id is None:
                continue
            target_task = task_cache.get(target_task_id)
            if target_task is None:
                async with self.conn.execute(
                    "SELECT * FROM agent_tasks WHERE task_id = ?",
                    (target_task_id,),
                ) as cur:
                    raw_task = await cur.fetchone()
                if raw_task is None:
                    continue
                target_task = AgentTaskRecord(**_deserialize_row(dict(raw_task)))
                task_cache[target_task_id] = target_task

            async with self.conn.execute(
                entry_sql,
                (
                    receipt.session_id,
                    receipt.message_id,
                    receipt.session_id,
                    receipt.message_id,
                ),
            ) as cur:
                raw_entry = await cur.fetchone()
            if raw_entry is None:
                continue
            entry = TranscriptEntry(**_deserialize_row(dict(raw_entry)))
            context = entry.turn_context
            if (
                entry.role != "user"
                or not isinstance(context, dict)
                or context.get("intent") != "steer"
                or context.get("disposition") != "steering"
                or context.get("target_turn_id") != target_task_id
            ):
                continue
            stranded.append(
                StrandedSteerInput(
                    entry=entry,
                    receipt=receipt,
                    target_task=target_task,
                )
            )
        return stranded

    async def close_stranded_steer_inputs(
        self,
        *,
        target_task_id: str,
        message_ids: Sequence[str],
        disposition: str,
        failure_code: str | None = None,
        retryable: bool | None = None,
        recovery: str | None = None,
        application_evidence: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> list[str]:
        """Atomically terminalize still-pending steer rows without executing them."""

        if disposition not in {"applied", "cancelled", "rejected"}:
            raise ValueError("disposition must be applied, cancelled, or rejected")
        ordered_ids = list(dict.fromkeys(message_id for message_id in message_ids if message_id))
        if not ordered_ids:
            return []
        changed: list[str] = []
        async with self._write_transaction("close_stranded_steer_inputs") as conn:
            for message_id in ordered_ids:
                async with conn.execute(
                    """
                    SELECT receipt_id, session_id
                    FROM turn_ingress_receipts
                    WHERE message_id = ? AND task_id = ?
                    ORDER BY accepted_at ASC, receipt_id ASC
                    LIMIT 1
                    """,
                    (message_id, target_task_id),
                ) as cur:
                    receipt_row = await cur.fetchone()
                if receipt_row is None:
                    continue
                entry_row: Any | None = None
                entry_table: str | None = None
                for table in ("transcript_entries", "compacted_transcript_entries"):
                    async with conn.execute(
                        f"SELECT turn_context FROM {table} "  # noqa: S608
                        "WHERE session_id = ? AND message_id = ?",
                        (receipt_row["session_id"], message_id),
                    ) as cur:
                        candidate = await cur.fetchone()
                    if candidate is not None:
                        entry_row = candidate
                        entry_table = table
                        break
                if entry_row is None or entry_table is None:
                    continue
                context_raw = _deserialize_row(
                    {"turn_context": entry_row["turn_context"]}
                ).get("turn_context")
                if (
                    not isinstance(context_raw, dict)
                    or context_raw.get("intent") != "steer"
                    or context_raw.get("disposition") != "steering"
                    or context_raw.get("target_turn_id") != target_task_id
                ):
                    continue
                try:
                    revision = int(context_raw.get("revision", 1) or 1)
                except (TypeError, ValueError):
                    revision = 1
                context = {
                    **context_raw,
                    "turn_id": target_task_id,
                    "target_turn_id": target_task_id,
                    "disposition": disposition,
                    "revision": max(2, revision + 1),
                }
                if failure_code is not None:
                    context["failure_code"] = failure_code
                if retryable is not None:
                    context["retryable"] = retryable
                if recovery is not None:
                    context["recovery"] = recovery
                if disposition in {"cancelled", "rejected"}:
                    context["fallback_safe"] = disposition == "cancelled"
                if disposition == "applied" and application_evidence is not None:
                    evidence = application_evidence.get(message_id)
                    if isinstance(evidence, Mapping):
                        applied_iteration = evidence.get("applied_iteration")
                        model_call_id = evidence.get("model_call_id")
                        if applied_iteration is not None:
                            context["applied_iteration"] = applied_iteration
                        if model_call_id:
                            context["model_call_id"] = model_call_id
                async with conn.execute(
                    f"UPDATE {entry_table} SET turn_context = ? "  # noqa: S608
                    "WHERE session_id = ? AND message_id = ? AND turn_context = ?",
                    (
                        _serialize(context),
                        receipt_row["session_id"],
                        message_id,
                        entry_row["turn_context"],
                    ),
                ) as cur:
                    if (cur.rowcount or 0) > 0:
                        changed.append(message_id)
        return changed

    async def promote_stranded_steer_inputs(
        self,
        *,
        target_task_id: str,
        message_ids: Sequence[str],
        task_record: AgentTaskRecord,
        recovery: str = "process_restart_followup",
    ) -> list[str]:
        """Create one follow-up and claim its steer rows atomically."""

        ordered_ids = list(dict.fromkeys(message_id for message_id in message_ids if message_id))
        if not ordered_ids:
            return []
        task_record.session_key = canonicalize_session_key(task_record.session_key)
        task_record.agent_id = normalize_agent_id(task_record.agent_id)
        claimed: list[tuple[str, str, str, dict[str, Any]]] = []
        async with self._write_transaction("promote_stranded_steer_inputs") as conn:
            async with conn.execute(
                "SELECT status FROM agent_tasks WHERE task_id = ?",
                (target_task_id,),
            ) as cur:
                target_row = await cur.fetchone()
            if (
                target_row is None
                or AgentTaskStatus(target_row["status"])
                not in {
                    AgentTaskStatus.SUCCEEDED,
                    AgentTaskStatus.FAILED,
                    AgentTaskStatus.CANCELLED,
                    AgentTaskStatus.TIMEOUT,
                    AgentTaskStatus.ABANDONED,
                }
            ):
                return []

            for message_id in ordered_ids:
                async with conn.execute(
                    """
                    SELECT receipt_id, session_id
                    FROM turn_ingress_receipts
                    WHERE message_id = ? AND task_id = ?
                    ORDER BY accepted_at ASC, receipt_id ASC
                    LIMIT 1
                    """,
                    (message_id, target_task_id),
                ) as cur:
                    receipt_row = await cur.fetchone()
                if receipt_row is None:
                    continue
                for table in ("transcript_entries", "compacted_transcript_entries"):
                    async with conn.execute(
                        f"SELECT session_key, turn_context FROM {table} "  # noqa: S608
                        "WHERE session_id = ? AND message_id = ?",
                        (receipt_row["session_id"], message_id),
                    ) as cur:
                        entry_row = await cur.fetchone()
                    if entry_row is None:
                        continue
                    context_raw = _deserialize_row(
                        {"turn_context": entry_row["turn_context"]}
                    ).get("turn_context")
                    if (
                        isinstance(context_raw, dict)
                        and context_raw.get("intent") == "steer"
                        and context_raw.get("disposition") == "steering"
                        and context_raw.get("target_turn_id") == target_task_id
                        and entry_row["session_key"] == task_record.session_key
                    ):
                        claimed.append(
                            (
                                message_id,
                                table,
                                receipt_row["receipt_id"],
                                context_raw,
                            )
                        )
                    break

            # A partial batch would make the runtime prompt disagree with the
            # durable ownership transfer. Leave it untouched for the next
            # recovery scan instead of executing a duplicate or incomplete
            # follow-up.
            if len(claimed) != len(ordered_ids):
                return []

            await self._insert_agent_task(conn, task_record)
            for message_id, table, receipt_id, previous_context in claimed:
                try:
                    revision = int(previous_context.get("revision", 1) or 1)
                except (TypeError, ValueError):
                    revision = 1
                context = {
                    **previous_context,
                    "turn_id": task_record.task_id,
                    "target_turn_id": target_task_id,
                    "disposition": "promoted",
                    "promoted_from_turn_id": target_task_id,
                    "promoted_turn_id": task_record.task_id,
                    "revision": max(2, revision + 1),
                    "recovery": recovery,
                }
                await conn.execute(
                    f"UPDATE {table} SET turn_context = ? "  # noqa: S608
                    "WHERE session_key = ? AND message_id = ?",
                    (_serialize(context), task_record.session_key, message_id),
                )
                await conn.execute(
                    """
                    UPDATE turn_ingress_receipts
                    SET task_id = ?
                    WHERE receipt_id = ? AND task_id = ?
                    """,
                    (task_record.task_id, receipt_id, target_task_id),
                )
        return [message_id for message_id, *_rest in claimed]

    @_serialized_read
    async def list_retryable_steer_recovery_tasks(self) -> list[AgentTaskRecord]:
        """Return crash-abandoned recovery tasks that never reached RUNNING."""

        async with self.conn.execute(
            """
            SELECT *
            FROM agent_tasks
            WHERE status = ?
              AND terminal_reason = ?
              AND started_at IS NULL
            ORDER BY created_at ASC, task_id ASC
            """,
            (AgentTaskStatus.ABANDONED, "process_restart"),
        ) as cur:
            rows = await cur.fetchall()
        tasks: list[AgentTaskRecord] = []
        for row in rows:
            task = AgentTaskRecord(**_deserialize_row(dict(row)))
            details = task.details if isinstance(task.details, dict) else {}
            metadata = details.get("metadata")
            if (
                isinstance(metadata, dict)
                and metadata.get("steer_restart_recovery") is True
            ):
                tasks.append(task)
        return tasks

    async def requeue_steer_recovery_task(self, task_id: str) -> bool:
        """CAS a never-started recovery task back to QUEUED after restart."""

        async with self._write_transaction("requeue_steer_recovery_task") as conn:
            async with conn.execute(
                """
                UPDATE agent_tasks
                SET status = ?,
                    updated_at = ?,
                    finished_at = NULL,
                    terminal_reason = NULL,
                    error_class = NULL,
                    error_message = NULL
                WHERE task_id = ?
                  AND status = ?
                  AND terminal_reason = ?
                  AND started_at IS NULL
                """,
                (
                    AgentTaskStatus.QUEUED,
                    _now_ms(),
                    task_id,
                    AgentTaskStatus.ABANDONED,
                    "process_restart",
                ),
            ) as cur:
                changed = cur.rowcount or 0
            if changed:
                async with conn.execute(
                    "SELECT details FROM agent_tasks WHERE task_id = ?",
                    (task_id,),
                ) as cur:
                    row = await cur.fetchone()
                if row is not None:
                    details_raw = _deserialize_row({"details": row["details"]}).get(
                        "details"
                    )
                    details = (
                        dict(details_raw) if isinstance(details_raw, dict) else {}
                    )
                    details.pop("turn_outcome", None)
                    await conn.execute(
                        "UPDATE agent_tasks SET details = ? WHERE task_id = ?",
                        (_serialize(details), task_id),
                    )
        return changed > 0

    async def _canonical_transcript_cursor_exists(
        self,
        session_id: str,
        cursor: tuple[int, int],
    ) -> bool:
        created_at, entry_id = cursor
        sql = """
            SELECT 1
            FROM transcript_entries
            WHERE session_id = ? AND created_at = ? AND id = ?
            UNION ALL
            SELECT 1
            FROM compacted_transcript_entries
            WHERE session_id = ? AND created_at = ? AND original_entry_id = ?
            LIMIT 1
        """
        async with self.conn.execute(
            sql,
            (session_id, created_at, entry_id, session_id, created_at, entry_id),
        ) as cur:
            return await cur.fetchone() is not None

    @_serialized_read
    async def get_canonical_transcript_page(
        self,
        session_id: str,
        *,
        limit: int,
        before: tuple[int, int] | None = None,
        after: tuple[int, int] | None = None,
    ) -> tuple[list[TranscriptEntry], bool]:
        """Return one keyset page across archived and active transcript rows.

        Each source CTE is bounded to ``limit + 1`` rows and both are merged in
        one SQLite read snapshot. ``before`` keeps its historical precedence
        over ``after`` when both cursors exist; an unknown cursor is ignored,
        matching the legacy list-pagination path.
        """
        page_size = max(1, int(limit))
        fetch_size = page_size + 1

        resolved_before = before
        if resolved_before is not None and not await self._canonical_transcript_cursor_exists(
            session_id,
            resolved_before,
        ):
            resolved_before = None

        resolved_after = None
        if resolved_before is None and after is not None:
            if await self._canonical_transcript_cursor_exists(session_id, after):
                resolved_after = after

        cursor = resolved_before or resolved_after
        ascending = resolved_after is not None
        comparator = ">" if ascending else "<"
        direction = "ASC" if ascending else "DESC"

        active_params: list[Any] = [session_id]
        active_cursor_clause = ""
        if cursor is not None:
            created_at, entry_id = cursor
            active_cursor_clause = (
                f"AND (created_at {comparator} ? "
                f"OR (created_at = ? AND id {comparator} ?))"
            )
            active_params.extend((created_at, created_at, entry_id))
        active_params.append(fetch_size)
        archived_params: list[Any] = [session_id]
        archived_cursor_clause = ""
        if cursor is not None:
            created_at, entry_id = cursor
            archived_cursor_clause = (
                f"AND (created_at {comparator} ? "
                f"OR (created_at = ? AND original_entry_id {comparator} ?))"
            )
            archived_params.extend((created_at, created_at, entry_id))
        archived_params.append(fetch_size)
        sql = f"""
            WITH active_page AS (
                SELECT
                    id,
                    session_id,
                    session_key,
                    message_id,
                    role,
                    content,
                    tool_calls,
                    tool_call_id,
                    reasoning_content,
                    turn_usage,
                    turn_context,
                    created_at,
                    token_count,
                    provenance_kind,
                    provenance_origin_session_id,
                    provenance_source_session_key,
                    provenance_source_channel,
                    provenance_source_tool,
                    schema_version
                FROM transcript_entries
                WHERE session_id = ?
                  {active_cursor_clause}
                ORDER BY created_at {direction}, id {direction}
                LIMIT ?
            ),
            archived_page AS (
                SELECT
                    original_entry_id AS id,
                    session_id,
                    session_key,
                    message_id,
                    role,
                    content,
                    tool_calls,
                    tool_call_id,
                    reasoning_content,
                    turn_usage,
                    turn_context,
                    created_at,
                    token_count,
                    provenance_kind,
                    provenance_origin_session_id,
                    provenance_source_session_key,
                    provenance_source_channel,
                    provenance_source_tool,
                    schema_version
                FROM compacted_transcript_entries
                WHERE session_id = ?
                  {archived_cursor_clause}
                ORDER BY
                    created_at {direction},
                    original_entry_id {direction},
                    id {direction}
                LIMIT ?
            ),
            merged AS (
                SELECT * FROM active_page
                UNION ALL
                SELECT * FROM archived_page
            )
            SELECT *
            FROM merged
            ORDER BY created_at {direction}, id {direction}
            LIMIT ?
        """

        # Both sources must be read by one SQLite statement. A compaction moves
        # rows from transcript_entries into compacted_transcript_entries inside
        # one transaction; separate SELECT statements could otherwise observe
        # opposite sides of that move and duplicate or omit canonical rows.
        params = [*active_params, *archived_params, fetch_size]
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()

        entries = [TranscriptEntry(**_deserialize_row(dict(row))) for row in rows]
        has_more = len(entries) > page_size
        entries = entries[:page_size]
        if not ascending:
            entries.reverse()
        return entries, has_more

    @_serialized_read
    async def get_canonical_transcript_coverage(
        self,
        session_id: str,
    ) -> CanonicalTranscriptCoverage:
        """Read canonical coverage and current session metadata in one snapshot."""
        sql = """
            SELECT
                session.compaction_count,
                session.forked_from_parent,
                session.schema_version,
                (SELECT COUNT(*)
                 FROM session_summaries
                 WHERE session_id = session.session_id) AS summary_count,
                (SELECT COALESCE(SUM(removed_count), 0)
                 FROM session_summaries
                 WHERE session_id = session.session_id) AS removed_count,
                (SELECT COUNT(*)
                 FROM compacted_transcript_entries
                 WHERE session_id = session.session_id) AS archived_count,
                (SELECT COUNT(*)
                 FROM compacted_transcript_entries
                 WHERE session_id = session.session_id
                   AND original_entry_id IS NULL) AS missing_ids,
                (SELECT COUNT(*)
                 FROM session_summaries AS summary
                 WHERE summary.session_id = session.session_id
                   AND (
                     summary.compaction_id IS NULL
                     OR (summary.removed_count = 0 AND summary.covered_through_id > 0)
                     OR COALESCE((
                       SELECT COUNT(*)
                       FROM compacted_transcript_entries AS archived
                       WHERE archived.session_id = summary.session_id
                         AND archived.compaction_id = summary.compaction_id
                     ), 0) != summary.removed_count
                   )) AS mismatched_summaries
            FROM sessions AS session
            WHERE session.session_id = ?
            LIMIT 1
        """
        async with self.conn.execute(sql, (session_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return CanonicalTranscriptCoverage(
                canonical_complete=False,
                compaction_count=0,
                inherited_compactions=False,
            )
        summary_count = int(row["summary_count"] or 0)
        expected_compactions = max(0, int(row["compaction_count"] or 0))
        inherited_compactions = bool(row["forked_from_parent"])
        archived_count = int(row["archived_count"] or 0)
        fork_coverage_proven = not inherited_compactions
        if inherited_compactions:
            # A legacy fork stored only a reusable parent session key, not the
            # fork-time parent identity or coverage. Never let the parent's
            # current row—or the child's later compactions—retroactively prove
            # that an ambiguous inherited prefix retained every original row.
            fork_coverage_proven = (
                int(row["schema_version"] or 0)
                >= CANONICAL_FORK_PROOF_SCHEMA_VERSION
            )
        compaction_count_matches = (
            summary_count >= expected_compactions
            if inherited_compactions
            else summary_count == expected_compactions
        )
        canonical_complete = (
            fork_coverage_proven
            and compaction_count_matches
            and int(row["removed_count"] or 0) == archived_count
            and int(row["missing_ids"] or 0) == 0
            and int(row["mismatched_summaries"] or 0) == 0
        )
        return CanonicalTranscriptCoverage(
            canonical_complete=canonical_complete,
            compaction_count=expected_compactions,
            inherited_compactions=inherited_compactions,
        )

    async def is_canonical_transcript_complete(self, session_id: str) -> bool:
        """Return whether every current compaction has a complete raw archive."""
        coverage = await self.get_canonical_transcript_coverage(session_id)
        return coverage.canonical_complete

    async def copy_compacted_transcript_entries(
        self,
        *,
        source_session_id: str,
        target_session_id: str,
        target_session_key: str,
        terminal_outcome_projections: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        """Copy archived compacted transcript rows into a forked session."""
        async with self._write_transaction("copy_compacted_transcript_entries") as conn:
            await conn.execute(
                """
                INSERT INTO compacted_transcript_entries (
                session_id,
                session_key,
                compaction_id,
                compaction_index,
                original_entry_id,
                message_id,
                role,
                content,
                tool_calls,
                tool_call_id,
                reasoning_content,
                turn_usage,
                turn_context,
                created_at,
                token_count,
                provenance_kind,
                provenance_origin_session_id,
                provenance_source_session_key,
                provenance_source_channel,
                provenance_source_tool,
                archived_at,
                schema_version
            )
            SELECT
                ?,
                ?,
                compaction_id,
                compaction_index,
                original_entry_id,
                message_id,
                role,
                content,
                tool_calls,
                tool_call_id,
                reasoning_content,
                turn_usage,
                turn_context,
                created_at,
                token_count,
                provenance_kind,
                provenance_origin_session_id,
                provenance_source_session_key,
                provenance_source_channel,
                provenance_source_tool,
                archived_at,
                schema_version
            FROM compacted_transcript_entries
            WHERE session_id = ?
            ORDER BY created_at ASC, original_entry_id ASC, id ASC
                """,
                (target_session_id, target_session_key, source_session_id),
            )
            if terminal_outcome_projections is None:
                return
            async with conn.execute(
                "SELECT id, turn_context FROM compacted_transcript_entries "
                "WHERE session_id = ?",
                (target_session_id,),
            ) as cursor:
                rows = await cursor.fetchall()
            for row in rows:
                context = _json_object_or_none(row["turn_context"])
                turn_id = turn_id_from_context(context)
                rebound_context = attach_fork_terminal_outcome_projection(
                    context,
                    terminal_outcome_projections.get(turn_id or ""),
                )
                if rebound_context == context:
                    continue
                await conn.execute(
                    "UPDATE compacted_transcript_entries SET turn_context = ? WHERE id = ?",
                    (_serialize(rebound_context), row["id"]),
                )

    @_serialized_read
    async def count_transcript_entries(self, session_id: str) -> int:
        async with self.conn.execute(
            "SELECT COUNT(*) FROM transcript_entries WHERE session_id = ?", (session_id,)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    @_serialized_read
    async def count_transcript_entries_batch(
        self, session_ids: list[str]
    ) -> dict[str, int]:
        """Count transcript entries for many sessions in one round trip.

        Used by ``sessions.list`` (rpc_sessions.py) to avoid the N+1 pattern
        where the previous implementation awaited ``count_transcript_entries``
        once per row. Returns ``{session_id: count}`` with missing ids
        explicitly defaulted to 0. The single-id ``count_transcript_entries``
        is kept for backward compatibility with other callers.

        Chunk size 500 stays well below SQLite's default
        ``SQLITE_MAX_VARIABLE_NUMBER`` (999 since 3.32) with headroom.
        """
        if not session_ids:
            return {}
        chunk = 500
        result: dict[str, int] = {}
        for i in range(0, len(session_ids), chunk):
            batch = session_ids[i : i + chunk]
            placeholders = ",".join(["?"] * len(batch))
            sql = (
                f"SELECT session_id, COUNT(*) FROM transcript_entries "
                f"WHERE session_id IN ({placeholders}) GROUP BY session_id"
            )
            async with self.conn.execute(sql, batch) as cur:
                rows = await cur.fetchall()
            for sid, cnt in rows:
                result[sid] = cnt
        for sid in session_ids:
            result.setdefault(sid, 0)
        return result

    @_serialized_read
    async def list_user_transcript_content_batch(
        self,
        session_ids: list[str],
        *,
        limit_per_session: int = 3,
    ) -> dict[str, list[str]]:
        """Return early user transcript content for many sessions.

        ``sessions.list`` uses this to render semantic conversation titles
        without issuing one transcript query per session row.
        """
        if not session_ids:
            return {}
        chunk = 300
        result: dict[str, list[str]] = {sid: [] for sid in session_ids}
        for i in range(0, len(session_ids), chunk):
            batch = session_ids[i : i + chunk]
            placeholders = ",".join(["?"] * len(batch))
            sql = f"""
                SELECT session_id, content
                FROM (
                    SELECT
                        session_id,
                        content,
                        ROW_NUMBER() OVER (
                            PARTITION BY session_id
                            ORDER BY created_at ASC, id ASC
                        ) AS rn
                    FROM transcript_entries
                    WHERE session_id IN ({placeholders})
                        AND role = 'user'
                        AND COALESCE(content, '') != ''
                )
                WHERE rn <= ?
                ORDER BY session_id ASC, rn ASC
            """
            async with self.conn.execute(sql, [*batch, limit_per_session]) as cur:
                rows = await cur.fetchall()
            for sid, content in rows:
                if isinstance(content, str):
                    result.setdefault(sid, []).append(content)
        return result

    async def delete_transcript(self, session_id: str) -> None:
        async with self._write_transaction("delete_transcript") as conn:
            await conn.execute(
                "DELETE FROM transcript_entries WHERE session_id = ?", (session_id,)
            )
            await conn.execute(
                "DELETE FROM compacted_transcript_entries WHERE session_id = ?",
                (session_id,),
            )

    async def delete_transcript_entry(self, session_id: str, message_id: str) -> bool:
        """Delete a single transcript entry by ``message_id``.

        Returns True iff a row was actually removed. Used to roll back an
        ``append_message`` whose follow-up enqueue failed (e.g. the agent task
        queue is full), so the client can safely retry without leaving a
        ghost user turn behind.
        """
        async with self._write_transaction("delete_transcript_entry") as conn:
            async with conn.execute(
                "DELETE FROM transcript_entries WHERE session_id = ? AND message_id = ?",
                (session_id, message_id),
            ) as cur:
                removed = cur.rowcount or 0
        return removed > 0

    async def update_transcript_turn_context(
        self,
        session_key: str,
        message_id: str,
        turn_context: dict[str, Any],
    ) -> bool:
        """Replace one message's additive causal identity snapshot.

        The row can cross into the compacted archive while a queued turn waits,
        so update both canonical transcript tables in one transaction.
        """

        encoded = _serialize(turn_context)
        changed = 0
        async with self._write_transaction("update_transcript_turn_context") as conn:
            for table in ("transcript_entries", "compacted_transcript_entries"):
                async with conn.execute(
                    f"UPDATE {table} SET turn_context = ? "
                    "WHERE session_key = ? AND message_id = ?",
                    (encoded, session_key, message_id),
                ) as cur:
                    changed += cur.rowcount or 0
        return changed > 0

    async def delete_summaries(self, session_id: str) -> None:
        async with self._write_transaction("delete_summaries") as conn:
            await conn.execute(
                "DELETE FROM session_summaries WHERE session_id = ?", (session_id,)
            )

    @_serialized_read
    async def get_recent_transcript(self, session_id: str, n: int) -> list[TranscriptEntry]:
        """Return the most recent n entries, ordered oldest-first."""
        sql = (
            "SELECT * FROM (SELECT * FROM transcript_entries WHERE session_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?) ORDER BY created_at ASC, id ASC"
        )
        async with self.conn.execute(sql, (session_id, n)) as cur:
            rows = await cur.fetchall()
        return [TranscriptEntry(**_deserialize_row(dict(r))) for r in rows]

    # ── SessionSummary CRUD ──────────────────────────────────────────────────

    async def save_summary(self, summary: SessionSummary) -> SessionSummary:
        """Persist a compaction summary. Sets compaction_index automatically."""
        _next_idx_sql = (
            "SELECT COALESCE(MAX(compaction_index), -1) + 1 "
            "FROM session_summaries WHERE session_id = ?"
        )
        async with self._write_transaction("save_summary") as conn:
            async with conn.execute(_next_idx_sql, (summary.session_id,)) as cur:
                row = await cur.fetchone()
            summary.compaction_index = row[0] if row else 0

            data = summary.model_dump(exclude={"id"})
            cols = list(data.keys())
            placeholders = ", ".join("?" for _ in cols)
            values = [_serialize(data[c]) for c in cols]
            async with conn.execute(
                f"INSERT INTO session_summaries ({', '.join(cols)}) VALUES ({placeholders})",
                values,
            ) as cur:
                summary.id = cur.lastrowid
        return summary

    async def _archive_transcript_entries(
        self,
        *,
        node: SessionNode,
        entries: list[TranscriptEntry],
        compaction_id: str | None,
        compaction_index: int | None,
    ) -> None:
        if not entries:
            return
        archived_at = _now_ms()
        for entry in entries:
            entry_data = entry.model_dump(exclude={"id"})
            entry_data["session_id"] = node.session_id
            entry_data["session_key"] = node.session_key
            archive_data: dict[str, Any] = {
                "session_id": entry_data.pop("session_id"),
                "session_key": entry_data.pop("session_key"),
                "compaction_id": compaction_id,
                "compaction_index": compaction_index,
                "original_entry_id": entry.id,
                **entry_data,
                "archived_at": archived_at,
            }
            cols = list(archive_data.keys())
            placeholders = ", ".join("?" for _ in cols)
            values = [_serialize(archive_data[c]) for c in cols]
            await self.conn.execute(
                "INSERT INTO compacted_transcript_entries "
                f"({', '.join(cols)}) VALUES ({placeholders})",
                values,
            )

    async def rewrite_compacted_session(
        self,
        *,
        node: SessionNode,
        summary: SessionSummary | None,
        entries: list[TranscriptEntry],
        context_states: list[SessionContextState] | None = None,
        archived_entries: list[TranscriptEntry] | None = None,
        expected_source_entries: Sequence[TranscriptEntry] | None = None,
        expected_source_preimage: Sequence[Sequence[Any]] | None = None,
        expected_source_boundary_message_id: str | None = None,
        expected_source_boundary_entry_id: int | None = None,
        expected_context_fingerprint: str | None = None,
    ) -> bool:
        """Atomically persist a compaction rewrite for one session."""
        node.session_key = canonicalize_session_key(node.session_key)
        node.agent_id = normalize_agent_id(node.agent_id)

        async with self._write_transaction("rewrite_compacted_session") as conn:
            preserve_surviving_rows = expected_source_entries is not None
            if expected_source_entries is not None:
                expected_prefix = list(expected_source_entries)
                async with conn.execute(
                    "SELECT * FROM transcript_entries WHERE session_id = ? "
                    "ORDER BY created_at ASC, id ASC",
                    (node.session_id,),
                ) as cur:
                    current_rows = await cur.fetchall()
                current_entries = [
                    TranscriptEntry(**_deserialize_row(dict(row))) for row in current_rows
                ]
                source_count = len(expected_prefix)
                frozen_preimage = tuple(
                    tuple(item) for item in (expected_source_preimage or ())
                )
                if (
                    len(current_entries) < source_count
                    or frozen_preimage != _transcript_preimage(expected_prefix)
                    or _transcript_preimage(current_entries[:source_count])
                    != frozen_preimage
                ):
                    return False
                boundary = expected_prefix[-1] if expected_prefix else None
                if expected_source_boundary_message_id is not None and (
                    boundary is None
                    or boundary.message_id != expected_source_boundary_message_id
                ):
                    return False
                if expected_source_boundary_entry_id is not None and (
                    boundary is None
                    or boundary.id != expected_source_boundary_entry_id
                ):
                    return False
                archived_prefix = list(archived_entries or [])
                archived_count = len(archived_prefix)
                if (
                    archived_count > source_count
                    or _transcript_preimage(archived_prefix)
                    != _transcript_preimage(expected_prefix[:archived_count])
                    or _transcript_preimage(entries)
                    != _transcript_preimage(expected_prefix[archived_count:])
                    or any(entry.id is None for entry in archived_prefix)
                ):
                    return False

            if expected_context_fingerprint is not None:
                from opensquilla.session.context_view import (
                    compaction_context_fingerprint,
                )

                current_summaries = await self._select_all_summaries(
                    conn,
                    node.session_id,
                )
                async with conn.execute(
                    "SELECT * FROM session_context_states "
                    "WHERE session_key = ? AND valid = 1 "
                    "ORDER BY created_at ASC, id ASC",
                    (node.session_key,),
                ) as cur:
                    context_rows = await cur.fetchall()
                current_context_states = [
                    SessionContextState(**_deserialize_row(dict(row)))
                    for row in context_rows
                ]
                if (
                    compaction_context_fingerprint(
                        context_states=current_context_states,
                        summaries=current_summaries,
                    )
                    != expected_context_fingerprint
                ):
                    return False

            if summary is not None:
                summary.session_id = node.session_id
                summary.session_key = node.session_key
                async with conn.execute(
                    "SELECT COALESCE(MAX(compaction_index), -1) + 1 "
                    "FROM session_summaries WHERE session_id = ?",
                    (summary.session_id,),
                ) as cur:
                    row = await cur.fetchone()
                summary.compaction_index = row[0] if row else 0

            await self._archive_transcript_entries(
                node=node,
                entries=archived_entries or [],
                compaction_id=summary.compaction_id if summary is not None else None,
                compaction_index=summary.compaction_index
                if summary is not None
                else None,
            )

            if preserve_surviving_rows:
                # Delete only the archived frozen prefix. The surviving source
                # tail and rows appended after the frozen boundary stay in
                # place, preserving their stable transcript ids and external
                # keyset cursors.
                archived_ids = [
                    int(entry.id)
                    for entry in (archived_entries or [])
                    if entry.id is not None
                ]
                chunk_size = max(1, _SQLITE_VARIABLE_CHUNK_SIZE - 1)
                deleted_count = 0
                for start in range(0, len(archived_ids), chunk_size):
                    chunk = archived_ids[start : start + chunk_size]
                    placeholders = ", ".join("?" for _ in chunk)
                    cursor = await conn.execute(
                        "DELETE FROM transcript_entries "
                        f"WHERE session_id = ? AND id IN ({placeholders})",  # noqa: S608
                        (node.session_id, *chunk),
                    )
                    deleted_count += max(0, int(cursor.rowcount or 0))
                if deleted_count != len(archived_ids):
                    raise RuntimeError(
                        "compaction source changed while deleting frozen prefix"
                    )
            else:
                await conn.execute(
                    "DELETE FROM transcript_entries WHERE session_id = ?",
                    (node.session_id,),
                )

            if summary is not None:
                summary_data = summary.model_dump(exclude={"id"})
                summary_cols = list(summary_data.keys())
                summary_placeholders = ", ".join("?" for _ in summary_cols)
                summary_values = [_serialize(summary_data[c]) for c in summary_cols]
                async with conn.execute(
                    "INSERT INTO session_summaries "
                    f"({', '.join(summary_cols)}) VALUES ({summary_placeholders})",
                    summary_values,
                ) as cur:
                    summary.id = cur.lastrowid

            for state in context_states or []:
                state.session_id = node.session_id
                state.session_key = node.session_key
                state_data = state.model_dump(exclude={"id"})
                state_cols = list(state_data.keys())
                state_placeholders = ", ".join("?" for _ in state_cols)
                state_values = [_serialize(state_data[c]) for c in state_cols]
                async with conn.execute(
                    "INSERT INTO session_context_states "
                    f"({', '.join(state_cols)}) VALUES ({state_placeholders})",
                    state_values,
                ) as cur:
                    state.id = cur.lastrowid

            if not preserve_surviving_rows:
                for entry in entries:
                    entry.session_id = node.session_id
                    entry.session_key = node.session_key
                    entry_data = entry.model_dump(exclude={"id"})
                    entry_cols = list(entry_data.keys())
                    entry_placeholders = ", ".join("?" for _ in entry_cols)
                    entry_values = [_serialize(entry_data[c]) for c in entry_cols]
                    await conn.execute(
                        "INSERT INTO transcript_entries "
                        f"({', '.join(entry_cols)}) VALUES ({entry_placeholders})",
                        entry_values,
                    )

            if preserve_surviving_rows:
                # A suffix append is allowed after the frozen source boundary.
                # Its transaction has already advanced token totals/freshness
                # and updated_at, so never replace those fields from the stale
                # node snapshot captured before compaction began.
                async with conn.execute(
                    """
                    UPDATE sessions
                    SET compaction_count = MAX(compaction_count, ?),
                        updated_at = MAX(updated_at, ?)
                    WHERE session_key = ? AND session_id = ? AND epoch = ?
                    """,
                    (
                        node.compaction_count,
                        node.updated_at,
                        node.session_key,
                        node.session_id,
                        node.epoch,
                    ),
                ) as cur:
                    if (cur.rowcount or 0) != 1:
                        raise RuntimeError(
                            "session changed while committing compaction metadata"
                        )
            else:
                node_data = node.model_dump()
                node_cols = list(node_data.keys())
                node_placeholders = ", ".join("?" for _ in node_cols)
                node_updates: list[str] = []
                for col in node_cols:
                    if col == "session_key" or col in _SESSION_DEDICATED_WRITER_COLUMNS:
                        continue
                    if col == "epoch":
                        node_updates.append("epoch = MAX(sessions.epoch, excluded.epoch)")
                    else:
                        node_updates.append(f"{col}=excluded.{col}")
                node_values = [_serialize(node_data[c]) for c in node_cols]
                await conn.execute(
                    f"INSERT INTO sessions ({', '.join(node_cols)}) "
                    f"VALUES ({node_placeholders}) "
                    f"ON CONFLICT(session_key) DO UPDATE SET {', '.join(node_updates)}",
                    node_values,
                )
        return True

    @_serialized_read
    async def get_latest_summary(self, session_id: str) -> SessionSummary | None:
        async with self.conn.execute(
            "SELECT * FROM session_summaries WHERE session_id = ? "
            "ORDER BY compaction_index DESC LIMIT 1",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return SessionSummary(**_deserialize_row(dict(row)))

    @_serialized_read
    async def get_all_summaries(self, session_id: str) -> list[SessionSummary]:
        return await self._select_all_summaries(self.conn, session_id)

    @_serialized_read
    async def list_degraded_summaries(
        self,
        *,
        session_key_prefix: str | None = None,
        limit: int = 50,
    ) -> list[SessionSummary]:
        clauses = ["flush_receipt_status IN ('degraded_forensic', 'failed_retryable')"]
        params: list[Any] = []
        if session_key_prefix:
            clauses.append("session_key LIKE ?")
            params.append(f"{session_key_prefix}%")
        params.append(limit)
        sql = (
            "SELECT * FROM session_summaries "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at ASC LIMIT ?"
        )
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [SessionSummary(**_deserialize_row(dict(r))) for r in rows]

    @_serialized_read
    async def get_compacted_transcript_entries(
        self,
        *,
        session_id: str,
        compaction_id: str,
    ) -> list[TranscriptEntry]:
        sql = """
            SELECT
                original_entry_id AS id,
                session_id,
                session_key,
                message_id,
                role,
                content,
                tool_calls,
                tool_call_id,
                reasoning_content,
                turn_usage,
                turn_context,
                created_at,
                token_count,
                provenance_kind,
                provenance_origin_session_id,
                provenance_source_session_key,
                provenance_source_channel,
                provenance_source_tool,
                schema_version
            FROM compacted_transcript_entries
            WHERE session_id = ? AND compaction_id = ?
            ORDER BY created_at ASC, original_entry_id ASC, id ASC
        """
        async with self.conn.execute(sql, (session_id, compaction_id)) as cur:
            rows = await cur.fetchall()
        return [TranscriptEntry(**_deserialize_row(dict(r))) for r in rows]

    async def update_summary_flush_receipt_status(
        self,
        summary_id: int,
        status: str,
    ) -> None:
        async with self._write_transaction("update_summary_flush_receipt_status") as conn:
            await conn.execute(
                "UPDATE session_summaries SET flush_receipt_status = ? WHERE id = ?",
                (status, summary_id),
            )

    async def update_summary_flush_receipt_status_by_compaction(
        self,
        *,
        session_key: str,
        compaction_id: str,
        status: str,
    ) -> int:
        async with self._write_transaction(
            "update_summary_flush_receipt_status_by_compaction"
        ) as conn:
            cur = await conn.execute(
                """
                UPDATE session_summaries
                SET flush_receipt_status = ?
                WHERE session_key = ? AND compaction_id = ?
                """,
                (status, canonicalize_session_key(session_key), compaction_id),
            )
            count = int(cur.rowcount or 0)
        return count

    # ── SessionContextState CRUD ─────────────────────────────────────────────

    async def save_context_state(
        self, state: SessionContextState
    ) -> SessionContextState:
        """Persist portable or provider-native context state for later replay."""
        state.session_key = canonicalize_session_key(state.session_key)
        data = state.model_dump(exclude={"id"})
        cols = list(data.keys())
        placeholders = ", ".join("?" for _ in cols)
        values = [_serialize(data[c]) for c in cols]
        async with self._write_transaction("save_context_state") as conn:
            async with conn.execute(
                "INSERT INTO session_context_states "
                f"({', '.join(cols)}) VALUES ({placeholders})",
                values,
            ) as cur:
                state.id = cur.lastrowid
        return state

    @_serialized_read
    async def get_context_states(
        self,
        session_key: str,
        *,
        provider: str | None = None,
        state_kind: str | None = None,
        valid_only: bool = True,
    ) -> list[SessionContextState]:
        session_key = canonicalize_session_key(session_key)
        clauses = ["session_key = ?"]
        params: list[Any] = [session_key]
        if provider is not None:
            clauses.append("provider = ?")
            params.append(provider)
        if state_kind is not None:
            clauses.append("state_kind = ?")
            params.append(state_kind)
        if valid_only:
            clauses.append("valid = 1")
        where = " AND ".join(clauses)
        async with self.conn.execute(
            "SELECT * FROM session_context_states "
            f"WHERE {where} ORDER BY created_at ASC, id ASC",
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [SessionContextState(**_deserialize_row(dict(row))) for row in rows]

    async def invalidate_context_states(
        self,
        session_key: str,
        *,
        provider: str | None = None,
        state_kind: str | None = None,
        reason: str = "invalidated",
    ) -> int:
        session_key = canonicalize_session_key(session_key)
        clauses = ["session_key = ?", "valid = 1"]
        params: list[Any] = [session_key]
        if provider is not None:
            clauses.append("provider = ?")
            params.append(provider)
        if state_kind is not None:
            clauses.append("state_kind = ?")
            params.append(state_kind)
        async with self._write_transaction("invalidate_context_states") as conn:
            async with conn.execute(
                "UPDATE session_context_states "
                "SET valid = 0, invalid_reason = ? "
                f"WHERE {' AND '.join(clauses)}",
                [reason, *params],
            ) as cur:
                changed = cur.rowcount or 0
        return int(changed)

    # ── FTS5 Search ──────────────────────────────────────────────────────

    @staticmethod
    def sanitize_fts_query(raw: str) -> str:
        """Sanitize a user query for safe FTS5 MATCH.

        Strips FTS5 operators and special chars, wraps each token in quotes.
        """
        import re as _re

        # Whitelist: only allow alphanumeric and whitespace through
        cleaned = _re.sub(r"[^a-zA-Z0-9\s]", " ", raw)
        # Collapse whitespace and split into tokens
        tokens = cleaned.split()
        if not tokens:
            return '""'
        # Wrap each token in double-quotes for literal matching
        return " ".join(f'"{t}"' for t in tokens[:20])  # cap at 20 terms

    @_serialized_read
    async def search_transcript(
        self,
        query: str,
        session_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Full-text search across transcript entries.

        Returns dicts with: id, session_key, role, snippet, created_at.
        """
        safe_q = self.sanitize_fts_query(query)
        if safe_q == '""':
            return []

        if session_id:
            sql = (
                "SELECT t.id, t.session_key, t.role, t.created_at, "
                "snippet(transcript_fts, 0, '>>>', '<<<', '...', 48) AS snippet "
                "FROM transcript_fts f "
                "JOIN transcript_entries t ON f.rowid = t.id "
                "WHERE f.content MATCH ? AND t.session_id = ? "
                "ORDER BY f.rank LIMIT ?"
            )
            params: list[Any] = [safe_q, session_id, limit]
        else:
            sql = (
                "SELECT t.id, t.session_key, t.role, t.created_at, "
                "snippet(transcript_fts, 0, '>>>', '<<<', '...', 48) AS snippet "
                "FROM transcript_fts f "
                "JOIN transcript_entries t ON f.rowid = t.id "
                "WHERE f.content MATCH ? "
                "ORDER BY f.rank LIMIT ?"
            )
            params = [safe_q, limit]

        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _like_escape(raw: str) -> str:
        """Escape LIKE wildcards so user input matches literally under ESCAPE '\\'."""
        return raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @classmethod
    def _like_tokens(cls, query: str, max_tokens: int = 10) -> list[str]:
        """Whitespace-split a query into lowercased, wildcard-escaped LIKE patterns.

        Each token becomes ``%token%`` and callers AND them, so multi-word and
        mixed ASCII+CJK queries (e.g. ``deploy 部署``) match every term
        independently instead of requiring one contiguous substring. Lowercased
        to pair with the ``py_lower`` column side for Unicode case-insensitivity.
        """
        return [f"%{cls._like_escape(tok.lower())}%" for tok in query.split()[:max_tokens] if tok]

    @staticmethod
    def _needs_unicode_fold(query: str) -> bool:
        """Whether a query needs the per-row ``py_lower`` to match case-insensitively.

        Only non-ASCII *cased* scripts (Cyrillic, Greek, accented Latin, …) need
        it. ASCII is folded by SQLite's own LIKE, and caseless scripts (CJK,
        digits, symbols) don't differ by case — both take the faster plain-LIKE
        path. So the (Chinese-dominant) common case never pays the fold cost.
        """
        return any(ord(ch) > 127 and ch.lower() != ch.upper() for ch in query)

    @staticmethod
    def _make_snippet(content: str, needle: str, window: int = 40) -> str:
        """Build a ``>>>match<<<`` snippet around the first case-insensitive hit.

        Mirrors the delimiter contract of the FTS ``snippet()`` output so the UI
        highlighter treats LIKE and FTS results identically.
        """
        idx = content.lower().find(needle.lower())
        if idx < 0:
            return content[: window * 2]
        end_match = idx + len(needle)
        start = max(0, idx - window)
        end = min(len(content), end_match + window)
        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(content) else ""
        return (
            f"{prefix}{content[start:idx]}>>>{content[idx:end_match]}<<<"
            f"{content[end_match:end]}{suffix}"
        )

    @_serialized_read
    async def search_sessions_by_title(
        self,
        query: str,
        limit: int = 20,
    ) -> list[SessionNode]:
        """Substring match over title columns across ALL sessions (not a recent
        page). Every whitespace-separated term must match in one of the title
        columns (display_name / derived_title / subject / label). Matching is
        case-insensitive: ASCII via SQLite's own LIKE, and cased non-ASCII scripts
        via ``py_lower`` (only paid when the query actually contains one)."""
        tokens = self._like_tokens(query)
        if not tokens:
            return []
        col = (lambda c: f"py_lower({c})") if self._needs_unicode_fold(query) else (lambda c: c)
        cols = ("display_name", "derived_title", "subject", "label")
        clauses: list[str] = []
        params: list[Any] = []
        for token in tokens:
            clauses.append("(" + " OR ".join(f"{col(c)} LIKE ? ESCAPE '\\'" for c in cols) + ")")
            params.extend([token] * len(cols))
        params.append(limit)
        sql = (
            f"SELECT * FROM sessions WHERE {' AND '.join(clauses)} "
            "ORDER BY updated_at DESC LIMIT ?"
        )
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [SessionNode(**_deserialize_row(dict(r))) for r in rows]

    @_serialized_read
    async def search_transcript_like(
        self,
        query: str,
        session_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Substring content search for queries the FTS tokenizer can't handle.

        SQLite's default ``unicode61`` FTS tokenizer does not segment CJK and
        other scripts, and ``sanitize_fts_query`` strips non-ASCII entirely, so
        full-text search returns nothing for e.g. Chinese. Each whitespace term
        must appear in the content, so mixed/multi-word queries match all terms;
        cased non-ASCII scripts fold via ``py_lower`` (caseless CJK skips it for
        speed). The handler only reaches this for non-ASCII queries (ASCII stays
        on the indexed FTS path). Returns the same shape as ``search_transcript``.
        """
        tokens = self._like_tokens(query)
        if not tokens:
            return []
        col = "py_lower(content)" if self._needs_unicode_fold(query) else "content"
        clauses = [f"{col} LIKE ? ESCAPE '\\'" for _ in tokens]
        params: list[Any] = list(tokens)
        where = " AND ".join(clauses)
        if session_id:
            where += " AND session_id = ?"
            params.append(session_id)
        params.append(limit)
        sql = (
            "SELECT id, session_key, role, content, created_at "
            f"FROM transcript_entries WHERE {where} "
            "ORDER BY created_at DESC LIMIT ?"
        )
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        # Snippet highlights the first term; the others are guaranteed present too.
        first_term = query.split()[0]
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            out.append(
                {
                    "id": d.get("id"),
                    "session_key": d.get("session_key"),
                    "role": d.get("role"),
                    "created_at": d.get("created_at"),
                    "snippet": self._make_snippet(str(d.get("content") or ""), first_term),
                }
            )
        return out

    async def __aenter__(self) -> SessionStorage:
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()
