"""Session data models using SQLModel."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


class SessionStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    KILLED = "killed"
    TIMEOUT = "timeout"


class ChatType(StrEnum):
    DIRECT = "direct"
    GROUP = "group"
    CHANNEL = "channel"
    UNKNOWN = "unknown"


class QueueMode(StrEnum):
    STEER = "steer"
    FOLLOWUP = "followup"
    COLLECT = "collect"
    STEER_BACKLOG = "steer-backlog"
    STEER_PLUS_BACKLOG = "steer+backlog"
    QUEUE = "queue"
    INTERRUPT = "interrupt"


class SendPolicy(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class SessionIntent(StrEnum):
    CONTINUE = "continue"
    NEW_CHAT = "new_chat"
    RESET_SAME_KEY = "reset_same_key"


class CollaborationMode(StrEnum):
    DEFAULT = "default"
    PLAN = "plan"


class PlanRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class AgentTaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    ABANDONED = "abandoned"


class InputProvenanceKind(StrEnum):
    EXTERNAL_USER = "external_user"
    INTER_SESSION = "inter_session"
    INTERNAL_SYSTEM = "internal_system"


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _new_uuid() -> str:
    return str(uuid.uuid4())


class ProjectWorkspace(SQLModel, table=True):
    """A user-selected project directory that exists independently of sessions."""

    __tablename__ = "project_workspaces"

    workspace_id: str = Field(default_factory=_new_uuid, primary_key=True)
    path: str
    path_key: str = Field(unique=True)
    display_name: str
    created_at: int = Field(default_factory=_now_ms)
    updated_at: int = Field(default_factory=_now_ms)
    position_at: int = Field(default_factory=_now_ms)
    pinned_at: int | None = None
    removed_at: int | None = None
    trusted_at: int | None = None


class SessionNode(SQLModel, table=True):
    """Persisted session entry keyed by session_key."""

    __tablename__ = "sessions"

    # Primary identity
    session_key: str = Field(primary_key=True, max_length=512)
    session_id: str = Field(default_factory=_new_uuid)

    # Timestamps (epoch ms)
    created_at: int = Field(default_factory=_now_ms)
    updated_at: int = Field(default_factory=_now_ms)
    started_at: int | None = None
    ended_at: int | None = None
    runtime_ms: int | None = None

    # Routing fields
    last_channel: str | None = None
    last_to: str | None = None
    last_account_id: str | None = None
    last_thread_id: str | None = None
    delivery_context: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))

    # Model selection
    model: str | None = None
    model_provider: str | None = None
    provider_override: str | None = None
    model_override: str | None = None
    auth_profile_override: str | None = None
    auth_profile_override_source: str | None = None
    context_tokens: int | None = None

    # Token tracking
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    total_tokens_fresh: bool = False
    # Legacy display total. During the cost-source rollout this remains the
    # value older callers read, even when it contains provider-billed cost.
    estimated_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    billed_cost_usd: float = 0.0
    estimated_cost_component_usd: float = 0.0
    cost_source: str = "none"
    missing_cost_entries: int = 0
    cache_read: int = 0
    cache_write: int = 0

    # Compaction
    compaction_count: int = 0

    # Lifecycle
    session_file: str | None = None
    spawned_by: str | None = None
    parent_session_key: str | None = None
    forked_from_parent: bool = False
    spawn_depth: int = 0
    status: str = Field(default=SessionStatus.RUNNING)

    # Chat settings
    chat_type: str = Field(default=ChatType.UNKNOWN)
    thinking_level: str | None = None
    fast_mode: bool = False
    verbose_level: str | None = None
    reasoning_level: str | None = None
    send_policy: str = Field(default=SendPolicy.ALLOW)
    queue_mode: str = Field(default=QueueMode.STEER)
    collaboration_mode: str = Field(default=CollaborationMode.DEFAULT)
    collaboration_revision: int = 0
    active_plan_revision_id: str | None = None

    # Labels
    label: str | None = Field(default=None, max_length=512)
    display_name: str | None = None
    # LLM-generated session title. Sits below display_name (manual rename) and
    # above subject in the title precedence (see session_view._title), so an
    # auto-generated title never overrides a name the user set by hand.
    derived_title: str | None = None
    channel: str | None = None
    group_id: str | None = None
    subject: str | None = None

    # Origin metadata (JSON blob)
    origin: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))

    # Optional user-selected project workspace. Ordinary tasks keep this NULL
    # and continue to resolve the Agent/default OpenSquilla workspace.
    workspace_id: str | None = Field(default=None, index=True)

    # Agent id for multi-agent support
    agent_id: str = "main"

    # Schema generation (S-MIGRATE). Bumped by each yoyo migration that
    # widens or narrows this table so readers can reason about row shape.
    schema_version: int = 1

    # Session epoch counter — incremented on every reset so stale writes
    # from prior turns can be detected and rejected.
    epoch: int = 0


class TranscriptEntry(SQLModel, table=True):
    """Individual message stored in the transcript."""

    __tablename__ = "transcript_entries"

    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(index=True)
    session_key: str = Field(index=True)
    # Callers may supply a stable identity for streamed assistant publication;
    # storage treats that identity as an upsert key without requiring a schema
    # or migration change.
    message_id: str = Field(default_factory=_new_uuid)
    role: str  # user | assistant | system | tool
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))
    tool_call_id: str | None = None
    reasoning_content: str | None = None
    turn_usage: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    # Gateway-owned causal identity shared by every durable row in one turn.
    # Additive JSON keeps older readers and pre-identity transcript rows valid.
    turn_context: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    created_at: int = Field(default_factory=_now_ms)
    token_count: int | None = None

    # Input provenance (never overwritten once set)
    provenance_kind: str | None = None
    provenance_origin_session_id: str | None = None
    provenance_source_session_key: str | None = None
    provenance_source_channel: str | None = None
    provenance_source_tool: str | None = None

    # Schema generation (S-MIGRATE).
    schema_version: int = 1


class PlanRevisionRecord(SQLModel, table=True):
    """Immutable, structured plan proposed for a session."""

    __tablename__ = "plan_revisions"

    revision_id: str = Field(default_factory=_new_uuid, primary_key=True)
    plan_id: str = Field(default_factory=_new_uuid, index=True)
    parent_revision_id: str | None = Field(default=None, index=True)
    generation: int = 1
    source_session_key: str = Field(index=True, max_length=512)
    source_session_id: str = Field(index=True)
    source_epoch: int = 0
    source_turn_id: str | None = Field(default=None, index=True)
    source_message_id: str | None = Field(default=None, index=True)
    title: str
    markdown: str
    steps: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    content_hash: str = Field(index=True, max_length=128)
    created_at: int = Field(default_factory=_now_ms)
    schema_version: int = 1


class PlanRunRecord(SQLModel, table=True):
    """Server-authoritative execution overlay for one immutable plan revision."""

    __tablename__ = "plan_runs"

    run_id: str = Field(default_factory=_new_uuid, primary_key=True)
    session_key: str = Field(index=True, max_length=512)
    session_id: str = Field(index=True)
    session_epoch: int = 0
    plan_revision_id: str = Field(index=True)
    supersedes_run_id: str | None = Field(default=None, index=True)
    driver_kind: str = "manual"
    driver_id: str | None = Field(default=None, index=True)
    status: str = Field(default=PlanRunStatus.QUEUED, index=True)
    step_states: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    current_step_id: str | None = None
    state_revision: int = 0
    active_task_id: str | None = Field(default=None, index=True)
    pause_reason: str | None = None
    terminal_reason: str | None = None
    created_at: int = Field(default_factory=_now_ms)
    updated_at: int = Field(default_factory=_now_ms)
    started_at: int | None = None
    finished_at: int | None = None
    schema_version: int = 1


class GoalRecord(SQLModel, table=True):
    """The single server-authoritative Goal for one session generation.

    Goal execution is deliberately not a PlanRun overlay.  ``session_key`` is
    the current-row identity, while ``session_id`` plus ``session_epoch`` fence
    every task/tool write against reset and replacement races.
    """

    __tablename__ = "session_goals"

    session_key: str = Field(
        primary_key=True,
        max_length=512,
        foreign_key="sessions.session_key",
    )
    session_id: str = Field(index=True)
    session_epoch: int = 0
    goal_id: str = Field(default_factory=_new_uuid, unique=True, index=True)
    objective: str
    status: str = Field(default="active", index=True)

    # Monotonic compare-and-set fences.  Timestamps are presentation fields and
    # must never be used as optimistic-concurrency tokens.
    state_revision: int = 1
    objective_revision: int = 1
    progress_revision: int = 0
    progress_json: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSON),
    )

    continuation_seq: int = 0
    active_task_id: str | None = Field(default=None, index=True)
    # Durable product anchor for the user transcript row that created this
    # Goal.  Unlike objective text, this identity remains unambiguous after an
    # edit and lets reconnecting clients restore the lightweight origin label.
    source_user_message_id: str | None = None
    # Internal replay fence for the task that durably committed the current
    # structured complete/blocked result.  This is intentionally not exposed
    # under its storage name: snapshots only project its semantic turn
    # identity as ``terminalTurnId``.
    terminal_task_id: str | None = None
    turns_started: int = 0
    turns_settled: int = 0
    window_turns_started: int = 0

    active_time_ms: int = 0
    window_active_time_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0

    pause_reason: str | None = None
    blocked_reason: str | None = None
    terminal_reason: str | None = None
    created_at_ms: int = Field(default_factory=_now_ms)
    updated_at_ms: int = Field(default_factory=_now_ms)
    finished_at_ms: int | None = None
    schema_version: int = 1

class GoalCommandReceiptRecord(SQLModel, table=True):
    """Durable idempotency result for one accepted Goal mutation."""

    __tablename__ = "goal_command_receipts"

    receipt_id: str = Field(default_factory=_new_uuid, primary_key=True)
    source_scope: str = Field(index=True, max_length=256)
    request_session_key: str = Field(
        index=True,
        max_length=512,
        foreign_key="sessions.session_key",
    )
    client_request_id: str = Field(max_length=64)
    action: str = Field(max_length=32)
    request_fingerprint: str = Field(max_length=128)
    accepted_session_id: str = Field(index=True)
    accepted_session_epoch: int = 0
    response_json: dict[str, Any] = Field(sa_column=Column(JSON))
    created_at_ms: int = Field(default_factory=_now_ms)


class SessionSummary(SQLModel, table=True):
    """Compaction summary record — stores merged summaries of older transcript segments."""

    __tablename__ = "session_summaries"

    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(index=True)
    session_key: str = Field(index=True)
    compaction_index: int = 0  # monotonically increasing per session
    compaction_id: str | None = None
    trigger_reason: str | None = None
    summary_text: str
    summary_payload: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    summary_format: str = "text"
    summary_source: str = "unknown"
    coverage_status: str = "unknown"
    missing_obligations: list[str] | None = Field(default=None, sa_column=Column(JSON))
    critical_carry_forward: list[str] | None = Field(default=None, sa_column=Column(JSON))
    tokens_before: int | None = None
    tokens_after: int | None = None
    removed_count: int = 0
    kept_count: int = 0
    chunk_count: int = 0
    flush_receipt_status: str = "unknown"
    # The transcript entry id up to which this summary covers (inclusive)
    covered_through_id: int = 0
    created_at: int = Field(default_factory=_now_ms)

    # Schema generation (S-MIGRATE).
    schema_version: int = 1


class SessionContextState(SQLModel, table=True):
    """Portable or provider-specific context state derived from session history."""

    __tablename__ = "session_context_states"

    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(index=True)
    session_key: str = Field(index=True)
    provider: str = "portable"
    model: str | None = None
    state_kind: str
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    covered_through_id: int = 0
    created_at: int = Field(default_factory=_now_ms)
    expires_at: int | None = None
    portable: bool = False
    cacheable: bool = False
    valid: bool = True
    invalid_reason: str | None = None

    # Schema generation (S-MIGRATE).
    schema_version: int = 1


class MemoryDurableReceipt(SQLModel, table=True):
    """Durable ledger row for memory checkpoint and flush outcomes."""

    __tablename__ = "memory_durable_receipts"

    receipt_id: str = Field(default_factory=_new_uuid, primary_key=True)
    session_key: str = Field(index=True, max_length=512)
    session_id: str = Field(index=True)
    turn_id: str | None = Field(default=None, index=True)
    scope: str = Field(index=True)
    source_path: str | None = None
    target_path: str | None = None
    content_hash: str | None = None
    coverage_turn_id: str | None = Field(default=None, index=True)
    coverage_hash: str | None = Field(default=None, index=True)
    coverage_entry_count: int | None = None
    idempotency_key: str = Field(index=True, unique=True)
    status: str = Field(index=True)
    reason: str | None = None
    attempt_count: int = 0
    next_retry_at_ms: int | None = None
    created_at: int = Field(default_factory=_now_ms)
    updated_at: int = Field(default_factory=_now_ms)
    schema_version: int = 1


class AgentTaskRecord(SQLModel, table=True):
    """Persisted task-runtime ledger row."""

    __tablename__ = "agent_tasks"

    task_id: str = Field(default_factory=_new_uuid, primary_key=True)
    session_key: str = Field(index=True, max_length=512)
    agent_id: str = "main"
    source_kind: str = "system"
    queue_mode: str = Field(default=QueueMode.STEER)
    run_kind: str = "default"
    status: AgentTaskStatus = Field(default=AgentTaskStatus.QUEUED)

    created_at: int = Field(default_factory=_now_ms)
    updated_at: int = Field(default_factory=_now_ms)
    started_at: int | None = None
    finished_at: int | None = None

    terminal_reason: str | None = None
    error_class: str | None = None
    error_message: str | None = None
    details: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))

    schema_version: int = 1


class TurnIngressReceipt(SQLModel, table=True):
    """Durable idempotency receipt for one accepted inbound turn."""

    __tablename__ = "turn_ingress_receipts"

    receipt_id: str = Field(default_factory=_new_uuid, primary_key=True)
    source_scope: str = Field(index=True, max_length=256)
    request_session_key: str = Field(index=True, max_length=512)
    client_request_id: str = Field(max_length=256)
    request_fingerprint: str = Field(max_length=128)
    accepted_session_key: str = Field(index=True, max_length=512)
    session_id: str = Field(index=True)
    message_id: str
    task_id: str | None = Field(default=None, index=True)
    accepted_at: int = Field(default_factory=_now_ms)
    schema_version: int = 1


class MetaControlIntent(SQLModel, table=True):
    """Durable authorization for one hidden MetaSkill control turn.

    ``correlation_id`` is namespaced (``request:<client id>`` for a manual
    launch and ``nonce:<nonce>`` for a failed-step replay), so it contains no
    prompt or provider credential material.  Acceptance coordinates are bound
    in the same transaction as the turn-ingress receipt.
    """

    __tablename__ = "meta_control_intents"

    intent_id: str = Field(default_factory=_new_uuid, primary_key=True)
    session_key: str = Field(index=True, max_length=512)
    control_kind: str = Field(max_length=16)
    correlation_id: str = Field(max_length=272)
    meta_skill_name: str = Field(max_length=256)
    replay_run_id: str | None = None
    replay_mode: str | None = Field(default=None, max_length=32)
    status: str = Field(default="staged", max_length=16)
    accepted_source_scope: str | None = Field(default=None, max_length=256)
    accepted_request_session_key: str | None = Field(default=None, max_length=512)
    accepted_client_request_id: str | None = Field(default=None, max_length=256)
    accepted_request_fingerprint: str | None = Field(default=None, max_length=128)
    accepted_message_id: str | None = None
    accepted_task_id: str | None = Field(default=None, index=True)
    created_at: int = Field(default_factory=_now_ms)
    updated_at: int = Field(default_factory=_now_ms)
    schema_version: int = 1


class MetaLaunchDraft(SQLModel, table=True):
    """Unaccepted manual MetaSkill request retained for crash recovery.

    The launch text is user-authored content and receives the same local
    session-database treatment as a transcript message.  It is never copied to
    logs or provider metadata.  ``client_request_id`` is the stable identity
    used by ``meta.run`` and the eventual hidden ``chat.send``.
    """

    __tablename__ = "meta_launch_drafts"

    draft_id: str = Field(default_factory=_new_uuid, primary_key=True)
    session_key: str = Field(index=True, max_length=512)
    client_request_id: str = Field(max_length=256)
    meta_skill_name: str = Field(max_length=256)
    launch_text: str = Field(max_length=128_000)
    created_at: int = Field(default_factory=_now_ms)
    updated_at: int = Field(default_factory=_now_ms)
    expires_at: int
    schema_version: int = 1
