"""RPC handlers for the Goal driver surface (``goal_runs`` ledger).

The Goal controller owns a session's execution pipeline: ``goals.set`` replaces
any prior goal for the session (its cancelled ``goal_runs`` row and superseded
plan run stay durable), activates a single-step goal plan revision, and starts
the first implementation turn through the shared ``sessions.send`` pipeline with
the run bound to ``driver_kind="goal"`` / ``driver_id=<goal_id>``. Later turns
are driven by the runtime's goal continuation hook (WO-4); this module only
starts the first turn and manages the goal state machine.

``goals.observe`` / ``goals.unobserve`` are deliberately not implemented here —
they need the watcher registry delivered by WO-4.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, RpcUnavailableError, get_dispatcher
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.session.goals import (
    GoalConflictError,
    GoalValidationError,
    goal_run_snapshot,
    new_goal_run,
)
from opensquilla.session.keys import normalize_agent_id
from opensquilla.session.plans import (
    PLAN_RUN_ACTIVE_STATUSES,
    PlanConflictError,
    PlanRunConflictError,
    PlanValidationError,
    new_goal_plan_revision,
    plan_run_snapshot,
)

log = structlog.get_logger(__name__)

_d = get_dispatcher()

_GOAL_SET_MESSAGE = (
    "Pursue the goal: {goal_text}. "
    "Work toward it; end your reply with a goal marker line: "
    "[goal:continue] | [goal:complete] | [goal:blocked:<reason>]."
)


def _require_goal_storage(ctx: RpcContext) -> Any:
    if ctx.session_manager is None:
        raise RpcUnavailableError("Session manager is not configured")
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise RpcUnavailableError("Session storage is not configured")
    return storage


def _goal_changed_error(exc: Exception, goal: Any | None = None) -> RpcHandlerError:
    details: dict[str, Any] = {}
    if goal is not None:
        details["goal"] = goal_run_snapshot(goal)
    return RpcHandlerError(
        "GOAL_CHANGED",
        str(exc),
        details=details or None,
        retryable=True,
        accepted=False,
    )


@_d.method("goals.set", scope="operator.write")
async def _handle_goals_set(params: dict | None, ctx: RpcContext) -> dict:
    from opensquilla.gateway.rpc_sessions import (
        _emit_to_subscribers,
        _handle_sessions_send,
        _optional_string_param,
        _require_plan_session_key,
    )

    key = _require_plan_session_key(params)
    storage = _require_goal_storage(ctx)
    message = _optional_string_param(params, "message")
    if message is None:
        raise ValueError("params.message is required")
    session = await storage.get_session(key)
    if session is None:
        raise KeyError(f"Session not found: {key}")
    session_id = getattr(session, "session_id", None)
    session_id = (
        session_id if isinstance(session_id, str) and session_id else key.split(":")[-1] or key
    )
    session_epoch = int(getattr(session, "epoch", 0) or 0)
    agent_id = normalize_agent_id(
        _optional_string_param(params, "agentId")
        or getattr(session, "agent_id", None)
        or "main"
    )

    goal_id = uuid.uuid4().hex
    try:
        goal_run = new_goal_run(
            goal_id=goal_id,
            session_key=key,
            agent_id=agent_id,
            goal_text=message,
        )
    except GoalValidationError as exc:
        raise ValueError(str(exc)) from exc

    # A replacement goal takes over the whole execution pipeline: cancel the
    # old goal run and supersede any active plan run (goal-owned or manual) so
    # the shared send pipeline starts from a clean active-run slot.
    await storage.supersede_active_goal_runs(key)
    await storage.supersede_active_plan_runs(key, reason="superseded_by_new_goal")
    try:
        await storage.create_goal_run(goal_run)
    except GoalConflictError as exc:
        raise RpcHandlerError(
            "GOAL_ACTIVE",
            str(exc),
            retryable=False,
            accepted=False,
        ) from exc

    # Activate the goal plan revision before the send so the pipeline's
    # PLAN_REVISION_CHANGED guard sees the goal revision as current. The first
    # goal on a session starts a fresh lineage; replacing the active revision
    # requires a replan so the storage layer can atomically swap it.
    active_revision_id = str(
        getattr(session, "active_plan_revision_id", "") or ""
    ).strip() or None
    if active_revision_id is None:
        goal_revision = new_goal_plan_revision(
            source_session_key=key,
            source_session_id=session_id,
            source_epoch=session_epoch,
            goal_text=goal_run.goal_text,
        )
        expected_parent_revision_id = None
    else:
        parent = await storage.get_plan_revision(active_revision_id)
        if parent is None:
            raise RpcHandlerError(
                "PLAN_REVISION_CHANGED",
                "The active plan revision no longer exists.",
                retryable=False,
                accepted=False,
            )
        goal_revision = new_goal_plan_revision(
            source_session_key=key,
            source_session_id=session_id,
            source_epoch=session_epoch,
            goal_text=goal_run.goal_text,
            parent=parent,
        )
        expected_parent_revision_id = parent.revision_id
    try:
        goal_revision = await storage.create_plan_revision(
            goal_revision,
            expected_parent_revision_id=expected_parent_revision_id,
        )
    except (PlanConflictError, PlanRunConflictError, PlanValidationError) as exc:
        raise RpcHandlerError(
            "GOAL_PLAN_FAILED",
            str(exc),
            retryable=False,
            accepted=False,
        ) from exc

    client_request_id = (
        _optional_string_param(params, "clientRequestId") or uuid.uuid4().hex
    )
    provider_message = _GOAL_SET_MESSAGE.format(goal_text=goal_run.goal_text)
    send_params = {
        "key": key,
        "message": provider_message,
        "clientRequestId": client_request_id,
        "intent": "continue",
        "queueMode": "followup",
        "inputProvenanceKind": "goal_implementation",
        "noMemoryCapture": True,
        # Control-plane input: the goal instruction is durable and
        # provider-visible but must not appear in the visible transcript.
        "displayText": "",
        "source": {
            "caller_kind": "web",
            "source_name": "goals.set",
        },
    }
    target_before_acceptance = await storage.get_session(key)
    required_collaboration_revision = (
        int(target_before_acceptance.collaboration_revision or 0) + 1
        if target_before_acceptance is not None
        else 1
    )
    result = await _handle_sessions_send(
        send_params,
        ctx,
        fingerprint_params={
            "action": "goals.set",
            "sessionKey": key,
            "goalId": goal_id,
            "message": provider_message,
            "intent": "continue",
        },
        plan_revision_id=goal_revision.revision_id,
        plan_run_driver_kind="goal",
        plan_run_driver_id=goal_id,
        required_collaboration_mode="default",
        required_collaboration_revision=required_collaboration_revision,
    )
    accepted_key = str(result.get("session_key") or key)
    task_id = str(result.get("turn_id") or result.get("task_id") or "").strip()
    task_record = await storage.get_agent_task(task_id) if task_id else None
    task_details = (
        task_record.details
        if task_record is not None and isinstance(task_record.details, dict)
        else {}
    )
    task_metadata = task_details.get("metadata")
    task_metadata = task_metadata if isinstance(task_metadata, dict) else {}
    accepted_run_id = str(task_metadata.get("plan_run_id") or "").strip()
    if not accepted_run_id:
        raise RuntimeError("Accepted goal turn lost its durable plan run binding")
    accepted_run = await storage.get_plan_run(accepted_run_id)
    if accepted_run is None:
        raise RuntimeError("Accepted goal plan run no longer exists")
    if (
        str(accepted_run.driver_kind) != "goal"
        or str(accepted_run.driver_id or "") != goal_id
    ):
        raise RuntimeError("Accepted goal turn bound to a different execution driver")

    # Backfill the run id onto the goal ledger row created before the run.
    current_goal = await storage.get_goal_run(goal_id)
    if current_goal is not None and not current_goal.plan_run_id:
        try:
            current_goal = await storage.update_goal_run(
                goal_id,
                expected_updated_at=int(current_goal.updated_at),
                plan_run_id=accepted_run.run_id,
            )
        except GoalConflictError:
            current_goal = await storage.get_goal_run(goal_id)
    goal_snapshot = (
        goal_run_snapshot(current_goal)
        if current_goal is not None
        else goal_run_snapshot(goal_run)
    )
    run_snapshot = plan_run_snapshot(accepted_run)
    await _emit_to_subscribers(
        ctx,
        accepted_key,
        "session.event.plan_run",
        {"session_key": accepted_key, "plan_run": run_snapshot},
    )
    return {
        "goalId": goal_id,
        "sessionKey": accepted_key,
        "goal": goal_snapshot,
        "planRun": run_snapshot,
        "turnId": str(result.get("turn_id") or ""),
    }


@_d.method("goals.status", scope="operator.read")
async def _handle_goals_status(params: dict | None, ctx: RpcContext) -> dict:
    from opensquilla.gateway.rpc_sessions import _require_plan_session_key

    key = _require_plan_session_key(params)
    storage = _require_goal_storage(ctx)
    goal = await storage.get_active_goal_run(key)
    plan_run = (
        await storage.get_plan_run(goal.plan_run_id)
        if goal is not None and goal.plan_run_id
        else None
    )
    return {
        "sessionKey": key,
        "goal": goal_run_snapshot(goal) if goal is not None else None,
        "planRun": plan_run_snapshot(plan_run) if plan_run is not None else None,
    }


@_d.method("goals.clear", scope="operator.write")
async def _handle_goals_clear(params: dict | None, ctx: RpcContext) -> dict:
    from opensquilla.gateway.rpc_sessions import _require_plan_session_key

    key = _require_plan_session_key(params)
    storage = _require_goal_storage(ctx)
    goal = await storage.get_active_goal_run(key)
    plan_run = (
        await storage.get_plan_run(goal.plan_run_id)
        if goal is not None and goal.plan_run_id
        else None
    )
    before = {
        "sessionKey": key,
        "goal": goal_run_snapshot(goal) if goal is not None else None,
        "planRun": plan_run_snapshot(plan_run) if plan_run is not None else None,
    }
    await storage.supersede_active_goal_runs(key)
    # The goal's own plan run must not linger as an active overlay blocking
    # later plan operations once its goal is cleared.
    if goal is not None and goal.plan_run_id:
        for _attempt in range(3):
            candidate = plan_run
            if candidate is None or str(candidate.run_id) != goal.plan_run_id:
                candidate = await storage.get_plan_run(goal.plan_run_id)
            if candidate is None or candidate.status not in PLAN_RUN_ACTIVE_STATUSES:
                break
            try:
                await storage.cancel_plan_run(
                    candidate.run_id,
                    expected_state_revision=int(candidate.state_revision),
                    reason="cleared_by_goal_controller",
                )
                break
            except PlanRunConflictError:
                plan_run = None
    return before


@_d.method("goals.pause", scope="operator.write")
async def _handle_goals_pause(params: dict | None, ctx: RpcContext) -> dict:
    from opensquilla.gateway.rpc_sessions import _require_plan_session_key

    key = _require_plan_session_key(params)
    storage = _require_goal_storage(ctx)
    goal = await storage.get_active_goal_run(key)
    if goal is None:
        raise RpcHandlerError(
            "NO_ACTIVE_GOAL",
            "No active goal for this session.",
            retryable=False,
            accepted=False,
        )
    if goal.status != "running":
        raise RpcHandlerError(
            "GOAL_NOT_RUNNING",
            f"Cannot pause a {goal.status} goal run.",
            details={"goal": goal_run_snapshot(goal)},
            retryable=False,
            accepted=False,
        )
    try:
        updated = await storage.update_goal_run(
            goal.goal_id,
            expected_updated_at=int(goal.updated_at),
            status="paused",
        )
    except GoalConflictError as exc:
        raise _goal_changed_error(exc, goal) from exc
    return {"sessionKey": key, "goal": goal_run_snapshot(updated)}


@_d.method("goals.resume", scope="operator.write")
async def _handle_goals_resume(params: dict | None, ctx: RpcContext) -> dict:
    from opensquilla.gateway.rpc_sessions import _require_plan_session_key

    key = _require_plan_session_key(params)
    storage = _require_goal_storage(ctx)
    goal = await storage.get_active_goal_run(key)
    if goal is None:
        raise RpcHandlerError(
            "NO_ACTIVE_GOAL",
            "No active goal for this session.",
            retryable=False,
            accepted=False,
        )
    if goal.status != "paused":
        raise RpcHandlerError(
            "GOAL_NOT_PAUSED",
            f"Cannot resume a {goal.status} goal run.",
            details={"goal": goal_run_snapshot(goal)},
            retryable=False,
            accepted=False,
        )
    # Resuming only flips the goal state machine back to running; re-enqueueing
    # the next goal turn is the WO-4 continuation hook's responsibility.
    try:
        updated = await storage.update_goal_run(
            goal.goal_id,
            expected_updated_at=int(goal.updated_at),
            status="running",
        )
    except GoalConflictError as exc:
        raise _goal_changed_error(exc, goal) from exc
    return {"sessionKey": key, "goal": goal_run_snapshot(updated)}
