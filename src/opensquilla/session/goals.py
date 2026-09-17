"""Pure domain contracts for durable session Goals.

The module intentionally has no gateway, TaskRuntime, or Plan dependencies.
Goal execution uses ordinary AgentTasks; these types only describe the
generation-fenced durable state attached to those tasks.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from opensquilla.session.models import GoalRecord

MAX_GOAL_OBJECTIVE_CHARS = 4_000
MAX_GOAL_PROGRESS_STEPS = 20
MAX_GOAL_PROGRESS_STEP_CHARS = 200
MAX_GOAL_PROGRESS_EXPLANATION_CHARS = 1_000
MAX_GOAL_PROGRESS_JSON_BYTES = 16 * 1024
MAX_GOAL_REASON_CHARS = 1_000
GOAL_CONTEXT_SCHEMA_VERSION = 1
GOAL_OBJECTIVE_UPDATE_SCHEMA_VERSION = 1
GOAL_EFFECTIVE_CONTEXT_DETAIL_KEY = "goal_effective_context"
GOAL_OBJECTIVE_UPDATE_DETAIL_KEY = "goal_objective_update"


class GoalStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    USAGE_LIMITED = "usage_limited"
    COMPLETE = "complete"


GOAL_UNFINISHED_STATUSES = frozenset(
    {
        GoalStatus.ACTIVE.value,
        GoalStatus.PAUSED.value,
        GoalStatus.BLOCKED.value,
        GoalStatus.USAGE_LIMITED.value,
    }
)
GOAL_TERMINAL_STATUSES = frozenset({GoalStatus.COMPLETE.value})
GOAL_STATUSES = GOAL_UNFINISHED_STATUSES | GOAL_TERMINAL_STATUSES

class GoalValidationError(ValueError):
    """Raised when a Goal request violates the durable public contract."""

    def __init__(self, message: str, *, code: str = "INVALID_GOAL_OBJECTIVE") -> None:
        super().__init__(message)
        self.code = code


class GoalConflictError(RuntimeError):
    """A stable Goal command/CAS conflict suitable for an RPC error response."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        current: GoalRecord | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.current = current


@dataclass(frozen=True, slots=True)
class GoalCommandRequest:
    """Idempotency identity shared by every Goal mutation."""

    source_scope: str
    request_session_key: str
    client_request_id: str
    action: str
    request_fingerprint: str

    def validate(self) -> None:
        if not isinstance(self.source_scope, str) or not self.source_scope.strip():
            raise GoalValidationError(
                "source_scope is required",
                code="INVALID_GOAL_COMMAND",
            )
        if (
            not isinstance(self.request_session_key, str)
            or not self.request_session_key.strip()
        ):
            raise GoalValidationError(
                "request_session_key is required",
                code="INVALID_GOAL_COMMAND",
            )
        normalize_client_request_id(self.client_request_id)
        if self.action not in {"set", "edit", "pause", "resume", "clear"}:
            raise GoalValidationError(
                f"unknown Goal action: {self.action}",
                code="INVALID_GOAL_COMMAND",
            )
        fingerprint = self.request_fingerprint
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or fingerprint != fingerprint.lower()
            or any(char not in "0123456789abcdef" for char in fingerprint)
        ):
            raise GoalValidationError(
                "request_fingerprint must be a lower-case SHA-256 digest",
                code="INVALID_GOAL_COMMAND",
            )


@dataclass(frozen=True, slots=True)
class ExpectedGoal:
    """User-command fence for the current Goal row."""

    session_id: str
    epoch: int
    goal_id: str
    state_revision: int


@dataclass(frozen=True, slots=True)
class GoalClaimCandidate:
    """Best-effort Goal identity carried by a queued explicit user task."""

    session_id: str
    epoch: int
    goal_id: str

    def as_task_detail(self) -> dict[str, Any]:
        return {
            "schemaVersion": GOAL_CONTEXT_SCHEMA_VERSION,
            "sessionId": self.session_id,
            "epoch": self.epoch,
            "goalId": self.goal_id,
        }

    @classmethod
    def from_task_detail(cls, value: object) -> GoalClaimCandidate | None:
        if not isinstance(value, Mapping) or value.get("schemaVersion") != 1:
            return None
        session_id = value.get("sessionId")
        epoch = value.get("epoch")
        goal_id = value.get("goalId")
        if (
            not isinstance(session_id, str)
            or not session_id
            or isinstance(epoch, bool)
            or not isinstance(epoch, int)
            or epoch < 0
            or not isinstance(goal_id, str)
            or not goal_id
        ):
            return None
        return cls(session_id=session_id, epoch=epoch, goal_id=goal_id)


@dataclass(frozen=True, slots=True)
class GoalTurnContext:
    """Immutable Goal generation captured for exactly one AgentTask."""

    session_id: str
    epoch: int
    goal_id: str
    objective_revision: int
    objective_snapshot: str
    task_id: str
    continuation_seq: int = 0
    automatic: bool = False

    def as_task_detail(self) -> dict[str, Any]:
        return {
            "schemaVersion": GOAL_CONTEXT_SCHEMA_VERSION,
            "sessionId": self.session_id,
            "epoch": self.epoch,
            "goalId": self.goal_id,
            "objectiveRevision": self.objective_revision,
            "objectiveSnapshot": self.objective_snapshot,
            "taskId": self.task_id,
            "continuationSeq": self.continuation_seq,
            "automatic": self.automatic,
        }

    @classmethod
    def from_task_detail(cls, value: object) -> GoalTurnContext | None:
        if not isinstance(value, Mapping) or value.get("schemaVersion") != 1:
            return None
        fields = {
            "session_id": value.get("sessionId"),
            "epoch": value.get("epoch"),
            "goal_id": value.get("goalId"),
            "objective_revision": value.get("objectiveRevision"),
            "objective_snapshot": value.get("objectiveSnapshot"),
            "task_id": value.get("taskId"),
            "continuation_seq": value.get("continuationSeq", 0),
            "automatic": value.get("automatic", False),
        }
        if any(
            not isinstance(fields[name], str) or not fields[name]
            for name in ("session_id", "goal_id", "objective_snapshot", "task_id")
        ):
            return None
        for name in ("epoch", "objective_revision", "continuation_seq"):
            raw = fields[name]
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                return None
        if fields["objective_revision"] < 1:
            return None
        objective_snapshot = fields["objective_snapshot"]
        assert isinstance(objective_snapshot, str)
        if (
            objective_snapshot != objective_snapshot.strip()
            or len(objective_snapshot) > MAX_GOAL_OBJECTIVE_CHARS
        ):
            return None
        if not isinstance(fields["automatic"], bool):
            return None
        return cls(**fields)


@dataclass(frozen=True, slots=True)
class GoalObjectiveUpdate:
    """Internal, non-transcript objective update for one owning AgentTask.

    The original ``goal_context`` remains the immutable acceptance receipt.
    This value is stored separately in ``AgentTask.details`` and becomes tool
    authority only after a provider request has consumed its prompt context.
    """

    context: GoalTurnContext
    state_revision: int
    accepted_at_ms: int
    status: str = "pending"

    def as_task_detail(self) -> dict[str, Any]:
        return {
            "schemaVersion": GOAL_OBJECTIVE_UPDATE_SCHEMA_VERSION,
            "status": self.status,
            "stateRevision": self.state_revision,
            "acceptedAtMs": self.accepted_at_ms,
            "context": self.context.as_task_detail(),
        }

    @classmethod
    def from_task_detail(cls, value: object) -> GoalObjectiveUpdate | None:
        if (
            not isinstance(value, Mapping)
            or value.get("schemaVersion") != GOAL_OBJECTIVE_UPDATE_SCHEMA_VERSION
        ):
            return None
        context = GoalTurnContext.from_task_detail(value.get("context"))
        state_revision = value.get("stateRevision")
        accepted_at_ms = value.get("acceptedAtMs")
        status = value.get("status")
        if (
            context is None
            or isinstance(state_revision, bool)
            or not isinstance(state_revision, int)
            or state_revision < 1
            or isinstance(accepted_at_ms, bool)
            or not isinstance(accepted_at_ms, int)
            or accepted_at_ms < 0
            or status not in {"pending", "claimed", "applied", "revoked"}
        ):
            return None
        return cls(
            context=context,
            state_revision=state_revision,
            accepted_at_ms=accepted_at_ms,
            status=status,
        )


def effective_goal_turn_context(details: object) -> GoalTurnContext | None:
    """Resolve applied Goal authority while preserving old-task compatibility."""

    if not isinstance(details, Mapping):
        return None
    effective = GoalTurnContext.from_task_detail(
        details.get(GOAL_EFFECTIVE_CONTEXT_DETAIL_KEY)
    )
    if effective is not None:
        return effective
    return GoalTurnContext.from_task_detail(details.get("goal_context"))


@dataclass(frozen=True, slots=True)
class StartGoalMutation:
    """Atomic Goal-set payload attached to ``SessionStorage.accept_turn``."""

    goal: GoalRecord
    command: GoalCommandRequest


@dataclass(frozen=True, slots=True)
class ClaimGoalMutation:
    """Optional Goal claim attached to an explicit accepted user turn."""

    candidate: GoalClaimCandidate


@dataclass(frozen=True, slots=True)
class ClaimCurrentGoalMutation:
    """Request an atomic claim of the active Goal at turn acceptance."""


@dataclass(frozen=True, slots=True)
class GoalCommandResult:
    """Durable response returned by a Goal mutation or idempotent replay."""

    response: dict[str, Any]
    goal: GoalRecord | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class GoalTaskAcceptance:
    """Result of atomically binding an ordinary AgentTask to a Goal."""

    goal: GoalRecord
    context: GoalTurnContext


@dataclass(frozen=True, slots=True)
class GoalGuardrailPause:
    """A continuation admission that paused before creating an AgentTask."""

    goal: GoalRecord
    reason: str


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def normalize_client_request_id(value: object) -> str:
    """Require the canonical lower-case text form of a UUID v4."""

    if not isinstance(value, str):
        raise GoalValidationError(
            "clientRequestId must be a UUID v4",
            code="INVALID_GOAL_COMMAND",
        )
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise GoalValidationError(
            "clientRequestId must be a UUID v4",
            code="INVALID_GOAL_COMMAND",
        ) from exc
    if parsed.version != 4 or str(parsed) != value:
        raise GoalValidationError(
            "clientRequestId must be a canonical UUID v4",
            code="INVALID_GOAL_COMMAND",
        )
    return value


def automatic_goal_task_id(
    goal_id: str,
    objective_revision: int,
    continuation_seq: int,
) -> str:
    """Return the stable task identity for one automatic continuation."""

    if not goal_id:
        raise GoalValidationError(
            "goal_id is required for automatic continuation",
            code="INVALID_GOAL_COMMAND",
        )
    for name, value in {
        "objective_revision": objective_revision,
        "continuation_seq": continuation_seq,
    }.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise GoalValidationError(
                f"{name} must be a positive integer",
                code="INVALID_GOAL_COMMAND",
            )
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{goal_id}:{objective_revision}:{continuation_seq}",
        )
    )


def normalize_goal_objective(value: object) -> str:
    """Trim and validate a user-provided Goal objective."""

    if not isinstance(value, str):
        raise GoalValidationError("Goal objective must be a string")
    normalized = value.strip()
    if not normalized:
        raise GoalValidationError("Goal objective must not be empty")
    if len(normalized) > MAX_GOAL_OBJECTIVE_CHARS:
        raise GoalValidationError(
            f"Goal objective exceeds {MAX_GOAL_OBJECTIVE_CHARS} characters"
        )
    return normalized


def normalize_goal_reason(value: object | None) -> str | None:
    """Normalize a bounded, optional reason without persisting model prose."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise GoalValidationError(
            "Goal reason must be a string",
            code="INVALID_GOAL_REASON",
        )
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > MAX_GOAL_REASON_CHARS:
        raise GoalValidationError(
            f"Goal reason exceeds {MAX_GOAL_REASON_CHARS} characters",
            code="INVALID_GOAL_REASON",
        )
    return normalized


def normalize_goal_progress(
    *,
    explanation: object | None,
    steps: object,
) -> dict[str, Any]:
    """Validate and canonicalize the full structured Goal progress value."""

    normalized_explanation: str | None
    if explanation is None:
        normalized_explanation = None
    elif not isinstance(explanation, str):
        raise GoalValidationError(
            "Goal progress explanation must be a string",
            code="INVALID_GOAL_PROGRESS",
        )
    else:
        normalized_explanation = explanation.strip() or None
        if (
            normalized_explanation is not None
            and len(normalized_explanation) > MAX_GOAL_PROGRESS_EXPLANATION_CHARS
        ):
            raise GoalValidationError(
                "Goal progress explanation exceeds "
                f"{MAX_GOAL_PROGRESS_EXPLANATION_CHARS} characters",
                code="INVALID_GOAL_PROGRESS",
            )

    if isinstance(steps, (str, bytes)) or not isinstance(steps, Sequence):
        raise GoalValidationError(
            "Goal progress steps must be an array",
            code="INVALID_GOAL_PROGRESS",
        )
    if len(steps) > MAX_GOAL_PROGRESS_STEPS:
        raise GoalValidationError(
            f"Goal progress supports at most {MAX_GOAL_PROGRESS_STEPS} steps",
            code="INVALID_GOAL_PROGRESS",
        )

    normalized_steps: list[dict[str, str]] = []
    in_progress_count = 0
    for index, raw_step in enumerate(steps):
        if not isinstance(raw_step, Mapping):
            raise GoalValidationError(
                f"Goal progress step {index} must be an object",
                code="INVALID_GOAL_PROGRESS",
            )
        if set(raw_step) != {"step", "status"}:
            raise GoalValidationError(
                f"Goal progress step {index} must contain only step and status",
                code="INVALID_GOAL_PROGRESS",
            )
        text = raw_step.get("step")
        status = raw_step.get("status")
        if not isinstance(text, str) or not text.strip():
            raise GoalValidationError(
                f"Goal progress step {index} step must not be empty",
                code="INVALID_GOAL_PROGRESS",
            )
        text = text.strip()
        if len(text) > MAX_GOAL_PROGRESS_STEP_CHARS:
            raise GoalValidationError(
                f"Goal progress step {index} exceeds "
                f"{MAX_GOAL_PROGRESS_STEP_CHARS} characters",
                code="INVALID_GOAL_PROGRESS",
            )
        if status not in {"pending", "in_progress", "completed"}:
            raise GoalValidationError(
                f"Goal progress step {index} has an invalid status",
                code="INVALID_GOAL_PROGRESS",
            )
        if status == "in_progress":
            in_progress_count += 1
        normalized_steps.append({"step": text, "status": str(status)})

    if in_progress_count > 1:
        raise GoalValidationError(
            "Goal progress may contain at most one in_progress step",
            code="INVALID_GOAL_PROGRESS",
        )

    progress = {
        "explanation": normalized_explanation,
        "steps": normalized_steps,
    }
    encoded = json.dumps(
        progress,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_GOAL_PROGRESS_JSON_BYTES:
        raise GoalValidationError(
            f"Goal progress exceeds {MAX_GOAL_PROGRESS_JSON_BYTES} bytes",
            code="INVALID_GOAL_PROGRESS",
        )
    return progress


def new_goal(
    *,
    goal_id: str,
    session_key: str,
    session_id: str,
    session_epoch: int,
    objective: str,
    task_id: str | None = None,
    source_user_message_id: str | None = None,
    created_at_ms: int | None = None,
) -> GoalRecord:
    """Build a fresh current Goal with monotonic revisions initialized."""

    normalized = normalize_goal_objective(objective)
    if not goal_id or not session_key or not session_id:
        raise GoalValidationError(
            "goal_id, session_key and session_id are required",
            code="INVALID_GOAL_COMMAND",
        )
    if (
        isinstance(session_epoch, bool)
        or not isinstance(session_epoch, int)
        or session_epoch < 0
    ):
        raise GoalValidationError(
            "session_epoch must be a non-negative integer",
            code="INVALID_GOAL_COMMAND",
        )
    timestamp = _now_ms() if created_at_ms is None else created_at_ms
    if source_user_message_id is not None:
        source_user_message_id = source_user_message_id.strip()
        if not source_user_message_id:
            raise GoalValidationError(
                "source_user_message_id must not be blank",
                code="INVALID_GOAL_COMMAND",
            )
    started = int(task_id is not None)
    return GoalRecord(
        session_key=session_key,
        session_id=session_id,
        session_epoch=session_epoch,
        goal_id=goal_id,
        objective=normalized,
        status=GoalStatus.ACTIVE.value,
        state_revision=1,
        objective_revision=1,
        progress_revision=0,
        continuation_seq=0,
        active_task_id=task_id,
        source_user_message_id=source_user_message_id,
        turns_started=started,
        turns_settled=0,
        window_turns_started=started,
        created_at_ms=timestamp,
        updated_at_ms=timestamp,
    )


def goal_turn_context(
    goal: GoalRecord,
    *,
    task_id: str,
    automatic: bool,
) -> GoalTurnContext:
    """Freeze the exact Goal objective owned by ``task_id``."""

    return GoalTurnContext(
        session_id=goal.session_id,
        epoch=goal.session_epoch,
        goal_id=goal.goal_id,
        objective_revision=goal.objective_revision,
        objective_snapshot=goal.objective,
        task_id=task_id,
        continuation_seq=goal.continuation_seq,
        automatic=automatic,
    )


def goal_snapshot(
    goal: GoalRecord,
    *,
    execution_state: str | None = None,
    continuation_deferred_reason: str | None = None,
) -> dict[str, Any]:
    """Return the stable camelCase Goal payload used by RPC and events."""

    if execution_state is None:
        execution_state = "working" if goal.active_task_id is not None else "idle"
    return {
        "goalId": goal.goal_id,
        "sessionKey": goal.session_key,
        "sessionId": goal.session_id,
        "epoch": goal.session_epoch,
        "objective": goal.objective,
        "status": goal.status,
        "stateRevision": goal.state_revision,
        "objectiveRevision": goal.objective_revision,
        "progressRevision": goal.progress_revision,
        "progress": goal.progress_json,
        "continuationSeq": goal.continuation_seq,
        "activeTaskId": goal.active_task_id,
        "sourceMessageId": goal.source_user_message_id,
        "terminalTurnId": goal.terminal_task_id,
        "executionState": execution_state,
        "continuationDeferredReason": continuation_deferred_reason,
        "turnsStarted": goal.turns_started,
        "turnsSettled": goal.turns_settled,
        "windowTurnsStarted": goal.window_turns_started,
        "activeTimeMs": goal.active_time_ms,
        "windowActiveTimeMs": goal.window_active_time_ms,
        "usage": {
            "inputTokens": goal.input_tokens,
            "outputTokens": goal.output_tokens,
            "reasoningTokens": goal.reasoning_tokens,
            "cacheReadTokens": goal.cache_read_tokens,
            "cacheWriteTokens": goal.cache_write_tokens,
            "totalTokens": goal.total_tokens,
        },
        "pauseReason": goal.pause_reason,
        # ``blocked_reason`` is also the bounded, internal hand-off slot for
        # the blocker that preceded a Resume.  It is current public state only
        # while the Goal itself is blocked; active/paused snapshots must not
        # present that historical context as a live blocker.
        "blockedReason": (
            goal.blocked_reason
            if goal.status == GoalStatus.BLOCKED.value
            else None
        ),
        "terminalReason": goal.terminal_reason,
        "createdAt": goal.created_at_ms,
        "updatedAt": goal.updated_at_ms,
        "finishedAt": goal.finished_at_ms,
    }
