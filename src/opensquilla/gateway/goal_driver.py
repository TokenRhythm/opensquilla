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
import time
from dataclasses import replace
from typing import Any

import structlog

from opensquilla.gateway.config import GoalConfig
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.session.goals import (
    GoalConflictError,
    advance_goal_after_failure,
    advance_goal_after_turn,
    goal_retry_delay_ms,
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

_GOAL_PROGRESS_MAX_ENTRIES = 12
_GOAL_PROGRESS_ENTRY_MAX_CHARS = 1_000

# Plan-run terminal reasons applied by the driver when a goal run finishes.
_PLAN_RUN_TERMINAL_REASON_GOAL_COMPLETE = "goal_complete"
_PLAN_RUN_TERMINAL_REASON_GOAL_BLOCKED = "goal_blocked"

# Plan-run statuses that can never be resumed by a continuation attempt.
_PLAN_RUN_TERMINAL_STATUSES = frozenset({"completed", "cancelled", "superseded"})

# Goal plan runs are paused by ``_settle_attached_plan_run`` with exactly this
# reason when their owning turn succeeded. Failed/cancelled turns use
# ``goal_turn_<outcome>``.
_GOAL_TURN_FINISHED_PAUSE_REASON = "goal_turn_finished"

# Transient turn outcomes the driver may retry automatically after a backoff
# (provider overload / rate limit / transport errors surface as failed or
# timeout turns). Non-retryable outcomes (cancelled / abandoned) park the goal
# in ``paused`` instead of resurrecting user-interrupted or shutdown-dropped
# work.
_GOAL_RETRYABLE_PAUSE_REASONS = frozenset({"goal_turn_failed", "goal_turn_timeout"})

# Every pause reason that still has a resumable execution pipeline (the bound
# plan run is paused, not terminal), used by ``goals.resume`` and the restart
# recovery scan.
_GOAL_RESUMABLE_PAUSE_REASONS = frozenset(
    {
        _GOAL_TURN_FINISHED_PAUSE_REASON,
        "goal_turn_failed",
        "goal_turn_timeout",
        "goal_unwatched",
        "goal_turn_cancelled",
        "goal_turn_abandoned",
        "goal_run_reopened",
        "process_restart",
    }
)

# Goal-level pause reasons recorded on the ledger when the driver parks a run.
_GOAL_PAUSE_REASON_UNWATCHED = "goal_unwatched"
_GOAL_PAUSE_REASON_TURN_CANCELLED = "goal_turn_cancelled"
_GOAL_PAUSE_REASON_TURN_ABANDONED = "goal_turn_abandoned"
_GOAL_PAUSE_REASON_REOPENED = "goal_run_reopened"


class GoalWatcherRegistry:
    """Track connected clients observing a session's goal turns.

    A session with at least one watcher is eligible for automatic
    continuation when ``config.continue_unwatched`` is false. Client ids are
    per-connection identities (``RpcContext.conn_id`` or an explicit
    ``clientId``). Entries carry a ``last_seen`` timestamp refreshed on every
    ``observe``; ``has_watchers``/``watcher_count`` with a ``ttl_ms`` lazily
    evict stale entries so a hard-killed CLI or dropped connection stops
    gating auto-continuation instead of burning tokens to ``max_turns``.
    """

    def __init__(self) -> None:
        self._watchers: dict[str, dict[str, float]] = {}
        # Injectable clock (seconds since epoch) so tests can advance time.
        self._now = time.time

    def _now_ms(self) -> float:
        return self._now() * 1000.0

    def _evict_expired(self, session_key: str, ttl_ms: int) -> None:
        watchers = self._watchers.get(session_key)
        if not watchers:
            return
        now_ms = self._now_ms()
        expired = [
            client_id
            for client_id, last_seen_ms in watchers.items()
            if now_ms - last_seen_ms > ttl_ms
        ]
        for client_id in expired:
            watchers.pop(client_id, None)
        if not watchers:
            self._watchers.pop(session_key, None)

    def observe(self, session_key: str, client_id: str) -> int:
        """Register a watcher for a session; returns the watcher count."""
        key = canonicalize_session_key(session_key)
        if not client_id:
            raise ValueError("client_id must be non-empty")
        watchers = self._watchers.setdefault(key, {})
        watchers[client_id] = self._now_ms()
        return len(watchers)

    def unobserve(self, session_key: str, client_id: str) -> int:
        """Remove a watcher for a session; returns the remaining count."""
        key = canonicalize_session_key(session_key)
        watchers = self._watchers.get(key)
        if watchers is None:
            return 0
        watchers.pop(client_id, None)
        if not watchers:
            self._watchers.pop(key, None)
        return len(watchers)

    def has_watchers(self, session_key: str, ttl_ms: int | None = None) -> bool:
        """Return whether any client currently observes the session.

        With ``ttl_ms``, stale entries (no observe heartbeat within the
        window) are lazily evicted before answering.
        """
        key = canonicalize_session_key(session_key)
        if ttl_ms is not None:
            self._evict_expired(key, ttl_ms)
        watchers = self._watchers.get(key)
        return bool(watchers)

    def watcher_count(self, session_key: str, ttl_ms: int | None = None) -> int:
        """Return the number of active watchers for a session.

        With ``ttl_ms``, stale entries (no observe heartbeat within the
        window) are lazily evicted before answering.
        """
        key = canonicalize_session_key(session_key)
        if ttl_ms is not None:
            self._evict_expired(key, ttl_ms)
        watchers = self._watchers.get(key)
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
            return str(payload["text"])
    except (ValueError, TypeError):
        pass
    return content


def _append_goal_progress(
    progress: Any,
    *,
    turn_number: int,
    assistant_text: str | None,
    marker: tuple[str, str | None] | None,
) -> list[dict[str, Any]]:
    """Append one bounded turn summary to the durable goal progress log.

    The transcript remains the source of truth for the full response. The
    goal ledger keeps only a compact rolling handoff so later turns still have
    explicit progress context after history compaction or a restart.
    """

    entries = [entry for entry in list(progress or []) if isinstance(entry, dict)]
    text = str(assistant_text or "").strip()
    if text and marker is not None:
        lines = text.rstrip("\n").split("\n")
        if lines:
            text = "\n".join(lines[:-1]).strip()
    if len(text) > _GOAL_PROGRESS_ENTRY_MAX_CHARS:
        half = (_GOAL_PROGRESS_ENTRY_MAX_CHARS - 32) // 2
        text = f"{text[:half]}\n…\n{text[-half:]}"
    if not text:
        text = "No assistant summary was recorded for this turn."
    entries.append(
        {
            "turn": int(turn_number),
            "status": marker[0] if marker is not None else "unmarked",
            "summary": text,
        }
    )
    return entries[-_GOAL_PROGRESS_MAX_ENTRIES:]


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
    # Only a paused plan run at a resumable anchor may drive the next turn.
    # ``goal_turn_finished`` continues immediately; ``goal_turn_failed`` /
    # ``goal_turn_timeout`` schedule an automatic retry with backoff;
    # ``goal_turn_cancelled`` / ``goal_turn_abandoned`` park the goal paused so
    # user-interrupted or shutdown-dropped work is never resurrected silently.
    pause_reason = str(getattr(plan_run, "pause_reason", "") or "")
    if (
        str(getattr(plan_run, "status", "")) != "paused"
        or pause_reason not in _GOAL_RESUMABLE_PAUSE_REASONS
    ):
        return None
    goal_id = str(getattr(plan_run, "driver_id", "") or "").strip()
    goal = await get_goal_run(goal_id) if goal_id else None
    if goal is None or str(getattr(goal, "status", "")) != "running":
        return None

    now_ms = _now_ms()
    # Guardrail pre-checks: short-circuit before reading the transcript so a
    # budget/turn-limit stop never pays for marker parsing.
    # Same threshold as ``advance_goal_after_turn`` (``turns + 1 >= max_turns``):
    # the run blocks once it has executed ``max_turns`` turns. Checking here
    # short-circuits before the transcript read so a limit stop never pays for
    # marker parsing.
    if int(getattr(goal, "turns", 0) or 0) + 1 >= int(config.max_turns):
        await _apply_guardrail_block(
            storage,
            goal=goal,
            plan_run=plan_run,
            terminal_reason="goal_continuation_limit_reached",
            now_ms=now_ms,
        )
        return None
    budget_seconds = config.runtime_budget_seconds
    if (
        budget_seconds is not None
        and now_ms - int(getattr(goal, "started_at", now_ms) or now_ms)
        > int(budget_seconds) * 1000
    ):
        await _apply_guardrail_block(
            storage,
            goal=goal,
            plan_run=plan_run,
            terminal_reason="goal_runtime_budget_exceeded",
            now_ms=now_ms,
        )
        return None

    if pause_reason in _GOAL_RETRYABLE_PAUSE_REASONS:
        await _handle_goal_turn_failure(
            storage,
            task,
            plan_run,
            goal,
            config=config,
            now_ms=now_ms,
        )
        return None
    if pause_reason in {
        _GOAL_PAUSE_REASON_TURN_CANCELLED,
        _GOAL_PAUSE_REASON_TURN_ABANDONED,
    }:
        await _park_goal_after_non_retryable_failure(
            storage,
            goal,
            pause_reason=pause_reason,
            now_ms=now_ms,
        )
        return None

    # ``goal_turn_finished``: marker-driven continuation below.
    if not config.continue_unwatched and not get_goal_watcher_registry().has_watchers(
        session_key, ttl_ms=int(config.watcher_ttl_seconds) * 1000
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
        # A successfully finished turn starts a fresh transient-failure
        # budget. Otherwise an earlier provider/queue failure would keep
        # consuming the retry allowance after recovery.
        "failure_retries": 0,
        "next_retry_at_ms": None,
        "last_error": None,
    }
    fields["progress"] = _append_goal_progress(
        getattr(goal, "progress", None),
        turn_number=int(fields["turns"]),
        assistant_text=marker_text,
        marker=marker,
    )
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
            getattr(goal, "blocked_reason", None) is not None
            and str(getattr(goal, "blocked_reason", "") or "") == reason_text
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
        updated_goal = await storage.update_goal_run(
            goal.goal_id,
            expected_updated_at=int(getattr(goal, "updated_at", 0) or 0),
            **fields,
        )
    except GoalConflictError:
        return None

    message = GOAL_CONTINUATION_MESSAGE
    if advance.inject_prompt:
        message = f"{message}\n\n{advance.inject_prompt}"
    handle = await enqueue_goal_continuation(
        runtime,
        session_key=session_key,
        run_id=run_id,
        goal_id=goal_id,
        message=message,
        envelope_seed=envelope,
    )
    if handle is None:
        # Enqueue admission failed (session overflow / queue full): schedule an
        # automatic retry with backoff instead of silently parking the loop.
        await _record_enqueue_backoff(
            storage,
            updated_goal,
            config=config,
            now_ms=now_ms,
        )
    return handle


async def _handle_goal_turn_failure(
    storage: Any,
    task: Any,
    plan_run: Any,
    goal: Any,
    *,
    config: GoalConfig,
    now_ms: int,
) -> None:
    """Record one transient turn failure and schedule an automatic retry.

    Retryable outcomes (``failed`` / ``timeout``) increment ``failure_retries``
    and set ``next_retry_at_ms`` with exponential backoff; the retry loop
    (``drive_due_goal_retries``) enqueues the next attempt once due. Exhausting
    ``goal.failure_retries`` blocks the goal. Non-retryable outcomes park the
    goal in ``paused`` so the user decides (``/goal resume``).
    """

    raw_status = getattr(task, "status", None)
    task_status = str(
        getattr(raw_status, "value", None) or raw_status or "failed"
    )
    retries = int(getattr(goal, "failure_retries", 0) or 0)
    advance = advance_goal_after_failure(
        goal,
        task_status=task_status,
        failure_retries=retries,
        max_failure_retries=int(config.failure_retries),
        base_backoff_ms=int(config.retry_base_backoff_ms),
        max_backoff_ms=int(config.retry_max_backoff_ms),
        now_ms=now_ms,
    )
    fields: dict[str, Any] = {
        "turns": int(getattr(goal, "turns", 0) or 0) + 1,
        "last_turn_at": now_ms,
        "failure_retries": retries + 1,
        "last_error": f"turn_{task_status}",
        "progress": _append_goal_progress(
            getattr(goal, "progress", None),
            turn_number=int(getattr(goal, "turns", 0) or 0) + 1,
            assistant_text=f"Turn ended with {task_status}.",
            marker=None,
        ),
    }
    if advance.retry_at_ms is not None:
        fields["status"] = "running"
        fields["next_retry_at_ms"] = advance.retry_at_ms
        log.info(
            "goal_driver.turn_failure_retry_scheduled",
            session_key=goal.session_key,
            goal_id=goal.goal_id,
            task_status=task_status,
            failure_retries=retries + 1,
            retry_at_ms=advance.retry_at_ms,
        )
    elif advance.terminal:
        fields["status"] = "blocked"
        fields["finished_at"] = now_ms
        fields["terminal_reason"] = advance.terminal_reason
        fields["next_retry_at_ms"] = None
        log.warning(
            "goal_driver.turn_failure_exhausted",
            session_key=goal.session_key,
            goal_id=goal.goal_id,
            task_status=task_status,
            terminal_reason=advance.terminal_reason,
        )
        await _terminalize_plan_run(
            storage,
            plan_run,
            reason=_PLAN_RUN_TERMINAL_REASON_GOAL_BLOCKED,
        )
    else:
        fields["status"] = "paused"
        fields["pause_reason"] = (
            _GOAL_PAUSE_REASON_TURN_CANCELLED
            if task_status == "cancelled"
            else _GOAL_PAUSE_REASON_TURN_ABANDONED
        )
        fields["next_retry_at_ms"] = None
        log.info(
            "goal_driver.turn_failure_parked",
            session_key=goal.session_key,
            goal_id=goal.goal_id,
            task_status=task_status,
        )
    try:
        await storage.update_goal_run(
            goal.goal_id,
            expected_updated_at=int(getattr(goal, "updated_at", 0) or 0),
            **fields,
        )
    except GoalConflictError:
        # A concurrent controller (pause/clear/replacement) won; stop.
        return None
    return None


async def _park_goal_after_non_retryable_failure(
    storage: Any,
    goal: Any,
    *,
    pause_reason: str,
    now_ms: int,
) -> None:
    """Park a goal whose turn was cancelled/abandoned; never auto-retry."""

    try:
        await storage.update_goal_run(
            goal.goal_id,
            expected_updated_at=int(getattr(goal, "updated_at", 0) or 0),
            status="paused",
            pause_reason=pause_reason,
            next_retry_at_ms=None,
            last_error=f"turn_{pause_reason.removeprefix('goal_turn_')}",
        )
    except GoalConflictError:
        return None
    log.info(
        "goal_driver.goal_parked",
        session_key=goal.session_key,
        goal_id=goal.goal_id,
        pause_reason=pause_reason,
    )
    return None


async def _record_enqueue_backoff(
    storage: Any,
    goal: Any,
    *,
    config: GoalConfig,
    now_ms: int,
) -> None:
    """Record an enqueue admission failure so the retry loop tries again."""

    retries = int(getattr(goal, "failure_retries", 0) or 0)
    if retries >= int(config.failure_retries):
        fields: dict[str, Any] = {
            "status": "paused",
            "pause_reason": "goal_enqueue_blocked",
            "next_retry_at_ms": None,
            "failure_retries": retries,
            "last_error": "enqueue_failed",
        }
        log.warning(
            "goal_driver.enqueue_blocked",
            session_key=goal.session_key,
            goal_id=goal.goal_id,
        )
    else:
        retry_at_ms = now_ms + goal_retry_delay_ms(
            retries,
            base_ms=int(config.retry_base_backoff_ms),
            max_ms=int(config.retry_max_backoff_ms),
        )
        fields = {
            "failure_retries": retries + 1,
            "next_retry_at_ms": retry_at_ms,
            "last_error": "enqueue_failed",
        }
        log.warning(
            "goal_driver.enqueue_backoff_scheduled",
            session_key=goal.session_key,
            goal_id=goal.goal_id,
            failure_retries=retries + 1,
            retry_at_ms=retry_at_ms,
        )
    try:
        await storage.update_goal_run(
            goal.goal_id,
            expected_updated_at=int(getattr(goal, "updated_at", 0) or 0),
            **fields,
        )
    except GoalConflictError:
        return None
    return None


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


async def _enqueue_goal_resume(
    runtime: Any,
    storage: Any,
    goal: Any,
    plan_run: Any,
    *,
    config: GoalConfig,
    now_ms: int,
) -> bool:
    """Enqueue one continuation turn for a resumable goal/plan-run pair.

    Applies the watcher gate (parks the goal as ``paused`` when nobody is
    watching and ``continue_unwatched`` is false), schedules a backoff when
    enqueue admission fails, and clears the retry marker on success.
    """

    if not config.continue_unwatched and not get_goal_watcher_registry().has_watchers(
        goal.session_key, ttl_ms=int(config.watcher_ttl_seconds) * 1000
    ):
        await _park_goal_unwatched(storage, goal, now_ms=now_ms)
        return False
    session = await storage.get_session(goal.session_key)
    envelope_seed = build_goal_route_envelope(
        session_key=goal.session_key,
        agent_id=goal.agent_id,
        session_id=(
            str(getattr(session, "session_id", "") or "")
            if session is not None
            else None
        ),
        goal_id=goal.goal_id,
        run_id=plan_run.run_id,
        plan_revision_id=str(getattr(plan_run, "plan_revision_id", "") or "") or None,
        source_name="goal_retry_loop",
    )
    handle = await enqueue_goal_continuation(
        runtime,
        session_key=goal.session_key,
        run_id=plan_run.run_id,
        goal_id=goal.goal_id,
        message=GOAL_CONTINUATION_MESSAGE,
        envelope_seed=envelope_seed,
    )
    if handle is None:
        await _record_enqueue_backoff(
            storage,
            goal,
            config=config,
            now_ms=now_ms,
        )
        return False
    try:
        await storage.update_goal_run(
            goal.goal_id,
            expected_updated_at=int(getattr(goal, "updated_at", 0) or 0),
            next_retry_at_ms=None,
        )
    except GoalConflictError:
        pass
    return True


async def _park_goal_unwatched(
    storage: Any,
    goal: Any,
    *,
    now_ms: int,
) -> None:
    """Park a running goal as paused because nobody is observing it."""

    try:
        await storage.update_goal_run(
            goal.goal_id,
            expected_updated_at=int(getattr(goal, "updated_at", 0) or 0),
            status="paused",
            pause_reason=_GOAL_PAUSE_REASON_UNWATCHED,
            next_retry_at_ms=None,
            last_turn_at=now_ms,
        )
    except GoalConflictError:
        return None
    log.info(
        "goal_driver.goal_parked_unwatched",
        session_key=goal.session_key,
        goal_id=goal.goal_id,
    )
    return None


async def drive_due_goal_retries(
    runtime: Any,
    storage: Any,
    *,
    config: GoalConfig,
    now_ms: int | None = None,
) -> int:
    """Enqueue continuation turns for every goal whose retry time has arrived.

    The retry loop and restart recovery call this; it never raises. Returns the
    number of turns actually enqueued.
    """

    now = now_ms if now_ms is not None else _now_ms()
    list_due = getattr(storage, "list_goal_runs_due_for_retry", None)
    get_plan_run = getattr(storage, "get_plan_run", None)
    if not callable(list_due) or not callable(get_plan_run):
        return 0
    try:
        goals = await list_due(now)
    except Exception:  # noqa: BLE001 - driver must never raise into the loop
        log.warning("goal_driver.retry_scan_failed", exc_info=True)
        return 0
    driven = 0
    for goal in goals:
        plan_run = (
            await get_plan_run(goal.plan_run_id)
            if goal.plan_run_id
            else None
        )
        if plan_run is None:
            continue
        if (
            str(getattr(plan_run, "status", "")) != "paused"
            or str(getattr(plan_run, "pause_reason", "") or "")
            not in _GOAL_RESUMABLE_PAUSE_REASONS
        ):
            continue
        try:
            resumed = await _enqueue_goal_resume(
                runtime,
                storage,
                goal,
                plan_run,
                config=config,
                now_ms=now,
            )
        except Exception:  # noqa: BLE001 - best-effort per goal
            log.warning(
                "goal_driver.retry_goal_failed",
                goal_id=goal.goal_id,
                session_key=goal.session_key,
                exc_info=True,
            )
            continue
        if resumed:
            driven += 1
            log.info(
                "goal_driver.retry_driven",
                session_key=goal.session_key,
                goal_id=goal.goal_id,
            )
    return driven


async def recover_goal_runs_after_restart(
    runtime: Any,
    storage: Any,
    *,
    config: GoalConfig,
) -> dict[str, int]:
    """Reconcile durable goal runs after a gateway restart.

    Watchers are in-memory and gone after restart, so a goal whose plan run is
    paused at a resumable anchor is:

    - parked ``paused`` (``pause_reason="goal_unwatched"``) when
      ``continue_unwatched`` is false — no silent token burn; the user resumes
      with ``/goal resume`` after reopening a chat;
    - enqueued immediately when ``continue_unwatched`` is true;
    - left for the retry loop when a retry is already scheduled.

    Returns ``{"recovered": n, "paused_unwatched": n}`` for boot logging.
    """

    list_active = getattr(storage, "list_active_goal_runs", None)
    get_plan_run = getattr(storage, "get_plan_run", None)
    if not callable(list_active) or not callable(get_plan_run):
        return {"recovered": 0, "paused_unwatched": 0}
    now = _now_ms()
    recovered = 0
    paused_unwatched = 0
    try:
        goals = await list_active()
    except Exception:  # noqa: BLE001 - recovery must not fail boot
        log.warning("goal_driver.restart_recovery_scan_failed", exc_info=True)
        return {"recovered": 0, "paused_unwatched": 0}
    repaired = await repair_goal_runs_with_completed_plan_runs(
        runtime,
        storage,
        config=config,
        goals=goals,
        now_ms=now,
    )
    for goal in goals:
        plan_run = (
            await get_plan_run(goal.plan_run_id)
            if goal.plan_run_id
            else None
        )
        if plan_run is None:
            continue
        if (
            str(getattr(plan_run, "status", "")) != "paused"
            or str(getattr(plan_run, "pause_reason", "") or "")
            not in _GOAL_RESUMABLE_PAUSE_REASONS
        ):
            continue
        if not config.continue_unwatched:
            if str(getattr(goal, "status", "")) == "running":
                await _park_goal_unwatched(storage, goal, now_ms=now)
                paused_unwatched += 1
            continue
        if getattr(goal, "next_retry_at_ms", None) is not None:
            continue  # the retry loop drives this once due
        try:
            resumed = await _enqueue_goal_resume(
                runtime,
                storage,
                goal,
                plan_run,
                config=config,
                now_ms=now,
            )
        except Exception:  # noqa: BLE001 - best-effort per goal
            log.warning(
                "goal_driver.restart_recovery_goal_failed",
                goal_id=goal.goal_id,
                session_key=goal.session_key,
                exc_info=True,
            )
            continue
        if resumed:
            recovered += 1
            log.info(
                "goal_driver.restart_recovered",
                session_key=goal.session_key,
                goal_id=goal.goal_id,
            )
    return {
        "recovered": recovered,
        "paused_unwatched": paused_unwatched,
        "repaired_completed_runs": repaired,
    }


async def repair_goal_runs_with_completed_plan_runs(
    runtime: Any,
    storage: Any,
    *,
    config: GoalConfig,
    goals: Any | None = None,
    now_ms: int | None = None,
) -> int:
    """Reconcile active goal runs whose plan run was completed out from under
    them.

    Historical settle paths could complete a goal-driven plan run at a turn
    boundary (all steps checkpointed) before the continuation driver ran; the
    driver only operates on paused runs, so the goal ledger row stranded as
    "running" forever and the UI ribbon never showed the terminal outcome.
    This scan reopens the run at its first step with the resumable
    ``goal_run_reopened`` anchor, resolves the last goal turn's marker from
    the transcript, and applies the same terminal/continuation decision the
    post-turn hook would have made.
    """

    now = now_ms if now_ms is not None else _now_ms()
    list_active = getattr(storage, "list_active_goal_runs", None)
    get_plan_run = getattr(storage, "get_plan_run", None)
    reopen = getattr(storage, "reopen_completed_plan_run", None)
    list_tasks = getattr(storage, "list_recent_agent_tasks", None)
    if not callable(list_active) or not callable(get_plan_run) or not callable(reopen):
        return 0
    if goals is None:
        try:
            goals = await list_active()
        except Exception:  # noqa: BLE001 - repair must never break its caller
            log.warning("goal_driver.completed_run_repair_scan_failed", exc_info=True)
            return 0
    repaired = 0
    for goal in goals:
        if str(getattr(goal, "status", "")) != "running":
            continue
        if not goal.plan_run_id:
            continue
        try:
            plan_run = await get_plan_run(goal.plan_run_id)
        except Exception:  # noqa: BLE001 - best-effort per goal
            continue
        if plan_run is None or str(getattr(plan_run, "status", "")) != "completed":
            continue
        try:
            reopened = await reopen(
                plan_run.run_id,
                expected_state_revision=int(getattr(plan_run, "state_revision", 0) or 0),
                reason=_GOAL_PAUSE_REASON_REOPENED,
            )
        except Exception:  # noqa: BLE001 - concurrent controller may have won
            continue
        repaired += 1
        await _apply_reopened_goal_decision(
            runtime,
            storage,
            goal=goal,
            plan_run=reopened,
            config=config,
            list_tasks=list_tasks,
            now_ms=now,
        )
    if repaired:
        log.info("goal_driver.completed_run_repaired", count=repaired)
    return repaired


async def _apply_reopened_goal_decision(
    runtime: Any,
    storage: Any,
    *,
    goal: Any,
    plan_run: Any,
    config: GoalConfig,
    list_tasks: Any,
    now_ms: int,
) -> None:
    """Apply the marker-driven decision for a reopened goal run."""

    task_id: str | None = None
    if callable(list_tasks):
        try:
            tasks = await list_tasks(goal.session_key, limit=8)
        except Exception:  # noqa: BLE001 - transcript fallback below
            tasks = []
        for task in tasks:
            details = getattr(task, "details", None)
            metadata = details.get("metadata") if isinstance(details, dict) else None
            metadata = metadata if isinstance(metadata, dict) else {}
            if str(metadata.get("plan_run_id") or "") != str(plan_run.run_id):
                continue
            task_id = str(getattr(task, "task_id", "") or "") or None
            break
    marker = None
    if task_id:
        marker_text = await _last_assistant_text(storage, goal.session_key, task_id)
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
    fields: dict[str, Any] = {
        "turns": int(getattr(goal, "turns", 0) or 0) + 1,
        "last_turn_at": now_ms,
        # A successfully finished turn starts a fresh transient-failure
        # budget. Otherwise an earlier provider/queue failure would keep
        # consuming the retry allowance after recovery.
        "failure_retries": 0,
        "next_retry_at_ms": None,
        "last_error": None,
    }
    fields["progress"] = _append_goal_progress(
        getattr(goal, "progress", None),
        turn_number=int(fields["turns"]),
        assistant_text=marker_text,
        marker=marker,
    )
    if marker is None:
        if advance.inject_prompt is not None:
            fields["idle_turns"] = 0
        else:
            fields["idle_turns"] = int(getattr(goal, "idle_turns", 0) or 0) + 1
    elif marker[0] in {"continue", "complete"}:
        fields["idle_turns"] = 0
    elif marker[0] == "blocked":
        reason_text = marker[1] or ""
        same_cause = (
            getattr(goal, "blocked_reason", None) is not None
            and str(getattr(goal, "blocked_reason", "") or "") == reason_text
        )
        retries_after = (
            int(getattr(goal, "blocked_retries", 0) or 0) + 1
            if same_cause
            else 1
        )
        fields["blocked_reason"] = reason_text
        fields["blocked_retries"] = retries_after
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
            return
        await _terminalize_plan_run(storage, plan_run, reason=plan_terminal_reason)
        log.info(
            "goal_driver.goal_terminal",
            session_key=goal.session_key,
            run_id=plan_run.run_id,
            goal_id=goal.goal_id,
            status=fields["status"],
            terminal_reason=terminal_reason,
            turns=fields["turns"],
        )
        return

    try:
        updated_goal = await storage.update_goal_run(
            goal.goal_id,
            expected_updated_at=int(getattr(goal, "updated_at", 0) or 0),
            **fields,
        )
    except GoalConflictError:
        return

    # No terminal marker: preserve the same watcher gate and idle nudge policy
    # as the normal post-turn path.
    if config.continue_unwatched or get_goal_watcher_registry().has_watchers(
        goal.session_key, ttl_ms=int(config.watcher_ttl_seconds) * 1000
    ):
        session = await storage.get_session(goal.session_key)
        envelope_seed = build_goal_route_envelope(
            session_key=goal.session_key,
            agent_id=goal.agent_id,
            session_id=(
                str(getattr(session, "session_id", "") or "")
                if session is not None
                else None
            ),
            goal_id=goal.goal_id,
            run_id=plan_run.run_id,
            plan_revision_id=str(getattr(plan_run, "plan_revision_id", "") or "") or None,
            source_name="goal_completed_run_repair",
        )
        message = GOAL_CONTINUATION_MESSAGE
        if advance.inject_prompt:
            message = f"{message}\n\n{advance.inject_prompt}"
        handle = await enqueue_goal_continuation(
            runtime,
            session_key=goal.session_key,
            run_id=plan_run.run_id,
            goal_id=goal.goal_id,
            message=message,
            envelope_seed=envelope_seed,
        )
        if handle is None:
            await _record_enqueue_backoff(
                storage,
                updated_goal,
                config=config,
                now_ms=now_ms,
            )
        return

    try:
        await storage.update_goal_run(
            goal.goal_id,
            expected_updated_at=int(getattr(updated_goal, "updated_at", 0) or 0),
            status="paused",
            pause_reason=_GOAL_PAUSE_REASON_UNWATCHED,
            next_retry_at_ms=None,
        )
    except GoalConflictError:
        return

    return


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)
