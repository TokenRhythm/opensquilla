"""In-process task runtime for agent turns.

Lock ordering invariant:
    TaskRuntime owns two per-session lock classes used by gateway-dispatched turns.
    Gateway construction injects ``TaskRuntime._get_session_lock_for_turn`` as
    TurnRunner's ``session_lock_provider``. That provider returns the short
    write lock for transcript/session state mutation.

    ``TaskRuntime._execute()`` acquires a separate execution lock before
    calling the turn handler. ``TurnRunner.run()`` detects that TaskRuntime is
    already serializing the turn lifecycle and skips the old coarse acquire;
    TurnRunner append adapters still acquire the short write lock.

    CLI or standalone TurnRunner instances may use a different provider, but
    they are not nested inside TaskRuntime execution. Keep external I/O outside
    the short write lock.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import inspect
import json
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypeVar, cast

import structlog

from opensquilla.engine.agent_injection import PendingInputClaim, PendingInputProvider
from opensquilla.engine.outcome import completed_outcome, outcome_from_error
from opensquilla.engine.steps.inject_time_prefix import TIME_PREFIX_RE
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.session_lifecycle import (
    SessionTaskSnapshot,
    TaskLifecycleEvent,
    TaskLifecycleListener,
)
from opensquilla.gateway.terminal_activity import (
    is_usage_accounting_barrier,
    safe_primary_user_message_id,
    safe_retry_after_ms,
    terminal_activity_snapshot,
    usage_barrier_replay_proof,
)
from opensquilla.safety.injection_guard import xml_escape
from opensquilla.session.goals import (
    GOAL_OBJECTIVE_UPDATE_DETAIL_KEY,
    GoalObjectiveUpdate,
    effective_goal_turn_context,
)
from opensquilla.session.keys import canonicalize_session_key, normalize_agent_id, parse_agent_id
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus, QueueMode
from opensquilla.session.terminal_reply import (
    build_terminal_reply,
    is_context_payload_too_large,
    safe_provider_failure_code,
    safe_provider_failure_message,
    sanitize_agent_error,
)

if TYPE_CHECKING:
    from opensquilla.provider.types import ProviderRequestCorrelation

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Core metrics — names are LOCKED. Do not rename without updating
# README "Observability: Core Metrics" and the corresponding CI grep.
#   opensquilla_queue_depth   (gauge)   — pending queue depth per session
#   in_flight_turns_total     (counter) — cumulative turns entering _execute
#   turn_cancellations_total  (counter) — cumulative cancel/interrupt/timeout
#   queue_full_errors_total   (counter) — cumulative TaskQueueFullError raises
# ---------------------------------------------------------------------------


def _emit_metric(name: str, value: int = 1, **labels: Any) -> None:
    """Emit a structured log line for a core metric.

    Format: event=<name> metric=<name> value=<int> [labels...]
    Grep pattern: ``metric=<name>``
    """
    log.info(name, metric=name, value=value, **labels)


TERMINAL_STATUSES = frozenset(
    {
        AgentTaskStatus.SUCCEEDED,
        AgentTaskStatus.FAILED,
        AgentTaskStatus.CANCELLED,
        AgentTaskStatus.TIMEOUT,
        AgentTaskStatus.ABANDONED,
    }
)

TaskStreamEventSink = Callable[[Any], Awaitable[None]]
TaskActivationListener = Callable[
    [str, str, str, str, Mapping[str, Any]],
    Awaitable[Mapping[str, Any] | None],
]
RuntimeIdleListener = Callable[[str], Awaitable[None]]
_CollectResult = TypeVar("_CollectResult")
_MISSING_GOAL_ACCEPTANCE = object()


async def _complete_terminal_settlement[T](awaitable: Awaitable[T]) -> T:
    """Finish accepted-input settlement even if the task is cancelled again."""

    task = asyncio.ensure_future(awaitable)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


def _task_identity_payload(
    envelope: RouteEnvelope,
    task_id: str,
    *,
    user_message_id: str | None = None,
) -> dict[str, str]:
    payload = {"turn_id": task_id}
    session_id = getattr(envelope, "session_id", None)
    if isinstance(session_id, str) and session_id:
        payload["session_id"] = session_id
    metadata = getattr(envelope, "metadata", None)
    if isinstance(metadata, dict):
        for field in ("client_message_id", "surface_id"):
            value = metadata.get(field)
            if isinstance(value, str) and value:
                payload[field] = value
    if isinstance(user_message_id, str) and user_message_id:
        payload["user_message_id"] = user_message_id
    return payload


def _accepted_run_mode_payload(override: Any) -> dict[str, str] | None:
    from opensquilla.gateway.project_workspace_runtime import AcceptedRunModeOverride

    if not isinstance(override, AcceptedRunModeOverride):
        return None
    payload = {"run_mode": override.run_mode.value}
    if isinstance(override.run_mode_source, str) and override.run_mode_source:
        payload["run_mode_source"] = override.run_mode_source
    return payload


def _reusable_route_envelope(envelope: RouteEnvelope) -> RouteEnvelope:
    """Detach one route for reuse without execution-scoped freshness."""

    metadata = dict(envelope.metadata)
    if metadata.get("guest_safe") is True:
        for key in (
            "guest_profile_root",
            "guest_managed_root",
            "guest_environment",
            "sandbox_mounts",
            "sandbox_run_context",
        ):
            metadata.pop(key, None)
    return replace(
        envelope,
        metadata=metadata,
        sandbox_run_context_fresh=False,
    )


def _materialize_guest_task_envelope(
    envelope: RouteEnvelope,
    task_id: str,
) -> RouteEnvelope:
    """Attach one new process-local guest profile to an execution envelope."""

    if envelope.metadata.get("guest_safe") is not True:
        return envelope
    existing_root = envelope.metadata.get("guest_profile_root")
    if isinstance(existing_root, str) and existing_root:
        return envelope
    factory = envelope.runtime_services.get("guest_profile_factory")
    if not callable(factory):
        from opensquilla.sandbox.guest_profile import GuestProfileBoundaryError

        raise GuestProfileBoundaryError(
            f"{GuestProfileBoundaryError.code}: guest runtime profile factory is unavailable"
        )
    profile = factory(task_id)
    if profile is None:
        from opensquilla.sandbox.guest_profile import GuestProfileBoundaryError

        raise GuestProfileBoundaryError(
            f"{GuestProfileBoundaryError.code}: guest runtime profile is unavailable"
        )
    run_context_payload = profile.run_context().to_origin_payload()
    metadata = {
        **envelope.metadata,
        "guest_profile_root": str(profile.root),
        "guest_managed_root": str(profile.managed_root),
        "guest_environment": dict(profile.environment),
        "sandbox_mounts": run_context_payload["mounts"],
        "sandbox_run_context": run_context_payload,
    }
    return replace(
        envelope,
        metadata=metadata,
        sandbox_run_context_fresh=True,
    )


@dataclass(frozen=True)
class TaskHandle:
    task_id: str
    session_key: str
    status: AgentTaskStatus


@dataclass(frozen=True, slots=True)
class TaskRuntimeShutdownResult:
    """Observable outcome of one process-lifetime runtime shutdown."""

    clean: bool
    elapsed_ms: int
    abandoned_task_count: int
    remaining_driver_count: int
    remaining_reservation_count: int
    remaining_auxiliary_count: int


@dataclass(frozen=True)
class SteerAdmissionResult:
    """Result of one expected-turn same-turn input admission."""

    accepted: bool
    task_id: str | None = None
    persisted: Any | None = None
    failure_code: str | None = None
    capability: dict[str, Any] | None = None


@dataclass
class TaskReservation:
    """Reversible in-memory admission held until durable acceptance commits."""

    reservation_id: str
    task_record: AgentTaskRecord
    runtime_task: _RuntimeTask
    overflow_victim: _RuntimeTask | None = None
    update_envelope_cache: bool = True
    activated: bool = False
    aborted: bool = False
    queued_notification_pending: bool = False
    activation_queue_depth: int | None = None
    activation_queue_position: int | None = None

    @property
    def task_id(self) -> str:
        return self.task_record.task_id

    @property
    def session_key(self) -> str:
        return self.task_record.session_key

    @property
    def status(self) -> AgentTaskStatus:
        return self.task_record.status


@dataclass(frozen=True)
class _SteerPromotionResult:
    task_id: str
    deferred_notification: TaskReservation | None = None


@dataclass(frozen=True)
class TaskRun:
    task_id: str
    envelope: RouteEnvelope
    message: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    queue_mode: str = "followup"
    run_kind: str = "default"
    no_memory_capture: bool = False
    # Per-call ingress observability. Lives here, NOT on
    # ``envelope.metadata``, so the cached envelope in
    # ``_last_envelope_by_session`` cannot leak stale ingress markers into
    # later runtime sends (e.g. ``TaskRuntime.send`` reusing the cache).
    ingress_pipeline_steps: tuple[Any, ...] = ()
    # Raw user text used by semantic runtime processing when the runtime path
    # needs to diverge from ``message``. Channels
    # set this to the pre-stamping content; web/CLI leave it ``None`` so
    # ``TurnRunner.run`` falls back to ``message`` as the semantic input.
    semantic_message: str | None = None
    # Optional transcript entry id for the user message already persisted by
    # the ingress surface. Kept off RouteEnvelope.metadata so cached envelopes
    # cannot leak stale one-turn ids into later runtime sends.
    persisted_user_message_id: str | None = None
    # Every persisted user entry folded into this run, in transcript order.
    # ``persisted_user_message_id`` remains the earliest id and therefore the
    # history boundary; this collection is used for multi-message cleanup when
    # the provider rejects the request before a turn can start.
    persisted_user_message_ids: tuple[str, ...] = ()
    # True when the ingress surface observed an empty user transcript before
    # persisting this turn's user message.
    fresh_user_session: bool = False
    # Optional in-process sink for the structured events produced by this
    # specific task's turn stream. Used by channel delivery to mirror the
    # same live text stream that WebUI already receives without changing
    # the public WS event payload.
    stream_event_sink: TaskStreamEventSink | None = None
    pending_input_provider: PendingInputProvider | None = None
    # Immutable-at-acceptance projection used by the Gateway turn handler.
    # This is deliberately off RouteEnvelope.metadata so cached envelopes can
    # never leak one turn's strategy into a later proactive send.
    accepted_config: Any | None = None
    # Ingress-vetted per-turn run-mode selection. This remains off mutable
    # RouteEnvelope.metadata so execution cannot manufacture authority from an
    # arbitrary or cached envelope.
    accepted_run_mode_override: Any | None = None
    provider_request_correlation: ProviderRequestCorrelation | None = field(
        default=None,
        repr=False,
    )
    # Synchronous finalizer callback carrying the exact assistant transcript
    # row and content produced by this turn. Channel tasks persist it for
    # durable delivery after terminal commit; other run kinds leave it unset.
    assistant_message_sink: Callable[[str | None, str], None] | None = None
    input_mode: str = "user"
    persist_input: bool = False
    history_has_persisted_user: bool = True
    goal_context: Mapping[str, Any] | None = field(default=None, repr=False)

    @property
    def session_key(self) -> str:
        return self.envelope.session_key

    @property
    def agent_id(self) -> str:
        return self.envelope.agent_id

    @property
    def input_provenance(self) -> dict[str, Any]:
        return self.envelope.input_provenance


@dataclass(frozen=True)
class SubagentCompletionEvent:
    """Terminal event for a runtime-backed subagent task."""

    parent_session_key: str
    child_session_key: str
    task_id: str
    status: AgentTaskStatus
    terminal_reason: str
    agent_id: str | None = None
    parent_task_id: str | None = None
    error_class: str | None = None
    error_message: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "subagent_completion",
            "parent_session_key": self.parent_session_key,
            "child_session_key": self.child_session_key,
            "task_id": self.task_id,
            "status": self.status.value,
            "terminal_reason": self.terminal_reason,
        }
        if self.agent_id:
            payload["agent_id"] = self.agent_id
        if self.parent_task_id:
            payload["parent_task_id"] = self.parent_task_id
        if self.error_class:
            payload["error_class"] = self.error_class
        if self.error_message:
            payload["error_message"] = self.error_message
        if self.status != AgentTaskStatus.SUCCEEDED:
            payload["terminal_message"] = build_terminal_reply(payload)
        return payload


@dataclass(frozen=True)
class _CollectedPrimaryInput:
    """One durable prompt coalesced into an already queued collect turn."""

    persisted_user_message_id: str | None
    client_request_id: str | None
    client_message_id: str | None
    surface_id: str | None
    intent: str = "send"
    revision: int = 2


@dataclass
class _RuntimeTask:
    task_id: str
    envelope: RouteEnvelope
    message: str
    attachments: list[dict[str, Any]]
    queue_mode: str
    run_kind: str
    no_memory_capture: bool
    input_mode: str = "user"
    persist_input: bool = False
    history_has_persisted_user: bool = True
    goal_context: dict[str, Any] | None = None
    goal_candidate: dict[str, Any] | None = None
    goal_steer_candidate: dict[str, Any] | None = None
    pending_input_provider: _SteerPendingInputProvider = field(
        default_factory=lambda: _SteerPendingInputProvider()
    )
    status: AgentTaskStatus = AgentTaskStatus.QUEUED
    asyncio_task: asyncio.Task[None] | None = None
    ingress_pipeline_steps: tuple[Any, ...] = ()
    semantic_message: str | None = None
    persisted_user_message_id: str | None = None
    persisted_user_message_ids: list[str] = field(default_factory=list)
    message_count: int = 1
    fresh_user_session: bool = False
    terminal_assistant_message_id: str | None = None
    terminal_assistant_message_content: str | None = None
    stream_event_sink: TaskStreamEventSink | None = None
    accepted_config: Any | None = None
    provider_request_correlation: ProviderRequestCorrelation | None = field(
        default=None,
        repr=False,
    )
    accepted_config_captured: bool = False
    accepted_run_mode_override: Any | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    # Claimed synchronously before terminal persistence begins. This closes
    # admission while accepted inputs and the public handoff are still settling.
    terminal_settling: bool = False
    # Set only after the public terminal event boundary completes successfully.
    terminal_emitted: bool = False
    # Final idempotency fence, including observer-failure cleanup paths.
    terminal_settled: bool = False
    cancel_requested: bool = False
    execution_started: bool = False
    guest_profile_cleaned: bool = False
    acquired_slot: bool = False
    overflow_dropped: bool = False
    cancel_source: str | None = None
    cancel_reason: str | None = None
    # True only while an identity-aware primary input is still represented as
    # ``queued`` in transcript/live state.  The flag is cleared when execution
    # publishes ``applied`` or when a terminal path claims the input for a
    # canonical ``cancelled``/``rejected`` transition.
    primary_input_pending: bool = False
    # Public ``queueMode=collect`` may bind several durable prompt rows to one
    # runtime turn. Keep every additional identity until it reaches an applied
    # or terminal disposition so hydrate and live projection stay equivalent.
    collected_primary_inputs: list[_CollectedPrimaryInput] = field(default_factory=list)
    # Serializes collect persistence/application with this task's running and
    # terminal transitions. It is intentionally per-task: a slow SQLite write
    # for one session must never hold the runtime-wide state lock.
    collect_claim: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # Serializes same-turn durable acceptance with cancellation and terminal
    # closure. The storage transaction is intentionally outside _state_lock,
    # while this per-task gate prevents a terminal path from overtaking it.
    steer_claim: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def terminal_closing(self) -> bool:
        """Whether terminalization has been claimed or publicly completed."""

        return self.terminal_settling or self.terminal_emitted or self.terminal_settled

    def capture_terminal_assistant_message(
        self,
        message_id: str | None,
        content: str,
    ) -> None:
        self.terminal_assistant_message_id = message_id
        self.terminal_assistant_message_content = content


@dataclass
class _IngressIntentState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    count: int = 0
    borrowers: int = 0


@dataclass(slots=True)
class _ExplicitIngressIntentLease:
    """Exactly-once handle for user intent that outlives one coroutine frame."""

    runtime: TaskRuntime
    session_key: str
    state: _IngressIntentState
    released: bool = False
    _release_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _release_task: asyncio.Task[None] | None = field(default=None, repr=False)

    async def release(self) -> None:
        """Release the registered intent once, even across cancel/fire races."""

        async with self._release_lock:
            if self._release_task is None:
                self.released = True
                self._release_task = asyncio.create_task(
                    self.runtime._release_explicit_ingress_intent(
                        self.session_key,
                        self.state,
                    )
                )
            release_task = self._release_task
        # A request/debounce coroutine can be cancelled while unwinding its
        # finally block. Shield the shared decrement so cancellation cannot
        # strand a positive intent count and suppress Goal continuation forever.
        await asyncio.shield(release_task)


@dataclass(slots=True)
class _CollectAdmissionLockState:
    """One reclaimable per-session durable-admission gate."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    borrowers: int = 0


def _cleanup_guest_profile(task: _RuntimeTask) -> None:
    """Remove one task-owned guest profile exactly once."""

    if task.guest_profile_cleaned:
        return
    task.guest_profile_cleaned = True
    guest_profile_root = task.envelope.metadata.get("guest_profile_root")
    guest_managed_root = task.envelope.metadata.get("guest_managed_root")
    if not (
        isinstance(guest_profile_root, str)
        and guest_profile_root
        and isinstance(guest_managed_root, str)
        and guest_managed_root
    ):
        return
    from opensquilla.sandbox.guest_profile import cleanup_guest_profile_root

    cleanup_guest_profile_root(
        guest_profile_root,
        managed_root=guest_managed_root,
    )


@dataclass(frozen=True)
class _SteeredInput:
    text: str
    semantic_message: str | None = None
    persisted_user_message_id: str | None = None
    client_request_id: str | None = None
    client_message_id: str | None = None
    surface_id: str | None = None
    accepted_at_ms: int | None = None
    applied_iteration: int | None = None
    model_call_id: str | None = None


def _render_goal_objective_update(update: GoalObjectiveUpdate) -> str:
    """Render a non-transcript Goal control for one safe provider boundary."""

    context = update.context
    return (
        "[Persisted Goal objective update]\n"
        "This is internal Goal control, not a new user message. Re-evaluate the "
        "whole objective and adjust the current turn at this safe boundary. Do not "
        "treat prior progress as completion evidence for requirements that changed.\n"
        f'<goal_objective revision="{context.objective_revision}">\n'
        f"{xml_escape(context.objective_snapshot)}\n"
        "</goal_objective>"
    )


class _SteerPendingInputProvider:
    """Pending-input provider that can reclaim an undrained late steer.

    Agent drains this provider only at a safe tool-result boundary. If a turn
    finishes before reaching another boundary, TaskRuntime promotes the
    remaining item into a normal follow-up task so an accepted user message is
    never silently lost.
    """

    def __init__(self) -> None:
        self._pending: list[_SteeredInput] = []
        self._claimed: list[_SteeredInput] = []
        self._applied: list[_SteeredInput] = []
        self._goal_pending: GoalObjectiveUpdate | None = None
        self._goal_claimed: GoalObjectiveUpdate | None = None
        self._last_applied_goal_context: dict[str, Any] | None = None
        self._goal_lock = asyncio.Lock()
        self._applied_recorder: (
            Callable[
                [Sequence[_SteeredInput]],
                Awaitable[Sequence[_SteeredInput]],
            ]
            | None
        ) = None
        self._goal_claim_binder: (
            Callable[[GoalObjectiveUpdate], Awaitable[GoalObjectiveUpdate | None]]
            | None
        ) = None
        self._goal_applied_recorder: (
            Callable[
                [GoalObjectiveUpdate, int, str],
                Awaitable[GoalObjectiveUpdate | None],
            ]
            | None
        ) = None

    def set_applied_recorder(
        self,
        recorder: Callable[
            [Sequence[_SteeredInput]],
            Awaitable[Sequence[_SteeredInput]],
        ],
    ) -> None:
        """Attach the runtime callback that durably publishes application."""

        self._applied_recorder = recorder

    def set_goal_objective_recorders(
        self,
        *,
        claim_binder: Callable[
            [GoalObjectiveUpdate], Awaitable[GoalObjectiveUpdate | None]
        ],
        applied_recorder: Callable[
            [GoalObjectiveUpdate, int, str],
            Awaitable[GoalObjectiveUpdate | None],
        ],
    ) -> None:
        """Attach durable validation/application callbacks for Goal edits."""

        self._goal_claim_binder = claim_binder
        self._goal_applied_recorder = applied_recorder

    def append(self, item: _SteeredInput) -> None:
        if item.text.strip():
            self._pending.append(item)

    async def append_goal_objective_update(self, update: GoalObjectiveUpdate) -> None:
        """Coalesce unconsumed Goal edits to the newest objective revision."""

        async with self._goal_lock:
            current = self._goal_pending
            if (
                current is None
                or update.context.objective_revision
                >= current.context.objective_revision
            ):
                self._goal_pending = update

    def peek_pending(self) -> list[str]:
        """Return the next FIFO batch without claiming or mutating it."""

        if self._claimed or self._goal_claimed is not None:
            return []
        values: list[str] = []
        if self._goal_pending is not None:
            values.append(_render_goal_objective_update(self._goal_pending))
        values.extend(item.text for item in self._pending)
        return values

    def drain_pending(self) -> list[str]:
        if self._claimed:
            return []
        items = list(self._pending)
        self._pending = []
        # Draining only claims the input for construction of a later provider
        # request. It is not ``applied`` until the engine confirms that request
        # has actually started through ``mark_applied``.
        self._claimed.extend(items)
        return [item.text for item in items]

    async def claim_pending(self) -> PendingInputClaim:
        """Claim user steer plus a durably validated Goal objective update."""

        # Freeze the ordinary batch before the first await. Inputs arriving
        # while Goal authority is validated remain pending for a later safe
        # boundary instead of bypassing the previewed context budget.
        user_texts = self.drain_pending()
        goal_update: GoalObjectiveUpdate | None = None
        async with self._goal_lock:
            if self._goal_claimed is None and self._goal_pending is not None:
                candidate = self._goal_pending
                binder = self._goal_claim_binder
                bound = await binder(candidate) if binder is not None else None
                if bound is not None:
                    goal_update = bound
                    self._goal_claimed = bound
                if self._goal_pending is candidate:
                    self._goal_pending = None
        texts: list[str] = []
        if goal_update is not None:
            texts.append(_render_goal_objective_update(goal_update))
        texts.extend(user_texts)
        return PendingInputClaim(
            texts=tuple(texts),
            goal_context=(
                goal_update.context.as_task_detail()
                if goal_update is not None
                else None
            ),
        )

    def mark_applied(
        self,
        *,
        iteration: int,
        model_call_id: str,
    ) -> Awaitable[None] | None:
        """Confirm and immediately publish inputs that entered a provider call."""

        goal_update = self._goal_claimed
        self._goal_claimed = None
        self._last_applied_goal_context = None
        if not self._claimed and goal_update is None:
            return None
        items = [
            replace(
                item,
                applied_iteration=iteration,
                model_call_id=model_call_id,
            )
            for item in self._claimed
        ]
        self._claimed = []
        self._applied.extend(items)
        applied_recorder = self._applied_recorder
        goal_recorder = self._goal_applied_recorder
        if applied_recorder is None and (goal_update is None or goal_recorder is None):
            return None

        async def _record_and_acknowledge() -> None:
            if applied_recorder is not None and items:
                acknowledged = await applied_recorder(items)
                item_ids = {id(item) for item in acknowledged}
                self._applied = [
                    item for item in self._applied if id(item) not in item_ids
                ]
            if goal_update is not None and goal_recorder is not None:
                applied = await goal_recorder(goal_update, iteration, model_call_id)
                if applied is not None:
                    self._last_applied_goal_context = (
                        applied.context.as_task_detail()
                    )

        return _record_and_acknowledge()

    def take_applied_goal_context(self) -> dict[str, Any] | None:
        """Consume Goal authority durably applied to the started model call."""

        applied = self._last_applied_goal_context
        self._last_applied_goal_context = None
        return dict(applied) if applied is not None else None

    def pending_applied(self) -> list[_SteeredInput]:
        """Return applied inputs still awaiting durable acknowledgement."""

        return list(self._applied)

    def acknowledge_applied(
        self,
        items: Sequence[_SteeredInput],
    ) -> None:
        """Forget only applications whose durable transition succeeded."""

        item_ids = {id(item) for item in items}
        self._applied = [
            item for item in self._applied if id(item) not in item_ids
        ]

    def reclaim_pending(self) -> list[_SteeredInput]:
        pending = [*self._claimed, *self._pending]
        self._claimed = []
        self._pending = []
        self._goal_claimed = None
        self._goal_pending = None
        self._last_applied_goal_context = None
        return pending

    def reclaim_drained(self) -> list[_SteeredInput]:
        applied = list(self._applied)
        self._applied = []
        return applied

    def reclaim_all(self) -> list[_SteeredInput]:
        items = [*self._applied, *self._claimed, *self._pending]
        self._applied = []
        self._claimed = []
        self._pending = []
        self._goal_claimed = None
        self._goal_pending = None
        self._last_applied_goal_context = None
        return items

    async def revoke_goal_objective_updates(self) -> None:
        """Revoke future Goal adoption without recalling current-task input.

        A durable claim may already have been returned to the Agent and staged
        in its next provider request. Clearing this provider bookkeeping keeps
        a late ``mark_applied`` from advancing Goal authority, but deliberately
        does not attempt to rewrite an already assembled or started request.
        """

        async with self._goal_lock:
            self._goal_claimed = None
            self._goal_pending = None
            self._last_applied_goal_context = None

    def reject_claimed_goal_context(self) -> None:
        """Release a claim rejected by the Agent's in-memory identity fence."""

        self._goal_claimed = None
        self._last_applied_goal_context = None


TaskHandler = Callable[[TaskRun], Awaitable[Any]]
EventEmitter = Callable[[str, str, dict[str, Any]], Awaitable[None]]
TerminalListener = Callable[[SubagentCompletionEvent], Awaitable[None]]


def _ordered_message_ids(
    primary: str | None,
    message_ids: Iterable[str] | None = None,
) -> list[str]:
    """Return stable, non-empty, de-duplicated persisted message ids."""

    ordered: list[str] = []
    for value in (primary, *(message_ids or ())):
        if isinstance(value, str) and value and value not in ordered:
            ordered.append(value)
    return ordered


def _recover_meta_control_message(content: object) -> str | None:
    """Recover provider text from an accepted text-only control transcript."""

    if not isinstance(content, str) or not content:
        return None
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        # Plain user entries receive the standard timestamp prefix after the
        # provider-facing text is captured. Remove only that exact prefix.
        return TIME_PREFIX_RE.sub("", content, count=1)
    if not isinstance(parsed, dict):
        return TIME_PREFIX_RE.sub("", content, count=1)
    text = parsed.get("text")
    attachments = parsed.get("attachments")
    # MetaSkill launch and replay controls are text-only. Anything else is a
    # corrupted or mismatched recovery row and must fail closed.
    if not isinstance(text, str) or attachments != []:
        return None
    return text


class PendingOverflowPolicy(StrEnum):
    """Per-session pending queue overflow policy.

    ``REJECT_NEWEST``
        Default — refuse the new enqueue with ``TaskQueueFullError``.
        Backwards compatible behaviour.

    ``DROP_OLDEST``
        Evict the oldest QUEUED pending task on the same session, mark it
        ``CANCELLED`` with ``terminal_reason="dropped_by_overflow"``, and
        accept the new enqueue. Running tasks are never evicted.
    """

    REJECT_NEWEST = "reject_newest"
    DROP_OLDEST = "drop_oldest"


class TaskQueueFullError(RuntimeError):
    """Raised when a session's waiting queue reaches its configured limit."""

    def __init__(self, *, session_key: str, max_pending: int) -> None:
        super().__init__(
            f"task queue overflow for session '{session_key}': "
            f"max_pending_per_session={max_pending}"
        )
        self.session_key = session_key
        self.max_pending = max_pending


class TaskRuntimeShuttingDownError(RuntimeError):
    """Raised when new root work arrives after shutdown admission closes."""

    def __init__(self, *, session_key: str) -> None:
        super().__init__(f"task runtime is shutting down; rejected session '{session_key}'")
        self.session_key = session_key


class _CollectIdentityRebindError(RuntimeError):
    """Legacy collect could not durably bind its prompt to the queued turn."""


class _GoalPromptContextUnavailableError(RuntimeError):
    """Authoritative Goal prompt state could not be checked before execution."""

    code = "goal_prompt_context_unavailable"
    terminal_reason = "goal_prompt_context_unavailable"


class _TurnHardDeadlineExceeded(TimeoutError):  # noqa: N818
    """Internal breaker error raised when a turn exceeds its hard deadline.

    Subclasses TimeoutError so legacy ``except TimeoutError`` paths still
    classify the run as timed out, but the dedicated type lets the runtime
    annotate the terminal record with the breaker-specific reason.
    """

    def __init__(self, *, deadline_s: float) -> None:
        super().__init__(f"turn exceeded hard deadline of {deadline_s:g}s")
        self.deadline_s = deadline_s


def _clean_cancel_detail(value: str | None, default: str) -> str:
    text = str(value or "").strip()
    if not text:
        return default
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-", ".", ":"} else "_" for ch in text)
    return (safe.strip("_") or default)[:80]


class TaskRuntime:
    """Serialize same-session turns while allowing cross-session concurrency.

    Gateway lock invariant:
        ``self._session_execution_locks`` serializes task execution for a
        session. ``self._session_locks`` stores the short critical-section
        locks shared with TurnRunner and RPC ingress through
        ``_get_session_lock_for_turn``.

        The write lock serializes transcript/session mutations; it must not
        cover model streaming, tool execution, slot waits, or approval waits.
    """

    supported_queue_modes = frozenset(mode.value for mode in QueueMode)

    @classmethod
    def supports_queue_mode(cls, mode: str) -> bool:
        """Return whether ``enqueue`` implements the exact queue-mode value."""
        return mode in cls.supported_queue_modes

    def __init__(
        self,
        *,
        storage: Any,
        turn_handler: TaskHandler,
        event_emitter: EventEmitter | None = None,
        terminal_listener: TerminalListener | None = None,
        lifecycle_listener: TaskLifecycleListener | None = None,
        max_concurrency: int = 8,
        max_pending_per_session: int | None = 64,
        subagent_reserved_slots: int = 0,
        turn_hard_deadline_s: float | None = None,
        running_heartbeat_interval_s: float | None = 30.0,
        accepted_config_provider: Callable[[], Any] | None = None,
        pending_overflow_policy: PendingOverflowPolicy | str = (
            PendingOverflowPolicy.REJECT_NEWEST
        ),
        activation_listener: TaskActivationListener | None = None,
        idle_listener: RuntimeIdleListener | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if max_pending_per_session is not None and max_pending_per_session < 1:
            raise ValueError("max_pending_per_session must be >= 1")
        if subagent_reserved_slots < 0:
            raise ValueError("subagent_reserved_slots must be >= 0")
        if turn_hard_deadline_s is not None and turn_hard_deadline_s <= 0:
            raise ValueError("turn_hard_deadline_s must be > 0 or None")
        if running_heartbeat_interval_s is not None and running_heartbeat_interval_s <= 0:
            raise ValueError("running_heartbeat_interval_s must be > 0 or None")
        try:
            pending_overflow_policy = PendingOverflowPolicy(pending_overflow_policy)
        except ValueError as exc:
            valid = ", ".join(member.value for member in PendingOverflowPolicy)
            raise ValueError(f"pending_overflow_policy must be one of {{{valid}}}") from exc
        # Clamp so subagents can always acquire eventually. A reservation that
        # consumes the entire pool would deadlock the subagent lane.
        if subagent_reserved_slots >= max_concurrency:
            import structlog

            structlog.get_logger("opensquilla.gateway.task_runtime").warning(
                "task_runtime.subagent_reserved_slots_clamped",
                requested=subagent_reserved_slots,
                max_concurrency=max_concurrency,
                clamped_to=max(0, max_concurrency - 1),
            )
            subagent_reserved_slots = max(0, max_concurrency - 1)
        self._storage = storage
        self._turn_handler = turn_handler
        self._event_emitter = event_emitter
        self._terminal_listener = terminal_listener
        self._lifecycle_listener = lifecycle_listener
        self._max_pending_per_session = max_pending_per_session
        self._max_concurrency = max_concurrency
        self._subagent_reserved_slots = subagent_reserved_slots
        self._turn_hard_deadline_s = turn_hard_deadline_s
        self._running_heartbeat_interval_s = running_heartbeat_interval_s
        self._accepted_config_provider = accepted_config_provider
        self._pending_overflow_policy = pending_overflow_policy
        self._activation_listener = activation_listener
        self._idle_listener = idle_listener
        self._goal_service: Any | None = None
        from opensquilla.gateway.user_input_broker import StructuredUserInputBroker

        self._user_input_broker = StructuredUserInputBroker()
        # Per-session write locks shared with TurnRunner and RPC ingress on
        # gateway-dispatched turns. These guard short transcript/session state
        # mutations only.
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Per-session execution locks serialize whole turn lifecycles without
        # blocking transcript writes, browser queue acknowledgements, or approval
        # status updates behind external I/O.
        self._session_execution_locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[str, _RuntimeTask] = {}
        # Driver tasks remain tracked until their whole coroutine returns.
        # ``_mark_terminal`` intentionally sets ``task.done`` before the
        # subagent notification tail, so waiters that must fence every possible
        # follow-up write cannot rely on the durable-task event alone.
        self._driver_tasks_by_session: dict[str, set[asyncio.Task[None]]] = {}
        self._driver_state_changed = asyncio.Event()
        self._closing = False
        self._shutdown_task: asyncio.Task[TaskRuntimeShutdownResult] | None = None
        self._terminal_fallback_records: dict[str, AgentTaskRecord] = {}
        self._pending_by_session: dict[str, list[_RuntimeTask]] = {}
        self._running_by_session: dict[str, _RuntimeTask] = {}
        self._reservations_by_session: dict[str, list[TaskReservation]] = {}
        # Low-priority, non-durable provider work (currently prompt-cache
        # keepalive). A real enqueue cancels this task before reserving its
        # turn, so auxiliary work can never make user input wait for network I/O.
        self._auxiliary_tasks_by_session: dict[str, asyncio.Task[Any]] = {}
        self._auxiliary_slot = asyncio.Semaphore(1)
        self._reserved_overflow_victims: set[str] = set()
        self._last_envelope_by_session: dict[str, RouteEnvelope] = {}
        self._last_envelope_task_id_by_session: dict[str, str] = {}
        self._state_lock = asyncio.Lock()
        # Admission is per session so durable RPC ingress crosses reserve,
        # commit, and activation in order. This prevents resets from overtaking
        # committed-but-inert reservations; collect also uses the same gate so
        # two sends cannot both miss a candidate and create separate tasks. The
        # lower-level try_collect_atomically deliberately does not re-enter it.
        self._collect_admission_locks: dict[str, _CollectAdmissionLockState] = {}
        self._collect_admission_registry_lock = asyncio.Lock()
        self._ingress_intent_states: dict[str, _IngressIntentState] = {}
        self._ingress_intent_registry_lock = asyncio.Lock()
        # In-flight counters track tasks that have actually acquired a slot.
        # They drive the reserved-slot fairness gate for subagent runs.
        self._global_in_flight = 0
        self._subagent_in_flight = 0
        # Lazily constructed so the runtime can be instantiated outside an
        # event loop (some tests do this); the Condition is bound to the
        # running loop the first time a subagent waits on a slot.
        self._slot_cond: asyncio.Condition | None = None
        # Per-agent-id fair-queuing state (true round-robin).
        #
        # Design: true round-robin across sessions of the same agent_id.
        # ``_agent_session_rr[agent_id]`` is a deque of session_keys that have
        # active (pending or running) tasks for that agent. ``_agent_slot_waiters``
        # narrows that enrollment to sessions whose driver is genuinely waiting
        # for a global slot; running sessions must never block idle capacity.
        # After a waiter acquires, the RR deque rotates past that session so the
        # next waiting session goes next.
        # When a session has no more pending/running tasks it is removed from the
        # deque in ``_mark_terminal``.
        #
        # ``_agent_active_sessions[agent_id]`` tracks the set of session_keys
        # that currently have at least one pending or running task.  It is the
        # membership oracle that ``_mark_terminal`` uses to decide whether to
        # evict a session_key from the deque.
        #
        # The global slot cap (``_global_in_flight < _max_concurrency``) is
        # enforced as before.  Per-agent RR is the fairness layer inside that cap.
        #
        # Lazily initialised like _slot_cond.
        self._agent_session_rr: dict[str, deque[str]] = {}
        self._agent_active_sessions: dict[str, set[str]] = {}
        self._agent_slot_waiters: dict[str, set[str]] = {}
        self._agent_in_flight: dict[str, int] = {}
        self._fair_cond: asyncio.Condition | None = None

    async def recover_durable_meta_controls(self, *, limit: int = 64) -> int:
        """Reactivate accepted MetaSkill controls that never started.

        Session storage marks persisted QUEUED controls with a dedicated
        restart reason before this runtime is constructed.  RUNNING controls
        are intentionally excluded: once the durable running boundary was
        crossed, provider side effects may already have happened and automatic
        replay would not be safe.

        The original task id, transcript row, and server-bound ``meta_control``
        payload are reused.  No transcript row or ingress receipt is inserted
        during recovery.
        """

        claim = getattr(self._storage, "claim_recoverable_meta_control_tasks", None)
        if not callable(claim):
            return 0
        batch_limit = max(1, min(int(limit), 256))
        recovered = 0
        while True:
            claimed = await claim(limit=batch_limit)
            if not claimed:
                break
            batch_failed = False
            for item in claimed:
                task = item.task
                entry = item.entry
                reservation: TaskReservation | None = None
                try:
                    details = task.details if isinstance(task.details, dict) else {}
                    metadata = details.get("metadata")
                    if not isinstance(metadata, dict) or not isinstance(
                        metadata.get("meta_control"), dict
                    ):
                        raise ValueError("missing durable MetaSkill control metadata")
                    persisted_message = details.get("meta_control_message")
                    message = (
                        persisted_message
                        if isinstance(persisted_message, str)
                        else _recover_meta_control_message(entry.content)
                    )
                    if message is None:
                        raise ValueError("invalid durable MetaSkill control transcript")
                    persisted_semantic = details.get("meta_control_semantic_message")
                    semantic_message = (
                        persisted_semantic
                        if isinstance(persisted_semantic, str)
                        else message
                    )
                    source_name = details.get("source_name")
                    input_provenance = details.get("input_provenance")
                    persisted_ids = details.get("persisted_user_message_ids")
                    if not isinstance(persisted_ids, list):
                        persisted_ids = []
                    persisted_ids = [
                        value for value in persisted_ids if isinstance(value, str)
                    ]
                    if entry.message_id not in persisted_ids:
                        persisted_ids.insert(0, entry.message_id)
                    envelope = RouteEnvelope(
                        source_kind=SourceKind(task.source_kind),
                        source_name=(
                            source_name
                            if isinstance(source_name, str) and source_name
                            else "recovered_meta_control"
                        ),
                        agent_id=task.agent_id,
                        session_key=task.session_key,
                        session_id=entry.session_id,
                        input_provenance=(
                            dict(input_provenance)
                            if isinstance(input_provenance, dict)
                            else {}
                        ),
                        metadata=dict(metadata),
                    )
                    from opensquilla.engine.start_turn import reserve_turn_via_runtime

                    reservation = await reserve_turn_via_runtime(
                        self,
                        envelope,
                        message,
                        attachments=[],
                        mode="followup",
                        run_kind=task.run_kind,
                        no_memory_capture=bool(details.get("no_memory_capture", False)),
                        semantic_message=semantic_message,
                        persisted_user_message_id=entry.message_id,
                        fresh_user_session=bool(details.get("fresh_user_session", False)),
                        turn_id=task.task_id,
                        bypass_pending_limit=True,
                    )
                    await self.activate(
                        reservation,
                        persisted_user_message_id=entry.message_id,
                        persisted_user_message_ids=persisted_ids,
                        fresh_user_session=bool(details.get("fresh_user_session", False)),
                    )
                    recovered += 1
                except Exception as exc:  # noqa: BLE001 - preserve accepted work.
                    batch_failed = True
                    if reservation is not None and not reservation.activated:
                        with contextlib.suppress(Exception):
                            await self.abort_reservation(reservation)
                    with contextlib.suppress(Exception):
                        await self._storage.update_agent_task(
                            task.task_id,
                            status=AgentTaskStatus.ABANDONED,
                            finished_at=int(time.time() * 1000),
                            terminal_reason="meta_control_restart_before_start",
                            error_class=type(exc).__name__,
                            error_message=(
                                "Gateway could not reactivate the accepted MetaSkill control"
                            ),
                        )
                    log.error(
                        "task_runtime.meta_control_recovery_failed",
                        task_id=task.task_id,
                        session_key=task.session_key,
                        error_class=type(exc).__name__,
                        exc_info=True,
                    )
            # A failed row was returned to the same claim pool. Stop this boot
            # pass to avoid a tight retry loop; a later restart can retry it.
            if batch_failed or len(claimed) < batch_limit:
                break
        return recovered

    async def enqueue(
        self,
        envelope: RouteEnvelope,
        message: str,
        attachments: builtins.list[dict[str, Any]] | None = None,
        mode: str | None = None,
        run_kind: str = "default",
        no_memory_capture: bool = False,
        ingress_pipeline_steps: tuple[Any, ...] | list[Any] | None = None,
        semantic_message: str | None = None,
        persisted_user_message_id: str | None = None,
        persisted_user_message_ids: builtins.list[str] | tuple[str, ...] | None = None,
        message_count: int = 1,
        fresh_user_session: bool = False,
        stream_event_sink: TaskStreamEventSink | None = None,
        accepted_run_mode_override: Any | None = None,
        *,
        task_id: str | None = None,
        provider_request_correlation: ProviderRequestCorrelation | None = None,
        update_envelope_cache: bool = True,
        overflow_policy: PendingOverflowPolicy | str | None = None,
    ) -> TaskHandle:
        envelope = replace(
            envelope,
            agent_id=normalize_agent_id(envelope.agent_id),
            session_key=canonicalize_session_key(envelope.session_key),
        )
        queue_mode = mode or QueueMode.FOLLOWUP.value
        if not self.supports_queue_mode(queue_mode):
            valid = ", ".join(sorted(self.supported_queue_modes))
            raise ValueError(f"mode must be one of {{{valid}}}")
        async with self.collect_admission(envelope.session_key):
            await self.cancel_auxiliary(envelope.session_key)
            if queue_mode == "collect":
                collected = await self._try_collect(
                    envelope=envelope,
                    message=message,
                    attachments=attachments,
                    run_kind=run_kind,
                    no_memory_capture=no_memory_capture,
                    semantic_message=semantic_message,
                    persisted_user_message_id=persisted_user_message_id,
                    persisted_user_message_ids=persisted_user_message_ids,
                    message_count=message_count,
                    accepted_run_mode_override=accepted_run_mode_override,
                )
                if collected is not None:
                    return collected
            return await self._reserve_persist_and_activate(
                envelope,
                message,
                attachments=attachments,
                mode=queue_mode,
                run_kind=run_kind,
                no_memory_capture=no_memory_capture,
                ingress_pipeline_steps=ingress_pipeline_steps,
                semantic_message=semantic_message,
                persisted_user_message_id=persisted_user_message_id,
                persisted_user_message_ids=persisted_user_message_ids,
                message_count=message_count,
                fresh_user_session=fresh_user_session,
                stream_event_sink=stream_event_sink,
                accepted_run_mode_override=accepted_run_mode_override,
                task_id=task_id,
                provider_request_correlation=provider_request_correlation,
                update_envelope_cache=update_envelope_cache,
                overflow_policy=overflow_policy,
            )

    @contextlib.asynccontextmanager
    async def collect_admission(self, session_key: str) -> AsyncIterator[None]:
        """Serialize one session's durable ingress and collect decisions.

        Callers that own durable ingress hold this around reserve -> commit ->
        activate for every queue mode. Collect callers also keep it around
        ``try_collect_atomically`` so a miss and the following reservation are
        one admission decision. The lower-level helper does not acquire the gate
        because ``asyncio.Lock`` is not re-entrant.
        """

        key = canonicalize_session_key(session_key)
        async with self._collect_admission_registry_lock:
            state = self._collect_admission_locks.get(key)
            if state is None:
                state = _CollectAdmissionLockState()
                self._collect_admission_locks[key] = state
            state.borrowers += 1
        try:
            async with state.lock:
                yield
        finally:
            async with self._collect_admission_registry_lock:
                state.borrowers = max(0, state.borrowers - 1)
                if (
                    state.borrowers == 0
                    and not state.lock.locked()
                    and self._collect_admission_locks.get(key) is state
                ):
                    self._collect_admission_locks.pop(key, None)

    async def _borrow_ingress_intent_state(self, session_key: str) -> _IngressIntentState:
        key = canonicalize_session_key(session_key)
        async with self._ingress_intent_registry_lock:
            state = self._ingress_intent_states.setdefault(key, _IngressIntentState())
            state.borrowers += 1
            return state

    async def _release_ingress_intent_state(
        self,
        session_key: str,
        state: _IngressIntentState,
    ) -> None:
        key = canonicalize_session_key(session_key)
        async with self._ingress_intent_registry_lock:
            state.borrowers = max(0, state.borrowers - 1)
            if (
                state.borrowers == 0
                and state.count == 0
                and not state.lock.locked()
                and self._ingress_intent_states.get(key) is state
            ):
                self._ingress_intent_states.pop(key, None)

    @contextlib.asynccontextmanager
    async def explicit_ingress_intent(self, session_key: str) -> AsyncIterator[None]:
        """Register explicit user work without holding a lock during admission."""

        lease = await self.acquire_explicit_ingress_intent(session_key)
        try:
            yield
        finally:
            await lease.release()

    async def acquire_explicit_ingress_intent(
        self,
        session_key: str,
    ) -> _ExplicitIngressIntentLease:
        """Register explicit work and return an exactly-once transferable lease.

        Most ingress paths should use :meth:`explicit_ingress_intent`.  A
        debounce queue must keep the user's priority fence alive after the
        receive coroutine returns and release it from a later fire/cancel
        callback, which is why this lower-level handle exists.
        """

        key = canonicalize_session_key(session_key)
        state = await self._borrow_ingress_intent_state(key)
        async with state.lock:
            state.count += 1
        return _ExplicitIngressIntentLease(
            runtime=self,
            session_key=key,
            state=state,
        )

    async def _release_explicit_ingress_intent(
        self,
        session_key: str,
        state: _IngressIntentState,
    ) -> None:
        became_idle = False
        async with state.lock:
            state.count = max(0, state.count - 1)
            became_idle = state.count == 0
        await self._release_ingress_intent_state(session_key, state)
        if became_idle and self._idle_listener is not None:
            asyncio.create_task(self._notify_runtime_idle(session_key))

    @contextlib.asynccontextmanager
    async def automatic_ingress_fence(self, session_key: str) -> AsyncIterator[bool]:
        """Linearize automatic durable acceptance behind earlier user intent.

        Callers acquire ``collect_admission`` first, then hold this context
        through their short SQLite acceptance transaction.
        """

        state = await self._borrow_ingress_intent_state(session_key)
        await state.lock.acquire()
        try:
            yield state.count == 0
        finally:
            state.lock.release()
            await self._release_ingress_intent_state(session_key, state)

    async def has_explicit_ingress_intent(self, session_key: str) -> bool:
        state = await self._borrow_ingress_intent_state(session_key)
        try:
            async with state.lock:
                return state.count > 0
        finally:
            await self._release_ingress_intent_state(session_key, state)

    async def has_session_work(self, session_key: str) -> bool:
        """Return whether a session has reserved, queued, or running work.

        Automatic producers use this only as an early, in-memory admission
        check.  Their durable transaction remains the authoritative fence.
        """

        key = canonicalize_session_key(session_key)
        async with self._state_lock:
            return bool(
                self._reservations_by_session.get(key)
                or self._pending_by_session.get(key)
                or self._running_by_session.get(key)
            )

    async def cancel_auxiliary(self, session_key: str) -> None:
        """Cancel low-priority work for one session without waiting on it."""

        key = canonicalize_session_key(session_key)
        async with self._state_lock:
            task = self._auxiliary_tasks_by_session.get(key)
        current = asyncio.current_task()
        if task is not None and task is not current and not task.done():
            task.cancel()

    async def run_auxiliary_if_idle(
        self,
        session_key: str,
        operation: Callable[[], Awaitable[None]],
    ) -> bool:
        """Run cancellable work only while the session has no real task.

        Returns ``False`` when admission found real work or another auxiliary
        owner. The caller's task becomes the auxiliary owner so cancellation
        from ``enqueue`` propagates directly into the provider stream.
        """

        key = canonicalize_session_key(session_key)
        current = asyncio.current_task()
        if current is None:
            return False

        async with self.collect_admission(key):
            async with self._state_lock:
                if self._closing:
                    return False
                busy = bool(
                    self._pending_by_session.get(key)
                    or self._running_by_session.get(key)
                    or self._reservations_by_session.get(key)
                    or self._auxiliary_tasks_by_session.get(key)
                )
                if busy:
                    return False
                self._auxiliary_tasks_by_session[key] = current
                self._signal_driver_state_changed()

        execution_lock = self._session_execution_locks.setdefault(key, asyncio.Lock())
        try:
            async with self._auxiliary_slot:
                async with execution_lock:
                    async with self._state_lock:
                        real_work_arrived = bool(
                            self._pending_by_session.get(key)
                            or self._running_by_session.get(key)
                            or self._reservations_by_session.get(key)
                        )
                    if real_work_arrived:
                        return False
                    await operation()
                    return True
        finally:
            async with self._state_lock:
                if self._auxiliary_tasks_by_session.get(key) is current:
                    self._auxiliary_tasks_by_session.pop(key, None)
                    self._signal_driver_state_changed()

    @contextlib.asynccontextmanager
    async def quiesce_sessions(
        self,
        session_keys: Iterable[str],
    ) -> AsyncIterator[None]:
        """Fence runtime work for a stable, ordered set of sessions.

        Cancellation first drains the real ``_execute`` driver coroutines,
        including their post-terminal notification/promotion tails. Execution
        and admission locks are then acquired in stable key order. A final
        state check closes commit-before-activate and reservation races; if
        anything appeared while the drivers were draining, every lock is
        released and the sequence retries.
        """

        keys = tuple(
            sorted(
                {
                    canonicalize_session_key(session_key)
                    for session_key in session_keys
                }
            )
        )
        if not keys:
            yield
            return

        key_set = frozenset(keys)
        while True:
            await asyncio.gather(
                *(self.cancel_auxiliary(key) for key in keys),
            )
            await self._cancel_and_drain_session_drivers(keys, key_set)

            async with contextlib.AsyncExitStack() as fences:
                for session_key in keys:
                    execution_lock = self._session_execution_locks.setdefault(
                        session_key,
                        asyncio.Lock(),
                    )
                    await self._acquire_execution_lock_while_quiescing(
                        execution_lock,
                        keys,
                        key_set,
                    )
                    fences.callback(execution_lock.release)
                for session_key in keys:
                    await fences.enter_async_context(
                        self.collect_admission(session_key)
                    )

                async with self._state_lock:
                    active = any(
                        self._driver_tasks_by_session.get(session_key)
                        for session_key in keys
                    )
                    if not active:
                        active = any(
                            task.envelope.session_key in key_set
                            for task in self._tasks.values()
                        )
                    if not active:
                        active = any(
                            self._reservations_by_session.get(session_key)
                            for session_key in keys
                        )
                if active:
                    continue

                yield
                return

    async def _acquire_execution_lock_while_quiescing(
        self,
        execution_lock: asyncio.Lock,
        keys: Sequence[str],
        key_set: frozenset[str],
    ) -> None:
        """Acquire one execution fence without waiting behind an uncancelled driver."""

        acquiring = asyncio.create_task(execution_lock.acquire())
        changed: asyncio.Task[bool] | None = None
        try:
            while not acquiring.done():
                # Capture the current generation's broadcast event before the
                # state snapshot. A mutation swaps in a fresh event and sets
                # this one, so concurrent quiescers cannot erase each other's
                # wake-up by clearing shared state.
                driver_state_changed = self._driver_state_changed
                await self._cancel_and_drain_session_drivers(keys, key_set)
                if acquiring.done():
                    break
                if driver_state_changed.is_set():
                    continue

                changed = asyncio.create_task(driver_state_changed.wait())
                try:
                    await asyncio.wait(
                        {acquiring, changed},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    if not changed.done():
                        changed.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await changed
                    changed = None
            await acquiring
        except BaseException:
            if changed is not None and not changed.done():
                changed.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await changed
            if acquiring.done():
                try:
                    acquiring.result()
                except BaseException:
                    pass
                else:
                    execution_lock.release()
            else:
                acquiring.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await acquiring
            raise

    def _signal_driver_state_changed(self) -> None:
        """Broadcast one driver-state generation change."""

        changed = self._driver_state_changed
        self._driver_state_changed = asyncio.Event()
        changed.set()

    async def _cancel_and_drain_session_drivers(
        self,
        keys: Sequence[str],
        key_set: frozenset[str],
    ) -> None:
        async with self._state_lock:
            runtime_tasks = [
                task
                for task in self._tasks.values()
                if task.envelope.session_key in key_set
                and task.status not in TERMINAL_STATUSES
            ]
            drivers = {
                driver
                for session_key in keys
                for driver in self._driver_tasks_by_session.get(
                    session_key,
                    (),
                )
                if not driver.done()
            }

        if runtime_tasks:
            await self._cancel_runtime_tasks(
                runtime_tasks,
                source="workspace_history_delete",
                reason="project_history_deleted",
            )
        if drivers:
            await asyncio.gather(*drivers, return_exceptions=True)
            for driver in drivers:
                for session_key in keys:
                    self._discard_session_driver(session_key, driver)

    def _discard_session_driver(
        self,
        session_key: str,
        driver: asyncio.Task[None],
    ) -> None:
        drivers = self._driver_tasks_by_session.get(session_key)
        if drivers is None:
            return
        drivers.discard(driver)
        if not drivers:
            self._driver_tasks_by_session.pop(session_key, None)
        self._signal_driver_state_changed()

    async def _reserve_persist_and_activate(
        self,
        envelope: RouteEnvelope,
        message: str,
        attachments: builtins.list[dict[str, Any]] | None = None,
        mode: str | None = None,
        run_kind: str = "default",
        no_memory_capture: bool = False,
        ingress_pipeline_steps: tuple[Any, ...] | list[Any] | None = None,
        semantic_message: str | None = None,
        persisted_user_message_id: str | None = None,
        persisted_user_message_ids: builtins.list[str] | tuple[str, ...] | None = None,
        message_count: int = 1,
        fresh_user_session: bool = False,
        stream_event_sink: TaskStreamEventSink | None = None,
        accepted_run_mode_override: Any | None = None,
        *,
        task_id: str | None = None,
        provider_request_correlation: ProviderRequestCorrelation | None = None,
        update_envelope_cache: bool = True,
        overflow_policy: PendingOverflowPolicy | str | None = None,
    ) -> TaskHandle:
        """Persist and activate one direct enqueue without cancellation drift."""

        reservation = await self.reserve(
            envelope,
            message,
            attachments=attachments,
            mode=mode,
            run_kind=run_kind,
            no_memory_capture=no_memory_capture,
            ingress_pipeline_steps=ingress_pipeline_steps,
            semantic_message=semantic_message,
            persisted_user_message_id=persisted_user_message_id,
            persisted_user_message_ids=persisted_user_message_ids,
            message_count=message_count,
            fresh_user_session=fresh_user_session,
            stream_event_sink=stream_event_sink,
            accepted_run_mode_override=accepted_run_mode_override,
            task_id=task_id,
            provider_request_correlation=provider_request_correlation,
            update_envelope_cache=update_envelope_cache,
            overflow_policy=overflow_policy,
        )
        try:
            await self._storage.create_agent_task(reservation.task_record)
        except asyncio.CancelledError:
            # The shared storage layer may finish COMMIT after its caller is
            # cancelled. Settle the operation, read back by task_id, and cross
            # exactly one in-memory boundary: persisted tasks activate; absent
            # tasks release their inert reservation.
            persisted = await self._wait_for_task_settlement(
                asyncio.create_task(self._storage.get_agent_task(reservation.task_id))
            )
            if persisted is None:
                await self._wait_for_task_settlement(
                    asyncio.create_task(self.abort_reservation(reservation))
                )
            else:
                await self._wait_for_task_settlement(
                    asyncio.create_task(self.activate(reservation))
                )
            raise
        except BaseException:
            await self.abort_reservation(reservation)
            raise
        try:
            return await self.activate(reservation)
        except asyncio.CancelledError:
            # Persistence has definitely returned successfully. If cancellation
            # arrived before ``activate`` crossed its in-memory boundary, finish
            # activation in a fresh child; otherwise the reservation already owns
            # the live task and only the caller cancellation remains to propagate.
            if not reservation.activated:
                await self._wait_for_task_settlement(
                    asyncio.create_task(self.activate(reservation))
                )
            raise

    @staticmethod
    async def _wait_for_task_settlement[T](task: asyncio.Task[T]) -> T:
        """Wait for a child operation without forwarding caller cancellation."""

        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        return task.result()

    async def reserve(
        self,
        envelope: RouteEnvelope,
        message: str,
        attachments: builtins.list[dict[str, Any]] | None = None,
        mode: str | None = None,
        run_kind: str = "default",
        no_memory_capture: bool = False,
        input_mode: str = "user",
        persist_input: bool = False,
        history_has_persisted_user: bool = True,
        goal_context: Mapping[str, Any] | None = None,
        goal_candidate: Mapping[str, Any] | None = None,
        ingress_pipeline_steps: tuple[Any, ...] | list[Any] | None = None,
        semantic_message: str | None = None,
        persisted_user_message_id: str | None = None,
        persisted_user_message_ids: builtins.list[str] | tuple[str, ...] | None = None,
        message_count: int = 1,
        fresh_user_session: bool = False,
        stream_event_sink: TaskStreamEventSink | None = None,
        accepted_run_mode_override: Any | None = None,
        *,
        task_id: str | None = None,
        provider_request_correlation: ProviderRequestCorrelation | None = None,
        update_envelope_cache: bool = True,
        overflow_policy: PendingOverflowPolicy | str | None = None,
        bypass_pending_limit: bool = False,
        _allow_during_shutdown: bool = False,
    ) -> TaskReservation:
        """Reserve queue admission without persistence, cancellation, or execution."""

        envelope = replace(
            envelope,
            agent_id=normalize_agent_id(envelope.agent_id),
            session_key=canonicalize_session_key(envelope.session_key),
        )
        queue_mode = mode or "followup"
        normalized_message_ids = _ordered_message_ids(
            persisted_user_message_id,
            persisted_user_message_ids,
        )
        persisted_user_message_id = (
            normalized_message_ids[0] if normalized_message_ids else None
        )
        message_count = max(1, int(message_count))
        effective_policy = self._pending_overflow_policy
        if overflow_policy is not None:
            try:
                effective_policy = PendingOverflowPolicy(overflow_policy)
            except ValueError as exc:
                valid = ", ".join(member.value for member in PendingOverflowPolicy)
                raise ValueError(
                    f"overflow_policy must be one of {{{valid}}}"
                ) from exc

        record_kwargs: dict[str, Any] = {}
        if task_id is not None:
            record_kwargs["task_id"] = task_id
        record = AgentTaskRecord(
            **record_kwargs,
            session_key=envelope.session_key,
            agent_id=envelope.agent_id,
            source_kind=envelope.source_kind.value,
            queue_mode=queue_mode,
            run_kind=run_kind,
            status=AgentTaskStatus.QUEUED,
            details={
                "source_name": envelope.source_name,
                "input_provenance": envelope.input_provenance,
                "no_memory_capture": no_memory_capture,
                "input_mode": input_mode,
                "persist_input": persist_input,
                "history_has_persisted_user": history_has_persisted_user,
                "goal_context": dict(goal_context) if goal_context is not None else None,
                "goal_candidate": dict(goal_candidate) if goal_candidate is not None else None,
                "metadata": envelope.metadata,
                "persisted_user_message_id": persisted_user_message_id,
                "persisted_user_message_ids": normalized_message_ids,
                "message_count": message_count,
                "fresh_user_session": fresh_user_session,
            },
        )
        if isinstance(envelope.metadata.get("meta_control"), dict):
            # Controls are text-only and already present in the transcript.
            # Persist their exact provider/semantic projections so restart
            # recovery is independent of display envelopes and time stamping.
            assert record.details is not None
            record.details["meta_control_message"] = message
            record.details["meta_control_semantic_message"] = (
                semantic_message if isinstance(semantic_message, str) else message
            )
        record.details = {
            **(record.details or {}),
            **_task_identity_payload(
                envelope,
                record.task_id,
                user_message_id=persisted_user_message_id,
            ),
        }
        accepted_run_mode_payload = _accepted_run_mode_payload(
            accepted_run_mode_override,
        )
        if accepted_run_mode_payload is not None:
            record.details["accepted_run_mode"] = accepted_run_mode_payload
        runtime_task = _RuntimeTask(
            task_id=record.task_id,
            envelope=envelope,
            message=message,
            attachments=list(attachments or []),
            queue_mode=queue_mode,
            run_kind=run_kind,
            no_memory_capture=no_memory_capture,
            input_mode=input_mode,
            persist_input=persist_input,
            history_has_persisted_user=history_has_persisted_user,
            goal_context=(dict(goal_context) if goal_context is not None else None),
            goal_candidate=(dict(goal_candidate) if goal_candidate is not None else None),
            ingress_pipeline_steps=tuple(ingress_pipeline_steps or ()),
            semantic_message=semantic_message,
            persisted_user_message_id=persisted_user_message_id,
            persisted_user_message_ids=normalized_message_ids,
            message_count=message_count,
            fresh_user_session=fresh_user_session,
            stream_event_sink=stream_event_sink,
            accepted_run_mode_override=accepted_run_mode_override,
            provider_request_correlation=provider_request_correlation,
            primary_input_pending=bool(
                (persisted_user_message_id or envelope.metadata.get("client_message_id"))
                and envelope.metadata.get("turn_context_disposition", "queued") == "queued"
            ),
        )

        async def _record_applied_steers(
            items: Sequence[_SteeredInput],
        ) -> Sequence[_SteeredInput]:
            return await self._record_steer_dispositions(
                runtime_task,
                items,
                disposition="applied",
                turn_id=runtime_task.task_id,
                revision=2,
            )

        async def _claim_goal_objective_update(
            update: GoalObjectiveUpdate,
        ) -> GoalObjectiveUpdate | None:
            claim = getattr(self._storage, "claim_goal_objective_update", None)
            if not callable(claim):
                return None
            result = claim(update)
            if inspect.isawaitable(result):
                result = await result
            return result if isinstance(result, GoalObjectiveUpdate) else None

        async def _record_applied_goal_objective_update(
            update: GoalObjectiveUpdate,
            iteration: int,
            model_call_id: str,
        ) -> GoalObjectiveUpdate | None:
            apply = getattr(self._storage, "apply_goal_objective_update", None)
            if not callable(apply):
                return None
            result = apply(
                update,
                iteration=iteration,
                model_call_id=model_call_id,
            )
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, GoalObjectiveUpdate):
                runtime_task.goal_context = result.context.as_task_detail()
                services = dict(runtime_task.envelope.runtime_services)
                services["goal_context"] = dict(runtime_task.goal_context)
                runtime_task.envelope = replace(
                    runtime_task.envelope,
                    runtime_services=services,
                )
                return result
            return None

        runtime_task.pending_input_provider.set_applied_recorder(
            _record_applied_steers
        )
        runtime_task.pending_input_provider.set_goal_objective_recorders(
            claim_binder=_claim_goal_objective_update,
            applied_recorder=_record_applied_goal_objective_update,
        )
        reservation = TaskReservation(
            reservation_id=str(uuid.uuid4()),
            task_record=record,
            runtime_task=runtime_task,
            update_envelope_cache=update_envelope_cache,
        )

        async with self._state_lock:
            if self._closing and not _allow_during_shutdown:
                raise TaskRuntimeShuttingDownError(
                    session_key=envelope.session_key,
                )
            if (
                not bypass_pending_limit
                and queue_mode not in {QueueMode.STEER.value, QueueMode.INTERRUPT.value}
                and self._max_pending_per_session is not None
            ):
                pending = [
                    task
                    for task in self._pending_by_session.get(envelope.session_key, [])
                    if task.task_id not in self._reserved_overflow_victims
                ]
                reservations = self._reservations_by_session.get(envelope.session_key, [])
                if len(pending) + len(reservations) >= self._max_pending_per_session:
                    victim: _RuntimeTask | None = None
                    if effective_policy is PendingOverflowPolicy.DROP_OLDEST:
                        victim = next(
                            (
                                task
                                for task in pending
                                if task.status == AgentTaskStatus.QUEUED
                            ),
                            None,
                        )
                    if victim is None:
                        _emit_metric(
                            "queue_full_errors_total",
                            value=1,
                            session_key=envelope.session_key,
                            policy=str(effective_policy),
                        )
                        raise TaskQueueFullError(
                            session_key=envelope.session_key,
                            max_pending=self._max_pending_per_session,
                        )
                    reservation.overflow_victim = victim
                    self._reserved_overflow_victims.add(victim.task_id)
            self._reservations_by_session.setdefault(envelope.session_key, []).append(
                reservation
            )
            self._signal_driver_state_changed()
        try:
            runtime_task.envelope = _materialize_guest_task_envelope(
                runtime_task.envelope,
                runtime_task.task_id,
            )
            if isinstance(reservation.task_record.details, dict):
                reservation.task_record.details["metadata"] = dict(
                    runtime_task.envelope.metadata
                )
        except BaseException:
            await self.abort_reservation(reservation)
            raise
        return reservation

    async def abort_reservation(self, reservation: TaskReservation) -> None:
        """Release a reservation after its persistence transaction fails."""

        async with self._state_lock:
            if reservation.activated or reservation.aborted:
                return
            reservations = self._reservations_by_session.get(reservation.session_key, [])
            with contextlib.suppress(ValueError):
                reservations.remove(reservation)
            if not reservations:
                self._reservations_by_session.pop(reservation.session_key, None)
            if reservation.overflow_victim is not None:
                self._reserved_overflow_victims.discard(
                    reservation.overflow_victim.task_id
                )
            reservation.aborted = True
            self._signal_driver_state_changed()
        _cleanup_guest_profile(reservation.runtime_task)

    async def _emit_queued_activation(
        self,
        envelope: RouteEnvelope,
        *,
        task_id: str,
        queue_depth: int,
        queue_position: int,
        run_kind: str,
        user_message_id: str | None = None,
    ) -> None:
        """Publish the queued lifecycle after irreversible activation."""

        await self._emit(
            envelope.session_key,
            "task.queued",
            {
                "task_id": task_id,
                "session_key": envelope.session_key,
                "queue_depth": queue_depth,
                "queue_position": queue_position,
                **_task_identity_payload(
                    envelope,
                    task_id,
                    user_message_id=user_message_id,
                ),
            },
        )
        await self._notify_task_lifecycle(
            TaskLifecycleEvent(
                phase="queued",
                session_key=envelope.session_key,
                task_id=task_id,
                task_status=AgentTaskStatus.QUEUED,
                run_kind=run_kind,
            )
        )

    async def activate(
        self,
        reservation: TaskReservation,
        *,
        persisted_user_message_id: str | None = None,
        persisted_user_message_ids: list[str] | tuple[str, ...] | None = None,
        fresh_user_session: bool | None = None,
        defer_queued_notification: bool = False,
    ) -> TaskHandle:
        """Idempotently activate a reservation after its DB transaction commits."""

        interrupt_targets: list[_RuntimeTask] = []
        victim: _RuntimeTask | None = None
        async with self._state_lock:
            if reservation.aborted:
                raise RuntimeError("Cannot activate an aborted task reservation")
            if reservation.activated:
                return TaskHandle(
                    task_id=reservation.task_id,
                    session_key=reservation.session_key,
                    status=reservation.status,
                )
            reservations = self._reservations_by_session.get(reservation.session_key, [])
            if reservation not in reservations:
                raise RuntimeError("Unknown task reservation")

            runtime_task = reservation.runtime_task
            persisted_details = reservation.task_record.details
            if isinstance(persisted_details, dict):
                persisted_metadata = persisted_details.get("metadata")
                if isinstance(persisted_metadata, dict):
                    # Durable acceptance may resolve authoritative metadata
                    # (notably the actual collaboration revision) inside the
                    # same transaction that admits the task. Activate that
                    # exact snapshot instead of a caller-predicted value.
                    runtime_task.envelope = replace(
                        runtime_task.envelope,
                        metadata=dict(persisted_metadata),
                    )
                from opensquilla.session.goals import GoalClaimCandidate

                accepted_goal_context = effective_goal_turn_context(
                    persisted_details
                )
                accepted_goal_candidate = GoalClaimCandidate.from_task_detail(
                    persisted_details.get("goal_candidate")
                )
                if accepted_goal_context is not None:
                    runtime_task.goal_context = accepted_goal_context.as_task_detail()
                    runtime_task.goal_candidate = None
                elif accepted_goal_candidate is not None:
                    runtime_task.goal_candidate = accepted_goal_candidate.as_task_detail()
            if not runtime_task.accepted_config_captured:
                accepted_config = (
                    self._accepted_config_provider()
                    if self._accepted_config_provider is not None
                    else None
                )
                runtime_task.accepted_config = accepted_config
                runtime_task.accepted_config_captured = True

            reservations.remove(reservation)
            if not reservations:
                self._reservations_by_session.pop(reservation.session_key, None)

            activated_message_ids = _ordered_message_ids(
                runtime_task.persisted_user_message_id,
                (
                    *runtime_task.persisted_user_message_ids,
                    *([persisted_user_message_id] if persisted_user_message_id else []),
                    *(persisted_user_message_ids or ()),
                ),
            )
            runtime_task.persisted_user_message_ids = activated_message_ids
            runtime_task.persisted_user_message_id = (
                activated_message_ids[0] if activated_message_ids else None
            )
            if fresh_user_session is not None:
                runtime_task.fresh_user_session = fresh_user_session
            if (
                not runtime_task.primary_input_pending
                and (
                    runtime_task.persisted_user_message_id
                    or runtime_task.envelope.metadata.get("client_message_id")
                )
                and runtime_task.envelope.metadata.get(
                    "turn_context_disposition", "queued"
                )
                == "queued"
            ):
                runtime_task.primary_input_pending = True

            if runtime_task.queue_mode in {
                QueueMode.STEER.value,
                QueueMode.INTERRUPT.value,
            }:
                interrupt_targets = [
                    task
                    for task in self._tasks.values()
                    if task.envelope.session_key == reservation.session_key
                    and task.status not in TERMINAL_STATUSES
                ]
                for target in interrupt_targets:
                    target.cancel_requested = True
                    target.cancel_source = f"queue_{runtime_task.queue_mode}"
                    target.cancel_reason = f"queue_mode_{runtime_task.queue_mode}"

            victim = reservation.overflow_victim
            if victim is not None:
                self._reserved_overflow_victims.discard(victim.task_id)
                pending = self._pending_by_session.get(reservation.session_key, [])
                if (
                    victim.status != AgentTaskStatus.QUEUED
                    or victim not in pending
                ):
                    # The durable acceptance window may be long enough for the
                    # reserved victim to start running. DROP_OLDEST only evicts
                    # waiting work; once the victim has left the pending queue,
                    # its slot has already made room for this replacement.
                    victim = None
                else:
                    victim.cancel_requested = True
                    victim.overflow_dropped = True
                    victim.cancel_source = "queue_overflow"
                    victim.cancel_reason = "dropped_by_overflow"

            self._tasks[reservation.task_id] = runtime_task
            self._pending_by_session.setdefault(reservation.session_key, []).append(
                runtime_task
            )
            agent_id = runtime_task.envelope.agent_id
            session_key = runtime_task.envelope.session_key
            if agent_id not in self._agent_session_rr:
                self._agent_session_rr[agent_id] = deque()
                self._agent_active_sessions[agent_id] = set()
            active = self._agent_active_sessions[agent_id]
            rr = self._agent_session_rr[agent_id]
            if session_key not in active:
                active.add(session_key)
                rr.append(session_key)
            if reservation.update_envelope_cache:
                self._last_envelope_by_session[session_key] = (
                    _reusable_route_envelope(runtime_task.envelope)
                )
                self._last_envelope_task_id_by_session[session_key] = (
                    runtime_task.task_id
                )
            driver = asyncio.create_task(self._execute(runtime_task))
            runtime_task.asyncio_task = driver
            self._driver_tasks_by_session.setdefault(session_key, set()).add(driver)
            self._signal_driver_state_changed()

            def _discard_driver(completed: asyncio.Task[None]) -> None:
                self._discard_session_driver(session_key, completed)
                if self._idle_listener is not None:
                    asyncio.create_task(self._notify_runtime_idle(session_key))

            driver.add_done_callback(_discard_driver)
            reservation.activated = True
            queue_depth = len(self._pending_by_session.get(session_key, []))
            queue_position = queue_depth
            reservation.queued_notification_pending = True
            reservation.activation_queue_depth = queue_depth
            reservation.activation_queue_position = queue_position

        for target in interrupt_targets:
            asyncio_task = target.asyncio_task
            if asyncio_task is not None and not asyncio_task.done():
                asyncio_task.cancel()
        if victim is not None:
            asyncio_task = victim.asyncio_task
            if asyncio_task is not None and not asyncio_task.done():
                asyncio_task.cancel()
            try:
                await self._mark_terminal(
                    victim,
                    AgentTaskStatus.CANCELLED,
                    terminal_reason="dropped_by_overflow",
                )
            except Exception as exc:  # noqa: BLE001 - new task is already active.
                # Activation has crossed its irreversible boundary: the newly
                # accepted task is registered and executing. A best-effort
                # terminal update for the evicted task must not make callers
                # treat that accepted task as rejected or leave the victim's
                # waiters hanging forever.
                log.warning(
                    "task_runtime.overflow_victim_terminal_failed",
                    task_id=victim.task_id,
                    session_key=victim.envelope.session_key,
                    error=str(exc),
                )
            finally:
                victim.done.set()

        _emit_metric(
            "opensquilla_queue_depth",
            value=queue_depth,
            session_key=reservation.session_key,
        )
        if not defer_queued_notification:
            await self._publish_deferred_queued_activation(reservation)
        return TaskHandle(
            task_id=reservation.task_id,
            session_key=reservation.session_key,
            status=AgentTaskStatus.QUEUED,
        )

    async def _publish_deferred_queued_activation(
        self,
        reservation: TaskReservation,
    ) -> None:
        """Publish one already-registered task's queued boundary exactly once."""

        if not reservation.activated or not reservation.queued_notification_pending:
            return
        # No await before claiming the notification: event-loop callers cannot
        # race a second publisher past this exactly-once fence.
        reservation.queued_notification_pending = False
        queue_depth = reservation.activation_queue_depth
        queue_position = reservation.activation_queue_position
        runtime_task = reservation.runtime_task
        envelope = runtime_task.envelope
        try:
            await self._emit_queued_activation(
                envelope,
                task_id=reservation.task_id,
                queue_depth=max(1, int(queue_depth or 1)),
                queue_position=max(1, int(queue_position or 1)),
                run_kind=runtime_task.run_kind,
                user_message_id=runtime_task.persisted_user_message_id,
            )
        except Exception:  # noqa: BLE001 - acceptance is already durable.
            log.warning(
                "task_runtime.activation_notification_failed",
                task_id=reservation.task_id,
                session_key=reservation.session_key,
                exc_info=True,
            )

    async def status(self, task_id: str) -> AgentTaskRecord:
        fallback = self._terminal_fallback_records.get(task_id)
        if fallback is not None:
            return fallback
        record = await self._storage.get_agent_task(task_id)
        if record is None:
            raise KeyError(f"Agent task not found: {task_id}")
        return cast(AgentTaskRecord, record)

    async def list(
        self,
        session_key: str | None = None,
        status: str | AgentTaskStatus | None = None,
    ) -> list[AgentTaskRecord]:
        if session_key is not None:
            session_key = canonicalize_session_key(session_key)
        return cast(
            list[AgentTaskRecord],
            await self._storage.list_agent_tasks(session_key=session_key, status=status),
        )

    async def active_task_id(self, session_key: str) -> str | None:
        """Return the currently running turn id for one canonical session."""

        session_key = canonicalize_session_key(session_key)
        async with self._state_lock:
            task = self._running_by_session.get(session_key)
            if (
                task is None
                or task.terminal_closing
                or task.cancel_requested
                or task.status is not AgentTaskStatus.RUNNING
            ):
                return None
            return task.task_id

    async def session_task_snapshot(
        self,
        session_key: str,
        *,
        excluding_task_id: str | None = None,
    ) -> SessionTaskSnapshot:
        """Return the running-first in-memory task projection for a session.

        The snapshot is captured under the runtime state lock and never reads
        SQLite. A running task remains the foreground owner while cancellation
        is requested; it leaves the projection only at its terminal boundary.
        """

        key = canonicalize_session_key(session_key)
        async with self._state_lock:
            running = self._running_by_session.get(key)
            running_task_id = (
                running.task_id
                if running is not None
                and running.task_id != excluding_task_id
                and running.status is AgentTaskStatus.RUNNING
                else None
            )
            queued_task_ids = tuple(
                task.task_id
                for task in self._pending_by_session.get(key, ())
                if task.task_id != excluding_task_id
                and task.status is AgentTaskStatus.QUEUED
            )
        return SessionTaskSnapshot(
            running_task_id=running_task_id,
            queued_task_ids=queued_task_ids,
        )

    @staticmethod
    def _steer_capability_for_task(task: _RuntimeTask) -> dict[str, Any]:
        """Describe whether the active task accepts first-phase same-turn input."""

        if task.run_kind == "channel_turn":
            return {
                "mode": "queue_only",
                "expected_turn_id": task.task_id,
                "input_kinds": ["text"],
                "reason": "restart_recovery_unavailable",
            }

        interactive_run_kinds = {"default", "goal", "session_turn", "web_turn"}
        if task.run_kind not in interactive_run_kinds:
            return {
                "mode": "disabled",
                "expected_turn_id": task.task_id,
                "input_kinds": [],
                "reason": "task_kind_not_steerable",
            }

        from opensquilla.gateway.model_routing import model_routing_snapshot

        routing = model_routing_snapshot(task.accepted_config)
        if routing.get("mode") == "ensemble":
            return {
                "mode": "queue_only",
                "expected_turn_id": task.task_id,
                "input_kinds": ["text"],
                "reason": "ensemble_requires_followup_turn",
            }
        return {
            "mode": "same_turn",
            "expected_turn_id": task.task_id,
            "input_kinds": ["text"],
            "reason": None,
        }

    async def steer_capability(self, session_key: str) -> dict[str, Any]:
        """Return the authoritative capability of one currently running task."""

        session_key = canonicalize_session_key(session_key)
        async with self._state_lock:
            task = self._running_by_session.get(session_key)
            if task is None:
                return {
                    "mode": "disabled",
                    "expected_turn_id": None,
                    "input_kinds": [],
                    "reason": "no_active_turn",
                }
            if (
                task.terminal_closing
                or task.cancel_requested
                or task.status is not AgentTaskStatus.RUNNING
            ):
                return {
                    "mode": "disabled",
                    "expected_turn_id": task.task_id,
                    "input_kinds": [],
                    "reason": "turn_closing",
                }
            return self._steer_capability_for_task(task)

    async def admit_steer(
        self,
        session_key: str,
        expected_turn_id: str,
        message: str,
        *,
        persist: Callable[[str], Awaitable[Any]],
        semantic_message: str | None = None,
        client_request_id: str | None = None,
        client_message_id: str | None = None,
        surface_id: str | None = None,
    ) -> SteerAdmissionResult:
        """Atomically persist and attach text to one explicitly named live turn.

        The per-task gate spans the durable persistence callback and in-memory
        append. Cancellation and terminalization take the same gate, so an
        accepted transcript row can never require an optimistic rollback.
        """

        session_key = canonicalize_session_key(session_key)
        async with self._state_lock:
            task = self._running_by_session.get(session_key)
            if task is None:
                return SteerAdmissionResult(
                    accepted=False,
                    failure_code="NO_ACTIVE_TURN",
                    capability={
                        "mode": "disabled",
                        "expected_turn_id": None,
                        "input_kinds": [],
                        "reason": "no_active_turn",
                    },
                )
            if task.task_id != expected_turn_id:
                return SteerAdmissionResult(
                    accepted=False,
                    task_id=task.task_id,
                    failure_code="EXPECTED_TURN_MISMATCH",
                    capability=self._steer_capability_for_task(task),
                )

        async with task.steer_claim:
            async with self._state_lock:
                current = self._running_by_session.get(session_key)
                if current is not task:
                    return SteerAdmissionResult(
                        accepted=False,
                        failure_code="NO_ACTIVE_TURN",
                        capability={
                            "mode": "disabled",
                            "expected_turn_id": None,
                            "input_kinds": [],
                            "reason": "no_active_turn",
                        },
                    )
                if (
                    task.terminal_closing
                    or task.cancel_requested
                    or task.status is not AgentTaskStatus.RUNNING
                ):
                    return SteerAdmissionResult(
                        accepted=False,
                        task_id=task.task_id,
                        failure_code="ACTIVE_TURN_NOT_STEERABLE",
                        capability={
                            "mode": "disabled",
                            "expected_turn_id": task.task_id,
                            "input_kinds": [],
                            "reason": "turn_closing",
                        },
                    )
                capability = self._steer_capability_for_task(task)
                if capability["mode"] != "same_turn":
                    return SteerAdmissionResult(
                        accepted=False,
                        task_id=task.task_id,
                        failure_code="ACTIVE_TURN_NOT_STEERABLE",
                        capability=capability,
                    )

            persisted = await persist(task.task_id)
            replayed = bool(getattr(persisted, "replayed", False))
            if not replayed:
                task.pending_input_provider.append(
                    _SteeredInput(
                        text=message,
                        semantic_message=semantic_message,
                        persisted_user_message_id=getattr(
                            getattr(persisted, "receipt", None),
                            "message_id",
                            None,
                        ),
                        client_request_id=client_request_id,
                        client_message_id=client_message_id,
                        surface_id=surface_id,
                        accepted_at_ms=getattr(
                            getattr(persisted, "receipt", None),
                            "accepted_at",
                            None,
                        ),
                    )
                )
            return SteerAdmissionResult(
                accepted=True,
                task_id=task.task_id,
                persisted=persisted,
                capability=capability,
            )

    async def apply_goal_objective_edit(
        self,
        session_key: str,
        *,
        persist: Callable[[str | None], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Persist a Goal edit and steer an eligible owner without user input.

        The owner gate encloses the Goal lock and SQLite transaction invoked by
        ``persist``. Ensemble and closing tasks intentionally fall back to the
        next ordinary Goal turn; the durable edit still succeeds.
        """

        session_key = canonicalize_session_key(session_key)
        async with self._state_lock:
            task = self._running_by_session.get(session_key)
            same_turn = bool(
                task is not None
                and not task.terminal_closing
                and not task.cancel_requested
                and task.status is AgentTaskStatus.RUNNING
                and self._steer_capability_for_task(task)["mode"] == "same_turn"
            )
        if task is None or not same_turn:
            return await persist(None)

        async with task.steer_claim:
            async with self._state_lock:
                current = self._running_by_session.get(session_key)
                eligible = not (
                    current is not task
                    or task.terminal_closing
                    or task.cancel_requested
                    or task.status is not AgentTaskStatus.RUNNING
                    or self._steer_capability_for_task(task)["mode"] != "same_turn"
                )
            if not eligible:
                response = None
            else:
                response = await persist(task.task_id)
                try:
                    record = await self._storage.get_agent_task(task.task_id)
                    details = (
                        dict(record.details or {})
                        if record is not None and isinstance(record.details, dict)
                        else {}
                    )
                    update = GoalObjectiveUpdate.from_task_detail(
                        details.get(GOAL_OBJECTIVE_UPDATE_DETAIL_KEY)
                    )
                    if (
                        update is not None
                        and update.status == "pending"
                        and update.context.task_id == task.task_id
                    ):
                        await task.pending_input_provider.append_goal_objective_update(
                            update
                        )
                except Exception:  # noqa: BLE001 - durable edit already committed
                    # The objective and command receipt are already durable.
                    # Same-turn projection is only an optimization; a later
                    # ordinary Goal turn must adopt the new objective if this
                    # best-effort read or in-memory enqueue fails.
                    log.exception(
                        "task_runtime.goal_edit_projection_failed",
                        session_key=session_key,
                        task_id=task.task_id,
                    )

        if response is None:
            return await persist(None)
        else:
            return response

    async def revoke_goal_objective_updates(self, session_key: str) -> None:
        """Fence future Goal adoption after durable Clear.

        The owning task keeps running. If it already claimed the internal edit,
        its assembled provider input is not recalled; the missing Goal row and
        revoked provider acknowledgement prevent durable authority from
        advancing or recreating the Goal.
        """

        session_key = canonicalize_session_key(session_key)
        async with self._state_lock:
            task = self._running_by_session.get(session_key)
        if task is None:
            return
        async with task.steer_claim:
            await task.pending_input_provider.revoke_goal_objective_updates()

    async def resolve_user_input(
        self,
        *,
        session_key: str,
        request_id: str,
        fields: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Resolve one structured interaction without creating a new turn."""

        return self._user_input_broker.resolve(
            session_key=session_key,
            request_id=request_id,
            fields=fields,
        )

    def pending_user_inputs(
        self,
        session_key: str,
    ) -> builtins.list[dict[str, Any]]:
        """Return public pending-interaction payloads for reconnect hydration."""

        return self._user_input_broker.pending_for_session(session_key)

    async def _update_transcript_turn_context(
        self,
        session_key: str,
        message_id: str | None,
        turn_context: dict[str, Any],
    ) -> bool:
        update = getattr(self._storage, "update_transcript_turn_context", None)
        if not message_id or not callable(update):
            return False
        result = update(session_key, message_id, turn_context)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    async def steer(
        self,
        session_key: str,
        message: str,
        *,
        semantic_message: str | None = None,
        persisted_user_message_id: str | None = None,
        client_request_id: str | None = None,
        client_message_id: str | None = None,
        surface_id: str | None = None,
    ) -> str | None:
        """Inject input into a running turn at its next safe model boundary.

        Returns the active turn id when accepted. A turn that ends before the
        next provider call starts promotes it to a follow-up in ``_execute``.
        """

        session_key = canonicalize_session_key(session_key)
        async with self._state_lock:
            task = self._running_by_session.get(session_key)
            if (
                task is None
                or task.terminal_closing
                or task.cancel_requested
                or task.status is not AgentTaskStatus.RUNNING
            ):
                return None
        async with task.steer_claim:
            async with self._state_lock:
                if (
                    self._running_by_session.get(session_key) is not task
                    or task.terminal_closing
                    or task.cancel_requested
                    or task.status is not AgentTaskStatus.RUNNING
                ):
                    return None
                task.pending_input_provider.append(
                    _SteeredInput(
                        text=message,
                        semantic_message=semantic_message,
                        persisted_user_message_id=persisted_user_message_id,
                        client_request_id=client_request_id,
                        client_message_id=client_message_id,
                        surface_id=surface_id,
                    )
                )
                return task.task_id

    async def cancel(
        self,
        task_id: str | None = None,
        session_key: str | None = None,
        *,
        source: str | None = None,
        reason: str | None = None,
    ) -> int:
        if task_id is None and session_key is None:
            raise ValueError("task_id or session_key is required")
        if session_key is not None:
            session_key = canonicalize_session_key(session_key)
        candidates = [
            task
            for task in list(self._tasks.values())
            if (task_id is None or task.task_id == task_id)
            and (session_key is None or task.envelope.session_key == session_key)
        ]
        return await self._cancel_runtime_tasks(
            candidates,
            source=source,
            reason=reason,
        )

    async def cancel_exact(
        self,
        *,
        task_id: str,
        session_key: str,
        source: str | None = None,
        reason: str | None = None,
    ) -> int:
        """Cancel one task after fencing durable admission for its session.

        A turn receipt becomes visible in SQLite immediately before its
        reservation is activated in memory.  Serializing exact cancellation
        on the same admission gate closes that commit-to-activate window: once
        this method owns the gate, the task is either cancellable in memory or
        definitively never became active.
        """

        key = canonicalize_session_key(session_key)
        async with self.collect_admission(key):
            return await self.cancel(
                task_id=task_id,
                session_key=key,
                source=source,
                reason=reason,
            )

    async def _cancel_runtime_tasks(
        self,
        candidates: Sequence[_RuntimeTask],
        *,
        source: str | None,
        reason: str | None,
    ) -> int:
        """Atomically close cancellation and same-turn acceptance for a batch."""

        queued_tasks: list[_RuntimeTask] = []
        ordered_candidates = sorted(
            {task.task_id: task for task in candidates}.values(),
            key=lambda task: task.task_id,
        )
        async with contextlib.AsyncExitStack() as steer_fences:
            for task in ordered_candidates:
                await steer_fences.enter_async_context(task.steer_claim)
            async with self._state_lock:
                tasks = [
                    task
                    for task in ordered_candidates
                    if self._tasks.get(task.task_id) is task
                    and task.status not in TERMINAL_STATUSES
                ]
                for task in tasks:
                    if (
                        task.status == AgentTaskStatus.QUEUED
                        and not task.execution_started
                    ):
                        queued_tasks.append(task)
                    task.cancel_requested = True
                    task.cancel_source = _clean_cancel_detail(source, "unknown")
                    task.cancel_reason = _clean_cancel_detail(reason, "cancelled")
            # Persist the user/system cancellation distinction before signalling
            # the coroutine. If the process dies during disposition cleanup,
            # startup recovery can still decide whether pending steer text must
            # be restored to the composer or promoted as system-abandoned work.
            for task in tasks:
                try:
                    existing = await self._storage.get_agent_task(task.task_id)
                    details_raw = getattr(existing, "details", None)
                    details = (
                        dict(details_raw)
                        if isinstance(details_raw, dict)
                        else {}
                    )
                    details["cancellation_requested"] = {
                        "source": task.cancel_source,
                        "reason": task.cancel_reason,
                    }
                    await self._storage.update_agent_task(
                        task.task_id,
                        details=details,
                    )
                except Exception:  # noqa: BLE001 - cancellation must still proceed
                    log.warning(
                        "task_runtime.cancellation_request_persist_failed",
                        task_id=task.task_id,
                        session_key=task.envelope.session_key,
                        exc_info=True,
                    )
            for task in tasks:
                if task.asyncio_task is not None and not task.asyncio_task.done():
                    task.asyncio_task.cancel()
        # A coroutine cancelled before its first event-loop step never enters
        # ``_execute`` and therefore cannot run its CancelledError cleanup.
        # Finalise only tasks whose coroutine never started. A started task may
        # still report QUEUED while it waits for a collect claim; synchronously
        # waiting for that same claim here would deadlock cancel() against the
        # collector. Started tasks finish through _execute's cancellation path.
        for task in queued_tasks:
            await self._mark_terminal(
                task,
                AgentTaskStatus.CANCELLED,
                terminal_reason="cancelled_before_start",
            )
        return len(tasks)

    async def send(
        self,
        session_key: str,
        message: str,
        provenance: dict[str, Any] | None = None,
        stream_event_sink: TaskStreamEventSink | None = None,
    ) -> TaskHandle:
        """Enqueue a system follow-up without classifying it as a user turn."""
        session_key = canonicalize_session_key(session_key)
        cached = self._last_envelope_by_session.get(session_key)
        if cached is None:
            envelope = RouteEnvelope(
                source_kind=SourceKind.SYSTEM,
                source_name="task_runtime",
                agent_id=parse_agent_id(session_key),
                session_key=session_key,
                input_provenance=provenance or {"kind": "runtime_send"},
            )
            return await self.enqueue(
                envelope,
                message,
                mode="followup",
                run_kind="runtime_send",
                stream_event_sink=stream_event_sink,
            )
        cached = _reusable_route_envelope(cached)
        if provenance is None:
            return await self.enqueue(
                cached,
                message,
                mode="followup",
                run_kind="runtime_send",
                stream_event_sink=stream_event_sink,
            )
        # Caller-provided provenance is a one-shot override: build an
        # ephemeral envelope from the cached metadata but with this
        # provenance, and skip writing it back to the cache so subsequent
        # ``send(provenance=None)`` calls fall back to the original cached
        # provenance instead of inheriting the override.
        ephemeral = replace(cached, input_provenance=provenance)
        return await self.enqueue(
            ephemeral,
            message,
            mode="followup",
            run_kind="runtime_send",
            stream_event_sink=stream_event_sink,
            update_envelope_cache=False,
        )

    async def send_with_envelope(
        self,
        envelope: RouteEnvelope,
        message: str,
        provenance: dict[str, Any] | None = None,
        stream_event_sink: TaskStreamEventSink | None = None,
        accepted_run_mode_override: Any | None = None,
    ) -> TaskHandle:
        """Send a follow-up while preserving authoritative routing and mode context."""
        if not isinstance(envelope, RouteEnvelope):
            raise TypeError("envelope must be a RouteEnvelope")
        canonical_session_key = canonicalize_session_key(envelope.session_key)
        routed = replace(
            envelope,
            session_key=canonical_session_key,
            agent_id=parse_agent_id(canonical_session_key),
            input_provenance=provenance or envelope.input_provenance,
        )
        return await self.enqueue(
            _reusable_route_envelope(routed),
            message,
            mode="followup",
            stream_event_sink=stream_event_sink,
            accepted_run_mode_override=accepted_run_mode_override,
            update_envelope_cache=False,
        )

    async def wait(self, task_id: str, timeout: float | None = None) -> AgentTaskRecord:
        runtime_task = self._tasks.get(task_id)
        if runtime_task is None:
            return await self.status(task_id)
        await asyncio.wait_for(runtime_task.done.wait(), timeout=timeout)
        return await self.status(task_id)

    async def shutdown(
        self,
        *,
        cancel: bool = True,
        timeout: float = 5.0,
        graceful: bool = False,
        graceful_timeout: float | None = None,
    ) -> TaskRuntimeShutdownResult:
        """Shut down all in-flight tasks.

        Parameters
        ----------
        cancel:
            When ``True`` (default), cancel all in-flight tasks immediately
            before waiting.  Set to ``False`` for a drain-only wait.
        timeout:
            How long to wait for tasks after cancellation (or without it when
            ``cancel=False``).  Tasks still running after this deadline are
            marked ABANDONED.
        graceful:
            Convenience flag for graceful-drain mode: waits for all in-flight
            tasks to complete naturally before falling back to cancel.  When
            ``True``, ``cancel`` is ignored for the initial wait phase and the
            ``graceful_timeout`` deadline is used.  After the deadline (if any),
            remaining tasks are cancelled with a short ``timeout`` wait.
        graceful_timeout:
            Deadline (seconds) for the graceful drain phase.  ``None`` means
            wait indefinitely (use with care in production; set a finite value).

        The first call closes admission and owns the shutdown budgets. Concurrent
        and later callers await that same process-lifetime shutdown result.
        """
        async with self._state_lock:
            shutdown_task = self._shutdown_task
            if shutdown_task is None:
                self._closing = True
                shutdown_task = asyncio.create_task(
                    self._shutdown_impl(
                        cancel=cancel,
                        timeout=timeout,
                        graceful=graceful,
                        graceful_timeout=graceful_timeout,
                    )
                )
                self._shutdown_task = shutdown_task
                self._signal_driver_state_changed()
        return await asyncio.shield(shutdown_task)

    async def _shutdown_impl(
        self,
        *,
        cancel: bool,
        timeout: float,
        graceful: bool,
        graceful_timeout: float | None,
    ) -> TaskRuntimeShutdownResult:
        started = time.monotonic()
        abandoned_task_count = 0

        async with self._state_lock:
            auxiliary_tasks = {
                task
                for task in self._auxiliary_tasks_by_session.values()
                if not task.done()
            }
        for auxiliary_task in auxiliary_tasks:
            auxiliary_task.cancel()

        if graceful:
            graceful_deadline = (
                None
                if graceful_timeout is None
                else time.monotonic() + max(0.0, graceful_timeout)
            )
            if await self._wait_for_shutdown_quiescence(deadline=graceful_deadline):
                return await self._shutdown_result(started, abandoned_task_count)
            drivers, reservations, auxiliaries = await self._shutdown_counts()
            log.warning(
                "task_runtime.graceful_shutdown_timeout",
                graceful_timeout=graceful_timeout,
                remaining_drivers=drivers,
                remaining_reservations=reservations,
                remaining_auxiliary=auxiliaries,
            )

        cancel_deadline = time.monotonic() + max(0.0, timeout)
        cancelled_drivers: set[asyncio.Task[None]] = set()
        if cancel:
            await self._request_shutdown_cancellation(
                source="gateway_shutdown",
                reason="graceful_timeout" if graceful else "shutdown",
                cancelled_drivers=cancelled_drivers,
            )
        if await self._wait_for_shutdown_quiescence(
            deadline=cancel_deadline,
            cancel_source=("gateway_shutdown" if cancel else None),
            cancel_reason=("graceful_timeout" if graceful else "shutdown"),
            cancelled_drivers=cancelled_drivers,
        ):
            return await self._shutdown_result(started, abandoned_task_count)

        # Claim and persist the timeout terminal state before delivering the
        # final cancellation. Otherwise the driver can win the cancellation
        # race, publish an earlier classification, and leave the shutdown
        # coordinator without a durable ABANDONED record at return time.
        marked_abandoned = await self._mark_unfinished_abandoned()
        timed_out_tasks = await self._request_shutdown_cancellation(
            source="gateway_shutdown_timeout",
            reason="shutdown_timeout",
            cancelled_drivers=cancelled_drivers,
            force=True,
        )
        abandoned_task_count = max(timed_out_tasks, marked_abandoned)
        return await self._shutdown_result(started, abandoned_task_count)

    async def _request_shutdown_cancellation(
        self,
        *,
        source: str,
        reason: str,
        cancelled_drivers: set[asyncio.Task[None]],
        force: bool = False,
    ) -> int:
        """Cancel every currently visible driver once per shutdown phase."""

        async with self._state_lock:
            runtime_tasks = [
                task
                for task in self._tasks.values()
                if task.status not in TERMINAL_STATUSES
                and (
                    force
                    or not task.cancel_requested
                    or task.cancel_source != source
                )
            ]
            drivers = {
                driver
                for session_drivers in self._driver_tasks_by_session.values()
                for driver in session_drivers
                if not driver.done()
            }
            runtime_drivers = {
                task.asyncio_task
                for task in runtime_tasks
                if task.asyncio_task is not None
            }
            for task in runtime_tasks:
                if task.asyncio_task is not None:
                    cancelled_drivers.add(task.asyncio_task)

        if runtime_tasks:
            await self._cancel_runtime_tasks(
                runtime_tasks,
                source=source,
                reason=reason,
            )
        for driver in drivers:
            if driver not in runtime_drivers and (
                force or driver not in cancelled_drivers
            ):
                driver.cancel()
                cancelled_drivers.add(driver)
        return len(runtime_tasks)

    async def _wait_for_shutdown_quiescence(
        self,
        *,
        deadline: float | None,
        cancel_source: str | None = None,
        cancel_reason: str | None = None,
        cancelled_drivers: set[asyncio.Task[None]] | None = None,
    ) -> bool:
        """Wait for the driver registry and admission gap to reach a fixed point."""

        if cancelled_drivers is None:
            cancelled_drivers = set()
        while True:
            if cancel_source is not None:
                await self._request_shutdown_cancellation(
                    source=cancel_source,
                    reason=cancel_reason or "shutdown",
                    cancelled_drivers=cancelled_drivers,
                )

            async with self._state_lock:
                state_changed = self._driver_state_changed
                drivers = {
                    driver
                    for session_drivers in self._driver_tasks_by_session.values()
                    for driver in session_drivers
                }
                reservations = sum(
                    len(items) for items in self._reservations_by_session.values()
                )
                auxiliaries = {
                    task for task in self._auxiliary_tasks_by_session.values()
                }
            if not drivers and reservations == 0 and not auxiliaries:
                return True

            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False

            state_waiter = asyncio.create_task(state_changed.wait())
            waiters: set[asyncio.Task[Any]] = {
                state_waiter,
                *(driver for driver in drivers if not driver.done()),
                *(task for task in auxiliaries if not task.done()),
            }
            done, _pending = await asyncio.wait(
                waiters,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not state_waiter.done():
                state_waiter.cancel()
                await asyncio.gather(state_waiter, return_exceptions=True)
            if not done:
                return False

    async def _shutdown_counts(self) -> tuple[int, int, int]:
        async with self._state_lock:
            drivers = sum(
                len(items) for items in self._driver_tasks_by_session.values()
            )
            reservations = sum(
                len(items) for items in self._reservations_by_session.values()
            )
            auxiliaries = len(self._auxiliary_tasks_by_session)
        return drivers, reservations, auxiliaries

    async def _shutdown_result(
        self,
        started: float,
        abandoned_task_count: int,
    ) -> TaskRuntimeShutdownResult:
        drivers, reservations, auxiliaries = await self._shutdown_counts()
        return TaskRuntimeShutdownResult(
            clean=drivers == 0 and reservations == 0 and auxiliaries == 0,
            elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            abandoned_task_count=abandoned_task_count,
            remaining_driver_count=drivers,
            remaining_reservation_count=reservations,
            remaining_auxiliary_count=auxiliaries,
        )

    async def apply_overflow_policy(
        self,
        session_key: str,
        *,
        policy: PendingOverflowPolicy | str | None = None,
    ) -> None:
        """Public entry point for per-channel overflow enforcement.

        Channel adapters call this before issuing the per-session
        ``start_turn_via_runtime`` so they can override the runtime default
        (e.g. ``DROP_OLDEST`` for noisy realtime channels). When ``policy``
        is ``None`` the runtime's own default is used.
        """
        if self._max_pending_per_session is None:
            return
        resolved: PendingOverflowPolicy | None = None
        if policy is not None:
            try:
                resolved = PendingOverflowPolicy(policy)
            except ValueError as exc:
                valid = ", ".join(member.value for member in PendingOverflowPolicy)
                raise ValueError(f"overflow_policy must be one of {{{valid}}}") from exc
        await self._apply_overflow_policy(
            canonicalize_session_key(session_key),
            policy=resolved,
        )

    async def _apply_overflow_policy(
        self,
        session_key: str,
        *,
        policy: PendingOverflowPolicy | None = None,
    ) -> None:
        """Enforce ``max_pending_per_session`` per the resolved policy.

        ``policy`` overrides the runtime default for this single call so a
        channel adapter may pick its own behaviour (e.g. ``DROP_OLDEST`` for
        noisy realtime channels).

        Holds ``_state_lock`` only while inspecting/snapshotting pending state
        and (for ``drop_oldest``) selecting the eviction candidate. The
        cancellation work itself runs outside the lock so ``_mark_terminal``
        can re-acquire ``_state_lock`` safely.
        """
        assert self._max_pending_per_session is not None
        if policy is None:
            policy = self._pending_overflow_policy
        async with self._state_lock:
            pending = list(self._pending_by_session.get(session_key, []))
            pending_count = len(pending)
            victim: _RuntimeTask | None = None
            if pending_count >= self._max_pending_per_session:
                if policy == PendingOverflowPolicy.DROP_OLDEST:
                    victim = next(
                        (task for task in pending if task.status == AgentTaskStatus.QUEUED),
                        None,
                    )
                if policy != PendingOverflowPolicy.DROP_OLDEST or victim is None:
                    _emit_metric(
                        "queue_full_errors_total",
                        value=1,
                        session_key=session_key,
                        policy=str(policy),
                    )
                    raise TaskQueueFullError(
                        session_key=session_key,
                        max_pending=self._max_pending_per_session,
                    )
                # Mark before releasing the lock so a concurrent enqueue
                # cannot pick the same victim and double-cancel.
                victim.cancel_requested = True
                victim.overflow_dropped = True
        if victim is not None:
            _emit_metric(
                "queue_full_errors_total",
                value=1,
                session_key=session_key,
                policy=str(PendingOverflowPolicy.DROP_OLDEST),
                action="dropped_oldest",
            )
            # Cancel the asyncio task driving _execute(). The asyncio.Lock
            # acquire path may swallow the cancel via a race when the lock
            # holder releases at the same instant, so we always finalise
            # the record ourselves: _mark_terminal is idempotent (guarded
            # by terminal_closing) so a redundant call from the _execute
            # cancel branch is a no-op.
            asyncio_task = victim.asyncio_task
            if asyncio_task is not None and not asyncio_task.done():
                asyncio_task.cancel()
            await self._mark_terminal(
                victim,
                AgentTaskStatus.CANCELLED,
                terminal_reason="dropped_by_overflow",
            )

    async def _try_collect(
        self,
        *,
        envelope: RouteEnvelope,
        message: str,
        attachments: builtins.list[dict[str, Any]] | None = None,
        run_kind: str,
        no_memory_capture: bool,
        semantic_message: str | None = None,
        persisted_user_message_id: str | None = None,
        persisted_user_message_ids: builtins.list[str] | tuple[str, ...] | None = None,
        message_count: int = 1,
        accepted_run_mode_override: Any | None = None,
    ) -> TaskHandle | None:
        async def persist(
            handle: TaskHandle,
            details: dict[str, Any],
        ) -> None:
            identity_rebound = False
            metadata = envelope.metadata
            if persisted_user_message_id:
                try:
                    revision = max(
                        2,
                        int(metadata.get("turn_context_revision", 1) or 1) + 1,
                    )
                except (TypeError, ValueError):
                    revision = 2
                try:
                    context: dict[str, Any] = {
                        "turn_id": handle.task_id,
                        "client_message_id": metadata.get("client_message_id"),
                        "surface_id": metadata.get("surface_id"),
                        "intent": metadata.get("turn_context_intent", "send"),
                        "disposition": "queued",
                        "target_turn_id": handle.task_id,
                        "revision": revision,
                    }
                    client_request_id = metadata.get("client_request_id")
                    if isinstance(client_request_id, str) and client_request_id:
                        context["client_request_id"] = client_request_id
                    identity_rebound = await self._update_transcript_turn_context(
                        envelope.session_key,
                        persisted_user_message_id,
                        context,
                    )
                except Exception as exc:
                    log.warning(
                        "task_runtime.collect_identity_rebind_failed",
                        session_key=envelope.session_key,
                        candidate_task_id=handle.task_id,
                        message_id=persisted_user_message_id,
                        exc_info=True,
                    )
                    raise _CollectIdentityRebindError from exc
                if not identity_rebound:
                    raise _CollectIdentityRebindError
            try:
                await self._storage.update_agent_task(handle.task_id, details=details)
            except Exception:
                # Collect acceptance is owned by the queued runtime task (and,
                # for identity-aware input, the transcript rebind above).
                # Agent-task details are diagnostic only. Surfacing their write
                # failure would invite the caller to retry an input that this
                # process has already accepted and will execute.
                log.warning(
                    "task_runtime.collect_details_update_failed",
                    session_key=envelope.session_key,
                    task_id=handle.task_id,
                    exc_info=True,
                )

        try:
            collected = await self.try_collect_atomically(
                envelope=envelope,
                message=message,
                attachments=attachments,
                run_kind=run_kind,
                no_memory_capture=no_memory_capture,
                semantic_message=semantic_message,
                persisted_user_message_id=persisted_user_message_id,
                persisted_user_message_ids=persisted_user_message_ids,
                message_count=message_count,
                accepted_run_mode_override=accepted_run_mode_override,
                persist=persist,
            )
        except _CollectIdentityRebindError:
            return None
        return collected[0] if collected is not None else None

    async def try_collect_atomically(
        self,
        *,
        envelope: RouteEnvelope,
        message: str,
        attachments: builtins.list[dict[str, Any]] | None = None,
        run_kind: str,
        no_memory_capture: bool,
        semantic_message: str | None = None,
        persisted_user_message_id: str | None = None,
        persisted_user_message_ids: builtins.list[str] | tuple[str, ...] | None = None,
        message_count: int = 1,
        accepted_run_mode_override: Any | None = None,
        persist: Callable[
            [TaskHandle, dict[str, Any]], Awaitable[_CollectResult]
        ],
    ) -> tuple[TaskHandle, _CollectResult] | None:
        """Persist and apply one collect while the candidate remains queued.

        Persistence runs under a per-task claim, never the runtime-wide state
        lock. Running and terminal transitions for the candidate wait for that
        claim; unrelated sessions continue reserving, cancelling, and
        finalising normally. A raised persistence error leaves the candidate
        unchanged. Receipt replays are returned without applying the input a
        second time.
        """

        operation = asyncio.create_task(
            self._try_collect_atomically_impl(
                envelope=envelope,
                message=message,
                attachments=attachments,
                run_kind=run_kind,
                no_memory_capture=no_memory_capture,
                semantic_message=semantic_message,
                persisted_user_message_id=persisted_user_message_id,
                persisted_user_message_ids=persisted_user_message_ids,
                message_count=message_count,
                accepted_run_mode_override=accepted_run_mode_override,
                persist=persist,
            )
        )
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # Once collection begins, settle both persistence and the matching
            # runtime apply before propagating cancellation to the caller.
            await self._wait_for_task_settlement(operation)
            raise

    async def _try_collect_atomically_impl(
        self,
        *,
        envelope: RouteEnvelope,
        message: str,
        attachments: builtins.list[dict[str, Any]] | None,
        run_kind: str,
        no_memory_capture: bool,
        semantic_message: str | None,
        persisted_user_message_id: str | None,
        persisted_user_message_ids: builtins.list[str] | tuple[str, ...] | None,
        message_count: int,
        accepted_run_mode_override: Any | None,
        persist: Callable[
            [TaskHandle, dict[str, Any]], Awaitable[_CollectResult]
        ],
    ) -> tuple[TaskHandle, _CollectResult] | None:
        """Claim, persist, then apply one collection operation."""

        envelope = replace(
            envelope,
            agent_id=normalize_agent_id(envelope.agent_id),
            session_key=canonicalize_session_key(envelope.session_key),
        )
        async with self._state_lock:
            pending = self._pending_by_session.get(envelope.session_key, [])
            candidate = next(
                (
                    task
                    for task in reversed(pending)
                    if task.queue_mode == "collect" and task.status == AgentTaskStatus.QUEUED
                ),
                None,
            )
            if candidate is None:
                return None

        async with candidate.collect_claim:
            # The task may have crossed into RUNNING while this coroutine was
            # waiting for an earlier per-task claim. Re-check under the state
            # lock before any durable side effect.
            async with self._state_lock:
                pending = self._pending_by_session.get(envelope.session_key, [])
                if (
                    candidate not in pending
                    or candidate.queue_mode != "collect"
                    or candidate.status != AgentTaskStatus.QUEUED
                ):
                    return None
                if (
                    candidate.accepted_run_mode_override
                    != accepted_run_mode_override
                ):
                    return None
                collected_no_memory_capture = candidate.no_memory_capture
                if (
                    no_memory_capture
                    or candidate.run_kind != run_kind
                    or candidate.envelope.input_provenance != envelope.input_provenance
                ):
                    collected_no_memory_capture = True
                collected_message = f"{candidate.message}\n{message}"
                if candidate.semantic_message is not None or semantic_message is not None:
                    first_semantic = (
                        candidate.semantic_message
                        if candidate.semantic_message is not None
                        else candidate.message
                    )
                    next_semantic = (
                        semantic_message if semantic_message is not None else message
                    )
                    collected_semantic_message = f"{first_semantic}\n\n{next_semantic}"
                else:
                    collected_semantic_message = None
                collected_message_ids = _ordered_message_ids(
                    candidate.persisted_user_message_id,
                    (
                        *candidate.persisted_user_message_ids,
                        *(
                            [persisted_user_message_id]
                            if persisted_user_message_id
                            else []
                        ),
                        *(persisted_user_message_ids or ()),
                    ),
                )
                collected_message_count = candidate.message_count + max(
                    1, int(message_count)
                )
                metadata = envelope.metadata
                collected_identity: _CollectedPrimaryInput | None = None
                if persisted_user_message_id or metadata.get("client_message_id"):
                    client_request_id = metadata.get("client_request_id")
                    if not isinstance(client_request_id, str) or not client_request_id:
                        client_request_id = None
                    try:
                        revision = max(
                            2,
                            int(metadata.get("turn_context_revision", 1) or 1) + 1,
                        )
                    except (TypeError, ValueError):
                        revision = 2
                    collected_identity = _CollectedPrimaryInput(
                        persisted_user_message_id=persisted_user_message_id,
                        client_request_id=client_request_id,
                        client_message_id=metadata.get("client_message_id"),
                        surface_id=metadata.get("surface_id"),
                        intent=metadata.get("turn_context_intent", "send"),
                        revision=revision,
                    )
                details = {
                    "source_name": candidate.envelope.source_name,
                    "input_provenance": candidate.envelope.input_provenance,
                    "metadata": candidate.envelope.metadata,
                    "collected": True,
                    "message_count": collected_message_count,
                    "no_memory_capture": collected_no_memory_capture,
                    "persisted_user_message_id": (
                        collected_message_ids[0] if collected_message_ids else None
                    ),
                    "persisted_user_message_ids": collected_message_ids,
                    "fresh_user_session": candidate.fresh_user_session,
                }
                handle = TaskHandle(
                    task_id=candidate.task_id,
                    session_key=envelope.session_key,
                    status=AgentTaskStatus.QUEUED,
                )

            persisted = await persist(handle, details)
            if getattr(persisted, "replayed", False) is not True:
                # The claim prevents running/terminal transitions while the DB
                # operation settles. Apply every aggregate field together only
                # after a non-replay commit.
                async with self._state_lock:
                    # ``accept_turn`` may attach, refresh, or discard a Goal
                    # candidate while it atomically merges this input into the
                    # queued task. Mirror that authoritative durable result in
                    # memory before the task can cross queued -> running.
                    accepted_goal_context = getattr(
                        persisted,
                        "goal_context",
                        _MISSING_GOAL_ACCEPTANCE,
                    )
                    accepted_goal_candidate = getattr(
                        persisted,
                        "goal_candidate",
                        _MISSING_GOAL_ACCEPTANCE,
                    )
                    if accepted_goal_context is not _MISSING_GOAL_ACCEPTANCE:
                        candidate.goal_context = (
                            cast(Any, accepted_goal_context).as_task_detail()
                            if accepted_goal_context is not None
                            else None
                        )
                        if accepted_goal_context is not None:
                            candidate.goal_candidate = None
                    if (
                        accepted_goal_context is None
                        and accepted_goal_candidate is not _MISSING_GOAL_ACCEPTANCE
                    ):
                        candidate.goal_candidate = (
                            cast(Any, accepted_goal_candidate).as_task_detail()
                            if accepted_goal_candidate is not None
                            else None
                        )
                    candidate.no_memory_capture = collected_no_memory_capture
                    candidate.message = collected_message
                    candidate.attachments.extend(list(attachments or []))
                    candidate.semantic_message = collected_semantic_message
                    candidate.persisted_user_message_ids = collected_message_ids
                    candidate.persisted_user_message_id = (
                        collected_message_ids[0] if collected_message_ids else None
                    )
                    candidate.message_count = collected_message_count
                    if collected_identity is not None:
                        candidate.collected_primary_inputs.append(collected_identity)
            return handle, persisted

    async def _execute(self, task: _RuntimeTask) -> None:
        # Set before the first await so cancellation can distinguish a
        # never-started coroutine from one that owns runtime cleanup, even
        # while its durable status is still QUEUED.
        task.execution_started = True
        session_key = task.envelope.session_key
        write_lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        execution_lock = self._session_execution_locks.setdefault(session_key, asyncio.Lock())
        try:
            async with execution_lock:
                if task.cancel_requested:
                    reason = "overflow_drop" if task.overflow_dropped else "user_cancel"
                    terminal_reason = (
                        "dropped_by_overflow" if task.overflow_dropped else "cancelled_before_start"
                    )
                    _emit_metric(
                        "turn_cancellations_total",
                        value=1,
                        reason=reason,
                        session_key=task.envelope.session_key,
                    )
                    await self._mark_terminal(
                        task,
                        AgentTaskStatus.CANCELLED,
                        terminal_reason=terminal_reason,
                    )
                    return
                await self._wait_for_subagent_slot(task)
                acquired = False
                heartbeat_task: asyncio.Task[None] | None = None
                try:
                    await self._acquire_fair_slot(task)
                    acquired = True
                    async with write_lock:
                        pass
                    heartbeat_task = self._start_running_heartbeat(task)
                    metadata = task.envelope.metadata
                    turn_context = {
                        "turn_id": task.task_id,
                        "client_message_id": metadata.get("client_message_id"),
                        "surface_id": metadata.get("surface_id"),
                        "intent": metadata.get("turn_context_intent", "send"),
                        "disposition": metadata.get(
                            "turn_context_disposition",
                            "applied",
                        ),
                        "revision": int(metadata.get("turn_context_revision", 1) or 1),
                    }
                    client_request_id = metadata.get("client_request_id")
                    if isinstance(client_request_id, str) and client_request_id:
                        turn_context["client_request_id"] = client_request_id
                    meta_control = metadata.get("meta_control")
                    if isinstance(meta_control, dict):
                        turn_context["meta_control"] = dict(meta_control)
                    if (
                        metadata.get("collaboration_mode") == "plan"
                        or int(metadata.get("collaboration_revision", 0) or 0) > 0
                        or metadata.get("active_plan_revision_id")
                        or metadata.get("plan_run_id")
                    ):
                        turn_context["collaboration_mode"] = metadata.get(
                            "collaboration_mode",
                            "default",
                        )
                        turn_context["collaboration_revision"] = int(
                            metadata.get("collaboration_revision", 0) or 0
                        )
                    if metadata.get("active_plan_revision_id"):
                        turn_context["active_plan_revision_id"] = metadata[
                            "active_plan_revision_id"
                        ]
                    if metadata.get("plan_run_id"):
                        turn_context["plan_run_id"] = metadata["plan_run_id"]
                    for field in ("target_turn_id", "promoted_from_turn_id"):
                        value = metadata.get(field)
                        if isinstance(value, str) and value:
                            turn_context[field] = value
                    # Promotion updates every merged steer before this queued
                    # follow-up can acquire the same-session execution lock.
                    # Do not publish the same transition twice at start.
                    identity_tracked = bool(
                        task.persisted_user_message_id
                        or metadata.get("client_message_id")
                    )
                    if turn_context["disposition"] != "promoted" and identity_tracked:
                        await self._update_transcript_turn_context(
                            task.envelope.session_key,
                            task.persisted_user_message_id,
                            turn_context,
                        )
                        await self._emit(
                            task.envelope.session_key,
                            "session.event.input_disposition",
                            {
                                "session_key": task.envelope.session_key,
                                "user_message_id": task.persisted_user_message_id,
                                **turn_context,
                            },
                        )
                        task.primary_input_pending = False
                    await self._record_collected_primary_inputs_applied(task)
                    run = TaskRun(
                        task_id=task.task_id,
                        envelope=task.envelope,
                        message=task.message,
                        attachments=task.attachments,
                        queue_mode=task.queue_mode,
                        run_kind=task.run_kind,
                        no_memory_capture=task.no_memory_capture,
                        input_mode=task.input_mode,
                        persist_input=task.persist_input,
                        history_has_persisted_user=task.history_has_persisted_user,
                        goal_context=task.goal_context,
                        ingress_pipeline_steps=task.ingress_pipeline_steps,
                        semantic_message=task.semantic_message,
                        persisted_user_message_id=task.persisted_user_message_id,
                        persisted_user_message_ids=tuple(
                            task.persisted_user_message_ids
                        ),
                        fresh_user_session=task.fresh_user_session,
                        stream_event_sink=task.stream_event_sink,
                        pending_input_provider=task.pending_input_provider,
                        accepted_config=task.accepted_config,
                        accepted_run_mode_override=task.accepted_run_mode_override,
                        provider_request_correlation=task.provider_request_correlation,
                        assistant_message_sink=(
                            task.capture_terminal_assistant_message
                            if task.run_kind == "channel_turn"
                            else None
                        ),
                    )
                    from opensquilla.session.turn_context import turn_context_scope

                    with turn_context_scope(turn_context):
                        await self._run_turn_handler_with_write_lock_bypass(
                            run,
                            write_lock=write_lock,
                        )
                    await self._emit_plan_revision_if_changed(task)
                    await self._record_drained_steers(task)
                    if heartbeat_task is not None:
                        await self._stop_running_heartbeat(heartbeat_task)
                        heartbeat_task = None
                    if acquired:
                        await self._release_slot(task)
                        acquired = False
                    await self._mark_terminal(
                        task,
                        AgentTaskStatus.SUCCEEDED,
                        terminal_reason="completed",
                        promote_pending_steers=True,
                    )
                finally:
                    if heartbeat_task is not None:
                        await self._stop_running_heartbeat(heartbeat_task)
                    if acquired:
                        await self._release_slot(task)
        except asyncio.CancelledError:
            shutdown_timed_out = task.cancel_source == "gateway_shutdown_timeout"
            reason = (
                "shutdown_timeout"
                if shutdown_timed_out
                else "overflow_drop"
                if task.overflow_dropped
                else "interrupt"
            )
            terminal_reason = (
                "shutdown_timeout"
                if shutdown_timed_out
                else "dropped_by_overflow"
                if task.overflow_dropped
                else "cancelled"
            )
            terminal_status = (
                AgentTaskStatus.ABANDONED
                if shutdown_timed_out
                else AgentTaskStatus.CANCELLED
            )
            _emit_metric(
                "turn_cancellations_total",
                value=1,
                reason=reason,
                session_key=task.envelope.session_key,
            )
            # Close steer acceptance atomically before reclaiming inputs or
            # awaiting their durable disposition writes. Otherwise a steer can
            # land after reclaim_all() and remain permanently ``steering`` once
            # _mark_terminal() removes the task.
            async with self._state_lock:
                if not task.terminal_closing:
                    task.status = terminal_status
            system_cancel_sources = {
                "gateway_shutdown",
                "gateway_shutdown_timeout",
                "parent_session_kill",
                "queue_interrupt",
                "queue_overflow",
                "queue_steer",
                "sessions_reset",
            }
            # An unspecified direct TaskRuntime.cancel() is retained as the
            # historical user-stop behavior. System callers must identify
            # themselves so accepted steer text is promoted instead.
            explicit_user_stop = task.cancel_source not in system_cancel_sources
            if explicit_user_stop:
                await self._record_cancelled_steers(task)
            else:
                # System cancellation does not discard an accepted user steer.
                # Persist already-applied inputs, close this task, then transfer
                # pending ownership to one follow-up.
                await self._record_drained_steers(task)
            await self._mark_terminal(
                task,
                terminal_status,
                terminal_reason=terminal_reason,
                promote_pending_steers=not explicit_user_stop,
                activate_promoted_steers=task.cancel_source
                not in {"gateway_shutdown", "gateway_shutdown_timeout"},
            )
        except _TurnHardDeadlineExceeded as exc:
            _emit_metric(
                "turn_cancellations_total",
                value=1,
                reason="hard_deadline",
                session_key=task.envelope.session_key,
            )
            await self._record_drained_steers(task)
            await self._mark_terminal(
                task,
                AgentTaskStatus.TIMEOUT,
                terminal_reason="hard_deadline_exceeded",
                error_class=type(exc).__name__,
                error_message=str(exc),
                promote_pending_steers=True,
            )
        except TimeoutError as exc:
            _emit_metric(
                "turn_cancellations_total",
                value=1,
                reason="timeout",
                session_key=task.envelope.session_key,
            )
            await self._record_drained_steers(task)
            await self._mark_terminal(
                task,
                AgentTaskStatus.TIMEOUT,
                terminal_reason="timeout",
                error_class=type(exc).__name__,
                error_message=str(exc),
                promote_pending_steers=True,
            )
        except Exception as exc:  # noqa: BLE001 - runtime ledger records the class.
            terminal_reason = str(getattr(exc, "terminal_reason", None) or "error")
            failure_kind = str(getattr(exc, "failure_kind", None) or "") or None
            status = (
                AgentTaskStatus.TIMEOUT if terminal_reason == "timeout" else AgentTaskStatus.FAILED
            )
            await self._record_drained_steers(task)
            await self._mark_terminal(
                task,
                status,
                terminal_reason=terminal_reason,
                error_class=str(getattr(exc, "code", None) or type(exc).__name__),
                error_message=str(exc),
                failure_kind=failure_kind,
                retry_after_ms=safe_retry_after_ms(getattr(exc, "retry_after_ms", None)),
                activity_snapshot=getattr(exc, "activity_snapshot", None),
                usage_call_index=getattr(exc, "usage_call_index", None),
                no_prior_provider_dispatch=(
                    getattr(exc, "no_prior_provider_dispatch", False) is True
                ),
                replay_safe=getattr(exc, "replay_safe", False) is True,
                promote_pending_steers=True,
            )
        finally:
            self._user_input_broker.cancel_task(task.task_id)
            await self._settle_attached_plan_run(task)
            _cleanup_guest_profile(task)

    async def _freeze_collaboration_context(self, task: _RuntimeTask) -> None:
        """Snapshot session collaboration state at the actual turn boundary.

        A queued task intentionally does not capture Plan/Default at admission:
        a user may toggle while earlier work is still running. Once this task
        owns both the same-session execution lane and a global slot, the
        snapshot is immutable for the complete provider/tool loop.
        """

        metadata = dict(task.envelope.metadata)
        getter = getattr(self._storage, "get_session", None)
        node = None
        if callable(getter):
            candidate = getter(task.envelope.session_key)
            node = await candidate if inspect.isawaitable(candidate) else candidate
        stored_mode = getattr(node, "collaboration_mode", None)
        stored_revision = getattr(node, "collaboration_revision", None)
        if stored_mode in {"default", "plan"} and isinstance(stored_revision, int):
            metadata["collaboration_mode"] = stored_mode
            metadata["collaboration_revision"] = stored_revision
            active_revision = getattr(node, "active_plan_revision_id", None)
            if isinstance(active_revision, str) and active_revision:
                metadata["active_plan_revision_id"] = str(active_revision)
            else:
                metadata.pop("active_plan_revision_id", None)
        else:
            metadata.setdefault("collaboration_mode", "default")
            metadata.setdefault("collaboration_revision", 0)
        required_mode = metadata.get("required_collaboration_mode")
        if required_mode is not None:
            if required_mode not in {"default", "plan"}:
                raise RuntimeError("Invalid required collaboration mode")
            # Explicit Plan operations own their turn capability. A sticky mode
            # toggle made while they wait applies to later ordinary turns; it
            # cannot turn an implementation into a read-only Plan turn, or a
            # replan into a write-capable Default turn.
            metadata["collaboration_mode"] = required_mode
        required_revision = metadata.get("required_collaboration_revision")
        if required_revision is not None:
            if (
                not isinstance(required_revision, int)
                or isinstance(required_revision, bool)
                or required_revision < 0
            ):
                raise RuntimeError("Invalid required collaboration revision")
            metadata["collaboration_revision"] = required_revision
        metadata["task_id"] = task.task_id
        runtime_services = {
            **task.envelope.runtime_services,
            "plan_storage": self._storage,
            "plan_event_emitter": self._emit,
        }
        # WebChat has a request-id response RPC and reconnect hydration. Other
        # interactive surfaces retain the terminating compatibility protocol
        # until they expose the same reply transport; injecting a waiter there
        # would strand the turn behind its own session execution lock.
        if task.envelope.source_kind is SourceKind.WEB:
            runtime_services["user_input_provider"] = self._user_input_broker
        attached_run_id = str(metadata.get("plan_run_id") or "").strip()
        if attached_run_id and not str(
            metadata.get("plan_revision_id") or ""
        ).strip():
            get_plan_run = getattr(self._storage, "get_plan_run", None)
            if not callable(get_plan_run):
                raise RuntimeError("PlanRun storage is unavailable")
            run_candidate = get_plan_run(attached_run_id)
            attached_run = (
                await run_candidate
                if inspect.isawaitable(run_candidate)
                else run_candidate
            )
            if attached_run is None:
                raise RuntimeError("The accepted PlanRun no longer exists")
            if str(getattr(attached_run, "session_key", "") or "") != (
                task.envelope.session_key
            ):
                raise RuntimeError("The accepted PlanRun belongs to another session")
            derived_revision_id = str(
                getattr(attached_run, "plan_revision_id", "") or ""
            ).strip()
            if not derived_revision_id:
                raise RuntimeError("The accepted PlanRun lost its PlanRevision binding")
            # Goal controllers only need to attach their durable run id. The
            # immutable revision is derived authoritatively instead of copied
            # into every future attempt envelope.
            metadata["plan_revision_id"] = derived_revision_id
        requested_revision_id = str(
            metadata.get("plan_revision_id")
            or (
                metadata.get("active_plan_revision_id")
                if metadata.get("collaboration_mode") == "plan"
                else ""
            )
            or ""
        ).strip()
        if requested_revision_id:
            active_revision_id = str(
                getattr(node, "active_plan_revision_id", "") or ""
            )
            if (
                bool(metadata.get("require_current_plan_revision"))
                and active_revision_id != requested_revision_id
            ):
                raise RuntimeError(
                    "The selected plan revision is no longer current"
                )
            get_revision = getattr(self._storage, "get_plan_revision", None)
            if not callable(get_revision):
                raise RuntimeError("PlanRevision storage is unavailable")
            revision_candidate = get_revision(requested_revision_id)
            plan_revision = (
                await revision_candidate
                if inspect.isawaitable(revision_candidate)
                else revision_candidate
            )
            if plan_revision is None:
                raise RuntimeError("The selected plan revision no longer exists")
            runtime_services["plan_revision"] = plan_revision
        previous_envelope = task.envelope
        task.envelope = replace(
            task.envelope,
            metadata=metadata,
            runtime_services=runtime_services,
        )
        # ``_last_envelope_by_session`` uses identity to avoid deleting a
        # newer queued envelope during terminal cleanup.  Preserve that
        # invariant when this turn replaces its envelope with the frozen
        # collaboration snapshot.
        async with self._state_lock:
            if (
                self._last_envelope_by_session.get(task.envelope.session_key)
                is previous_envelope
            ):
                self._last_envelope_by_session[task.envelope.session_key] = task.envelope
        if metadata["collaboration_mode"] == "plan":
            task.no_memory_capture = True
        plan_run = await self._start_attached_plan_run(task)
        if plan_run is not None:
            previous_envelope = task.envelope
            task.envelope = replace(
                task.envelope,
                runtime_services={
                    **task.envelope.runtime_services,
                    "plan_run": plan_run,
                },
            )
            async with self._state_lock:
                if (
                    self._last_envelope_by_session.get(task.envelope.session_key)
                    is previous_envelope
                ):
                    self._last_envelope_by_session[task.envelope.session_key] = task.envelope

    async def _start_attached_plan_run(self, task: _RuntimeTask) -> Any | None:
        run_id = str(task.envelope.metadata.get("plan_run_id") or "").strip()
        if not run_id:
            return None
        getter = getattr(self._storage, "get_plan_run", None)
        mark_running = getattr(self._storage, "mark_plan_run_running", None)
        if not callable(getter) or not callable(mark_running):
            raise RuntimeError("PlanRun storage is unavailable")
        current = await getter(run_id)
        if current is None:
            raise RuntimeError("The accepted PlanRun no longer exists")
        if str(getattr(current, "session_key", "") or "") != (
            task.envelope.session_key
        ):
            raise RuntimeError("The accepted PlanRun belongs to another session")
        expected_revision_id = str(
            task.envelope.metadata.get("plan_revision_id") or ""
        ).strip()
        if (
            expected_revision_id
            and str(getattr(current, "plan_revision_id", "") or "")
            != expected_revision_id
        ):
            raise RuntimeError("The accepted PlanRun changed its PlanRevision binding")
        updated = await mark_running(
            run_id,
            expected_state_revision=int(current.state_revision),
            active_task_id=task.task_id,
        )
        await self._emit_plan_run(task.envelope.session_key, updated)
        if str(getattr(updated, "status", "")) != "running":
            raise RuntimeError("The selected plan revision is no longer executable")
        return updated

    async def _settle_attached_plan_run(self, task: _RuntimeTask) -> None:
        """Pause an unfinished manual run when its single turn terminates."""

        run_id = str(task.envelope.metadata.get("plan_run_id") or "").strip()
        if not run_id:
            return
        getter = getattr(self._storage, "get_plan_run", None)
        complete = getattr(self._storage, "complete_plan_run", None)
        pause = getattr(self._storage, "pause_plan_run", None)
        cancel = getattr(self._storage, "cancel_plan_run", None)
        if not callable(getter):
            return
        try:
            current = await getter(run_id)
            if current is None:
                return
            status = str(getattr(current, "status", ""))
            if status == "queued" and callable(cancel):
                updated = await cancel(
                    run_id,
                    expected_state_revision=int(current.state_revision),
                    reason="implementation_turn_ended_before_start",
                    expected_active_task_id=task.task_id,
                )
            elif status == "running" and callable(pause):
                driver_kind = str(getattr(current, "driver_kind", "manual"))
                step_states = list(getattr(current, "step_states", []) or [])
                delivery_ready = (
                    getattr(current, "current_step_id", None) is None
                    and bool(step_states)
                    and all(
                        isinstance(state, dict)
                        and str(state.get("status") or "")
                        in {"completed", "skipped"}
                        for state in step_states
                    )
                )
                if (
                    task.status == AgentTaskStatus.SUCCEEDED
                    and delivery_ready
                    and callable(complete)
                ):
                    updated = await complete(
                        run_id,
                        expected_state_revision=int(current.state_revision),
                        expected_active_task_id=task.task_id,
                    )
                    await self._emit_plan_run(task.envelope.session_key, updated)
                    return
                task_outcome = str(
                    getattr(task.status, "value", task.status) or "unknown"
                )
                updated = await pause(
                    run_id,
                    expected_state_revision=int(current.state_revision),
                    reason=(
                        (
                            "manual_turn_finished"
                            if driver_kind == "manual"
                            else "goal_turn_finished"
                        )
                        if task.status == AgentTaskStatus.SUCCEEDED
                        else f"{driver_kind}_turn_{task_outcome}"
                    ),
                    expected_active_task_id=task.task_id,
                    expected_driver_kind=driver_kind,
                    expected_driver_id=(
                        str(getattr(current, "driver_id", "") or "") or None
                    ),
                )
            else:
                return
            await self._emit_plan_run(task.envelope.session_key, updated)
        except Exception:  # noqa: BLE001 - plan overlay must not mask task terminal state
            log.warning(
                "task_runtime.plan_run_settle_failed",
                session_key=task.envelope.session_key,
                task_id=task.task_id,
                plan_run_id=run_id,
                exc_info=True,
            )

    async def _emit_plan_run(self, session_key: str, run: Any) -> None:
        from opensquilla.session.plans import plan_run_snapshot

        await self._emit(
            session_key,
            "session.event.plan_run",
            {
                "session_key": session_key,
                "plan_run": plan_run_snapshot(run),
            },
        )

    async def _emit_plan_revision_if_changed(self, task: _RuntimeTask) -> None:
        getter = getattr(self._storage, "get_session", None)
        get_revision = getattr(self._storage, "get_plan_revision", None)
        if not callable(getter) or not callable(get_revision):
            return
        try:
            node_candidate = getter(task.envelope.session_key)
            node = (
                await node_candidate
                if inspect.isawaitable(node_candidate)
                else node_candidate
            )
            if getattr(node, "active_plan_revision_id", None) is not None and not isinstance(
                getattr(node, "active_plan_revision_id", None),
                str,
            ):
                return
            current_id = (
                str(getattr(node, "active_plan_revision_id", "") or "")
                if node is not None
                else ""
            )
            starting_id = str(
                task.envelope.metadata.get("active_plan_revision_id") or ""
            )
            if not current_id or current_id == starting_id:
                return
            revision_candidate = get_revision(current_id)
            revision = (
                await revision_candidate
                if inspect.isawaitable(revision_candidate)
                else revision_candidate
            )
            if revision is None:
                return
            from opensquilla.session.plans import plan_revision_snapshot

            await self._emit(
                task.envelope.session_key,
                "session.event.plan_revision",
                {
                    "session_key": task.envelope.session_key,
                    "plan_revision": plan_revision_snapshot(
                        revision,
                        current=True,
                    ),
                    "collaboration": {
                        "mode": str(
                            getattr(node, "collaboration_mode", "default")
                            or "default"
                        ),
                        "revision": int(
                            getattr(node, "collaboration_revision", 0) or 0
                        ),
                    },
                },
            )
            # Committing a revision also advances the collaboration CAS
            # revision.  Publish the authoritative snapshot so clients do not
            # have to guess that increment before their next mode mutation.
            await self._emit(
                task.envelope.session_key,
                "session.event.collaboration_mode",
                {
                    "session_key": task.envelope.session_key,
                    "collaboration": {
                        "mode": str(
                            getattr(node, "collaboration_mode", "default")
                            or "default"
                        ),
                        "revision": int(
                            getattr(node, "collaboration_revision", 0) or 0
                        ),
                        "appliesTo": "next_turn",
                    },
                },
            )
        except Exception:  # noqa: BLE001 - observer must not change task result
            log.warning(
                "task_runtime.plan_revision_emit_failed",
                session_key=task.envelope.session_key,
                task_id=task.task_id,
                exc_info=True,
            )

    @staticmethod
    def _is_explicit_user_cancelled_task(task: AgentTaskRecord) -> bool:
        if task.status not in {
            AgentTaskStatus.CANCELLED,
            AgentTaskStatus.ABANDONED,
        }:
            return False
        details = task.details if isinstance(task.details, dict) else {}
        cancellation_requested = details.get("cancellation_requested")
        if isinstance(cancellation_requested, dict):
            if cancellation_requested.get("reason") == "user_abort":
                return True
            source = str(cancellation_requested.get("source") or "")
            if source in {
                "sessions_abort",
                "webui_abort",
                "webui_escape",
                "webui_stop",
            }:
                return True
        cancellation = details.get("cancellation")
        if isinstance(cancellation, dict):
            if cancellation.get("reason") == "user_abort":
                return True
            source = str(cancellation.get("source") or "")
            if source in {
                "sessions_abort",
                "webui_abort",
                "webui_escape",
                "webui_stop",
            }:
                return True
        outcome = details.get("turn_outcome")
        if isinstance(outcome, dict):
            return str(outcome.get("cancellation_source") or "") in {
                "sessions_abort",
                "webui_abort",
                "webui_escape",
                "webui_stop",
            }
        return False

    @staticmethod
    def _restart_recovery_envelope(
        target_task: AgentTaskRecord,
        entries: Sequence[Any],
    ) -> RouteEnvelope | None:
        """Rebuild only route kinds whose authority does not need live handles."""

        try:
            source_kind = SourceKind(str(target_task.source_kind))
        except ValueError:
            return None
        if source_kind not in {SourceKind.WEB, SourceKind.CLI, SourceKind.SYSTEM}:
            return None
        if not entries:
            return None
        details = target_task.details if isinstance(target_task.details, dict) else {}
        metadata_raw = details.get("metadata")
        metadata = dict(metadata_raw) if isinstance(metadata_raw, dict) else {}
        recovery_target_turn_id = str(
            metadata.get("promoted_from_turn_id")
            or metadata.get("target_turn_id")
            or target_task.task_id
        )
        contexts = [
            entry.turn_context
            for entry in entries
            if isinstance(getattr(entry, "turn_context", None), dict)
        ]
        last_context = contexts[-1] if contexts else {}
        revisions: list[int] = []
        for context in contexts:
            try:
                revisions.append(int(context.get("revision", 1) or 1))
            except (TypeError, ValueError):
                revisions.append(1)
        metadata.update(
            {
                "client_message_id": last_context.get("client_message_id"),
                "surface_id": last_context.get("surface_id"),
                "turn_context_intent": "steer",
                "turn_context_disposition": "promoted",
                "turn_context_revision": max([1, *revisions]) + 1,
                "target_turn_id": recovery_target_turn_id,
                "promoted_from_turn_id": recovery_target_turn_id,
                "steer_restart_recovery": True,
            }
        )
        metadata.pop("task_id", None)
        input_provenance_raw = details.get("input_provenance")
        input_provenance = (
            dict(input_provenance_raw)
            if isinstance(input_provenance_raw, dict)
            else {"kind": "steer_restart_recovery"}
        )
        source_name = str(details.get("source_name") or "steer_restart_recovery")
        first_entry = entries[0]
        return RouteEnvelope(
            source_kind=source_kind,
            source_name=source_name,
            agent_id=target_task.agent_id,
            session_key=target_task.session_key,
            session_id=getattr(first_entry, "session_id", None),
            input_provenance=input_provenance,
            metadata=metadata,
        )

    async def _recovery_entries_for_task(
        self,
        task: AgentTaskRecord,
    ) -> builtins.list[Any]:
        details = task.details if isinstance(task.details, dict) else {}
        message_ids = _ordered_message_ids(
            details.get("persisted_user_message_id"),
            details.get("persisted_user_message_ids"),
        )
        session_id = details.get("session_id")
        get_entry = getattr(self._storage, "get_canonical_transcript_entry", None)
        if (
            not message_ids
            or not isinstance(session_id, str)
            or not inspect.iscoroutinefunction(get_entry)
        ):
            return []
        entries: builtins.list[Any] = []
        for message_id in message_ids:
            entry = await get_entry(session_id, message_id)
            if entry is None:
                return []
            entries.append(entry)
        return entries

    async def _resume_never_started_steer_recovery_tasks(
        self,
        result: dict[str, Any],
    ) -> None:
        list_tasks = getattr(self._storage, "list_retryable_steer_recovery_tasks", None)
        requeue = getattr(self._storage, "requeue_steer_recovery_task", None)
        if (
            not inspect.iscoroutinefunction(list_tasks)
            or not inspect.iscoroutinefunction(requeue)
        ):
            return
        for task in await list_tasks():
            entries = await self._recovery_entries_for_task(task)
            envelope = self._restart_recovery_envelope(task, entries)
            texts = [
                entry.content
                for entry in entries
                if isinstance(getattr(entry, "content", None), str)
                and entry.content.strip()
            ]
            if envelope is None or len(texts) != len(entries):
                result["rejected"] += len(entries)
                continue
            details = task.details if isinstance(task.details, dict) else {}
            message_ids = [entry.message_id for entry in entries]
            try:
                reservation = await self.reserve(
                    envelope,
                    "\n\n".join(texts),
                    mode="followup",
                    run_kind=task.run_kind,
                    no_memory_capture=bool(details.get("no_memory_capture", False)),
                    semantic_message="\n\n".join(texts),
                    persisted_user_message_id=message_ids[0],
                    persisted_user_message_ids=message_ids,
                    message_count=len(message_ids),
                    fresh_user_session=False,
                    task_id=task.task_id,
                    update_envelope_cache=False,
                )
            except TaskQueueFullError:
                result["rejected"] += len(entries)
                continue
            if not await requeue(task.task_id):
                await self.abort_reservation(reservation)
                continue
            try:
                handle = await self.activate(reservation)
            except BaseException:
                if not reservation.activated:
                    await self.abort_reservation(reservation)
                raise
            result["resumed"] += len(entries)
            result["task_ids"].append(handle.task_id)

    async def recover_stranded_steers(self) -> dict[str, Any]:
        """Recover every durable ``steering`` input left by process death.

        Promotion ownership moves in the same transaction that creates the new
        queued task. A second startup therefore either resumes that exact
        never-started task or observes the already-promoted terminal evidence;
        it never creates a duplicate follow-up.
        """

        result: dict[str, Any] = {
            "applied": 0,
            "promoted": 0,
            "cancelled": 0,
            "rejected": 0,
            "resumed": 0,
            "task_ids": [],
        }
        await self._resume_never_started_steer_recovery_tasks(result)
        list_inputs = getattr(self._storage, "list_stranded_steer_inputs", None)
        close_inputs = getattr(self._storage, "close_stranded_steer_inputs", None)
        promote_inputs = getattr(self._storage, "promote_stranded_steer_inputs", None)
        if (
            not inspect.iscoroutinefunction(list_inputs)
            or not inspect.iscoroutinefunction(close_inputs)
            or not inspect.iscoroutinefunction(promote_inputs)
        ):
            return result

        grouped: dict[str, list[Any]] = {}
        for item in await list_inputs():
            grouped.setdefault(item.target_task.task_id, []).append(item)

        for target_task_id, items in grouped.items():
            items.sort(
                key=lambda item: (
                    (
                        0,
                        int(item.entry.id),
                    )
                    if getattr(item.entry, "id", None) is not None
                    else (
                        1,
                        int(getattr(item.receipt, "accepted_at", 0) or 0),
                        str(getattr(item.receipt, "receipt_id", "")),
                    )
                )
            )
            target_task = items[0].target_task
            target_details = (
                target_task.details if isinstance(target_task.details, dict) else {}
            )
            evidence_rows = target_details.get("applied_steer_evidence")
            evidence_by_id = {
                str(row["message_id"]): dict(row)
                for row in (evidence_rows if isinstance(evidence_rows, list) else [])
                if isinstance(row, dict)
                and isinstance(row.get("message_id"), str)
            }
            applied_message_ids = [
                item.entry.message_id
                for item in items
                if item.entry.message_id in evidence_by_id
            ]
            if applied_message_ids:
                changed = await close_inputs(
                    target_task_id=target_task_id,
                    message_ids=applied_message_ids,
                    disposition="applied",
                    recovery="terminal_task_evidence",
                    application_evidence=evidence_by_id,
                )
                changed_set = set(changed)
                result["applied"] += len(changed)
                items = [
                    item
                    for item in items
                    if item.entry.message_id not in changed_set
                ]
                if not items:
                    continue
            message_ids = [item.entry.message_id for item in items]
            if self._is_explicit_user_cancelled_task(target_task):
                changed = await close_inputs(
                    target_task_id=target_task_id,
                    message_ids=message_ids,
                    disposition="cancelled",
                    failure_code="TURN_CANCELLED",
                    retryable=True,
                    recovery="restore_to_composer",
                )
                result["cancelled"] += len(changed)
                continue

            entries = [item.entry for item in items]
            envelope = self._restart_recovery_envelope(target_task, entries)
            texts = [
                entry.content
                for entry in entries
                if isinstance(entry.content, str) and entry.content.strip()
            ]
            if envelope is None or len(texts) != len(entries):
                changed = await close_inputs(
                    target_task_id=target_task_id,
                    message_ids=message_ids,
                    disposition="rejected",
                    failure_code="STEER_RESTART_ROUTE_UNAVAILABLE",
                    retryable=True,
                    recovery="resend_as_followup",
                )
                result["rejected"] += len(changed)
                continue

            details = (
                target_task.details if isinstance(target_task.details, dict) else {}
            )
            try:
                reservation = await self.reserve(
                    envelope,
                    "\n\n".join(texts),
                    mode="followup",
                    run_kind=target_task.run_kind,
                    no_memory_capture=bool(details.get("no_memory_capture", False)),
                    semantic_message="\n\n".join(texts),
                    persisted_user_message_id=message_ids[0],
                    persisted_user_message_ids=message_ids,
                    message_count=len(message_ids),
                    fresh_user_session=False,
                    update_envelope_cache=False,
                )
            except TaskQueueFullError:
                changed = await close_inputs(
                    target_task_id=target_task_id,
                    message_ids=message_ids,
                    disposition="rejected",
                    failure_code="STEER_PROMOTION_QUEUE_FULL",
                    retryable=True,
                    recovery="resend_after_queue_drains",
                )
                result["rejected"] += len(changed)
                continue

            claimed = await promote_inputs(
                target_task_id=target_task_id,
                message_ids=message_ids,
                task_record=reservation.task_record,
            )
            if len(claimed) != len(message_ids):
                await self.abort_reservation(reservation)
                continue
            try:
                handle = await self.activate(reservation)
            except BaseException:
                if not reservation.activated:
                    await self.abort_reservation(reservation)
                raise
            result["promoted"] += len(claimed)
            result["task_ids"].append(handle.task_id)
        return result

    async def _promote_undrained_steers(
        self,
        completed_task: _RuntimeTask,
        items: Sequence[_SteeredInput],
        *,
        activate: bool = True,
        defer_queued_notification: bool = False,
    ) -> _SteerPromotionResult | None:
        """Turn a too-late steer into one durable follow-up task."""

        if not items:
            return None
        last = items[-1]
        message_ids = [
            item.persisted_user_message_id
            for item in items
            if item.persisted_user_message_id
        ]
        metadata = dict(completed_task.envelope.metadata)
        if last.client_message_id:
            metadata["client_message_id"] = last.client_message_id
        if last.surface_id:
            metadata["surface_id"] = last.surface_id
        metadata.update(
            {
                "turn_context_intent": "steer",
                "turn_context_disposition": "promoted",
                "target_turn_id": completed_task.task_id,
                "promoted_from_turn_id": completed_task.task_id,
                "turn_context_revision": 2,
                # A process may stop after the durable ownership transfer but
                # before this queued follow-up reaches RUNNING. Startup recovery
                # must resume that exact task instead of creating another one.
                "steer_restart_recovery": True,
            }
        )
        envelope = _reusable_route_envelope(
            replace(completed_task.envelope, metadata=metadata)
        )
        message = "\n\n".join(item.text for item in items if item.text.strip())
        semantic_parts = [
            item.semantic_message or item.text for item in items if item.text.strip()
        ]
        reservation: TaskReservation | None = None
        promoted_task_id: str | None = None
        promotion_committed = False
        promote_inputs = getattr(
            self._storage,
            "promote_stranded_steer_inputs",
            None,
        )
        atomic_promotion = (
            len(message_ids) == len(items)
            and all(item.client_request_id for item in items)
            and inspect.iscoroutinefunction(promote_inputs)
        )
        promoted_goal_candidate: dict[str, Any] | None = None
        if (
            completed_task.goal_steer_candidate is not None
            or completed_task.goal_context is not None
        ):
            from opensquilla.session.goals import GoalClaimCandidate, GoalTurnContext

            stale_candidate = GoalClaimCandidate.from_task_detail(
                completed_task.goal_steer_candidate
            )
            if stale_candidate is not None:
                promoted_goal_candidate = stale_candidate.as_task_detail()
            completed_goal = GoalTurnContext.from_task_detail(
                completed_task.goal_context
            )
            if promoted_goal_candidate is None and completed_goal is not None:
                promoted_goal_candidate = GoalClaimCandidate(
                    session_id=completed_goal.session_id,
                    epoch=completed_goal.epoch,
                    goal_id=completed_goal.goal_id,
                ).as_task_detail()
        try:
            reservation = await self.reserve(
                envelope,
                message,
                mode="followup",
                run_kind=(
                    "session_turn"
                    if completed_task.run_kind == "goal"
                    else completed_task.run_kind
                ),
                no_memory_capture=(
                    False
                    if completed_task.run_kind == "goal"
                    else completed_task.no_memory_capture
                ),
                input_mode="user",
                persist_input=False,
                history_has_persisted_user=True,
                goal_candidate=promoted_goal_candidate,
                semantic_message="\n\n".join(semantic_parts),
                persisted_user_message_id=message_ids[0] if message_ids else None,
                persisted_user_message_ids=message_ids,
                message_count=max(1, len(message_ids)),
                fresh_user_session=False,
                update_envelope_cache=False,
                # These inputs were accepted before shutdown admission closed.
                # Their durable ownership transfer must still complete, but the
                # ``activate=False`` shutdown path below leaves execution for
                # startup recovery.
                _allow_during_shutdown=True,
            )
            if atomic_promotion:
                promote_inputs_fn = cast(
                    Callable[..., Awaitable[builtins.list[str]]],
                    promote_inputs,
                )
                claimed = await promote_inputs_fn(
                    target_task_id=completed_task.task_id,
                    message_ids=message_ids,
                    task_record=reservation.task_record,
                    recovery="late_steer_followup",
                )
                if len(claimed) != len(message_ids):
                    await self.abort_reservation(reservation)
                    log.warning(
                        "task_runtime.steer_followup_promotion_claim_lost",
                        session_key=completed_task.envelope.session_key,
                        completed_task_id=completed_task.task_id,
                        count=len(items),
                    )
                    return None
            else:
                await self._storage.create_agent_task(reservation.task_record)
            promotion_committed = True
            promoted_task_id = reservation.task_id
        except Exception as exc:  # noqa: BLE001 - accepted input must leave evidence
            if reservation is not None and not reservation.activated:
                await self.abort_reservation(reservation)
            if promotion_committed:
                # The ownership transfer already committed. Never rewrite it
                # as rejected merely because in-memory activation failed; the
                # exact queued task is restart-recoverable.
                log.exception(
                    "task_runtime.steer_followup_activation_failed",
                    session_key=completed_task.envelope.session_key,
                    completed_task_id=completed_task.task_id,
                    promoted_task_id=promoted_task_id,
                )
                assert promoted_task_id is not None
                return _SteerPromotionResult(task_id=promoted_task_id)
            failure_code = (
                "STEER_PROMOTION_QUEUE_FULL"
                if isinstance(exc, TaskQueueFullError)
                else "STEER_PROMOTION_FAILED"
            )
            await self._record_steer_dispositions(
                completed_task,
                items,
                disposition="rejected",
                turn_id=completed_task.task_id,
                revision=2,
                promoted_from_turn_id=completed_task.task_id,
                event_details={
                    "failure_code": failure_code,
                    "retryable": isinstance(exc, TaskQueueFullError),
                    "recovery": (
                        "resend_after_queue_drains"
                        if isinstance(exc, TaskQueueFullError)
                        else "inspect_transcript_and_resend"
                    ),
                },
            )
            log.exception(
                "task_runtime.steer_followup_promotion_failed",
                session_key=completed_task.envelope.session_key,
                completed_task_id=completed_task.task_id,
                count=len(items),
                failure_code=failure_code,
            )
            return None

        assert reservation is not None
        assert promoted_task_id is not None
        # The atomic path already committed every transcript transition and
        # receipt rebind with the queued task. The legacy compatibility path
        # still persists the transition here.
        await self._record_steer_dispositions(
            completed_task,
            items,
            disposition="promoted",
            turn_id=promoted_task_id,
            revision=2,
            promoted_from_turn_id=completed_task.task_id,
            event_details={"recovery": "late_steer_followup"},
            persist=not atomic_promotion,
        )
        if activate:
            try:
                await self.activate(
                    reservation,
                    defer_queued_notification=defer_queued_notification,
                )
            except BaseException:
                if not reservation.activated:
                    await self.abort_reservation(reservation)
                # The queued task and its ownership transfer are durable. A
                # later process resumes the same task; do not reclassify it.
                log.exception(
                    "task_runtime.steer_followup_activation_failed",
                    session_key=completed_task.envelope.session_key,
                    completed_task_id=completed_task.task_id,
                    promoted_task_id=promoted_task_id,
                )
        else:
            # Gateway shutdown must not start fresh model work. Leave the
            # durable QUEUED task for startup recovery and release only the
            # in-process reservation.
            await self.abort_reservation(reservation)
        log.info(
            "task_runtime.steer_promoted_to_followup",
            session_key=completed_task.envelope.session_key,
            completed_task_id=completed_task.task_id,
            promoted_task_id=promoted_task_id,
            count=len(items),
            activated=activate,
        )
        return _SteerPromotionResult(
            task_id=promoted_task_id,
            deferred_notification=(
                reservation if activate and defer_queued_notification else None
            ),
        )

    async def _record_drained_steers(self, task: _RuntimeTask) -> None:
        """Persist steer input confirmed as part of a started provider call."""

        items = task.pending_input_provider.pending_applied()
        if not items:
            return
        acknowledged = await self._record_steer_dispositions(
            task,
            items,
            disposition="applied",
            turn_id=task.task_id,
            revision=2,
        )
        task.pending_input_provider.acknowledge_applied(acknowledged)

    async def _record_cancelled_steers(self, task: _RuntimeTask) -> None:
        """Close applied and still-pending steer input on cancellation."""

        applied = task.pending_input_provider.pending_applied()
        if applied:
            acknowledged = await self._record_steer_dispositions(
                task,
                applied,
                disposition="applied",
                turn_id=task.task_id,
                revision=2,
            )
            task.pending_input_provider.acknowledge_applied(acknowledged)
        pending = task.pending_input_provider.reclaim_pending()
        if pending:
            await self._record_steer_dispositions(
                task,
                pending,
                disposition="cancelled",
                turn_id=task.task_id,
                revision=2,
                event_details={
                    "failure_code": "TURN_CANCELLED",
                    "retryable": True,
                    "recovery": "restore_to_composer",
                    "fallback_safe": True,
                },
            )

    async def _record_steer_dispositions(
        self,
        task: _RuntimeTask,
        items: Sequence[_SteeredInput],
        *,
        disposition: str,
        turn_id: str,
        revision: int,
        promoted_from_turn_id: str | None = None,
        event_details: dict[str, Any] | None = None,
        persist: bool = True,
    ) -> builtins.list[_SteeredInput]:
        """Durably transition accepted steer inputs before publishing them.

        Items whose durable row could not be updated remain unacknowledged in
        the pending-input provider so terminal cleanup can retry. Observer
        failures stay best-effort after persistence succeeds.
        """

        acknowledged: builtins.list[_SteeredInput] = []
        for item in items:
            context: dict[str, Any] = {
                "turn_id": turn_id,
                "client_message_id": item.client_message_id,
                "surface_id": item.surface_id,
                "intent": "steer",
                "disposition": disposition,
                "target_turn_id": task.task_id,
                "revision": revision,
            }
            if item.client_request_id:
                context["client_request_id"] = item.client_request_id
            if promoted_from_turn_id:
                context["promoted_from_turn_id"] = promoted_from_turn_id
            if disposition == "promoted":
                context["promoted_turn_id"] = turn_id
            if disposition == "applied":
                if item.applied_iteration is not None:
                    context["applied_iteration"] = item.applied_iteration
                if item.model_call_id:
                    context["model_call_id"] = item.model_call_id
            if event_details:
                context.update(event_details)
            durable = not item.persisted_user_message_id or not persist
            if persist and item.persisted_user_message_id:
                try:
                    durable = await self._update_transcript_turn_context(
                        task.envelope.session_key,
                        item.persisted_user_message_id,
                        context,
                    )
                    if not durable:
                        log.warning(
                            "task_runtime.steer_disposition_persist_missed",
                            session_key=task.envelope.session_key,
                            task_id=task.task_id,
                            message_id=item.persisted_user_message_id,
                            disposition=disposition,
                        )
                except Exception:  # noqa: BLE001 - terminal cleanup retries it
                    log.warning(
                        "task_runtime.steer_disposition_persist_failed",
                        session_key=task.envelope.session_key,
                        task_id=task.task_id,
                        message_id=item.persisted_user_message_id,
                        disposition=disposition,
                        exc_info=True,
                    )
                    durable = False
            if not durable:
                continue
            acknowledged.append(item)
            try:
                await self._emit(
                    task.envelope.session_key,
                    "session.event.input_disposition",
                    {
                        "key": task.envelope.session_key,
                        "session_key": task.envelope.session_key,
                        "task_id": task.task_id,
                        "user_message_id": item.persisted_user_message_id,
                        **context,
                    },
                )
            except Exception:  # noqa: BLE001 - terminal flow must continue
                log.warning(
                    "task_runtime.steer_disposition_emit_failed",
                    session_key=task.envelope.session_key,
                    task_id=task.task_id,
                    message_id=item.persisted_user_message_id,
                    disposition=disposition,
                    exc_info=True,
                )
            log.info(
                "task_runtime.steer_disposition",
                session_key=task.envelope.session_key,
                task_id=task.task_id,
                disposition=disposition,
                client_request_id=item.client_request_id,
            )
            _emit_metric(
                "steer_inputs_total",
                value=1,
                disposition=disposition,
                session_key=task.envelope.session_key,
            )
            if disposition == "applied" and item.accepted_at_ms is not None:
                _emit_metric(
                    "steer_application_latency_ms",
                    value=max(0, _epoch_time_ms() - item.accepted_at_ms),
                    session_key=task.envelope.session_key,
                )
        return acknowledged

    async def _run_turn_handler_with_write_lock_bypass(
        self,
        run: TaskRun,
        *,
        write_lock: asyncio.Lock,
    ) -> None:
        """Run the handler while TurnRunner transcript writes use short locks."""
        from opensquilla.engine.runtime import (
            _SESSION_LOCK_BYPASS_ONLY,
            _SESSION_LOCK_OWNER,
        )

        current_task = asyncio.current_task()
        prev_map = _SESSION_LOCK_OWNER.get(None)
        new_map: dict[int, Any] = dict(prev_map or {})
        if current_task is not None:
            new_map[id(write_lock)] = current_task
        owner_token = _SESSION_LOCK_OWNER.set(new_map)
        prev_bypass = _SESSION_LOCK_BYPASS_ONLY.get(None)
        new_bypass = set(prev_bypass or set())
        new_bypass.add(id(write_lock))
        bypass_token = _SESSION_LOCK_BYPASS_ONLY.set(new_bypass)
        try:
            if self._turn_hard_deadline_s is not None:
                deadline_start = time.monotonic()
                try:
                    await asyncio.wait_for(
                        self._turn_handler(run),
                        timeout=self._turn_hard_deadline_s,
                    )
                except TimeoutError as exc:
                    # Only reclassify when the hard-deadline budget was actually
                    # exhausted. A TimeoutError from inside the handler should
                    # keep its original cause.
                    elapsed = time.monotonic() - deadline_start
                    if elapsed + 0.01 >= self._turn_hard_deadline_s:
                        raise _TurnHardDeadlineExceeded(
                            deadline_s=self._turn_hard_deadline_s,
                        ) from exc
                    raise
            else:
                await self._turn_handler(run)
        finally:
            _SESSION_LOCK_BYPASS_ONLY.reset(bypass_token)
            _SESSION_LOCK_OWNER.reset(owner_token)

    def _ensure_slot_cond(self) -> asyncio.Condition:
        if self._slot_cond is None:
            self._slot_cond = asyncio.Condition()
        return self._slot_cond

    def _ensure_fair_cond(self) -> asyncio.Condition:
        if self._fair_cond is None:
            self._fair_cond = asyncio.Condition()
        return self._fair_cond

    async def _acquire_fair_slot(self, task: _RuntimeTask) -> None:
        """Acquire one global slot with round-robin among genuine slot waiters.

        A task must satisfy one predicate before it is granted a slot:

        1. A slot is available: ``_global_in_flight < _max_concurrency``.

        The active-session RR deque supplies stable ordering, but eligibility is
        filtered through ``_agent_slot_waiters``. Existing running sessions and
        sessions whose next task is still behind its execution lock therefore
        cannot become a phantom head that leaves global capacity idle.

        When only one slot is left (``_global_in_flight == _max_concurrency - 1``),
        the first *waiting* session in RR order is preferred. Other waiters for
        the same agent yield so the last slot remains starvation-free without
        being blocked by a session that is already running.

        When a slot is released ``_fair_cond.notify_all()`` wakes all waiters
        so they re-check the predicate.
        """
        cond = self._ensure_fair_cond()
        agent_id = task.envelope.agent_id
        session_key = task.envelope.session_key

        async with cond:
            waiters = self._agent_slot_waiters.setdefault(agent_id, set())
            waiters.add(session_key)
            try:
                while True:
                    # Predicate 1: global slot available.
                    if self._global_in_flight >= self._max_concurrency:
                        await cond.wait()
                        continue
                    # Tie-break only among sessions that are actually inside
                    # this global-slot wait. Active/running RR entries are not
                    # eligible and cannot strand the last idle slot.
                    idle_slots = self._max_concurrency - self._global_in_flight
                    rr = self._agent_session_rr.get(agent_id)
                    fair_session = next(
                        (candidate for candidate in (rr or ()) if candidate in waiters),
                        None,
                    )
                    if idle_slots == 1 and fair_session != session_key:
                        await cond.wait()
                        continue
                    # Predicate satisfied — rotate past the granted session and
                    # claim the slot. Rotation works even when non-waiting RR
                    # entries precede this session.
                    if rr and session_key in rr:
                        rr.rotate(-(rr.index(session_key) + 1))
                    self._global_in_flight += 1
                    if task.run_kind == "subagent":
                        self._subagent_in_flight += 1
                    self._agent_in_flight[agent_id] = (
                        self._agent_in_flight.get(agent_id, 0) + 1
                    )
                    task.acquired_slot = True
                    break
            finally:
                waiters.discard(session_key)
                if not waiters:
                    self._agent_slot_waiters.pop(agent_id, None)
                # Cancellation or a successful grant changes the genuine RR
                # head. Wake peers even when no global slot count changed.
                cond.notify_all()

        # Update storage and emit running metric outside the condition lock. A
        # collect claim can keep this await open; if cancellation or persistence
        # failure wins that race, release the slot claimed above before the
        # caller's ``acquired`` flag has been set.
        try:
            marked_running = await self._mark_running(task)
        except BaseException:
            await self._release_slot(task)
            raise
        if not marked_running:
            await self._release_slot(task)
            raise asyncio.CancelledError
        _emit_metric(
            "in_flight_turns_total",
            value=1,
            session_key=task.envelope.session_key,
        )

    async def _wait_for_subagent_slot(self, task: _RuntimeTask) -> None:
        """Block subagent tasks until at least ``reserved_slots+1`` capacity
        is free, so non-subagent tasks always have a fair runway.
        """
        if task.run_kind != "subagent" or self._subagent_reserved_slots <= 0:
            return
        cond = self._ensure_slot_cond()
        async with cond:
            while self._max_concurrency - self._global_in_flight <= self._subagent_reserved_slots:
                await cond.wait()

    async def _release_slot(self, task: _RuntimeTask) -> None:
        async with self._state_lock:
            if task.acquired_slot:
                self._global_in_flight = max(0, self._global_in_flight - 1)
                if task.run_kind == "subagent":
                    self._subagent_in_flight = max(0, self._subagent_in_flight - 1)
                agent_id = task.envelope.agent_id
                new_count = max(0, self._agent_in_flight.get(agent_id, 0) - 1)
                if new_count == 0:
                    self._agent_in_flight.pop(agent_id, None)
                else:
                    self._agent_in_flight[agent_id] = new_count
                task.acquired_slot = False
        # Wake all tasks waiting for a slot: both the subagent-reserved gate
        # (_slot_cond) and the fair-queuing gate (_fair_cond).
        if self._slot_cond is not None:
            async with self._slot_cond:
                self._slot_cond.notify_all()
        if self._fair_cond is not None:
            async with self._fair_cond:
                self._fair_cond.notify_all()

    def _get_session_lock_for_turn(self, session_key: str) -> asyncio.Lock:
        """Return the OUTER per-session lock for *session_key*.

        Exposed as a ``session_lock_provider`` callable for ``TurnRunner`` so
        that both classes share the same ``asyncio.Lock`` per session. With a
        shared provider this is the only per-session lock; TurnRunner no
        longer owns an internal ``_session_locks`` dict.

        ``setdefault`` is atomic in CPython — avoids TOCTOU race on insertion.
        """
        return self._session_locks.setdefault(session_key, asyncio.Lock())

    def _start_running_heartbeat(self, task: _RuntimeTask) -> asyncio.Task[None] | None:
        interval = self._running_heartbeat_interval_s
        if interval is None:
            return None
        return asyncio.create_task(
            self._heartbeat_running_task(task, interval),
            name=f"opensquilla-task-heartbeat:{task.task_id}",
        )

    async def _stop_running_heartbeat(self, heartbeat_task: asyncio.Task[None]) -> None:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            return

    async def _heartbeat_running_task(
        self,
        task: _RuntimeTask,
        interval: float,
    ) -> None:
        while True:
            await asyncio.sleep(interval)
            async with self._state_lock:
                still_running = (
                    not task.terminal_closing
                    and task.status == AgentTaskStatus.RUNNING
                    and self._running_by_session.get(task.envelope.session_key) is task
                )
            if not still_running:
                return
            try:
                await self._storage.update_agent_task(
                    task.task_id,
                    updated_at=_epoch_time_ms(),
                )
            except Exception as exc:  # noqa: BLE001 - heartbeat is best-effort
                log.warning(
                    "task_runtime.running_heartbeat_failed",
                    task_id=task.task_id,
                    session_key=task.envelope.session_key,
                    error=str(exc),
                )

    async def _mark_running(self, task: _RuntimeTask) -> bool:
        """Move one task to RUNNING after any active collect claim settles."""

        async with task.collect_claim:
            return await self._mark_running_claimed(task)

    async def _mark_running_claimed(self, task: _RuntimeTask) -> bool:
        await self._freeze_collaboration_context(task)
        await self._prepare_goal_context_for_activation(task)
        async with self._state_lock:
            if (
                task.terminal_closing
                or task.status in TERMINAL_STATUSES
                or task.cancel_requested
            ):
                return False
            task.status = AgentTaskStatus.RUNNING
            self._remove_pending(task)
            self._running_by_session[task.envelope.session_key] = task
        await self._storage.update_agent_task(
            task.task_id,
            status=AgentTaskStatus.RUNNING,
            started_at=_epoch_time_ms(),
        )
        await self._emit(
            task.envelope.session_key,
            "task.running",
            {
                "task_id": task.task_id,
                "session_key": task.envelope.session_key,
                "steer_capability": self._steer_capability_for_task(task),
                **_task_identity_payload(
                    task.envelope,
                    task.task_id,
                    user_message_id=task.persisted_user_message_id,
                ),
            },
        )
        await self._notify_task_lifecycle(
            TaskLifecycleEvent(
                phase="running",
                session_key=task.envelope.session_key,
                task_id=task.task_id,
                task_status=AgentTaskStatus.RUNNING,
                run_kind=task.run_kind,
            )
        )
        return True

    async def _prepare_goal_context_for_activation(self, task: _RuntimeTask) -> None:
        """Resolve a queued user's Goal candidate before the provider starts.

        The callback owns the durable claim. Failure is deliberately fail-open
        for the explicit user turn: the Goal service pauses its own state while
        the user's already accepted message continues as an ordinary turn.
        """

        if task.goal_context is None and task.goal_candidate is not None:
            listener = self._activation_listener
            candidate = dict(task.goal_candidate)
            task.goal_candidate = None
            if listener is not None:
                try:
                    claimed = await listener(
                        task.envelope.session_key,
                        task.task_id,
                        task.run_kind,
                        str(task.envelope.metadata.get("collaboration_mode") or "default"),
                        candidate,
                    )
                except Exception:
                    log.warning(
                        "task_runtime.activation_listener_failed",
                        session_key=task.envelope.session_key,
                        task_id=task.task_id,
                        exc_info=True,
                    )
                    claimed = None
                if claimed is not None:
                    task.goal_context = dict(claimed)
        if task.goal_context is not None:
            from opensquilla.session.goals import GoalClaimCandidate, GoalTurnContext

            frozen_context = GoalTurnContext.from_task_detail(task.goal_context)
            rendered_context: dict[str, Any] | None = None
            build_prompt_context = getattr(
                self._goal_service,
                "build_prompt_context",
                None,
            )
            if not callable(build_prompt_context):
                raise _GoalPromptContextUnavailableError(
                    "Authoritative Goal prompt context is unavailable"
                )
            try:
                rendered = await build_prompt_context(task.goal_context)
            except Exception as exc:
                log.warning(
                    "task_runtime.goal_prompt_context_failed",
                    session_key=task.envelope.session_key,
                    task_id=task.task_id,
                    exc_info=True,
                )
                raise _GoalPromptContextUnavailableError(
                    "Authoritative Goal prompt context is unavailable"
                ) from exc
            if isinstance(rendered, Mapping):
                rendered_context = dict(rendered)
            elif rendered is not None:
                raise _GoalPromptContextUnavailableError(
                    "Authoritative Goal prompt context is invalid"
                )
            elif frozen_context is not None:
                task.goal_steer_candidate = GoalClaimCandidate(
                    session_id=frozen_context.session_id,
                    epoch=frozen_context.epoch,
                    goal_id=frozen_context.goal_id,
                ).as_task_detail()
            task.goal_context = rendered_context
            services = dict(task.envelope.runtime_services)
            services.pop("goal_context", None)
            services.pop("goal_service", None)
            if task.goal_context is not None and self._goal_service is not None:
                services["goal_context"] = dict(task.goal_context)
                services["goal_service"] = self._goal_service
            task.envelope = replace(task.envelope, runtime_services=services)

    async def _mark_terminal(
        self,
        task: _RuntimeTask,
        status: AgentTaskStatus,
        *,
        terminal_reason: str,
        error_class: str | None = None,
        error_message: str | None = None,
        failure_kind: str | None = None,
        retry_after_ms: int | None = None,
        activity_snapshot: object = None,
        usage_call_index: int | None = None,
        no_prior_provider_dispatch: bool = False,
        replay_safe: bool = False,
        promote_pending_steers: bool = False,
        activate_promoted_steers: bool = True,
    ) -> None:
        """Finalize one task after collect and same-turn admissions settle."""

        try:
            async with task.collect_claim:
                async with task.steer_claim:
                    await self._mark_terminal_claimed(
                        task,
                        status,
                        terminal_reason=terminal_reason,
                        error_class=error_class,
                        error_message=error_message,
                        failure_kind=failure_kind,
                        retry_after_ms=retry_after_ms,
                        activity_snapshot=activity_snapshot,
                        usage_call_index=usage_call_index,
                        no_prior_provider_dispatch=no_prior_provider_dispatch,
                        replay_safe=replay_safe,
                        promote_pending_steers=promote_pending_steers,
                        activate_promoted_steers=activate_promoted_steers,
                    )
        finally:
            # A driver cancelled before its first event-loop step never enters
            # ``_execute`` and therefore has no execution ``finally`` block.
            if not task.execution_started:
                _cleanup_guest_profile(task)

    async def _mark_terminal_claimed(
        self,
        task: _RuntimeTask,
        status: AgentTaskStatus,
        *,
        terminal_reason: str,
        error_class: str | None = None,
        error_message: str | None = None,
        failure_kind: str | None = None,
        retry_after_ms: int | None = None,
        activity_snapshot: object = None,
        usage_call_index: int | None = None,
        no_prior_provider_dispatch: bool = False,
        replay_safe: bool = False,
        promote_pending_steers: bool = False,
        activate_promoted_steers: bool = True,
    ) -> None:
        record_primary_terminal_disposition = False
        collected_terminal_inputs: list[_CollectedPrimaryInput] = []
        was_running_owner = False
        async with self._state_lock:
            if task.terminal_closing:
                return
            was_running_owner = (
                self._running_by_session.get(task.envelope.session_key) is task
            )
            task.terminal_settling = True
            if task.primary_input_pending:
                task.primary_input_pending = False
                record_primary_terminal_disposition = True
            if task.collected_primary_inputs:
                collected_terminal_inputs = list(task.collected_primary_inputs)
                task.collected_primary_inputs.clear()
            task.status = status
        if record_primary_terminal_disposition:
            await self._record_primary_terminal_disposition(
                task,
                status=status,
                terminal_reason=terminal_reason,
            )
        # ApprovalQueue is session-addressed. A queued task that is cancelled
        # before execution cannot own an approval, and expiring the session
        # here would reject the actual running owner's request. Capture
        # ownership atomically with the terminal transition; if this task won
        # the execution lane immediately before cancellation, it is the owner
        # and must still fail its own orphaned approval closed.
        if was_running_owner:
            try:
                from opensquilla.application.approval_queue import get_approval_queue

                get_approval_queue().expire_pending_for_session(
                    task.envelope.session_key,
                )
            except Exception as exc:  # noqa: BLE001 - terminalization must continue.
                log.warning(
                    "task_runtime.approval_cleanup_failed",
                    task_id=task.task_id,
                    session_key=task.envelope.session_key,
                    error=str(exc),
                )
        for collected_input in collected_terminal_inputs:
            await self._record_collected_primary_input_disposition(
                task,
                collected_input,
                disposition=(
                    "cancelled" if status == AgentTaskStatus.CANCELLED else "rejected"
                ),
                terminal_reason=terminal_reason,
            )
        terminal_payload = {
            "status": status,
            "terminal_reason": terminal_reason,
            "error_class": error_class,
            "error_message": error_message,
        }
        if failure_kind:
            error_class = safe_provider_failure_code(error_class, failure_kind)
            error_message = safe_provider_failure_message(failure_kind)
            terminal_payload["error_class"] = error_class
            terminal_payload["error_message"] = error_message
        elif (
            (status == AgentTaskStatus.TIMEOUT and terminal_reason != "hard_deadline_exceeded")
            or terminal_reason == "timeout"
            or is_context_payload_too_large(terminal_payload)
            or (terminal_reason == "output_truncated" or error_class == "provider_output_truncated")
        ):
            error_class, error_message = sanitize_agent_error(
                terminal_payload,
                fallback_error_class=error_class,
                fallback_error_message=error_message or "Agent error",
            )
            terminal_payload["error_class"] = error_class
            terminal_payload["error_message"] = error_message
        terminal_update: dict[str, Any] = {
            "status": status,
            "finished_at": _epoch_time_ms(),
            "terminal_reason": terminal_reason,
            "error_class": error_class,
            "error_message": error_message,
        }
        try:
            terminal_update.update(
                await self._terminal_details_update(
                    task,
                    status=status,
                    terminal_reason=terminal_reason,
                    error_class=error_class,
                    error_message=error_message,
                    failure_kind=failure_kind,
                    retry_after_ms=retry_after_ms,
                    activity_snapshot=activity_snapshot,
                    usage_call_index=usage_call_index,
                    no_prior_provider_dispatch=no_prior_provider_dispatch,
                    replay_safe=replay_safe,
                )
            )
        except Exception as exc:  # noqa: BLE001 - terminal feedback still matters.
            log.warning(
                "task_runtime.terminal_details_failed",
                task_id=task.task_id,
                session_key=task.envelope.session_key,
                error=str(exc),
            )
        terminal_persisted = True
        promotion_result: _SteerPromotionResult | None = None
        try:
            try:
                await self._storage.update_agent_task(
                    task.task_id,
                    **terminal_update,
                )
            except Exception as exc:  # noqa: BLE001 - do not strand the UI in running.
                terminal_persisted = False
                log.warning(
                    "task_runtime.terminal_persist_failed",
                    task_id=task.task_id,
                    session_key=task.envelope.session_key,
                    status=status,
                    error=str(exc),
                )
                await self._cache_terminal_fallback_record(
                    task,
                    status=status,
                    terminal_update=terminal_update,
                )
            if terminal_persisted and promote_pending_steers:
                # The terminal AgentTask row is now durable, but no public
                # terminal/idle signal has escaped. Close every accepted steer
                # under the same admission gate before observers can settle the
                # old task. The promoted task is activated while the old turn
                # still owns the per-session execution lock, so it can queue but
                # cannot start early.
                pending_steers = task.pending_input_provider.reclaim_pending()
                if pending_steers:
                    promotion_result = await _complete_terminal_settlement(
                        self._promote_undrained_steers(
                            task,
                            pending_steers,
                            activate=activate_promoted_steers,
                            defer_queued_notification=activate_promoted_steers,
                        )
                    )
            payload: dict[str, Any] = {
                "task_id": task.task_id,
                "session_key": task.envelope.session_key,
                "terminal_reason": terminal_reason,
                **_task_identity_payload(
                    task.envelope,
                    task.task_id,
                    user_message_id=task.persisted_user_message_id,
                ),
            }
            if status != AgentTaskStatus.SUCCEEDED:
                payload["terminal_message"] = build_terminal_reply(terminal_payload)
            if status != AgentTaskStatus.SUCCEEDED and is_usage_accounting_barrier(error_class):
                details = terminal_update.get("details")
                details = details if isinstance(details, dict) else {}
                turn_outcome = details.get("turn_outcome")
                snapshot = details.get("activity_snapshot")
                payload.update(
                    {
                        "code": error_class,
                        "error_class": error_class,
                        "retryable": True,
                    }
                )
                replay_proof = usage_barrier_replay_proof(
                    usage_call_index=usage_call_index,
                    no_prior_provider_dispatch=no_prior_provider_dispatch,
                    replay_safe=replay_safe,
                )
                payload.update(replay_proof)
                terminal_payload.update(replay_proof)
                payload["terminal_message"] = build_terminal_reply(terminal_payload)
                if not isinstance(turn_outcome, dict):
                    turn_outcome = outcome_from_error(
                        code=error_class,
                        message=error_message,
                        error_class=error_class,
                        failure_kind=failure_kind,
                    ).to_dict()
                    if retry_after_ms is not None:
                        turn_outcome["retry_after_ms"] = retry_after_ms
                turn_outcome.update(replay_proof)
                payload["turn_outcome"] = dict(turn_outcome)
                if not isinstance(snapshot, dict):
                    snapshot = terminal_activity_snapshot(
                        activity_snapshot,
                        task_id=task.task_id,
                        turn_id=task.task_id,
                    )
                if snapshot is not None:
                    payload["activity_snapshot"] = dict(snapshot)
                safe_retry = safe_retry_after_ms(details.get("retry_after_ms"))
                if safe_retry is None:
                    safe_retry = safe_retry_after_ms(retry_after_ms)
                if safe_retry is not None:
                    payload["retry_after_ms"] = safe_retry
            try:
                await _complete_terminal_settlement(
                    self._emit(task.envelope.session_key, f"task.{status.value}", payload)
                )
                async with self._state_lock:
                    task.terminal_emitted = True
            finally:
                if not terminal_persisted:
                    # The first write already exhausted the storage layer's bounded
                    # busy budget.  Publish the terminal event before making one
                    # more bounded, idempotent update of the same task row so a
                    # lock released by an observer cannot leave startup recovery
                    # looking at RUNNING.  This does not append transcript or usage
                    # rows and remains part of the driver's shutdown/drain fence.
                    terminal_persisted = await self._compensate_terminal_persistence(
                        task,
                        terminal_update=terminal_update,
                        status=status,
                        terminal_reason=terminal_reason,
                        error_class=error_class,
                        error_message=error_message,
                        failure_kind=failure_kind,
                        retry_after_ms=retry_after_ms,
                        activity_snapshot=activity_snapshot,
                        usage_call_index=usage_call_index,
                        no_prior_provider_dispatch=no_prior_provider_dispatch,
                        replay_safe=replay_safe,
                    )
            await _complete_terminal_settlement(
                self._notify_task_lifecycle(
                    TaskLifecycleEvent(
                        phase="terminal",
                        session_key=task.envelope.session_key,
                        task_id=task.task_id,
                        task_status=status,
                        run_kind=task.run_kind,
                        terminal_reason=terminal_reason,
                        error_class=error_class,
                        error_message=error_message,
                        terminal_persisted=terminal_persisted,
                        continuation_task_id=(
                            promotion_result.task_id
                            if promotion_result is not None
                            else None
                        ),
                    )
                )
            )
            if (
                promotion_result is not None
                and promotion_result.deferred_notification is not None
            ):
                await _complete_terminal_settlement(
                    self._publish_deferred_queued_activation(
                        promotion_result.deferred_notification
                    )
                )
        finally:
            fairness_changed = False
            async with self._state_lock:
                task.terminal_settling = False
                task.terminal_settled = True
                # Keep the session lane occupied through durable terminal
                # persistence and ordered lifecycle settlement. In particular,
                # a Goal idle hook must not admit its successor while the
                # previous owner is still being settled.
                self._remove_pending(task)
                session_key = task.envelope.session_key
                if self._running_by_session.get(session_key) is task:
                    self._running_by_session.pop(session_key, None)
                if (
                    self._last_envelope_task_id_by_session.get(session_key)
                    == task.task_id
                ):
                    self._last_envelope_by_session.pop(session_key, None)
                    self._last_envelope_task_id_by_session.pop(session_key, None)
                # Keep the short write lock stable for this session. Popping it
                # can split callers across old/new lock objects while callbacks
                # or late lifecycle events still reference the old one. The
                # dict grows at most by unique session_keys, which is acceptable.
                if (
                    not self._pending_by_session.get(session_key)
                    and self._running_by_session.get(session_key) is None
                ):
                    agent_id = task.envelope.agent_id
                    active = self._agent_active_sessions.get(agent_id)
                    if active is not None:
                        fairness_changed = session_key in active
                        active.discard(session_key)
                        rr = self._agent_session_rr.get(agent_id)
                        if rr is not None:
                            try:
                                rr.remove(session_key)
                            except ValueError:
                                # A prior cleanup path may already have removed
                                # this session from the fairness rotation.
                                pass
                        if not active:
                            self._agent_active_sessions.pop(agent_id, None)
                            self._agent_session_rr.pop(agent_id, None)
                if self._tasks.get(task.task_id) is task:
                    self._tasks.pop(task.task_id, None)
            # A task releases its global slot before ordered terminal
            # persistence and lifecycle settlement. A waiter can therefore
            # wake while this just-finished session is still the RR head, go
            # back to sleep, and miss the later lane/RR cleanup unless that
            # eligibility change is signalled explicitly.
            if fairness_changed and self._fair_cond is not None:
                async with self._fair_cond:
                    self._fair_cond.notify_all()
            # Wake waiters only after the task and its session lane have both
            # left the runtime ledger. Otherwise a caller can observe a
            # terminal record while automatic admission still sees stale work.
            task.done.set()
        await self._notify_subagent_terminal(
            task,
            status,
            terminal_reason=terminal_reason,
            error_class=error_class,
            error_message=error_message,
        )

    async def _record_primary_terminal_disposition(
        self,
        task: _RuntimeTask,
        *,
        status: AgentTaskStatus,
        terminal_reason: str,
    ) -> None:
        """Close an identity-aware input that never reached ``applied``.

        Explicit cancellation (including overflow eviction) is ``cancelled``;
        other pre-application terminal outcomes are ``rejected``.  Persistence
        and live projection are attempted independently so one observer failure
        cannot strand the task ledger or suppress the other canonical signal.
        """

        metadata = task.envelope.metadata
        disposition = "cancelled" if status == AgentTaskStatus.CANCELLED else "rejected"
        try:
            base_revision = int(metadata.get("turn_context_revision", 1) or 1)
        except (TypeError, ValueError):
            base_revision = 1
        context: dict[str, Any] = {
            "turn_id": task.task_id,
            "client_message_id": metadata.get("client_message_id"),
            "surface_id": metadata.get("surface_id"),
            "intent": metadata.get("turn_context_intent", "send"),
            "disposition": disposition,
            "revision": max(2, base_revision + 1),
        }
        client_request_id = metadata.get("client_request_id")
        if isinstance(client_request_id, str) and client_request_id:
            context["client_request_id"] = client_request_id
        meta_control = metadata.get("meta_control")
        if isinstance(meta_control, dict):
            context["meta_control"] = dict(meta_control)
        for context_field in ("target_turn_id", "promoted_from_turn_id"):
            value = metadata.get(context_field)
            if isinstance(value, str) and value:
                context[context_field] = value

        try:
            updated = await self._update_transcript_turn_context(
                task.envelope.session_key,
                task.persisted_user_message_id,
                context,
            )
            if task.persisted_user_message_id and not updated:
                log.warning(
                    "task_runtime.primary_input_terminal_persist_missed",
                    session_key=task.envelope.session_key,
                    task_id=task.task_id,
                    message_id=task.persisted_user_message_id,
                    disposition=disposition,
                )
        except Exception:  # noqa: BLE001 - live evidence must still be emitted
            log.warning(
                "task_runtime.primary_input_terminal_persist_failed",
                session_key=task.envelope.session_key,
                task_id=task.task_id,
                message_id=task.persisted_user_message_id,
                disposition=disposition,
                exc_info=True,
            )
        try:
            await self._emit(
                task.envelope.session_key,
                "session.event.input_disposition",
                {
                    "session_key": task.envelope.session_key,
                    "user_message_id": task.persisted_user_message_id,
                    **context,
                    "terminal_reason": terminal_reason,
                },
            )
        except Exception:  # noqa: BLE001 - task terminal cleanup must continue
            log.warning(
                "task_runtime.primary_input_terminal_emit_failed",
                session_key=task.envelope.session_key,
                task_id=task.task_id,
                message_id=task.persisted_user_message_id,
                disposition=disposition,
                exc_info=True,
            )

    async def _record_collected_primary_inputs_applied(self, task: _RuntimeTask) -> None:
        """Advance every durable prompt coalesced into this turn to applied."""

        while task.collected_primary_inputs:
            item = task.collected_primary_inputs[0]
            await self._record_collected_primary_input_disposition(
                task,
                item,
                disposition="applied",
            )
            # There is deliberately no await between the successful observer
            # writes and removal. Cancellation therefore either leaves the
            # identity pending for terminal cleanup or sees it fully applied.
            task.collected_primary_inputs.pop(0)

    async def _record_collected_primary_input_disposition(
        self,
        task: _RuntimeTask,
        item: _CollectedPrimaryInput,
        *,
        disposition: str,
        terminal_reason: str | None = None,
    ) -> None:
        context: dict[str, Any] = {
            "turn_id": task.task_id,
            "client_message_id": item.client_message_id,
            "surface_id": item.surface_id,
            "intent": item.intent,
            "disposition": disposition,
            "target_turn_id": task.task_id,
            "revision": item.revision,
        }
        if item.client_request_id is not None:
            context["client_request_id"] = item.client_request_id
        try:
            updated = await self._update_transcript_turn_context(
                task.envelope.session_key,
                item.persisted_user_message_id,
                context,
            )
            if item.persisted_user_message_id and not updated:
                log.warning(
                    "task_runtime.collected_input_disposition_persist_missed",
                    session_key=task.envelope.session_key,
                    task_id=task.task_id,
                    message_id=item.persisted_user_message_id,
                    disposition=disposition,
                )
        except Exception:  # noqa: BLE001 - live evidence must still be emitted
            log.warning(
                "task_runtime.collected_input_disposition_persist_failed",
                session_key=task.envelope.session_key,
                task_id=task.task_id,
                message_id=item.persisted_user_message_id,
                disposition=disposition,
                exc_info=True,
            )
        event_details = {"terminal_reason": terminal_reason} if terminal_reason else {}
        try:
            await self._emit(
                task.envelope.session_key,
                "session.event.input_disposition",
                {
                    "session_key": task.envelope.session_key,
                    "user_message_id": item.persisted_user_message_id,
                    **context,
                    **event_details,
                },
            )
        except Exception:  # noqa: BLE001 - task cleanup must continue
            log.warning(
                "task_runtime.collected_input_disposition_emit_failed",
                session_key=task.envelope.session_key,
                task_id=task.task_id,
                message_id=item.persisted_user_message_id,
                disposition=disposition,
                exc_info=True,
            )

    async def _mark_unfinished_abandoned(self) -> int:
        async with self._state_lock:
            unfinished = [
                task for task in self._tasks.values() if not task.terminal_closing
            ]
        for task in unfinished:
            await self._mark_terminal(
                task,
                AgentTaskStatus.ABANDONED,
                terminal_reason="shutdown_timeout",
                promote_pending_steers=True,
                activate_promoted_steers=False,
            )
        return len(unfinished)

    def _remove_pending(self, task: _RuntimeTask) -> None:
        pending = self._pending_by_session.get(task.envelope.session_key)
        if not pending:
            return
        try:
            pending.remove(task)
        except ValueError:
            return
        if not pending:
            self._pending_by_session.pop(task.envelope.session_key, None)

    async def _emit(self, session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        if self._event_emitter is None:
            return
        await self._event_emitter(session_key, event_name, payload)

    async def _notify_task_lifecycle(self, event: TaskLifecycleEvent) -> None:
        if self._lifecycle_listener is None:
            return
        try:
            snapshot = await self.session_task_snapshot(
                event.session_key,
                excluding_task_id=(event.task_id if event.phase == "terminal" else None),
            )
        except Exception:
            # The changed task remains useful evidence, but listeners must not
            # infer a foreground owner from the callback that happened to fire.
            log.warning(
                "task_runtime.session_task_snapshot_failed",
                session_key=event.session_key,
                task_id=event.task_id,
                phase=event.phase,
                exc_info=True,
            )
        else:
            event = replace(event, task_snapshot=snapshot)
        try:
            await self._lifecycle_listener(event)
        except Exception:
            log.warning(
                "task_runtime.lifecycle_listener_failed",
                session_key=event.session_key,
                task_id=event.task_id,
                phase=event.phase,
                task_status=event.task_status,
                exc_info=True,
            )

    def set_activation_listener(
        self,
        listener: TaskActivationListener | None,
    ) -> None:
        """Install the shared pre-running activation hook."""

        self._activation_listener = listener

    def set_idle_listener(self, listener: RuntimeIdleListener | None) -> None:
        """Install a post-driver cleanup idle hook."""

        self._idle_listener = listener

    def set_lifecycle_listener(
        self,
        listener: TaskLifecycleListener | None,
    ) -> None:
        """Install the ordered lifecycle fan-out used by gateway services."""

        self._lifecycle_listener = listener

    def set_goal_service(self, service: Any | None) -> None:
        """Install the process-local Goal tool authority."""

        self._goal_service = service

    @property
    def goal_service(self) -> Any | None:
        """Return the installed Goal coordinator without exposing its storage."""

        return self._goal_service

    async def _notify_runtime_idle(self, session_key: str) -> None:
        listener = self._idle_listener
        if listener is None:
            return
        try:
            await listener(session_key)
        except Exception:
            log.warning(
                "task_runtime.idle_listener_failed",
                session_key=session_key,
                exc_info=True,
            )

    async def _notify_subagent_terminal(
        self,
        task: _RuntimeTask,
        status: AgentTaskStatus,
        *,
        terminal_reason: str,
        error_class: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if self._terminal_listener is None or task.run_kind != "subagent":
            return
        parent_session_key = task.envelope.metadata.get("parent_session_key")
        if not isinstance(parent_session_key, str) or not parent_session_key:
            return
        event = SubagentCompletionEvent(
            parent_session_key=parent_session_key,
            child_session_key=task.envelope.session_key,
            task_id=task.task_id,
            status=status,
            terminal_reason=terminal_reason,
            agent_id=task.envelope.agent_id,
            parent_task_id=task.envelope.metadata.get("parent_task_id"),
            error_class=error_class,
            error_message=error_message,
        )
        try:
            await self._terminal_listener(event)
        except Exception:
            return

    async def _terminal_details_update(
        self,
        task: _RuntimeTask,
        *,
        status: AgentTaskStatus,
        terminal_reason: str,
        error_class: str | None,
        error_message: str | None,
        failure_kind: str | None,
        retry_after_ms: int | None,
        activity_snapshot: object,
        usage_call_index: int | None,
        no_prior_provider_dispatch: bool,
        replay_safe: bool,
    ) -> dict[str, Any]:
        outcome = _subagent_group_outcome_from_provenance(task.envelope.input_provenance)
        existing = await self._storage.get_agent_task(task.task_id)
        current_details = getattr(existing, "details", None)
        details = dict(current_details) if isinstance(current_details, dict) else {}
        pending_applied = task.pending_input_provider.pending_applied()
        if pending_applied:
            # If transcript persistence was temporarily unavailable after a
            # provider call started, commit enough evidence with the terminal
            # task row for startup recovery to close the input as ``applied``
            # instead of replaying it as a follow-up.
            details["applied_steer_evidence"] = [
                {
                    "message_id": item.persisted_user_message_id,
                    "applied_iteration": item.applied_iteration,
                    "model_call_id": item.model_call_id,
                }
                for item in pending_applied
                if item.persisted_user_message_id
            ]
        else:
            details.pop("applied_steer_evidence", None)
        cancellation: dict[str, str] | None = None
        if status == AgentTaskStatus.CANCELLED:
            cancellation = {
                "source": task.cancel_source
                or ("overflow_drop" if task.overflow_dropped else "unknown"),
                "reason": task.cancel_reason
                or ("overflow_drop" if task.overflow_dropped else terminal_reason),
            }
            details["cancellation"] = cancellation
        details.pop("cancellation_requested", None)
        if status == AgentTaskStatus.SUCCEEDED:
            details["turn_outcome"] = completed_outcome().to_dict()
            if task.terminal_assistant_message_content is not None:
                # This is a compact durable channel outbox payload. It keeps
                # delivery exact after compaction/reset and avoids inferring
                # ownership from unrelated assistant writers in the session.
                details["terminal_assistant_message_content"] = (
                    task.terminal_assistant_message_content
                )
                if task.terminal_assistant_message_id is not None:
                    details["terminal_assistant_message_id"] = (
                        task.terminal_assistant_message_id
                    )
        else:
            turn_outcome = outcome_from_error(
                code=terminal_reason if terminal_reason != "error" else error_class,
                message=error_message,
                error_class=error_class,
                failure_kind=failure_kind,
            ).to_dict()
            if cancellation is not None:
                turn_outcome["cancellation_source"] = cancellation["source"]
            if is_usage_accounting_barrier(error_class):
                replay_proof = usage_barrier_replay_proof(
                    usage_call_index=usage_call_index,
                    no_prior_provider_dispatch=no_prior_provider_dispatch,
                    replay_safe=replay_safe,
                )
                turn_outcome.update(replay_proof)
                details.update(replay_proof)
                primary_user_message_id = safe_primary_user_message_id(
                    task.persisted_user_message_id
                )
                if primary_user_message_id is not None:
                    turn_outcome["user_message_id"] = primary_user_message_id
                safe_retry = safe_retry_after_ms(retry_after_ms)
                if safe_retry is not None:
                    turn_outcome["retry_after_ms"] = safe_retry
                    details["retry_after_ms"] = safe_retry
                snapshot = terminal_activity_snapshot(
                    activity_snapshot,
                    task_id=task.task_id,
                    turn_id=task.task_id,
                )
                if snapshot is not None:
                    details["activity_snapshot"] = snapshot
            details["turn_outcome"] = turn_outcome
        if outcome is not None:
            details["subagent_group_outcome"] = outcome
            disclosure_required = task.envelope.input_provenance.get(
                "runtime_partial_failure_disclosure_required"
            )
            if disclosure_required is True:
                details["runtime_partial_failure_disclosure_required"] = True
        return {"details": details}

    async def _cache_terminal_fallback_record(
        self,
        task: _RuntimeTask,
        *,
        status: AgentTaskStatus,
        terminal_update: dict[str, Any],
    ) -> None:
        try:
            existing = await self._storage.get_agent_task(task.task_id)
        except Exception as exc:  # noqa: BLE001 - fallback must not fail terminalization.
            log.warning(
                "task_runtime.terminal_fallback_read_failed",
                task_id=task.task_id,
                session_key=task.envelope.session_key,
                error=str(exc),
            )
            existing = None
        if existing is not None:
            record = existing.model_copy(deep=True)
        else:
            now = _epoch_time_ms()
            record = AgentTaskRecord(
                task_id=task.task_id,
                session_key=task.envelope.session_key,
                agent_id=task.envelope.agent_id,
                source_kind=task.envelope.source_kind.value,
                queue_mode=task.queue_mode,
                run_kind=task.run_kind,
                status=status,
                created_at=now,
                updated_at=now,
            )
        for key, value in terminal_update.items():
            if hasattr(record, key):
                setattr(record, key, value)
        self._terminal_fallback_records[task.task_id] = record

    async def _compensate_terminal_persistence(
        self,
        task: _RuntimeTask,
        *,
        terminal_update: dict[str, Any],
        status: AgentTaskStatus,
        terminal_reason: str,
        error_class: str | None,
        error_message: str | None,
        failure_kind: str | None,
        retry_after_ms: int | None,
        activity_snapshot: object,
        usage_call_index: int | None,
        no_prior_provider_dispatch: bool,
        replay_safe: bool,
    ) -> bool:
        """Retry one terminal task-row update after its public fallback event."""

        try:
            terminal_update.update(
                await self._terminal_details_update(
                    task,
                    status=status,
                    terminal_reason=terminal_reason,
                    error_class=error_class,
                    error_message=error_message,
                    failure_kind=failure_kind,
                    retry_after_ms=retry_after_ms,
                    activity_snapshot=activity_snapshot,
                    usage_call_index=usage_call_index,
                    no_prior_provider_dispatch=no_prior_provider_dispatch,
                    replay_safe=replay_safe,
                )
            )
        except Exception as exc:  # noqa: BLE001 - retain any first-pass details.
            log.warning(
                "task_runtime.terminal_details_compensation_failed",
                task_id=task.task_id,
                session_key=task.envelope.session_key,
                error=str(exc),
            )
        try:
            await self._storage.update_agent_task(task.task_id, **terminal_update)
        except Exception as exc:  # noqa: BLE001 - the in-memory terminal stays usable.
            log.warning(
                "task_runtime.terminal_persist_compensation_failed",
                task_id=task.task_id,
                session_key=task.envelope.session_key,
                error=str(exc),
            )
            return False
        self._terminal_fallback_records.pop(task.task_id, None)
        log.info(
            "task_runtime.terminal_persist_compensated",
            task_id=task.task_id,
            session_key=task.envelope.session_key,
        )
        return True


def _subagent_group_outcome_from_provenance(
    input_provenance: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(input_provenance, dict):
        return None
    outcome = input_provenance.get("subagent_group_outcome")
    if not isinstance(outcome, dict):
        return None
    return dict(outcome)


def _epoch_time_ms() -> int:
    return int(time.time() * 1000)
