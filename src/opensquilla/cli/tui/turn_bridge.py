"""TUI-owned default bridge for shared turn streaming.

This module keeps the legacy TUI turn-stream facade while frontend presentation
defaults live in ``turn_stream_defaults``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from opensquilla.cli.chat import turn_stream as _turn_stream
from opensquilla.cli.chat.turn import TurnResult, UsageSummary
from opensquilla.cli.tui.backend.contracts import TuiOutputHandle
from opensquilla.engine.commands import Surface

if TYPE_CHECKING:
    from opensquilla.engine.agent_injection import PendingInputProvider

TurnStreamDependencies = _turn_stream.TurnStreamDependencies

ORIGINAL_TURN_STREAM_WRAP = _turn_stream.wrap_cli_turn_stream
DEFAULT_STREAM_HEARTBEAT_INTERVAL_SECONDS = (
    _turn_stream._DEFAULT_STREAM_HEARTBEAT_INTERVAL_SECONDS
)
DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = _turn_stream._DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS


def tool_result_success_from_status(status: Any, *, legacy_is_error: bool) -> bool:
    return _turn_stream._tool_result_success_from_status(
        status,
        legacy_is_error=legacy_is_error,
    )


def turn_stream_error_message(event: Any) -> str:
    return _turn_stream.turn_stream_error_message(event)


def timeout_exception_message(exc: BaseException) -> str:
    return _turn_stream.timeout_exception_message(exc)


def optional_positive_config_float(config_source: Any, attr: str, default: float) -> float | None:
    return _turn_stream.optional_positive_config_float(config_source, attr, default)


def wrap_cli_turn_stream(stream: Any, config_source: Any) -> Any:
    return ORIGINAL_TURN_STREAM_WRAP(stream, config_source)


def is_approval_or_blocked_result(result: Any) -> bool:
    return _turn_stream.is_approval_or_blocked_result(result)


def approval_surface_for_tui_output(
    tui_output: TuiOutputHandle | None,
    default: Surface,
) -> Surface:
    import opensquilla.cli.tui.adapters.turn_stream_defaults as turn_stream_defaults  # noqa: PLC0415

    return turn_stream_defaults.approval_surface_for_tui_output(tui_output, default)


def _approval_surface_for_terminal_output(
    tui_output: TuiOutputHandle | None,
    default: object | None,
) -> object | None:
    import opensquilla.cli.tui.adapters.turn_stream_defaults as turn_stream_defaults  # noqa: PLC0415

    return turn_stream_defaults._approval_surface_for_terminal_output(
        tui_output,
        default,
    )


def image_prompt_and_attachments(command: str) -> tuple[str, list[dict[str, str]]]:
    import opensquilla.cli.tui.adapters.turn_stream_defaults as turn_stream_defaults  # noqa: PLC0415

    return turn_stream_defaults.image_prompt_and_attachments(command)


def default_turn_stream_dependencies(
    *,
    renderer_factory: Callable[..., Any] | None = None,
    stream_wrapper: Callable[[Any, Any], Any] | None = None,
    approval_handler: Callable[..., Awaitable[None]] | None = None,
    cancel_clearer: Callable[[], None] | None = None,
    image_attachment_builder: Callable[[str], tuple[str, list[dict[str, str]]]]
    | None = None,
    output_console: Any | None = None,
    error_panel_factory: Callable[[str], Any] | None = None,
) -> TurnStreamDependencies:
    import opensquilla.cli.tui.adapters.turn_stream_defaults as turn_stream_defaults  # noqa: PLC0415

    return turn_stream_defaults.default_turn_stream_dependencies(
        renderer_factory=renderer_factory,
        stream_wrapper=stream_wrapper,
        approval_handler=approval_handler,
        cancel_clearer=cancel_clearer,
        image_attachment_builder=image_attachment_builder,
        output_console=output_console,
        error_panel_factory=error_panel_factory,
    )


def render_gateway_task_group_status(
    event_name: str,
    event: dict[str, Any],
    renderer: Any,
    *,
    deps: TurnStreamDependencies | None = None,
) -> None:
    _turn_stream.render_gateway_task_group_status(
        event_name,
        event,
        renderer,
        deps=deps,
    )


def gateway_task_group_status(
    event_name: str,
    event: dict[str, Any],
) -> tuple[str, str] | None:
    return _turn_stream.gateway_task_group_status(event_name, event)


async def arender_gateway_task_group_status(
    event_name: str,
    event: dict[str, Any],
    renderer: Any,
    *,
    deps: TurnStreamDependencies | None = None,
) -> None:
    await _turn_stream.arender_gateway_task_group_status(
        event_name,
        event,
        renderer,
        deps=deps,
    )


async def renderer_status(
    renderer: Any,
    message: str,
    *,
    style: str = "dim",
    deps: TurnStreamDependencies | None = None,
) -> None:
    await _turn_stream.renderer_status(
        renderer,
        message,
        style=style,
        deps=deps,
    )


async def renderer_tool_start(
    renderer: Any,
    name: str,
    args: dict | None,
    tool_use_id: str | None,
) -> None:
    await _turn_stream.renderer_tool_start(renderer, name, args, tool_use_id)


async def renderer_tool_finished(
    renderer: Any,
    tool_use_id: str | None,
    *,
    success: bool,
) -> None:
    await _turn_stream.renderer_tool_finished(
        renderer,
        tool_use_id,
        success=success,
    )


async def renderer_error(renderer: Any, message: str) -> None:
    await _turn_stream.renderer_error(renderer, message)


async def renderer_finalize(
    renderer: Any,
    usage: UsageSummary | None = None,
    *,
    cancelled: bool = False,
) -> None:
    await _turn_stream.renderer_finalize(renderer, usage, cancelled=cancelled)


async def renderer_close(renderer: Any) -> None:
    await _turn_stream.renderer_close(renderer)


def artifact_event_payload(event: Any) -> dict[str, Any]:
    return _turn_stream.artifact_event_payload(event)


def artifact_status_line(artifact: dict[str, Any]) -> str:
    return _turn_stream.artifact_status_line(artifact)


async def stream_response_gateway(
    client: Any,
    session_key: str,
    message: str,
    elevated_state: dict[str, str | None] | None = None,
    attachments: list[dict] | None = None,
    *,
    tui_output: TuiOutputHandle | None = None,
    deps: TurnStreamDependencies | None = None,
) -> TurnResult:
    return await _turn_stream.stream_response_gateway(
        client,
        session_key,
        message,
        elevated_state,
        attachments=attachments,
        tui_output=tui_output,
        deps=deps,
    )


def local_approval_resolver(**kwargs: object) -> Callable[..., Awaitable[None]]:
    return _turn_stream.local_approval_resolver(**kwargs)


async def stream_response_turnrunner(
    turn_runner: object,
    session_key: str,
    tool_ctx: object,
    message: str,
    model: str | None = None,
    svc: object = None,
    timeout: float | None = None,
    *,
    tui_output: TuiOutputHandle | None = None,
    deps: TurnStreamDependencies | None = None,
    pending_input_provider: PendingInputProvider | None = None,
) -> TurnResult:
    return await _turn_stream.stream_response_turnrunner(
        turn_runner,
        session_key,
        tool_ctx,
        message,
        model=model,
        svc=svc,
        timeout=timeout,
        tui_output=tui_output,
        deps=deps,
        pending_input_provider=pending_input_provider,
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
    tui_output: TuiOutputHandle | None = None,
    deps: TurnStreamDependencies | None = None,
    pending_input_provider: PendingInputProvider | None = None,
) -> TurnResult:
    return await _turn_stream.handle_image_command_turnrunner(
        turn_runner,
        session_key,
        tool_ctx,
        command,
        model=model,
        svc=svc,
        timeout=timeout,
        tui_output=tui_output,
        deps=deps,
        pending_input_provider=pending_input_provider,
    )
