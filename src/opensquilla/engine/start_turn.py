"""Shared enqueue helper for turn ingress."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from opensquilla.observability.decision_log import PipelineStepRecord

if TYPE_CHECKING:
    # Type-check only: runtime import would cycle through opensquilla.tools.
    from opensquilla.gateway.routing import RouteEnvelope
    from opensquilla.gateway.task_runtime import TaskHandle, TaskReservation, TaskRuntime


def _ingress_step_record() -> PipelineStepRecord:
    """The single ``PipelineStepRecord`` the helper records per call."""
    return PipelineStepRecord(
        step_name="start_turn_via_runtime",
        applied=True,
        routing_source="none",
    )


def _turn_kwargs(
    *,
    attachments: list[dict[str, Any]] | None,
    mode: str | None,
    run_kind: str,
    no_memory_capture: bool,
    semantic_message: str | None,
    persisted_user_message_id: str | None,
    fresh_user_session: bool | None,
    stream_event_sink: Callable[[Any], Awaitable[None]] | None,
    turn_id: str | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "attachments": attachments,
        "mode": mode,
        "run_kind": run_kind,
        "ingress_pipeline_steps": (_ingress_step_record(),),
    }
    if no_memory_capture:
        kwargs["no_memory_capture"] = True
    if semantic_message is not None:
        kwargs["semantic_message"] = semantic_message
    if persisted_user_message_id is not None:
        kwargs["persisted_user_message_id"] = persisted_user_message_id
    if fresh_user_session is not None:
        kwargs["fresh_user_session"] = fresh_user_session
    if stream_event_sink is not None:
        kwargs["stream_event_sink"] = stream_event_sink
    if turn_id is not None:
        kwargs["task_id"] = turn_id
    return kwargs


async def reserve_turn_via_runtime(
    runtime: TaskRuntime,
    envelope: RouteEnvelope,
    message: str,
    *,
    attachments: list[dict[str, Any]] | None = None,
    mode: str | None = None,
    run_kind: str = "default",
    no_memory_capture: bool = False,
    semantic_message: str | None = None,
    persisted_user_message_id: str | None = None,
    fresh_user_session: bool | None = None,
    stream_event_sink: Callable[[Any], Awaitable[None]] | None = None,
    turn_id: str | None = None,
    overflow_policy: Any = None,
) -> TaskReservation:
    """Reserve runtime admission while preserving shared ingress metadata."""

    kwargs = _turn_kwargs(
        attachments=attachments,
        mode=mode,
        run_kind=run_kind,
        no_memory_capture=no_memory_capture,
        semantic_message=semantic_message,
        persisted_user_message_id=persisted_user_message_id,
        fresh_user_session=fresh_user_session,
        stream_event_sink=stream_event_sink,
        turn_id=turn_id,
    )
    if overflow_policy is not None:
        kwargs["overflow_policy"] = overflow_policy
    return await runtime.reserve(envelope, message, **kwargs)


async def start_turn_via_runtime(
    runtime: TaskRuntime,
    envelope: RouteEnvelope,
    message: str,
    *,
    attachments: list[dict[str, Any]] | None = None,
    mode: str | None = None,
    run_kind: str = "default",
    no_memory_capture: bool = False,
    semantic_message: str | None = None,
    persisted_user_message_id: str | None = None,
    fresh_user_session: bool | None = None,
    stream_event_sink: Callable[[Any], Awaitable[None]] | None = None,
    turn_id: str | None = None,
) -> TaskHandle:
    """Enqueue a turn. Exceptions propagate — recovery is surface-specific.

    For DecisionLog ownership: the helper passes a
    ``PipelineStepRecord`` to ``TaskRuntime.enqueue`` via the
    ``ingress_pipeline_steps`` kwarg. The runtime stores it on ``TaskRun``
    (not on ``envelope.metadata``) so the cached envelope in
    ``_last_envelope_by_session`` cannot leak stale ingress markers into
    later proactive sends via ``TaskRuntime.send``.

    ``semantic_message`` is the raw user text used by semantic runtime
    processing when the runtime path needs to diverge from the persisted
    ``message`` (for example, transcript stamping after persistence).
    Forwarded only when set so legacy callers and mocks pre-dating the kwarg work.

    ``no_memory_capture`` is forwarded only when truthy for the same
    legacy-compatibility reason.
    """
    kwargs = _turn_kwargs(
        attachments=attachments,
        mode=mode,
        run_kind=run_kind,
        no_memory_capture=no_memory_capture,
        semantic_message=semantic_message,
        persisted_user_message_id=persisted_user_message_id,
        fresh_user_session=fresh_user_session,
        stream_event_sink=stream_event_sink,
        turn_id=turn_id,
    )
    return await runtime.enqueue(envelope, message, **kwargs)
