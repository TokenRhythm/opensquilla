"""Data records owned by the orchestration domain.

The records deliberately separate durable task/session identity from a single
resource-owning activation attempt.  Runtime-only handles never belong here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class OrchestrationMode(StrEnum):
    ORDINARY = "ordinary"
    COMPLEX = "complex"


class RunLifecycle(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class TaskOutcome(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    TIMED_OUT = "timed_out"


class TaskBoardStatus(StrEnum):
    PLANNED = "planned"
    WORKING = "working"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class SessionLifecycle(StrEnum):
    ACTIVE = "active"
    IDLE = "idle"
    ARCHIVED = "archived"


class ActivationPhase(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    RELEASED = "released"


class AgentState(StrEnum):
    STOPPING = "stopping"
    WORKING = "working"
    WAITING_CHILDREN = "waiting_children"
    WAITING_EXTERNAL = "waiting_external"
    QUEUED = "queued"
    IDLE = "idle"
    INVALID_STATE = "invalid_state"


@dataclass(slots=True, kw_only=True)
class OrchestrationRunRecord:
    run_id: str
    root_session_id: str
    root_task_id: str
    mode: OrchestrationMode
    worker_template_tools: frozenset[str]
    lifecycle: RunLifecycle = RunLifecycle.ACTIVE
    created_at: float | None = None
    completed_at: float | None = None
    archived_at: float | None = None
    final_synthesis_completed: bool = False


@dataclass(slots=True, kw_only=True)
class DelegatedTaskRecord:
    task_id: str
    run_id: str
    task_key: str
    owner_session_id: str
    description: str
    background: bool = False
    parent_task_id: str | None = None
    board_status: TaskBoardStatus = TaskBoardStatus.PLANNED
    acceptance_criteria: str | None = None
    evidence: tuple[str, ...] = ()
    board_only: bool = False
    outcome: TaskOutcome = TaskOutcome.PENDING
    result: dict[str, Any] | None = None
    retry_of_activation_id: str | None = None
    replaces_session_id: str | None = None
    effective_tools: frozenset[str] = field(default_factory=frozenset)
    runtime_context: dict[str, Any] = field(default_factory=dict)
    created_at: float | None = None
    finished_at: float | None = None


@dataclass(slots=True, kw_only=True)
class AgentSessionRecord:
    session_id: str
    run_id: str
    profile: str
    runtime_session_key: str | None = None
    lifecycle: SessionLifecycle = SessionLifecycle.ACTIVE
    parent_session_id: str | None = None
    depth: int = 0
    effective_tools: frozenset[str] = field(default_factory=frozenset)
    runtime_context: dict[str, Any] = field(default_factory=dict)
    checkpoint: dict[str, Any] | None = None
    created_at: float | None = None
    updated_at: float | None = None
    archived_at: float | None = None


@dataclass(slots=True, kw_only=True)
class AgentActivationRecord:
    activation_id: str
    session_id: str
    task_id: str
    phase: ActivationPhase
    route_required: bool = False
    live_model_call: bool = False
    live_tool_call: bool = False
    external_wait_id: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    terminal_reason: str | None = None


@dataclass(slots=True, kw_only=True)
class InboxMessageRecord:
    message_id: str
    session_id: str
    sequence: int
    kind: str
    payload: dict[str, Any]
    created_at: float
    acknowledged_at: float | None = None
    processed_at: float | None = None
    delivery_owner: str | None = None
    delivery_lease_expires_at: float | None = None
    delivery_attempts: int = 0
    idempotency_key: str | None = None


@dataclass(slots=True, kw_only=True)
class AgentSessionAttachmentRecord:
    session_id: str
    run_id: str
    parent_session_id: str | None
    depth: int
    attached_at: float | None = None
