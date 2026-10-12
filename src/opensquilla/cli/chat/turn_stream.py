"""Shared turn streaming adapter for chat surfaces.

This module owns the bridge from gateway/runtime turn events to renderer
updates. It is deliberately independent of concrete terminal input apps:
callers pass typed output handles, renderers, and session/tool dependencies.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from opensquilla.cli.chat.output import ChatOutputHandle
from opensquilla.cli.chat.turn import TurnResult, UsageSummary
from opensquilla.cli.tui.backend.domain_events import (
    KIND_DONE,
    KIND_ERROR,
    KIND_REASONING_FLUSH,
    KIND_ROUTER_DECISION,
    KIND_STATUS,
    KIND_TOOL_FINISHED,
    KIND_TOOL_STARTED,
    KIND_WARNING,
    TuiDomainEvent,
    TuiDomainEventSource,
    now_ms,
)
from opensquilla.cli.tui.backend.streaming import StreamingPlane
from opensquilla.engine.types import done_text_snapshot
from opensquilla.execution_status import derive_is_error
from opensquilla.router_tiers import tier_index
from opensquilla.session.terminal_reply import build_terminal_reply

_DEFAULT_STREAM_HEARTBEAT_INTERVAL_SECONDS = 15.0
_DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = 600.0

ApprovalSurfaceResolver = Callable[
    [ChatOutputHandle | None, object | None],
    object | None,
]
TuiEventSinkFactory = Callable[
    [ChatOutputHandle | None],
    Callable[[TuiDomainEvent], None] | None,
]

if TYPE_CHECKING:
    from opensquilla.engine.agent_injection import PendingInputProvider


class GatewayStreamingClient(Protocol):
    def send_message(
        self,
        session_key: str,
        message: str,
        attachments: list[dict] | None = None,
        elevated: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]: ...

    async def resolve_approval(
        self,
        approval_id: str,
        approved: bool,
        *,
        choice: str | None = None,
    ) -> Any: ...

    async def abort_session(self, key: str) -> Any: ...


@dataclass(frozen=True)
class TurnStreamDependencies:
    renderer_factory: Callable[..., Any]
    stream_wrapper: Callable[[Any, Any], Any]
    approval_handler: Callable[..., Awaitable[None]]
    cancel_clearer: Callable[[], None]
    image_attachment_builder: Callable[[str], tuple[str, list[dict[str, str]]]]
    output_console: Any
    error_panel_factory: Callable[[str], Any]
    gateway_approval_surface: object | None = None
    standalone_approval_surface: object | None = None
    approval_surface_resolver: ApprovalSurfaceResolver | None = None
    tui_event_sink: Callable[[TuiDomainEvent], None] | None = None
    tui_event_sink_factory: TuiEventSinkFactory | None = None


class _BackendFallbackRenderer:
    def __init__(self, **_kwargs: Any) -> None:
        self.buffer = ""

    def __enter__(self) -> _BackendFallbackRenderer:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> Literal[False]:
        return False

    async def aappend_text(self, delta: str, *, presentation: str = "answer") -> None:
        del presentation
        self.buffer += delta

    async def areconcile_final_text(self, text: str) -> None:
        self.buffer = text

    def pulse(self) -> None:
        return None

    def tool_start(
        self,
        _name: str,
        _args: dict | None,
        _tool_use_id: str | None,
    ) -> None:
        return None

    def tool_finished(self, _tool_use_id: str | None, **_kwargs: Any) -> None:
        return None

    def status(self, _message: str, **_kwargs: Any) -> None:
        return None

    def error(self, _message: str) -> None:
        return None

    def finalize(
        self,
        _usage: UsageSummary | None = None,
        **_kwargs: Any,
    ) -> None:
        return None

    async def aclose(self) -> None:
        return None


class _NoopConsole:
    def print(self, *_objects: Any, **_kwargs: Any) -> None:
        return None


async def _noop_approval_handler(*_args: Any, **_kwargs: Any) -> None:
    return None


def _noop_cancel_clearer() -> None:
    return None


def _plain_error_panel(message: str) -> str:
    return message


def image_prompt_and_attachments(command: str) -> tuple[str, list[dict[str, str]]]:
    raise ValueError("Image attachments are not configured.")


def default_turn_stream_dependencies(
    *,
    renderer_factory: Callable[..., Any] | None = None,
    stream_wrapper: Callable[[Any, Any], Any] | None = None,
    approval_handler: Callable[..., Awaitable[None]] | None = None,
    cancel_clearer: Callable[[], None] | None = None,
    image_attachment_builder: Callable[[str], tuple[str, list[dict[str, str]]]] | None = None,
    output_console: Any | None = None,
    error_panel_factory: Callable[[str], Any] | None = None,
    gateway_approval_surface: object | None = None,
    standalone_approval_surface: object | None = None,
    approval_surface_resolver: ApprovalSurfaceResolver | None = None,
    tui_event_sink: Callable[[TuiDomainEvent], None] | None = None,
    tui_event_sink_factory: TuiEventSinkFactory | None = None,
) -> TurnStreamDependencies:
    return TurnStreamDependencies(
        renderer_factory=(
            _BackendFallbackRenderer if renderer_factory is None else renderer_factory
        ),
        stream_wrapper=wrap_cli_turn_stream if stream_wrapper is None else stream_wrapper,
        approval_handler=(
            _noop_approval_handler if approval_handler is None else approval_handler
        ),
        cancel_clearer=_noop_cancel_clearer if cancel_clearer is None else cancel_clearer,
        image_attachment_builder=(
            image_prompt_and_attachments
            if image_attachment_builder is None
            else image_attachment_builder
        ),
        output_console=_NoopConsole() if output_console is None else output_console,
        error_panel_factory=(
            _plain_error_panel if error_panel_factory is None else error_panel_factory
        ),
        gateway_approval_surface=gateway_approval_surface,
        standalone_approval_surface=standalone_approval_surface,
        approval_surface_resolver=approval_surface_resolver,
        tui_event_sink=tui_event_sink,
        tui_event_sink_factory=tui_event_sink_factory,
    )


def _resolve_deps(deps: TurnStreamDependencies | None) -> TurnStreamDependencies:
    if deps is not None:
        return deps
    return default_turn_stream_dependencies()


def _resolve_tui_event_sink_for_output(
    deps: TurnStreamDependencies,
    tui_output: ChatOutputHandle | None,
) -> TurnStreamDependencies:
    if deps.tui_event_sink_factory is None:
        return deps
    output_sink = deps.tui_event_sink_factory(tui_output)
    if output_sink is None:
        return deps
    if deps.tui_event_sink is None:
        return replace(deps, tui_event_sink=output_sink)

    existing_sink = deps.tui_event_sink

    def _combined_sink(event: TuiDomainEvent) -> None:
        existing_sink(event)
        output_sink(event)

    return replace(deps, tui_event_sink=_combined_sink)


def _emit_tui_domain_event(
    deps: TurnStreamDependencies,
    *,
    kind: str,
    source: TuiDomainEventSource,
    payload: dict[str, Any],
    turn_id: str | None,
) -> None:
    if deps.tui_event_sink is None:
        return
    deps.tui_event_sink(
        TuiDomainEvent(
            kind=kind,
            source=source,
            payload=payload,
            turn_id=turn_id,
            timestamp_ms=now_ms(),
        )
    )


def _string_field(payload: Mapping[str, Any], key: str, default: str = "") -> str:
    value = payload.get(key, default)
    if value is None:
        return default
    return str(value)


def _bool_field(payload: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = payload.get(key)
    if value is None:
        return default
    return bool(value)


def _float_field(payload: Mapping[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if isinstance(value, int | float):
        return float(value)
    return None


def _int_field(payload: Mapping[str, Any], key: str, default: int) -> int:
    value = payload.get(key)
    if isinstance(value, int):
        return value
    return default


def _optional_int_field(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _float_list_field(payload: Mapping[str, Any], key: str) -> list[float]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    values: list[float] = []
    for item in value:
        values.append(float(item) if isinstance(item, int | float) else 0.0)
    return values


def _tier_index_from_tier(tier: str) -> int:
    return tier_index(tier)


def normalize_router_decision_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    tier = _string_field(payload, "tier")
    source = _string_field(payload, "source", "none")
    return {
        "tier": tier,
        "tier_index": _int_field(
            payload,
            "tier_index",
            _tier_index_from_tier(tier),
        ),
        "model": _string_field(payload, "model"),
        "baseline_model": _string_field(payload, "baseline_model"),
        "source": source,
        "confidence": _float_field(payload, "confidence"),
        "probs": _float_list_field(payload, "probs"),
        "savings_pct": _float_field(payload, "savings_pct"),
        "fallback": _bool_field(payload, "fallback", source == "fallback"),
        "thinking_mode": _string_field(payload, "thinking_mode"),
        "prompt_policy": _string_field(payload, "prompt_policy"),
        "routing_applied": _bool_field(payload, "routing_applied", True),
        "rollout_phase": _string_field(payload, "rollout_phase", "full"),
        "context_window": _optional_int_field(payload, "context_window"),
    }


def normalize_ensemble_progress_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the renderer-facing subset of a real ensemble progress event.

    Gateway frames also carry session/turn identities while TurnRunner events
    carry a ``kind`` field.  Keeping only the provider lifecycle payload gives
    both paths one stable renderer contract without inferring execution from
    the current Gateway configuration.
    """

    return {
        "event_type": _string_field(payload, "event_type", "proposer_start"),
        "proposer_index": _int_field(payload, "proposer_index", -1),
        "proposer_label": _string_field(payload, "proposer_label"),
        "proposer_model": _string_field(payload, "proposer_model"),
        "proposer_provider": _string_field(payload, "proposer_provider"),
        "sample_index": _int_field(payload, "sample_index", 0),
        "elapsed_ms": _int_field(payload, "elapsed_ms", 0),
        "input_tokens": _int_field(payload, "input_tokens", 0),
        "output_tokens": _int_field(payload, "output_tokens", 0),
        "cost_usd": _float_field(payload, "cost_usd") or 0.0,
        "error": _string_field(payload, "error"),
    }


async def _renderer_append_text(
    renderer: Any,
    text: str,
    presentation: str,
) -> None:
    # Pass presentation when the renderer supports it; fall back for renderers
    # (tests, fallbacks) whose aappend_text takes only the delta.
    try:
        await renderer.aappend_text(text, presentation=presentation)
    except TypeError:
        await renderer.aappend_text(text)


async def _flush_streaming_text(
    renderer: Any,
    plane: StreamingPlane,
    text: str,
) -> None:
    await _renderer_append_text(renderer, text, getattr(plane, "_text_presentation", "answer"))


async def _append_text_delta(
    renderer: Any,
    deps: TurnStreamDependencies,
    plane: StreamingPlane | None,
    delta: str,
    *,
    source: TuiDomainEventSource,
    turn_id: str | None,
    presentation: str = "answer",
) -> None:
    if plane is None:
        await _renderer_append_text(renderer, delta, presentation)
        return
    # A presentation switch (intermediate -> answer) is a hard boundary: flush
    # whatever is buffered under the old presentation before mixing in the new,
    # so the renderer opens the right block kind for each.
    prev = getattr(plane, "_text_presentation", None)
    if prev is not None and prev != presentation:
        flush = plane.finish()
        if flush is not None:
            await _renderer_append_text(renderer, flush.text, prev)
    plane._text_presentation = presentation  # type: ignore[attr-defined]
    flush = plane.append(delta)
    if flush is not None:
        await _flush_streaming_text(renderer, plane, flush.text)


async def _finish_text_delta_stream(
    renderer: Any,
    deps: TurnStreamDependencies,
    plane: StreamingPlane | None,
    *,
    source: TuiDomainEventSource,
    turn_id: str | None,
) -> None:
    if plane is None:
        return
    flush = plane.finish()
    if flush is not None:
        await _flush_streaming_text(
            renderer,
            plane,
            flush.text,
        )


async def _drain_stalled_text_plane(
    renderer: Any,
    plane: StreamingPlane | None,
) -> None:
    """Drain a buffered text tail while the stream is stalled.

    ``StreamingPlane.append`` only evaluates flush conditions when the next
    delta arrives, so a burst tail followed by a long stall (e.g. the model
    pausing before a tool call) would sit unrendered until the stream
    resumes — heartbeat ticks call this to keep the tail flowing.
    """
    if plane is None:
        return
    flush = plane.flush()
    if flush is not None:
        await _flush_streaming_text(renderer, plane, flush.text)


async def _drain_stalled_reasoning_plane(
    renderer: Any,
    plane: StreamingPlane | None,
) -> None:
    if plane is None:
        return
    flush = plane.flush()
    if flush is not None:
        await _flush_streaming_reasoning(renderer, flush.text)


async def _drain_stalled_planes(
    renderer: Any,
    text_plane: StreamingPlane | None,
    reasoning_plane: StreamingPlane | None,
    *,
    reasoning_mid_stream: bool,
) -> None:
    """Drain stalled buffers without splitting an open reasoning marker.

    Rendering answer text closes any open reasoning block, so while the
    reasoning stream is the active one a buffered text tail must stay
    buffered: draining it mid-stall would end the thinking marker and split
    one thought into two blocks. The tail still flushes at the next text
    delta or boundary event.
    """
    if not reasoning_mid_stream:
        await _drain_stalled_text_plane(renderer, text_plane)
    await _drain_stalled_reasoning_plane(renderer, reasoning_plane)


async def _flush_streaming_reasoning(
    renderer: Any,
    text: str,
) -> None:
    append = getattr(renderer, "aappend_reasoning", None)
    if append is not None:
        await append(text)


async def _append_reasoning_delta(
    renderer: Any,
    deps: TurnStreamDependencies,
    plane: StreamingPlane | None,
    delta: str,
    *,
    source: TuiDomainEventSource,
    turn_id: str | None,
) -> None:
    if plane is None:
        await _flush_streaming_reasoning(renderer, delta)
        return
    flush = plane.append(delta)
    if flush is not None:
        await _flush_streaming_reasoning(renderer, flush.text)


async def _finish_reasoning_stream(
    renderer: Any,
    deps: TurnStreamDependencies,
    plane: StreamingPlane | None,
    *,
    source: TuiDomainEventSource,
    turn_id: str | None,
) -> None:
    if plane is None:
        return
    flush = plane.finish()
    if flush is not None:
        await _flush_streaming_reasoning(renderer, flush.text)


def _async_renderer_method(method: object) -> Callable[..., Awaitable[None]]:
    return cast(Callable[..., Awaitable[None]], method)


def _tool_result_success_from_status(status: Any, *, legacy_is_error: bool) -> bool:
    if isinstance(status, dict):
        return status.get("status") == "success" and not derive_is_error(status)
    return not legacy_is_error


def turn_stream_error_message(event: Any) -> str:
    message = getattr(event, "message", "")
    code = str(getattr(event, "code", "") or "").lower()
    message_text = str(message)
    if "timeout" in code or "stream idle" in message_text.lower():
        return build_terminal_reply(
            {
                "status": "timeout",
                "terminal_reason": "timeout",
                "error_class": getattr(event, "code", None),
                "error_message": message_text,
            }
        )
    return message_text


def timeout_exception_message(exc: BaseException) -> str:
    return build_terminal_reply(
        {
            "status": "timeout",
            "terminal_reason": "timeout",
            "error_class": exc.__class__.__name__,
            "error_message": str(exc),
        }
    )


def optional_positive_config_float(config_source: Any, attr: str, default: float) -> float | None:
    config = getattr(config_source, "config", config_source)
    raw = getattr(config, attr, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    return value if value > 0 else None


def wrap_cli_turn_stream(stream: Any, config_source: Any) -> Any:
    from opensquilla.engine.stream_wrappers import wrap_stream

    return wrap_stream(
        stream,
        idle_timeout=optional_positive_config_float(
            config_source,
            "agent_stream_idle_timeout_seconds",
            _DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
        ),
        heartbeat_interval=optional_positive_config_float(
            config_source,
            "agent_stream_heartbeat_interval_seconds",
            _DEFAULT_STREAM_HEARTBEAT_INTERVAL_SECONDS,
        ),
        heartbeat_phase="cli",
        heartbeat_message="Still working",
    )


def is_approval_or_blocked_result(result: Any) -> bool:
    """Return True when a tool_result payload is an approval/block envelope."""
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (ValueError, TypeError):
            return False
        if not isinstance(parsed, dict):
            return False
        payload = parsed
    elif isinstance(result, dict):
        payload = result
    else:
        return False
    return payload.get("status") in {"approval_required", "approval_pending", "blocked"}


def approval_surface_for_tui_output(
    tui_output: ChatOutputHandle | None,
    default: object | None,
) -> object | None:
    if tui_output is None:
        return default
    approval_surface: object | None = getattr(tui_output, "approval_surface", None)
    if approval_surface is not None:
        return approval_surface
    return default


def _resolve_approval_surface(
    tui_output: ChatOutputHandle | None,
    default: object | None,
    deps: TurnStreamDependencies,
) -> object | None:
    resolver = deps.approval_surface_resolver or approval_surface_for_tui_output
    return resolver(tui_output, default)


def render_gateway_task_group_status(
    event_name: str,
    event: dict[str, Any],
    renderer: Any,
    *,
    deps: TurnStreamDependencies | None = None,
) -> None:
    stream_deps = _resolve_deps(deps)
    status_item = gateway_task_group_status(event_name, event)
    if status_item is None:
        return
    message, style = status_item
    status = getattr(renderer, "status", None)
    if callable(status):
        status(message, style=style)
    else:
        stream_deps.output_console.print(f"[{style}]{message}[/]")


def gateway_task_group_status(
    event_name: str,
    event: dict[str, Any],
) -> tuple[str, str] | None:
    phase = event_name.rsplit(".", 1)[-1]
    style = "dim"
    if phase == "waiting":
        pending = event.get("pending_count")
        suffix = f" ({pending} pending)" if isinstance(pending, int) and pending >= 0 else ""
        message = f"subagents waiting{suffix}"
    elif phase == "synthesizing":
        child_count = event.get("child_count")
        suffix = f" from {child_count} children" if isinstance(child_count, int) else ""
        message = f"subagents complete; synthesizing final answer{suffix}"
    elif phase == "done":
        delivery_status = event.get("delivery_status")
        suffix = f" (delivery: {delivery_status})" if isinstance(delivery_status, str) else ""
        message = f"background synthesis complete{suffix}"
    elif phase == "failed":
        error_message = event.get("error_message")
        suffix = f": {error_message}" if isinstance(error_message, str) and error_message else ""
        message = f"background synthesis failed{suffix}"
        style = "yellow"
    else:
        return None
    return message, style


async def arender_gateway_task_group_status(
    event_name: str,
    event: dict[str, Any],
    renderer: Any,
    *,
    deps: TurnStreamDependencies | None = None,
) -> None:
    status_item = gateway_task_group_status(event_name, event)
    if status_item is None:
        return
    message, style = status_item
    await renderer_status(renderer, message, style=style, deps=deps)


async def renderer_status(
    renderer: Any,
    message: str,
    *,
    style: str = "dim",
    deps: TurnStreamDependencies | None = None,
) -> None:
    stream_deps = _resolve_deps(deps)
    astatus = getattr(renderer, "astatus", None)
    if callable(astatus):
        await _async_renderer_method(astatus)(message, style=style)
        return
    status = getattr(renderer, "status", None)
    if callable(status):
        status(message, style=style)
    else:
        stream_deps.output_console.print(f"[{style}]{message}[/]")


async def renderer_ensemble_progress(
    renderer: Any,
    payload: dict[str, Any],
) -> None:
    """Forward ensemble lifecycle data when the renderer opts into the hook."""

    method = getattr(renderer, "aensemble_progress", None)
    if callable(method):
        await _async_renderer_method(method)(payload)


async def renderer_tool_start(
    renderer: Any,
    name: str,
    args: dict | None,
    tool_use_id: str | None,
) -> None:
    atool_start = getattr(renderer, "atool_start", None)
    if callable(atool_start):
        await _async_renderer_method(atool_start)(name, args, tool_use_id)
        return
    renderer.tool_start(name, args, tool_use_id)


async def renderer_tool_finished(
    renderer: Any,
    tool_use_id: str | None,
    *,
    success: bool,
    result: object | None = None,
) -> None:
    atool_finished = getattr(renderer, "atool_finished", None)
    if callable(atool_finished):
        try:
            await _async_renderer_method(atool_finished)(
                tool_use_id,
                success=success,
                result=result,
            )
        except TypeError:
            await _async_renderer_method(atool_finished)(tool_use_id, success=success)
        return
    try:
        renderer.tool_finished(tool_use_id, success=success, result=result)
    except TypeError:
        renderer.tool_finished(tool_use_id, success=success)


async def renderer_error(renderer: Any, message: str) -> None:
    aerror = getattr(renderer, "aerror", None)
    if callable(aerror):
        await _async_renderer_method(aerror)(message)
        return
    renderer.error(message)


async def renderer_finalize(
    renderer: Any,
    usage: UsageSummary | None = None,
    *,
    cancelled: bool = False,
) -> None:
    afinalize = getattr(renderer, "afinalize", None)
    if callable(afinalize):
        await _async_renderer_method(afinalize)(usage, cancelled=cancelled)
        return
    renderer.finalize(usage, cancelled=cancelled)


async def renderer_close(renderer: Any) -> None:
    aclose = getattr(renderer, "aclose", None)
    if callable(aclose):
        await _async_renderer_method(aclose)()


async def renderer_reconcile_final_text(renderer: Any, text: str) -> None:
    """Replace a renderer's logical answer with an authoritative terminal snapshot.

    Renderers may provide an async reconciliation hook when their UI can replace a
    live preview. The shared fallback still rewrites ``buffer`` so every caller's
    ``TurnResult`` observes the canonical terminal value, including an explicit
    empty snapshot.
    """

    reconcile = getattr(renderer, "areconcile_final_text", None)
    if callable(reconcile):
        await _async_renderer_method(reconcile)(text)
        return
    renderer.buffer = text


def artifact_event_payload(event: Any) -> dict[str, Any]:
    from opensquilla.artifacts import artifact_payload

    if isinstance(event, dict):
        return artifact_payload(
            {key: value for key, value in event.items() if key not in {"event", "payload"}}
        )

    return artifact_payload(event)


def artifact_status_line(artifact: dict[str, Any]) -> str:
    name = artifact.get("name") if isinstance(artifact.get("name"), str) else "artifact"
    target = artifact.get("download_url") if isinstance(artifact.get("download_url"), str) else ""
    return f"Generated file: {name} -> {target or artifact.get('id', '')}"


async def dispatch_gateway_stream(
    client: GatewayStreamingClient,
    session_key: str,
    message: str,
    elevated_state: dict[str, str | None] | None = None,
    attachments: list[dict] | None = None,
    *,
    tui_output: ChatOutputHandle | None = None,
    deps: TurnStreamDependencies | None = None,
) -> TurnResult:
    kwargs: dict[str, Any] = {"tui_output": tui_output}
    if deps is not None:
        kwargs["deps"] = deps
    return await stream_response_gateway(
        client, session_key, message, elevated_state, attachments=attachments, **kwargs
    )


async def stream_response_gateway(
    client: GatewayStreamingClient,
    session_key: str,
    message: str,
    elevated_state: dict[str, str | None] | None = None,
    attachments: list[dict] | None = None,
    *,
    tui_output: ChatOutputHandle | None = None,
    deps: TurnStreamDependencies | None = None,
) -> TurnResult:
    """Stream a response from the gateway into a renderer."""
    stream_deps = _resolve_deps(deps)
    stream_deps = _resolve_tui_event_sink_for_output(stream_deps, tui_output)
    elevated = elevated_state["mode"] if elevated_state else None
    usage: UsageSummary | None = None
    cancelled = False
    artifacts: list[dict[str, Any]] = []
    model_after: str | None = None

    approval_surface = _resolve_approval_surface(
        tui_output,
        stream_deps.gateway_approval_surface,
        stream_deps,
    )

    with stream_deps.renderer_factory(output_handle=tui_output) as renderer:
        # Announce the turn before the first provider event: without this the
        # UI sits visibly dead ("ready" pill, nothing pulsing) for the whole
        # model-thinking window between submit and the first token.
        _turn_started = getattr(renderer, "aturn_started", None)
        if _turn_started is not None:
            await _turn_started()
        streaming_plane = (
            StreamingPlane(
                event_sink=stream_deps.tui_event_sink,
                source="gateway",
                turn_id=session_key,
            )
            if tui_output is not None or stream_deps.tui_event_sink is not None
            else None
        )
        try:
            try:
                from opensquilla.cli.tui.backend.input_identity import (
                    tui_turn_identity_sink_scope,
                )

                async def _bind_turn_identity(
                    turn_id: str,
                    client_message_id: str,
                ) -> None:
                    bind_identity = getattr(renderer, "aset_turn_identity", None)
                    if callable(bind_identity):
                        await bind_identity(turn_id, client_message_id)

                async def _identity_bound_events() -> AsyncIterator[dict[str, Any]]:
                    with tui_turn_identity_sink_scope(_bind_turn_identity):
                        async for item in client.send_message(
                            session_key,
                            message,
                            attachments=attachments,
                            elevated=elevated,
                        ):
                            yield item

                async for event in _identity_bound_events():
                    event_name = event.get("event", "")
                    if event_name == "session.event.accepted":
                        bind_identity = getattr(renderer, "aset_turn_identity", None)
                        if callable(bind_identity):
                            turn_id = event.get("turn_id") or event.get("task_id")
                            client_message_id = event.get("client_message_id")
                            if isinstance(turn_id, str) and isinstance(
                                client_message_id, str
                            ):
                                await bind_identity(turn_id, client_message_id)
                    elif event_name == "session.event.text_delta":
                        await _append_text_delta(
                            renderer,
                            stream_deps,
                            streaming_plane,
                            event.get("text", ""),
                            source="gateway",
                            turn_id=session_key,
                            presentation=event.get("presentation", "answer"),
                        )
                    elif event_name == "session.event.thinking":
                        # The agent re-emits reasoning as ThinkingEvent, which
                        # rpc_sessions broadcasts as session.event.thinking. The
                        # Gateway path already emits each provider delta as an
                        # event, so drive the live transcript block directly.
                        # The renderer retains the complete safe text, shows a
                        # rolling stream while active, then folds it behind
                        # Ctrl+O when the next text/tool closes the block.
                        await _append_reasoning_delta(
                            renderer,
                            stream_deps,
                            None,
                            event.get("text", ""),
                            source="gateway",
                            turn_id=session_key,
                        )
                    elif event_name == "session.event.router_decision":
                        await _finish_text_delta_stream(
                            renderer,
                            stream_deps,
                            streaming_plane,
                            source="gateway",
                            turn_id=session_key,
                        )
                        _emit_tui_domain_event(
                            stream_deps,
                            kind=KIND_ROUTER_DECISION,
                            source="gateway",
                            payload=normalize_router_decision_payload(event),
                            turn_id=session_key,
                        )
                    elif event_name == "session.event.ensemble_progress":
                        await renderer_ensemble_progress(
                            renderer,
                            normalize_ensemble_progress_payload(event),
                        )
                    elif event_name == "session.event.tool_use_start":
                        await _finish_text_delta_stream(
                            renderer,
                            stream_deps,
                            streaming_plane,
                            source="gateway",
                            turn_id=session_key,
                        )
                        tool_name = (
                            event.get("tool_name") or event.get("toolName") or "tool"
                        )
                        tool_args = event.get("input") or event.get("arguments")
                        tool_use_id = event.get("tool_use_id") or event.get("toolUseId")
                        _emit_tui_domain_event(
                            stream_deps,
                            kind=KIND_TOOL_STARTED,
                            source="gateway",
                            payload={
                                "tool_name": tool_name,
                                "args": tool_args,
                                "tool_use_id": tool_use_id,
                            },
                            turn_id=session_key,
                        )
                        await renderer_tool_start(
                            renderer,
                            tool_name,
                            tool_args,
                            tool_use_id,
                        )
                    elif event_name == "session.event.tool_result":
                        await _finish_text_delta_stream(
                            renderer,
                            stream_deps,
                            streaming_plane,
                            source="gateway",
                            turn_id=session_key,
                        )
                        await stream_deps.approval_handler(
                            event.get("result"),
                            renderer,
                            client.resolve_approval,
                            elevated_state=elevated_state,
                            surface=approval_surface,
                        )
                        if not is_approval_or_blocked_result(event.get("result")):
                            tool_use_id = event.get("tool_use_id") or event.get("toolUseId")
                            success = _tool_result_success_from_status(
                                event.get("execution_status")
                                or event.get("executionStatus"),
                                legacy_is_error=bool(
                                    event.get("is_error") or event.get("isError")
                                ),
                            )
                            _emit_tui_domain_event(
                                stream_deps,
                                kind=KIND_TOOL_FINISHED,
                                source="gateway",
                                payload={
                                    "tool_use_id": tool_use_id,
                                    "success": success,
                                    "execution_status": event.get("execution_status")
                                    or event.get("executionStatus"),
                                    "is_error": bool(
                                        event.get("is_error") or event.get("isError")
                                    ),
                                },
                                turn_id=session_key,
                            )
                            await renderer_tool_finished(
                                renderer,
                                tool_use_id,
                                success=success,
                                result=event.get("result"),
                            )
                    elif event_name == "session.event.artifact":
                        await _finish_text_delta_stream(
                            renderer,
                            stream_deps,
                            streaming_plane,
                            source="gateway",
                            turn_id=session_key,
                        )
                        artifact = artifact_event_payload(event)
                        artifacts.append(artifact)
                        status_line = artifact_status_line(artifact)
                        _emit_tui_domain_event(
                            stream_deps,
                            kind=KIND_STATUS,
                            source="gateway",
                            payload={"message": status_line, "artifact": artifact},
                            turn_id=session_key,
                        )
                        await renderer_status(
                            renderer,
                            status_line,
                            deps=stream_deps,
                        )
                    elif event_name.startswith("session.event.task_group."):
                        await _finish_text_delta_stream(
                            renderer,
                            stream_deps,
                            streaming_plane,
                            source="gateway",
                            turn_id=session_key,
                        )
                        status_item = gateway_task_group_status(event_name, event)
                        if status_item is not None:
                            message, style = status_item
                            _emit_tui_domain_event(
                                stream_deps,
                                kind=KIND_STATUS,
                                source="gateway",
                                payload={
                                    "message": message,
                                    "style": style,
                                    "event": event_name,
                                },
                                turn_id=session_key,
                            )
                            await renderer_status(
                                renderer,
                                message,
                                style=style,
                                deps=stream_deps,
                            )
                    elif event_name == "session.event.error":
                        message_text = event.get("message", "unknown")
                        await _finish_text_delta_stream(
                            renderer,
                            stream_deps,
                            streaming_plane,
                            source="gateway",
                            turn_id=session_key,
                        )
                        _emit_tui_domain_event(
                            stream_deps,
                            kind=KIND_ERROR,
                            source="gateway",
                            payload={"message": message_text},
                            turn_id=session_key,
                        )
                        await renderer_error(renderer, message_text)
                        return TurnResult(
                            text=renderer.buffer,
                            usage=usage,
                            error=message_text,
                            artifacts=artifacts,
                        )
                    elif event_name == "session.event.done":
                        usage = UsageSummary.from_gateway_payload(event)
                        cancelled = event.get("reason") == "aborted"
                        model_after = event.get("routed_model") or event.get("model") or None
                        snapshot_present, snapshot_text = done_text_snapshot(event)
                        await _finish_text_delta_stream(
                            renderer,
                            stream_deps,
                            streaming_plane,
                            source="gateway",
                            turn_id=session_key,
                        )
                        if snapshot_present:
                            await renderer_reconcile_final_text(renderer, snapshot_text)
                        done_payload: dict[str, Any] = {
                            "model": model_after,
                            "cancelled": cancelled,
                            "reason": event.get("reason"),
                        }
                        if snapshot_present:
                            done_payload["text_snapshot"] = snapshot_text
                        _emit_tui_domain_event(
                            stream_deps,
                            kind=KIND_DONE,
                            source="gateway",
                            payload=done_payload,
                            turn_id=session_key,
                        )
            except (KeyboardInterrupt, asyncio.CancelledError):
                stream_deps.cancel_clearer()
                await client.abort_session(session_key)
                cancelled = True
            except Exception:
                await _finish_text_delta_stream(
                    renderer,
                    stream_deps,
                    streaming_plane,
                    source="gateway",
                    turn_id=session_key,
                )
                raise
            await _finish_text_delta_stream(
                renderer,
                stream_deps,
                streaming_plane,
                source="gateway",
                turn_id=session_key,
            )
            await renderer_finalize(renderer, usage, cancelled=cancelled)
        finally:
            await renderer_close(renderer)
    return TurnResult(
        text=renderer.buffer,
        usage=usage,
        cancelled=cancelled,
        artifacts=artifacts,
        model_after=model_after,
    )


def local_approval_resolver(
    *,
    session_manager: object | None = None,
    config: object | None = None,
) -> Callable[..., Awaitable[None]]:
    """Return a resolver that talks directly to the in-process approval queue."""

    async def _resolve(
        approval_id: str,
        approved: bool,
        *,
        choice: str | None = None,
    ) -> None:
        from opensquilla.gateway.rpc import RpcContext, get_dispatcher

        params: dict[str, Any] = {
            "id": approval_id,
            "approved": approved,
        }
        if choice:
            params["choice"] = choice
        result = await get_dispatcher().dispatch(
            "cli-local-approval-resolve",
            "exec.approval.resolve",
            params,
            RpcContext(
                conn_id="cli-local-approval",
                session_manager=session_manager,
                config=config,
            ),
        )
        if result.error is not None:
            raise RuntimeError(result.error.message)

    return _resolve


async def stream_response_turnrunner(
    turn_runner: object,
    session_key: str,
    tool_ctx: object,
    message: str,
    model: str | None = None,
    svc: object = None,
    timeout: float | None = None,
    *,
    tui_output: ChatOutputHandle | None = None,
    deps: TurnStreamDependencies | None = None,
    pending_input_provider: PendingInputProvider | None = None,
) -> TurnResult:
    """Stream a TurnRunner response into a renderer."""
    from opensquilla.engine.runtime import TurnRunner
    from opensquilla.engine.types import (
        ArtifactEvent,
        DoneEvent,
        EnsembleProgressEvent,
        ErrorEvent,
        RouterDecisionEvent,
        RunHeartbeatEvent,
        TextDeltaEvent,
        ThinkingEvent,
        ToolResultEvent,
        ToolUseStartEvent,
        WarningEvent,
    )
    from opensquilla.tools.types import ToolContext

    assert isinstance(turn_runner, TurnRunner)
    assert isinstance(tool_ctx, ToolContext)

    stream_deps = _resolve_deps(deps)
    stream_deps = _resolve_tui_event_sink_for_output(stream_deps, tui_output)
    session_manager = getattr(svc, "session_manager", None) if svc is not None else None
    config = getattr(svc, "config", None) if svc is not None else None
    if session_manager is not None:
        _persisted = await session_manager.append_message(session_key, role="user", content=message)
        if _persisted is not None and isinstance(_persisted.content, str):
            message = _persisted.content

    resolver = local_approval_resolver(session_manager=session_manager, config=config)
    usage: UsageSummary | None = None
    cancelled = False
    artifacts: list[dict[str, Any]] = []
    model_after: str | None = None

    approval_surface = _resolve_approval_surface(
        tui_output,
        stream_deps.standalone_approval_surface,
        stream_deps,
    )

    with stream_deps.renderer_factory(output_handle=tui_output) as renderer:
        # Announce the turn before the first provider event: without this the
        # UI sits visibly dead ("ready" pill, nothing pulsing) for the whole
        # model-thinking window between submit and the first token.
        _turn_started = getattr(renderer, "aturn_started", None)
        if _turn_started is not None:
            await _turn_started()
        streaming_plane = (
            StreamingPlane(
                event_sink=stream_deps.tui_event_sink,
                source="turn_runner",
                turn_id=session_key,
            )
            if tui_output is not None or stream_deps.tui_event_sink is not None
            else None
        )
        # Reasoning streams on its own plane so its coalescing buffer never
        # interleaves with the answer text buffer (the two arrive as separate
        # event kinds and must stay separate on screen).
        reasoning_plane = (
            StreamingPlane(
                event_sink=stream_deps.tui_event_sink,
                source="turn_runner",
                turn_id=session_key,
                flush_kind=KIND_REASONING_FLUSH,
            )
            if tui_output is not None or stream_deps.tui_event_sink is not None
            else None
        )
        # Tracks which plane streamed most recently: the heartbeat drain must
        # not render a stale text tail while reasoning is mid-stream, or it
        # would close the open thinking marker and split the thought.
        reasoning_mid_stream = False
        try:
            try:
                stream = turn_runner.run(
                    message,
                    session_key,
                    tool_context=tool_ctx,
                    model=model,
                    timeout=timeout,
                    pending_input_provider=pending_input_provider,
                )
                async for event in stream_deps.stream_wrapper(stream, svc):
                    if isinstance(event, TextDeltaEvent):
                        reasoning_mid_stream = False
                        await _append_text_delta(
                            renderer,
                            stream_deps,
                            streaming_plane,
                            event.text,
                            source="turn_runner",
                            turn_id=session_key,
                            presentation=getattr(event, "presentation", "answer"),
                        )
                    elif isinstance(event, ThinkingEvent):
                        reasoning_mid_stream = True
                        await _append_reasoning_delta(
                            renderer,
                            stream_deps,
                            reasoning_plane,
                            event.text,
                            source="turn_runner",
                            turn_id=session_key,
                        )
                    elif isinstance(event, RouterDecisionEvent):
                        await _finish_text_delta_stream(
                            renderer,
                            stream_deps,
                            streaming_plane,
                            source="turn_runner",
                            turn_id=session_key,
                        )
                        _emit_tui_domain_event(
                            stream_deps,
                            kind=KIND_ROUTER_DECISION,
                            source="turn_runner",
                            payload=normalize_router_decision_payload(event.__dict__),
                            turn_id=session_key,
                        )
                    elif isinstance(event, EnsembleProgressEvent):
                        await renderer_ensemble_progress(
                            renderer,
                            normalize_ensemble_progress_payload(event.__dict__),
                        )
                    elif isinstance(event, RunHeartbeatEvent):
                        renderer.pulse()
                        await _drain_stalled_planes(
                            renderer,
                            streaming_plane,
                            reasoning_plane,
                            reasoning_mid_stream=reasoning_mid_stream,
                        )
                    elif isinstance(event, ToolUseStartEvent):
                        await _finish_text_delta_stream(
                            renderer,
                            stream_deps,
                            streaming_plane,
                            source="turn_runner",
                            turn_id=session_key,
                        )
                        # Reasoning ends when the model turns to a tool: flush
                        # the thinking block before the tool block opens.
                        await _finish_reasoning_stream(
                            renderer,
                            stream_deps,
                            reasoning_plane,
                            source="turn_runner",
                            turn_id=session_key,
                        )
                        reasoning_mid_stream = False
                        _emit_tui_domain_event(
                            stream_deps,
                            kind=KIND_TOOL_STARTED,
                            source="turn_runner",
                            payload={
                                "tool_name": event.tool_name,
                                "tool_use_id": event.tool_use_id,
                                "synthetic_from_text": event.synthetic_from_text,
                            },
                            turn_id=session_key,
                        )
                        await renderer_tool_start(
                            renderer,
                            event.tool_name,
                            None,
                            event.tool_use_id,
                        )
                    elif isinstance(event, ToolResultEvent):
                        await _finish_text_delta_stream(
                            renderer,
                            stream_deps,
                            streaming_plane,
                            source="turn_runner",
                            turn_id=session_key,
                        )
                        await stream_deps.approval_handler(
                            event.result,
                            renderer,
                            resolver,
                            surface=approval_surface,
                        )
                        if not is_approval_or_blocked_result(event.result):
                            success = _tool_result_success_from_status(
                                event.execution_status,
                                legacy_is_error=event.is_error,
                            )
                            _emit_tui_domain_event(
                                stream_deps,
                                kind=KIND_TOOL_FINISHED,
                                source="turn_runner",
                                payload={
                                    "tool_name": event.tool_name,
                                    "tool_use_id": event.tool_use_id,
                                    "success": success,
                                    "execution_status": event.execution_status,
                                    "is_error": event.is_error,
                                },
                                turn_id=session_key,
                            )
                            await renderer_tool_finished(
                                renderer,
                                event.tool_use_id,
                                success=success,
                                result=event.result,
                            )
                    elif isinstance(event, ArtifactEvent):
                        await _finish_text_delta_stream(
                            renderer,
                            stream_deps,
                            streaming_plane,
                            source="turn_runner",
                            turn_id=session_key,
                        )
                        artifact = artifact_event_payload(event)
                        artifacts.append(artifact)
                        status_line = artifact_status_line(artifact)
                        _emit_tui_domain_event(
                            stream_deps,
                            kind=KIND_STATUS,
                            source="turn_runner",
                            payload={"message": status_line, "artifact": artifact},
                            turn_id=session_key,
                        )
                        await renderer_status(
                            renderer,
                            status_line,
                            deps=stream_deps,
                        )
                    elif isinstance(event, WarningEvent):
                        await _finish_text_delta_stream(
                            renderer,
                            stream_deps,
                            streaming_plane,
                            source="turn_runner",
                            turn_id=session_key,
                        )
                        _emit_tui_domain_event(
                            stream_deps,
                            kind=KIND_WARNING,
                            source="turn_runner",
                            payload={"message": event.message},
                            turn_id=session_key,
                        )
                        await renderer_status(
                            renderer,
                            event.message,
                            style="yellow",
                            deps=stream_deps,
                        )
                    elif isinstance(event, ErrorEvent):
                        message_text = turn_stream_error_message(event)
                        await _finish_text_delta_stream(
                            renderer,
                            stream_deps,
                            streaming_plane,
                            source="turn_runner",
                            turn_id=session_key,
                        )
                        _emit_tui_domain_event(
                            stream_deps,
                            kind=KIND_ERROR,
                            source="turn_runner",
                            payload={"message": message_text},
                            turn_id=session_key,
                        )
                        await renderer_error(renderer, message_text)
                        return TurnResult(
                            text=renderer.buffer,
                            usage=usage,
                            error=message_text,
                            artifacts=artifacts,
                        )
                    elif isinstance(event, DoneEvent):
                        usage = UsageSummary.from_done_event(event)
                        model_after = usage.model or None
                        snapshot_present, snapshot_text = done_text_snapshot(event)
                        await _finish_text_delta_stream(
                            renderer,
                            stream_deps,
                            streaming_plane,
                            source="turn_runner",
                            turn_id=session_key,
                        )
                        if snapshot_present:
                            await renderer_reconcile_final_text(renderer, snapshot_text)
                        done_payload: dict[str, Any] = {
                            "model": model_after,
                            "cancelled": False,
                            "stop_reason": getattr(event, "stop_reason", None),
                        }
                        if snapshot_present:
                            done_payload["text_snapshot"] = snapshot_text
                        _emit_tui_domain_event(
                            stream_deps,
                            kind=KIND_DONE,
                            source="turn_runner",
                            payload=done_payload,
                            turn_id=session_key,
                        )
            except (KeyboardInterrupt, asyncio.CancelledError):
                stream_deps.cancel_clearer()
                cancelled = True
            except TimeoutError as exc:
                message_text = timeout_exception_message(exc)
                await _finish_text_delta_stream(
                    renderer,
                    stream_deps,
                    streaming_plane,
                    source="turn_runner",
                    turn_id=session_key,
                )
                await renderer_error(renderer, message_text)
                return TurnResult(text=renderer.buffer, error=message_text)
            except Exception:
                await _finish_text_delta_stream(
                    renderer,
                    stream_deps,
                    streaming_plane,
                    source="turn_runner",
                    turn_id=session_key,
                )
                raise
            await _finish_text_delta_stream(
                renderer,
                stream_deps,
                streaming_plane,
                source="turn_runner",
                turn_id=session_key,
            )
            await _finish_reasoning_stream(
                renderer,
                stream_deps,
                reasoning_plane,
                source="turn_runner",
                turn_id=session_key,
            )
            await renderer_finalize(renderer, usage, cancelled=cancelled)
        finally:
            await renderer_close(renderer)
    return TurnResult(
        text=renderer.buffer,
        usage=usage,
        cancelled=cancelled,
        artifacts=artifacts,
        model_after=model_after,
    )


async def handle_image_command_turnrunner(
    turn_runner: object,
    session_key: str,
    tool_ctx: object,
    command: str,
    model: str | None = None,
    svc: object = None,
    timeout: float | None = None,
    *,
    tui_output: ChatOutputHandle | None = None,
    deps: TurnStreamDependencies | None = None,
    pending_input_provider: PendingInputProvider | None = None,
) -> TurnResult:
    """Handle /image <path> [prompt] via TurnRunner attachments."""
    from opensquilla.engine.runtime import TurnRunner
    from opensquilla.engine.types import (
        DoneEvent,
        EnsembleProgressEvent,
        ErrorEvent,
        RouterDecisionEvent,
        RunHeartbeatEvent,
        TextDeltaEvent,
        ToolUseStartEvent,
    )
    from opensquilla.tools.types import ToolContext

    assert isinstance(turn_runner, TurnRunner)
    assert isinstance(tool_ctx, ToolContext)

    stream_deps = _resolve_deps(deps)
    stream_deps = _resolve_tui_event_sink_for_output(stream_deps, tui_output)
    try:
        prompt, attachments = stream_deps.image_attachment_builder(command)
    except ValueError as exc:
        stream_deps.output_console.print(stream_deps.error_panel_factory(str(exc)))
        return TurnResult(error=str(exc))

    session_manager = getattr(svc, "session_manager", None) if svc is not None else None
    if session_manager is not None:
        _persisted = await session_manager.append_message(session_key, role="user", content=prompt)
        if _persisted is not None and isinstance(_persisted.content, str):
            prompt = _persisted.content

    usage: UsageSummary | None = None
    model_after: str | None = None
    with stream_deps.renderer_factory(output_handle=tui_output) as renderer:
        # Announce the turn before the first provider event: without this the
        # UI sits visibly dead ("ready" pill, nothing pulsing) for the whole
        # model-thinking window between submit and the first token.
        _turn_started = getattr(renderer, "aturn_started", None)
        if _turn_started is not None:
            await _turn_started()
        streaming_plane = (
            StreamingPlane(
                event_sink=stream_deps.tui_event_sink,
                source="turn_runner",
                turn_id=session_key,
            )
            if tui_output is not None or stream_deps.tui_event_sink is not None
            else None
        )
        try:
            try:
                stream = turn_runner.run(
                    prompt,
                    session_key,
                    tool_context=tool_ctx,
                    model=model,
                    attachments=attachments,
                    timeout=timeout,
                    pending_input_provider=pending_input_provider,
                )
                async for event in stream_deps.stream_wrapper(stream, svc):
                    if isinstance(event, TextDeltaEvent):
                        await _append_text_delta(
                            renderer,
                            stream_deps,
                            streaming_plane,
                            event.text,
                            source="turn_runner",
                            turn_id=session_key,
                        )
                    elif isinstance(event, RouterDecisionEvent):
                        await _finish_text_delta_stream(
                            renderer,
                            stream_deps,
                            streaming_plane,
                            source="turn_runner",
                            turn_id=session_key,
                        )
                        _emit_tui_domain_event(
                            stream_deps,
                            kind=KIND_ROUTER_DECISION,
                            source="turn_runner",
                            payload=normalize_router_decision_payload(event.__dict__),
                            turn_id=session_key,
                        )
                    elif isinstance(event, EnsembleProgressEvent):
                        await renderer_ensemble_progress(
                            renderer,
                            normalize_ensemble_progress_payload(event.__dict__),
                        )
                    elif isinstance(event, RunHeartbeatEvent):
                        renderer.pulse()
                        await _drain_stalled_text_plane(renderer, streaming_plane)
                    elif isinstance(event, ToolUseStartEvent):
                        await _finish_text_delta_stream(
                            renderer,
                            stream_deps,
                            streaming_plane,
                            source="turn_runner",
                            turn_id=session_key,
                        )
                        _emit_tui_domain_event(
                            stream_deps,
                            kind=KIND_TOOL_STARTED,
                            source="turn_runner",
                            payload={
                                "tool_name": event.tool_name,
                                "tool_use_id": event.tool_use_id,
                                "synthetic_from_text": event.synthetic_from_text,
                            },
                            turn_id=session_key,
                        )
                        await renderer_tool_start(
                            renderer,
                            event.tool_name,
                            None,
                            event.tool_use_id,
                        )
                    elif isinstance(event, ErrorEvent):
                        message_text = turn_stream_error_message(event)
                        await _finish_text_delta_stream(
                            renderer,
                            stream_deps,
                            streaming_plane,
                            source="turn_runner",
                            turn_id=session_key,
                        )
                        _emit_tui_domain_event(
                            stream_deps,
                            kind=KIND_ERROR,
                            source="turn_runner",
                            payload={"message": message_text},
                            turn_id=session_key,
                        )
                        await renderer_error(renderer, message_text)
                        return TurnResult(text=renderer.buffer, usage=usage, error=message_text)
                    elif isinstance(event, DoneEvent):
                        usage = UsageSummary.from_done_event(event)
                        model_after = usage.model or None
                        snapshot_present, snapshot_text = done_text_snapshot(event)
                        await _finish_text_delta_stream(
                            renderer,
                            stream_deps,
                            streaming_plane,
                            source="turn_runner",
                            turn_id=session_key,
                        )
                        if snapshot_present:
                            await renderer_reconcile_final_text(renderer, snapshot_text)
                        done_payload: dict[str, Any] = {
                            "model": model_after,
                            "cancelled": False,
                            "stop_reason": getattr(event, "stop_reason", None),
                        }
                        if snapshot_present:
                            done_payload["text_snapshot"] = snapshot_text
                        _emit_tui_domain_event(
                            stream_deps,
                            kind=KIND_DONE,
                            source="turn_runner",
                            payload=done_payload,
                            turn_id=session_key,
                        )
            except TimeoutError as exc:
                message_text = timeout_exception_message(exc)
                await _finish_text_delta_stream(
                    renderer,
                    stream_deps,
                    streaming_plane,
                    source="turn_runner",
                    turn_id=session_key,
                )
                await renderer_error(renderer, message_text)
                return TurnResult(text=renderer.buffer, error=message_text)
            except Exception:
                await _finish_text_delta_stream(
                    renderer,
                    stream_deps,
                    streaming_plane,
                    source="turn_runner",
                    turn_id=session_key,
                )
                raise
            await _finish_text_delta_stream(
                renderer,
                stream_deps,
                streaming_plane,
                source="turn_runner",
                turn_id=session_key,
            )
            await renderer_finalize(renderer, usage)
        finally:
            await renderer_close(renderer)
    return TurnResult(text=renderer.buffer, usage=usage, model_after=model_after)
