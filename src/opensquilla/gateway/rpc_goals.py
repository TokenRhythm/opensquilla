"""RPC handlers for the Goal driver surface (``goal_runs`` ledger).

The Goal controller owns a session's execution pipeline: ``goals.set`` replaces
any prior goal for the session (its cancelled ``goal_runs`` row and superseded
plan run stay durable), activates a single-step goal plan revision, and starts
the first implementation turn through the shared ``sessions.send`` pipeline with
the run bound to ``driver_kind="goal"`` / ``driver_id=<goal_id>``. Later turns
are driven by the runtime's goal continuation hook (``gateway/goal_driver.py``);
``goals.observe`` / ``goals.unobserve`` register/unregister the watcher
eligibility that gates auto-continuation, and ``goals.resume`` restarts a
paused loop by enqueueing the next goal turn immediately.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import structlog

from opensquilla.gateway.goal_driver import (
    _GOAL_RESUMABLE_PAUSE_REASONS,
    GOAL_CONTINUATION_MESSAGE,
    build_goal_route_envelope,
    enqueue_goal_continuation,
    get_goal_watcher_registry,
)
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
    plan_run_event_name,
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


async def _cancel_orphan_goal_run(storage: Any, goal_id: str) -> None:
    """Best-effort cancel of a goal ledger row left without an accepted turn.

    ``goals.set`` persists the goal row before the send pipeline runs; if the
    send or the accepted-run readback fails, cancel it with
    ``terminal_reason="goal_set_failed"`` so it never lingers as "running but
    unturned". Reads the current ``updated_at`` first for the CAS, and swallows
    races (another controller already moved the row) and missing rows.
    """
    try:
        current = await storage.get_goal_run(goal_id)
    except Exception:  # noqa: BLE001 - cleanup must never mask the original error
        return
    if current is None:
        return
    try:
        await storage.cancel_goal_run(
            goal_id,
            expected_updated_at=int(current.updated_at),
            terminal_reason="goal_set_failed",
        )
    except (GoalConflictError, KeyError):
        # Someone else already terminalized or removed the goal; nothing to do.
        pass


@_d.method("goals.set", scope="operator.write")
async def _handle_goals_set(params: dict | None, ctx: RpcContext) -> dict:
    async def _ensure_goal_session(ctx: RpcContext, key: str) -> Any:
        manager = getattr(ctx, "session_manager", None)
        create = getattr(manager, "create", None)
        if not callable(create):
            return None
        try:
            from opensquilla.gateway.rpc_sessions import normalize_agent_id

            agent_id = normalize_agent_id(key.split(":")[1] if key.count(":") >= 2 else "main")
            return await create(session_key=key, agent_id=agent_id)
        except Exception:  # noqa: BLE001 - best effort; caller raises if still absent
            return None

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
        # A goal may target a session that has not been materialized yet (the
        # Web UI new-chat landing is a client-side draft until first send).
        # Create it on demand so a goal is self-contained end to end.
        session = await _ensure_goal_session(ctx, key)
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
        # The visible transcript keeps the user's own goal text (the composer
        # clears its draft after goals.set succeeds); the provider-facing
        # instruction envelope stays hidden from the UI.
        "displayText": message,
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
    try:
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
            raise RuntimeError(
                "Accepted goal turn bound to a different execution driver"
            )

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
            plan_run_event_name(accepted_run),
            {"session_key": accepted_key, "plan_run": run_snapshot},
        )
        return {
            "goalId": goal_id,
            "sessionKey": accepted_key,
            "goal": goal_snapshot,
            "planRun": run_snapshot,
            "turnId": str(result.get("turn_id") or ""),
        }
    except asyncio.CancelledError:
        raise
    except Exception:
        # The goal ledger row is already durable (created before the send). If
        # the send or the accepted-run readback fails, cancel the orphan so it
        # never lingers as "running but unturned"; then re-raise the original
        # error to the caller.
        await _cancel_orphan_goal_run(storage, goal_id)
        raise


@_d.method("goals.status", scope="operator.read")
async def _handle_goals_status(params: dict | None, ctx: RpcContext) -> dict:
    from opensquilla.gateway.rpc_sessions import _require_plan_session_key

    key = _require_plan_session_key(params)
    storage = _require_goal_storage(ctx)
    goal = await storage.get_active_goal_run(key)
    if goal is None:
        # No active run: fall back to the most recent goal (any status) so a
        # completed/blocked goal still reports its terminal outcome instead of
        # an empty active slot.
        goal = await storage.get_latest_goal_run(key)
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


@_d.method("goals.observe", scope="operator.write")
async def _handle_goals_observe(params: dict | None, ctx: RpcContext) -> dict:
    """Register or unregister a watcher for a session's goal turns.

    ``watch: true`` marks the caller as an active observer so the continuation
    driver keeps auto-enqueueing turns even when ``continue_unwatched`` is
    false; ``watch: false`` (or ``goals.unobserve``) releases it. The caller
    identity defaults to the connection id when ``clientId`` is absent.
    """

    return await _apply_goal_observe(params, ctx)


@_d.method("goals.unobserve", scope="operator.write")
async def _handle_goals_unobserve(params: dict | None, ctx: RpcContext) -> dict:
    """Release a goal watcher; thin symmetric wrapper over ``goals.observe``."""

    return await _apply_goal_observe(params, ctx, watch=False)


async def _apply_goal_observe(
    params: dict | None,
    ctx: RpcContext,
    *,
    watch: bool | None = None,
) -> dict:
    from opensquilla.gateway.rpc_sessions import _optional_string_param, _require_plan_session_key

    key = _require_plan_session_key(params)
    registry = get_goal_watcher_registry()
    requested = _optional_string_param(params, "clientId", "client_id")
    if requested:
        client_id = requested
    else:
        client_id = str(getattr(ctx, "conn_id", "") or "").strip() or uuid.uuid4().hex
    if watch is None:
        watch = bool(params.get("watch", True)) if isinstance(params, dict) else True
    if watch:
        count = registry.observe(key, client_id)
    else:
        count = registry.unobserve(key, client_id)
    return {
        "sessionKey": key,
        "clientId": client_id,
        "watching": bool(watch),
        "watchers": count,
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
            pause_reason="user_paused",
            next_retry_at_ms=None,
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
    # Flip the goal state machine back to running, then immediately enqueue
    # the next goal turn when the plan run is paused at the goal_turn_finished
    # anchor. This makes "reopen chat + /goal resume" restore the loop without
    # waiting for a live task to finish and trigger the runtime hook.
    try:
        updated = await storage.update_goal_run(
            goal.goal_id,
            expected_updated_at=int(goal.updated_at),
            status="running",
            pause_reason=None,
            next_retry_at_ms=None,
            last_error=None,
        )
    except GoalConflictError as exc:
        raise _goal_changed_error(exc, goal) from exc

    task_id: str | None = None
    plan_run = (
        await storage.get_plan_run(goal.plan_run_id)
        if goal.plan_run_id
        else None
    )
    if (
        plan_run is None
        or str(plan_run.status) != "paused"
        or str(plan_run.pause_reason or "") not in _GOAL_RESUMABLE_PAUSE_REASONS
    ):
        plan_run = None
    if plan_run is not None and ctx.task_runtime is not None:
        session = await storage.get_session(key)
        envelope_seed = build_goal_route_envelope(
            session_key=key,
            agent_id=goal.agent_id,
            session_id=(
                str(getattr(session, "session_id", "") or "")
                if session is not None
                else None
            ),
            goal_id=goal.goal_id,
            run_id=plan_run.run_id,
            plan_revision_id=str(plan_run.plan_revision_id or "") or None,
            source_name="goals.resume",
            conn_id=str(getattr(ctx, "conn_id", "") or "") or None,
            principal_is_owner=ctx.principal.is_owner,
        )
        handle = await enqueue_goal_continuation(
            ctx.task_runtime,
            session_key=key,
            run_id=plan_run.run_id,
            goal_id=goal.goal_id,
            message=GOAL_CONTINUATION_MESSAGE,
            envelope_seed=envelope_seed,
        )
        if handle is not None:
            task_id = str(getattr(handle, "task_id", "") or "") or None
    return {
        "sessionKey": key,
        "goal": goal_run_snapshot(updated),
        "taskId": task_id,
    }
