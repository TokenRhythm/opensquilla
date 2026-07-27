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
