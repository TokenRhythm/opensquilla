"""Domain validation and state transitions for durable collaboration plans."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from opensquilla.session.models import PlanRevisionRecord, PlanRunRecord, PlanRunStatus

MAX_PLAN_STEPS = 64
MAX_PLAN_TITLE_CHARS = 512
MAX_PLAN_MARKDOWN_CHARS = 100_000
MAX_PLAN_STEP_TITLE_CHARS = 240
MAX_PLAN_STEP_DETAILS_CHARS = 4_000
MAX_PLAN_STEP_REASON_CHARS = 2_000

PLAN_RUN_ACTIVE_STATUSES = frozenset(
    {
        PlanRunStatus.QUEUED.value,
        PlanRunStatus.RUNNING.value,
        PlanRunStatus.PAUSED.value,
        PlanRunStatus.BLOCKED.value,
    }
)
PLAN_RUN_TERMINAL_STATUSES = frozenset(
    {
        PlanRunStatus.COMPLETED.value,
        PlanRunStatus.CANCELLED.value,
        PlanRunStatus.SUPERSEDED.value,
    }
)
PLAN_STEP_STATUSES = frozenset(
    {"pending", "in_progress", "completed", "blocked", "skipped"}
)
PLAN_STEP_TERMINAL_STATUSES = frozenset({"completed", "skipped"})

_STEP_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class PlanValidationError(ValueError):
    """Raised when a plan or run violates its durable wire contract."""


class PlanConflictError(RuntimeError):
    """Raised when immutable plan state changed before a compare-and-set write."""


class PlanRunConflictError(RuntimeError):
    """Raised when a mutable run changed before a compare-and-set write."""


def new_plan_revision(
    *,
    source_session_key: str,
    source_session_id: str,
    source_epoch: int,
    title: str,
    markdown: str,
    steps: Sequence[Mapping[str, Any]],
    parent: PlanRevisionRecord | None = None,
    source_turn_id: str | None = None,
    source_message_id: str | None = None,
    created_at: int | None = None,
) -> PlanRevisionRecord:
    """Build a normalized initial revision or replan with stable lineage."""

    values: dict[str, Any] = {
        "plan_id": parent.plan_id if parent is not None else None,
        "parent_revision_id": parent.revision_id if parent is not None else None,
        "generation": parent.generation + 1 if parent is not None else 1,
        "source_session_key": source_session_key,
        "source_session_id": source_session_id,
        "source_epoch": source_epoch,
        "source_turn_id": source_turn_id,
        "source_message_id": source_message_id,
        "title": title,
        "markdown": markdown,
        "steps": list(steps),
        "content_hash": "",
    }
    if values["plan_id"] is None:
        values.pop("plan_id")
    if created_at is not None:
        values["created_at"] = created_at
    return prepare_plan_revision(PlanRevisionRecord(**values))


def _bounded_text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise PlanValidationError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise PlanValidationError(f"{field} must not be empty")
    if len(normalized) > maximum:
        raise PlanValidationError(f"{field} exceeds {maximum} characters")
    return normalized


def normalize_plan_steps(steps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return the canonical, immutable structured step representation."""

    if isinstance(steps, str | bytes) or not isinstance(steps, Sequence):
        raise PlanValidationError("steps must be a sequence")
    if not steps:
        raise PlanValidationError("a plan must contain at least one step")
    if len(steps) > MAX_PLAN_STEPS:
        raise PlanValidationError(f"a plan may contain at most {MAX_PLAN_STEPS} steps")

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(steps):
        if not isinstance(raw, Mapping):
            raise PlanValidationError(f"steps[{index}] must be an object")
        raw_step_id = raw.get("step_id", raw.get("stepId", f"step-{index + 1}"))
        if not isinstance(raw_step_id, str):
            raise PlanValidationError(f"steps[{index}].step_id must be a string")
        step_id = raw_step_id.strip()
        if not _STEP_ID_PATTERN.fullmatch(step_id):
            raise PlanValidationError(
                f"steps[{index}].step_id must be 1-128 portable identifier characters"
            )
        if step_id in seen_ids:
            raise PlanValidationError(f"duplicate plan step id: {step_id}")
        seen_ids.add(step_id)

        title = _bounded_text(
            raw.get("title"),
            field=f"steps[{index}].title",
            maximum=MAX_PLAN_STEP_TITLE_CHARS,
        )
        step: dict[str, Any] = {"step_id": step_id, "title": title}
        details = raw.get("details")
        if details is not None:
            step["details"] = _bounded_text(
                details,
                field=f"steps[{index}].details",
                maximum=MAX_PLAN_STEP_DETAILS_CHARS,
            )
        normalized.append(step)
    return normalized


def plan_content_hash(*, title: str, markdown: str, steps: Sequence[Mapping[str, Any]]) -> str:
    """Hash the canonical user-visible plan body for integrity and deduplication."""

    payload = json.dumps(
        {"title": title, "markdown": markdown, "steps": list(steps)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def prepare_plan_revision(revision: PlanRevisionRecord) -> PlanRevisionRecord:
    """Normalize a revision and verify any caller-supplied content hash."""

    title = _bounded_text(
        revision.title,
        field="title",
        maximum=MAX_PLAN_TITLE_CHARS,
    )
    markdown = _bounded_text(
        revision.markdown,
        field="markdown",
        maximum=MAX_PLAN_MARKDOWN_CHARS,
    )
    steps = normalize_plan_steps(revision.steps)
    expected_hash = plan_content_hash(title=title, markdown=markdown, steps=steps)
    if revision.content_hash and revision.content_hash != expected_hash:
        raise PlanValidationError("content_hash does not match the canonical plan body")
    if revision.generation < 1:
        raise PlanValidationError("generation must be positive")
    if not revision.revision_id or not revision.plan_id:
        raise PlanValidationError("revision_id and plan_id are required")
    if not revision.source_session_id:
        raise PlanValidationError("source_session_id is required")
    if revision.source_epoch < 0:
        raise PlanValidationError("source_epoch must be non-negative")
    return revision.model_copy(
        update={
            "title": title,
            "markdown": markdown,
            "steps": steps,
            "content_hash": expected_hash,
        }
    )


def initial_plan_step_states(
    steps: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Create the mutable execution overlay without mutating revision steps."""

    return [
        {
            "step_id": str(step["step_id"]),
            "title": str(step["title"]),
            "status": "pending",
        }
        for step in steps
    ]


def normalize_plan_step_states(
    states: Sequence[Mapping[str, Any]],
    *,
    revision_steps: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Validate a run overlay against the immutable revision step ordering."""

    if isinstance(states, str | bytes) or not isinstance(states, Sequence):
        raise PlanValidationError("step_states must be a sequence")
    if len(states) != len(revision_steps):
        raise PlanValidationError("step_states must cover every revision step exactly once")

    normalized: list[dict[str, Any]] = []
    for index, (raw, revision_step) in enumerate(zip(states, revision_steps, strict=True)):
        if not isinstance(raw, Mapping):
            raise PlanValidationError(f"step_states[{index}] must be an object")
        step_id = raw.get("step_id", raw.get("stepId"))
        if step_id != revision_step["step_id"]:
            raise PlanValidationError("step_states must preserve revision step order and ids")
        status = raw.get("status")
        if status not in PLAN_STEP_STATUSES:
            raise PlanValidationError(f"invalid plan step status: {status}")
        state = {
            "step_id": step_id,
            "title": str(revision_step["title"]),
            "status": status,
        }
        raw_reason = raw.get("reason")
        if raw_reason is not None:
            if status not in {"blocked", "skipped"}:
                raise PlanValidationError(
                    "a step reason is valid only for blocked or skipped state"
                )
            state["reason"] = _bounded_text(
                raw_reason,
                field=f"step_states[{index}].reason",
                maximum=MAX_PLAN_STEP_REASON_CHARS,
            )
        normalized.append(state)
    return normalized


def prepare_plan_run(
    run: PlanRunRecord,
    *,
    revision: PlanRevisionRecord,
) -> PlanRunRecord:
    """Normalize a newly queued run against its immutable plan revision."""

    if run.status != PlanRunStatus.QUEUED.value:
        raise PlanValidationError("a new plan run must start in queued status")
    if run.plan_revision_id != revision.revision_id:
        raise PlanValidationError("plan run revision does not match the selected revision")
    if not run.run_id or not run.session_id:
        raise PlanValidationError("run_id and session_id are required")
    if run.session_epoch < 0:
        raise PlanValidationError("session_epoch must be non-negative")
    if run.driver_kind not in {"manual", "goal"}:
        raise PlanValidationError("driver_kind must be manual or goal")
    if run.driver_kind == "goal" and not run.driver_id:
        raise PlanValidationError("goal-driven runs require driver_id")
    if run.state_revision != 0:
        raise PlanValidationError("a new plan run must start at state_revision 0")
    active_task_id = run.active_task_id
    if active_task_id is not None:
        active_task_id = _bounded_text(
            active_task_id,
            field="active_task_id",
            maximum=256,
        )

    states = (
        normalize_plan_step_states(run.step_states, revision_steps=revision.steps)
        if run.step_states
        else initial_plan_step_states(revision.steps)
    )
    if any(state["status"] != "pending" for state in states):
        raise PlanValidationError("a new plan run must start with pending steps")
    return run.model_copy(
        update={
            "step_states": states,
            "current_step_id": None,
            # Atomic turn admission may bind a queued run to its runtime task
            # before the task starts. Preserve that ownership so a queued
            # implementation can be cancelled safely.
            "active_task_id": active_task_id,
            "started_at": None,
            "finished_at": None,
            "pause_reason": None,
            "terminal_reason": None,
        }
    )


def checkpoint_plan_step_states(
    states: Sequence[Mapping[str, Any]],
    *,
    step_id: str,
    step_status: str,
    next_step_id: str | None = None,
    reason: str | None = None,
) -> tuple[list[dict[str, Any]], str | None, str]:
    """Apply one monotonic checkpoint and derive current step/run status."""

    if step_status not in {"in_progress", "completed", "blocked", "skipped"}:
        raise PlanValidationError(f"invalid checkpoint step status: {step_status}")
    updated = [dict(state) for state in states]
    by_id = {str(state.get("step_id")): index for index, state in enumerate(updated)}
    if step_id not in by_id:
        raise PlanValidationError(f"unknown plan step id: {step_id}")
    index = by_id[step_id]
    previous = updated[index].get("status")
    if previous in PLAN_STEP_TERMINAL_STATUSES:
        raise PlanValidationError(f"terminal plan step cannot transition from {previous}")
    updated[index]["status"] = step_status
    if step_status in {"blocked", "skipped"}:
        updated[index]["reason"] = _bounded_text(
            reason,
            field="reason",
            maximum=MAX_PLAN_STEP_REASON_CHARS,
        )
    else:
        updated[index].pop("reason", None)

    if step_status == "blocked":
        return updated, step_id, PlanRunStatus.BLOCKED.value
    if step_status == "in_progress":
        return updated, step_id, PlanRunStatus.RUNNING.value

    if all(state.get("status") in PLAN_STEP_TERMINAL_STATUSES for state in updated):
        # A final step checkpoint records execution progress only. The owning
        # task must still finish its reply/artifact work before storage marks
        # the run terminal through complete_plan_run().
        return updated, None, PlanRunStatus.RUNNING.value

    candidate = next_step_id
    if candidate is None:
        candidate = next(
            (
                str(state["step_id"])
                for state in updated[index + 1 :] + updated[:index]
                if state.get("status") not in PLAN_STEP_TERMINAL_STATUSES
            ),
            None,
        )
    if candidate is None or candidate not in by_id:
        raise PlanValidationError("next_step_id must identify a non-terminal plan step")
    candidate_state = updated[by_id[candidate]]
    if candidate_state.get("status") in PLAN_STEP_TERMINAL_STATUSES:
        raise PlanValidationError("next_step_id cannot identify a terminal plan step")
    candidate_state["status"] = "in_progress"
    return updated, candidate, PlanRunStatus.RUNNING.value


def plan_revision_snapshot(
    revision: PlanRevisionRecord,
    *,
    current: bool = False,
) -> dict[str, Any]:
    """Return the stable camelCase revision payload used by RPC and transcript parts."""

    return {
        "revisionId": revision.revision_id,
        "planId": revision.plan_id,
        "parentRevisionId": revision.parent_revision_id,
        "generation": revision.generation,
        "title": revision.title,
        "markdown": revision.markdown,
        "steps": [
            {
                "stepId": step["step_id"],
                "title": step["title"],
                **(
                    {"details": step["details"]}
                    if isinstance(step.get("details"), str)
                    else {}
                ),
            }
            for step in revision.steps
        ],
        "current": current,
        "createdAt": revision.created_at,
    }


def plan_run_snapshot(run: PlanRunRecord) -> dict[str, Any]:
    """Return the stable camelCase server-authoritative run payload."""

    return {
        "runId": run.run_id,
        "planRevisionId": run.plan_revision_id,
        "status": run.status,
        "currentStepId": run.current_step_id,
        "steps": [
            {
                "stepId": state["step_id"],
                "title": state["title"],
                "status": state["status"],
                **(
                    {"reason": state["reason"]}
                    if isinstance(state.get("reason"), str)
                    else {}
                ),
            }
            for state in run.step_states
        ],
        "stateRevision": run.state_revision,
        "driverKind": run.driver_kind,
        "driverId": run.driver_id,
        "activeTaskId": run.active_task_id,
        "pauseReason": run.pause_reason,
        "terminalReason": run.terminal_reason,
        "createdAt": run.created_at,
        "updatedAt": run.updated_at,
        "startedAt": run.started_at,
        "finishedAt": run.finished_at,
    }
