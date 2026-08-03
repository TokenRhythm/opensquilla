"""Goal continuation driver: watcher registry + post-turn auto-continuation.

WO-4: after ``task_runtime._execute`` settles an attached goal plan run
(paused with ``pause_reason="goal_turn_finished"``), ``maybe_continue_goal``
parses the last assistant marker line and either enqueues the next goal turn
or terminalizes the goal/plan run. Guardrails (``[goal]`` config section) and
the watcher registry gate every continuation.

Lock-safety contract: the hook is invoked from ``_execute``'s outer
``finally``, i.e. after the per-session execution lock is released. All
storage writes use CAS helpers (``update_goal_run`` keyed by
``updated_at``, plan-run CAS by ``state_revision``) so a concurrent
``goals.pause`` / ``goals.clear`` / replacement goal simply wins and the
driver stops (best-effort, never raises into the turn terminal flow).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import structlog

from opensquilla.gateway.config import GoalConfig
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.session.goals import (
    GoalConflictError,
    advance_goal_after_turn,
    parse_goal_status_marker,
)
from opensquilla.session.keys import canonicalize_session_key, normalize_agent_id

log = structlog.get_logger(__name__)

# Standard continuation instruction injected into every auto-enqueued goal
# turn. ``advance.inject_prompt`` (idle nudge) is appended when present.
GOAL_CONTINUATION_MESSAGE = (
    "Continue pursuing the active goal. Review current progress and take "
    "the next best action."
)

# Plan-run terminal reasons applied by the driver when a goal run finishes.
_PLAN_RUN_TERMINAL_REASON_GOAL_COMPLETE = "goal_complete"
_PLAN_RUN_TERMINAL_REASON_GOAL_BLOCKED = "goal_blocked"

# Plan-run statuses that can never be resumed by a continuation attempt.
_PLAN_RUN_TERMINAL_STATUSES = frozenset({"completed", "cancelled", "superseded"})

# Goal plan runs are paused by ``_settle_attached_plan_run`` with exactly this
# reason when their owning turn succeeded. Failed/cancelled turns use
# ``goal_turn_<outcome>`` and must NOT auto-continue (no resurrection loop).
_GOAL_TURN_FINISHED_PAUSE_REASON = "goal_turn_finished"


class GoalWatcherRegistry:
    """Track connected clients observing a session's goal turns.

    A session with at least one watcher is eligible for automatic
    continuation when ``config.continue_unwatched`` is false. Client ids are
    per-connection identities (``RpcContext.conn_id`` or an explicit
    ``clientId``); the registry never expires entries — observers must
    unregister (CLI watch flow, websocket disconnect).
    """

    def __init__(self) -> None:
        self._watchers: dict[str, set[str]] = {}

    def observe(self, session_key: str, client_id: str) -> int:
        """Register a watcher for a session; returns the watcher count."""
        key = canonicalize_session_key(session_key)
        if not client_id:
            raise ValueError("client_id must be non-empty")
        watchers = self._watchers.setdefault(key, set())
        watchers.add(client_id)
        return len(watchers)

    def unobserve(self, session_key: str, client_id: str) -> int:
        """Remove a watcher for a session; returns the remaining count."""
        key = canonicalize_session_key(session_key)
        watchers = self._watchers.get(key)
        if watchers is None:
            return 0
        watchers.discard(client_id)
        if not watchers:
            self._watchers.pop(key, None)
        return len(watchers)

    def has_watchers(self, session_key: str) -> bool:
        """Return whether any client currently observes the session."""
        watchers = self._watchers.get(canonicalize_session_key(session_key))
        return bool(watchers)

    def watcher_count(self, session_key: str) -> int:
        """Return the number of active watchers for a session."""
        watchers = self._watchers.get(canonicalize_session_key(session_key))
        return len(watchers) if watchers else 0


# Global singleton registry (mirrors ``get_agent_task_registry``).
_registry: GoalWatcherRegistry | None = None


def get_goal_watcher_registry() -> GoalWatcherRegistry:
    """Get or create the global goal watcher registry."""
    global _registry
    if _registry is None:
        _registry = GoalWatcherRegistry()
    return _registry


def build_goal_route_envelope(
    *,
    session_key: str,
    agent_id: str,
    session_id: str | None,
    goal_id: str,
    run_id: str,
    plan_revision_id: str | None = None,
    source_name: str = "goal_driver",
    conn_id: str | None = None,
    principal_is_owner: bool = False,
) -> RouteEnvelope:
    """Build the route envelope for a driver-originated goal continuation turn.

    Used by RPC callers (``goals.resume``) that lack a live task envelope to
    seed the continuation with; the runtime hook instead reuses the finished
    task's envelope directly. ``metadata["plan_run_id"]`` is the durable
    binding the acceptance path uses to re-claim the paused plan run; the
    plan revision is derived authoritatively when absent.
    """

    channel_id = f"web:{conn_id}" if conn_id else "web"
    return RouteEnvelope(
        source_kind=SourceKind.SYSTEM,
        source_name=source_name,
        agent_id=normalize_agent_id(agent_id),
        session_key=canonicalize_session_key(session_key),
        session_id=session_id,
        sender_id=conn_id,
        channel_type="web",
        channel_name="web",
        channel_id=channel_id,
        input_provenance={
            "kind": "goal_continuation",
            "goal_id": goal_id,
            "run_id": run_id,
        },
        delivery_context={"sender_id": conn_id or "goal_driver", "channel_id": channel_id},
        metadata={
            "conn_id": conn_id,
            "plan_run_id": run_id,
            "principal_is_owner": bool(principal_is_owner),
            **(  # optional durable binding kept for acceptance validation
                {"plan_revision_id": plan_revision_id} if plan_revision_id else {}
            ),
        },
    )


def _storage_of(runtime: Any) -> Any | None:
    return getattr(runtime, "_storage", None)


def _extract_transcript_text(content: Any) -> str | None:
    """Return the visible assistant text of a persisted transcript entry.

    Assistant entries may carry raw text or a JSON envelope
    (``{"text": ..., "artifacts": [...]}`` when tool artifacts exist).
    """

    if not isinstance(content, str) or not content.strip():
        return None
    try:
        payload = json.loads(content)
        if isinstance(payload, dict) and isinstance(payload.get("text"), str):
            return payload["text"]
    except (ValueError, TypeError):
        pass
    return content


async def _last_assistant_text(
    storage: Any,
    session_key: str,
    task_id: str,
) -> str | None:
    """Read the last assistant message of one finished turn from the ledger.

    Entries carry the gateway-owned causal ``turn_context.turn_id`` stamped by
    the ``turn_context_scope`` that wraps every ``TaskRuntime._execute`` turn,
    so the marker is resolved against exactly this turn's output. No output →
    ``None`` (treated as a no-marker turn by the caller).
    """

    session = await storage.get_session(session_key)
    if session is None:
        return None
    session_id = str(getattr(session, "session_id", "") or "")
    if not session_id:
        return None
    entries = await storage.get_transcript(session_id)
    if not entries:
        return None
    for entry in reversed(entries):
        if str(getattr(entry, "role", "")) != "assistant":
            continue
        turn_context = getattr(entry, "turn_context", None)
        if not isinstance(turn_context, dict):
            continue
        if str(turn_context.get("turn_id") or "") != task_id:
            continue
        return _extract_transcript_text(getattr(entry, "content", None))
    return None


async def _terminalize_plan_run(
    storage: Any,
    run: Any,
    *,
    reason: str,
) -> None:
    """Terminalize a paused goal plan run; CAS-safe, best-effort."""

    cancel = getattr(storage, "cancel_plan_run", None)
    if not callable(cancel):
        return
    try:
        await cancel(
            run.run_id,
            expected_state_revision=int(run.state_revision),
            reason=reason,
        )
    except Exception:  # noqa: BLE001 - goal driver must not raise into turn terminal flow
        log.warning(
            "goal_driver.plan_run_terminalize_failed",
            run_id=run.run_id,
            reason=reason,
            exc_info=True,
        )


async def enqueue_goal_continuation(
    runtime: Any,
    *,
    session_key: str,
    run_id: str,
    goal_id: str,
    message: str,
    envelope_seed: RouteEnvelope,
) -> Any | None:
    """Enqueue one more goal turn against an existing (paused) plan run.

    The paused → running claim is deliberately NOT performed here: the durable
    claim must be keyed by the real task id, which only exists once
    ``runtime.enqueue`` allocates the follow-up task. The acceptance path
    (``TaskRuntime._start_attached_plan_run``) performs the authoritative
    CAS transition (``mark_plan_run_running`` with the new task id) using the
    same ``plan_run_id`` binding — mirroring the existing manual-run resume
    contract exercised by ``test_goal_owned_plan_run_yields_for_later_driver_attempt``.

    This helper only validates the run is resumable (exists, non-terminal, not
    owned by a live task) so a stale/concurrent replacement fails fast, and
    swallows admission failures (overflow) with a warning instead of raising.
    """

    storage = _storage_of(runtime)
    if storage is None:
        return None
    get_plan_run = getattr(storage, "get_plan_run", None)
    if not callable(get_plan_run):
        return None
    try:
        current = await get_plan_run(run_id)
    except Exception:  # noqa: BLE001 - best-effort driver
        log.warning(
            "goal_driver.continuation_lookup_failed",
            session_key=session_key,
            run_id=run_id,
            exc_info=True,
        )
        return None
    if current is None:
        log.warning(
            "goal_driver.continuation_run_missing",
            session_key=session_key,
            run_id=run_id,
        )
        return None
    status = str(getattr(current, "status", "") or "")
    if status in _PLAN_RUN_TERMINAL_STATUSES:
        log.info(
            "goal_driver.continuation_run_terminal",
            session_key=session_key,
            run_id=run_id,
            status=status,
        )
        return None
    if getattr(current, "active_task_id", None) is not None:
        log.info(
            "goal_driver.continuation_run_busy",
            session_key=session_key,
            run_id=run_id,
            active_task_id=getattr(current, "active_task_id", None),
        )
        return None

    inherited_metadata = dict(getattr(envelope_seed, "metadata", {}) or {})
    # The seed is the finished task's frozen envelope, whose ``task_id``
    # belongs to that old task; carrying it into the follow-up task's durable
    # metadata would mislead consumers that key on it (the next turn's own
    # collaboration freeze stamps the new id). Keep the remaining keys, e.g.
    # ``plan_run_id`` / ``plan_revision_id``, which the acceptance path needs.
    inherited_metadata.pop("task_id", None)
    envelope = replace(
        envelope_seed,
        metadata={
            **inherited_metadata,
            "plan_run_id": run_id,
        },
    )
    try:
        handle = await runtime.enqueue(
            envelope,
            message,
            mode="followup",
            run_kind="goal_turn",
            no_memory_capture=True,
        )
    except Exception:  # noqa: BLE001 - admission failure must not break the driver
        log.warning(
            "goal_driver.continuation_enqueue_failed",
            session_key=session_key,
            run_id=run_id,
            goal_id=goal_id,
            exc_info=True,
        )
        return None
    log.info(
        "goal_driver.continuation_enqueued",
        session_key=session_key,
        run_id=run_id,
        goal_id=goal_id,
        task_id=getattr(handle, "task_id", None),
    )
    return handle


async def maybe_continue_goal(
    runtime: Any,
    task: Any,
    *,
    config: GoalConfig | None = None,
) -> Any | None:
    """Post-turn hook: decide and drive one goal continuation.

    Returns the enqueued ``TaskHandle`` when a continuation was scheduled and
    ``None`` otherwise (terminal, guardrail stop, or not a goal turn). Never
    raises: a goal driver failure must not mask the turn's terminal state.
    """

    config = config if config is not None else GoalConfig()
    storage = _storage_of(runtime)
    if storage is None:
        return None
    envelope = task.envelope
    run_id = str(getattr(envelope, "metadata", {}).get("plan_run_id") or "").strip()
    if not run_id:
        return None
    session_key = str(getattr(envelope, "session_key", "") or "")
    if not session_key:
        return None
    try:
        return await _maybe_continue_goal_impl(
            runtime,
            storage,
            task,
            envelope=envelope,
            run_id=run_id,
            session_key=session_key,
            config=config,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - goal driver must never break turn terminal flow
        log.error(
            "goal_driver.continue_failed",
            session_key=session_key,
            run_id=run_id,
            task_id=getattr(task, "task_id", None),
            exc_info=True,
        )
        return None


async def _maybe_continue_goal_impl(
    runtime: Any,
    storage: Any,
    task: Any,
    *,
    envelope: RouteEnvelope,
    run_id: str,
    session_key: str,
    config: GoalConfig,
) -> Any | None:
    get_plan_run = getattr(storage, "get_plan_run", None)
    get_goal_run = getattr(storage, "get_goal_run", None)
    if not callable(get_plan_run) or not callable(get_goal_run):
        return None

    plan_run = await get_plan_run(run_id)
    if plan_run is None:
        return None
    if str(getattr(plan_run, "driver_kind", "")) != "goal":
        return None
    # Only a successfully settled turn may drive the next one. Failed/cancelled
    # turns pause the run with ``goal_turn_<outcome>``; auto-continuing those
    # would resurrect interrupted or failed work without a user intent.
    if (
        str(getattr(plan_run, "status", "")) != "paused"
        or str(getattr(plan_run, "pause_reason", "") or "")
        != _GOAL_TURN_FINISHED_PAUSE_REASON
    ):
        return None
    goal_id = str(getattr(plan_run, "driver_id", "") or "").strip()
    goal = await get_goal_run(goal_id) if goal_id else None
    if goal is None or str(getattr(goal, "status", "")) != "running":
        return None

    now_ms = _now_ms()
    # Guardrail pre-checks: short-circuit before reading the transcript so a
    # budget/turn-limit stop never pays for marker parsing.
    if int(getattr(goal, "turns", 0) or 0) >= int(config.max_turns):
        return await _apply_guardrail_block(
            storage,
            goal=goal,
            plan_run=plan_run,
            terminal_reason="goal_continuation_limit_reached",
            now_ms=now_ms,
        )
    budget_seconds = config.runtime_budget_seconds
    if (
        budget_seconds is not None
        and now_ms - int(getattr(goal, "started_at", now_ms) or now_ms)
        > int(budget_seconds) * 1000
    ):
        return await _apply_guardrail_block(
            storage,
            goal=goal,
            plan_run=plan_run,
            terminal_reason="goal_runtime_budget_exceeded",
            now_ms=now_ms,
        )

    if not config.continue_unwatched and not get_goal_watcher_registry().has_watchers(
        session_key
    ):
        # No observer: stop the loop without touching the goal ledger. The plan
        # run stays paused at ``goal_turn_finished`` so a later
        # ``goals.resume`` (which flips the goal back to running and enqueues
        # the next turn) restarts cleanly.
        log.info(
            "goal_driver.continuation_unwatched",
            session_key=session_key,
            run_id=run_id,
            goal_id=goal_id,
        )
        return None

    marker_text = await _last_assistant_text(storage, session_key, task.task_id)
    marker = parse_goal_status_marker(marker_text) if marker_text else None
    advance = advance_goal_after_turn(
        goal,
        marker,
        max_turns=int(config.max_turns),
        idle_turns=int(config.idle_turns),
        blocked_retries=int(config.blocked_retries),
        runtime_budget_seconds=config.runtime_budget_seconds,
        now_ms=now_ms,
    )

    # Apply the per-turn fixed actions plus the advance decision.
    fields: dict[str, Any] = {
        "turns": int(getattr(goal, "turns", 0) or 0) + 1,
        "last_turn_at": now_ms,
    }
    if marker is None:
        if advance.inject_prompt is not None:
            fields["idle_turns"] = 0  # nudge injected; counter resets
        else:
            fields["idle_turns"] = int(getattr(goal, "idle_turns", 0) or 0) + 1
    elif marker[0] == "continue":
        fields["idle_turns"] = 0
    elif marker[0] == "complete":
        fields["idle_turns"] = 0
    elif marker[0] == "blocked":
        reason_text = marker[1] or ""
        same_cause = (
            str(getattr(goal, "blocked_reason", "") or "") == reason_text
            if reason_text
            else False
        )
        retries_after = (
            int(getattr(goal, "blocked_retries", 0) or 0) + 1
            if same_cause
            else 1
        )
        fields["blocked_reason"] = reason_text
        fields["blocked_retries"] = retries_after
        fields["idle_turns"] = 0
    else:  # pragma: no cover - advance validated the marker kind already
        fields["idle_turns"] = 0

    if advance.terminal:
        terminal_reason = advance.terminal_reason
        if terminal_reason is None:
            fields["status"] = "complete"
            fields["finished_at"] = now_ms
            fields["terminal_reason"] = None
            plan_terminal_reason = _PLAN_RUN_TERMINAL_REASON_GOAL_COMPLETE
        else:
            fields["status"] = "blocked"
            fields["finished_at"] = now_ms
            fields["terminal_reason"] = terminal_reason
            plan_terminal_reason = _PLAN_RUN_TERMINAL_REASON_GOAL_BLOCKED
        try:
            await storage.update_goal_run(
                goal.goal_id,
                expected_updated_at=int(getattr(goal, "updated_at", 0) or 0),
                **fields,
            )
        except GoalConflictError:
            # A concurrent controller (pause/clear/replacement) won; stop.
            return None
        await _terminalize_plan_run(
            storage,
            plan_run,
            reason=plan_terminal_reason,
        )
        log.info(
            "goal_driver.goal_terminal",
            session_key=session_key,
            run_id=run_id,
            goal_id=goal_id,
            status=fields["status"],
            terminal_reason=terminal_reason,
            turns=fields["turns"],
        )
        return None

    try:
        await storage.update_goal_run(
            goal.goal_id,
            expected_updated_at=int(getattr(goal, "updated_at", 0) or 0),
            **fields,
        )
    except GoalConflictError:
        return None

    message = GOAL_CONTINUATION_MESSAGE
    if advance.inject_prompt:
        message = f"{message}\n\n{advance.inject_prompt}"
    return await enqueue_goal_continuation(
        runtime,
        session_key=session_key,
        run_id=run_id,
        goal_id=goal_id,
        message=message,
        envelope_seed=envelope,
    )


async def _apply_guardrail_block(
    storage: Any,
    *,
    goal: Any,
    plan_run: Any,
    terminal_reason: str,
    now_ms: int,
) -> None:
    """Block a goal run that exceeded a configured guardrail budget."""

    try:
        await storage.update_goal_run(
            goal.goal_id,
            expected_updated_at=int(getattr(goal, "updated_at", 0) or 0),
            status="blocked",
            turns=int(getattr(goal, "turns", 0) or 0) + 1,
            last_turn_at=now_ms,
            finished_at=now_ms,
            terminal_reason=terminal_reason,
        )
    except GoalConflictError:
        return
    await _terminalize_plan_run(
        storage,
        plan_run,
        reason=_PLAN_RUN_TERMINAL_REASON_GOAL_BLOCKED,
    )
    log.info(
        "goal_driver.goal_guardrail_blocked",
        goal_id=goal.goal_id,
        terminal_reason=terminal_reason,
    )


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)
