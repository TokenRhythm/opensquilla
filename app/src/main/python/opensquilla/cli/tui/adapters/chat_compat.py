"""TUI-owned helpers for legacy ``chat_cmd`` private exports.

The Typer chat command is now a thin launch entrypoint. This module keeps the
older helper surface available for tests and legacy callers while TUI adapters
own the concrete input, slash, and turn-stream wiring.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import opensquilla.cli.tui.adapters.input_bridge as _input_bridge
from opensquilla.cli.chat.session_state import ChatSessionState
from opensquilla.cli.tui import turn_bridge as _turn_bridge
from opensquilla.cli.tui.adapters import runtime_bridge as _runtime_bridge
from opensquilla.cli.tui.adapters import slash_bridge as _slash_bridge
from opensquilla.cli.tui.backend.contracts import TuiOutputHandle
from opensquilla.engine.commands import Surface

if TYPE_CHECKING:
    from opensquilla.engine.agent_injection import PendingInputProvider

CLI_ALLOWED_FILE_MIMES = _input_bridge.CLI_ALLOWED_FILE_MIMES
CLI_INLINE_THRESHOLD_BYTES = _input_bridge.CLI_INLINE_THRESHOLD_BYTES
PATH_REMOTE_GATEWAY_MESSAGE = _input_bridge.PATH_REMOTE_GATEWAY_MESSAGE
CLI_ATTACHMENT_COMPAT_EXPORTS = (
    CLI_ALLOWED_FILE_MIMES,
    CLI_INLINE_THRESHOLD_BYTES,
)
GATEWAY_SLASH_HANDLER_WORDS = _slash_bridge.GATEWAY_SLASH_HANDLER_WORDS
STANDALONE_SLASH_HANDLER_WORDS = _slash_bridge.STANDALONE_SLASH_HANDLER_WORDS
GatewayClientLike = _slash_bridge.GatewayClientLike
GatewayStreamResponse = _slash_bridge.GatewayStreamResponse
TurnResult = _turn_bridge.TurnResult
UsageSummary = _turn_bridge.UsageSummary
TurnStreamDependencies = _turn_bridge.TurnStreamDependencies
ORIGINAL_TURN_STREAM_WRAP = _turn_bridge.ORIGINAL_TURN_STREAM_WRAP
DEFAULT_STREAM_HEARTBEAT_INTERVAL_SECONDS = (
    _turn_bridge.DEFAULT_STREAM_HEARTBEAT_INTERVAL_SECONDS
)
DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = _turn_bridge.DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS


def tool_result_success_from_status(status: Any, *, legacy_is_error: bool) -> bool:
    return _turn_bridge.tool_result_success_from_status(
        status,
        legacy_is_error=legacy_is_error,
    )


def turn_stream_error_message(event: Any) -> str:
    return _turn_bridge.turn_stream_error_message(event)


def timeout_exception_message(exc: BaseException) -> str:
    return _turn_bridge.timeout_exception_message(exc)


def optional_positive_config_float(
    config_source: Any,
    attr: str,
    default: float,
) -> float | None:
    return _turn_bridge.optional_positive_config_float(config_source, attr, default)


def wrap_cli_turn_stream(stream: Any, config_source: Any) -> Any:
    return ORIGINAL_TURN_STREAM_WRAP(stream, config_source)


def resolve_compaction_provider(
    provider_selector: Any,
    model_override: str | None = None,
) -> Any | None:
    return _slash_bridge.resolve_compaction_provider(
        provider_selector,
        model_override,
    )


def is_approval_or_blocked_result(result: Any) -> bool:
    return _turn_bridge.is_approval_or_blocked_result(result)


def approval_surface_for_tui_output(
    tui_output: TuiOutputHandle | None,
    default: Surface,
) -> Surface:
    return _turn_bridge.approval_surface_for_tui_output(tui_output, default)


async def flush_before_standalone_rewrite(
    svc: Any,
    session_key: str,
    *,
    operation: str,
) -> bool:
    return await _slash_bridge.flush_before_standalone_rewrite(
        _runtime_bridge.standalone_slash_services_from_runtime(svc),
        session_key,
        operation=operation,
    )


def default_turn_stream_dependencies() -> TurnStreamDependencies:
    return _turn_bridge.default_turn_stream_dependencies(
        renderer_factory=_runtime_bridge.renderer_factory_for_selected_backend(),
        stream_wrapper=wrap_cli_turn_stream,
        cancel_clearer=_runtime_bridge.clear_current_cancel,
        image_attachment_builder=image_prompt_and_attachments,
    )


async def handle_gateway_slash_command(
    cmd: str,
    state: ChatSessionState,
    client: _slash_bridge.GatewayClientLike,
    elevated_state: dict[str, str | None],
    *,
    tui_output: TuiOutputHandle | None = None,
    stream_response: GatewayStreamResponse | None = None,
) -> bool:
    return await _slash_bridge.handle_gateway_slash_command(
        cmd,
        state,
        client,
        elevated_state,
        tui_output=tui_output,
        stream_response=stream_response or stream_response_gateway,
    )


def sync_gateway_slash_adapter_io() -> None:
    _slash_bridge.sync_gateway_slash_adapter_io()


def sync_standalone_slash_adapter_io() -> None:
    _slash_bridge.sync_standalone_slash_adapter_io()


def print_sessions_table(rows: list[dict[str, Any]]) -> None:
    _slash_bridge.print_sessions_table(rows)


def print_models_table(rows: list[dict[str, Any]]) -> None:
    _slash_bridge.print_models_table(rows)


def save_transcript_command(cmd: str, state: ChatSessionState) -> None:
    _slash_bridge.save_transcript_command(cmd, state)


async def save_gateway_transcript_command(
    cmd: str,
    state: ChatSessionState,
    client: object,
) -> None:
    await _slash_bridge.save_gateway_transcript_command(
        cmd,
        state,
        client,
    )


def image_prompt_from_command(command: str) -> str:
    return _input_bridge.image_prompt_from_command(command)


def image_prompt_and_attachments(command: str) -> tuple[str, list[dict[str, str]]]:
    return _input_bridge.image_prompt_and_attachments(command)


def gateway_client_is_local(client: object) -> bool:
    return _input_bridge.gateway_client_is_local(client)


def parse_path_command(command: str) -> tuple[Path, str]:
    return _input_bridge.parse_path_command(command)


def path_strategy_hint(path: Path) -> str:
    return _input_bridge.path_strategy_hint(path)


def path_prompt_and_attachments(command: str) -> tuple[str, list[dict[str, Any]]]:
    return _input_bridge.path_prompt_and_attachments(command)


def file_prompt_and_attachments(
    command: str,
    *,
    upload_callable: Any | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    return _input_bridge.file_prompt_and_attachments(
        command,
        upload_callable=upload_callable,
    )


async def async_file_prompt_and_attachments(
    command: str,
    *,
    upload_callable: Any | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    return await _input_bridge.async_file_prompt_and_attachments(
        command,
        upload_callable=upload_callable,
    )


async def forget_server_approvals(
    client: object | None,
    target: str | None = None,
) -> bool:
    return await _slash_bridge.forget_server_approvals(
        client,
        target,
    )


async def handle_approvals_command(cmd: str, client: object | None = None) -> None:
    await _slash_bridge.handle_approvals_command(
        cmd,
        client,
    )


async def handle_forget_command(cmd: str, client: object | None = None) -> None:
    await _slash_bridge.handle_forget_command(
        cmd,
        client,
    )


async def handle_elevated_command(
    cmd: str,
    state: dict[str, str | None],
    client: object | None = None,
) -> None:
    await _slash_bridge.handle_elevated_command(
        cmd,
        state,
        client,
    )


def render_gateway_task_group_status(
    event_name: str,
    event: dict[str, Any],
    renderer: Any,
) -> None:
    _turn_bridge.render_gateway_task_group_status(
        event_name,
        event,
        renderer,
        deps=default_turn_stream_dependencies(),
    )


def gateway_task_group_status(
    event_name: str,
    event: dict[str, Any],
) -> tuple[str, str] | None:
    return _turn_bridge.gateway_task_group_status(event_name, event)


async def arender_gateway_task_group_status(
    event_name: str,
    event: dict[str, Any],
    renderer: Any,
) -> None:
    await _turn_bridge.arender_gateway_task_group_status(
        event_name,
        event,
        renderer,
        deps=default_turn_stream_dependencies(),
    )


async def renderer_status(renderer: Any, message: str, *, style: str = "dim") -> None:
    await _turn_bridge.renderer_status(
        renderer,
        message,
        style=style,
        deps=default_turn_stream_dependencies(),
    )


async def renderer_tool_start(
    renderer: Any,
    name: str,
    args: dict | None,
    tool_use_id: str | None,
) -> None:
    await _turn_bridge.renderer_tool_start(renderer, name, args, tool_use_id)


async def renderer_tool_finished(
    renderer: Any,
    tool_use_id: str | None,
    *,
    success: bool,
) -> None:
    await _turn_bridge.renderer_tool_finished(
        renderer,
        tool_use_id,
        success=success,
    )


async def renderer_error(renderer: Any, message: str) -> None:
    await _turn_bridge.renderer_error(renderer, message)


async def renderer_finalize(
    renderer: Any,
    usage: UsageSummary | None = None,
    *,
    cancelled: bool = False,
) -> None:
    await _turn_bridge.renderer_finalize(renderer, usage, cancelled=cancelled)


async def renderer_close(renderer: Any) -> None:
    await _turn_bridge.renderer_close(renderer)


def artifact_event_payload(event: Any) -> dict[str, Any]:
    return _turn_bridge.artifact_event_payload(event)


def artifact_status_line(artifact: dict[str, Any]) -> str:
    return _turn_bridge.artifact_status_line(artifact)


async def stream_response_gateway(
    client: Any,
    session_key: str,
    message: str,
    elevated_state: dict[str, str | None] | None = None,
    attachments: list[dict] | None = None,
    *,
    tui_output: TuiOutputHandle | None = None,
) -> TurnResult:
    return await _turn_bridge.stream_response_gateway(
        client,
        session_key,
        message,
        elevated_state,
        attachments=attachments,
        tui_output=tui_output,
        deps=default_turn_stream_dependencies(),
    )


def local_approval_resolver(**kwargs: object) -> Callable[..., Awaitable[None]]:
    return _turn_bridge.local_approval_resolver(**kwargs)


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
    pending_input_provider: PendingInputProvider | None = None,
) -> TurnResult:
    return await _turn_bridge.stream_response_turnrunner(
        turn_runner,
        session_key,
        tool_ctx,
        message,
        model=model,
        svc=svc,
        timeout=timeout,
        tui_output=tui_output,
        deps=default_turn_stream_dependencies(),
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
    pending_input_provider: PendingInputProvider | None = None,
) -> TurnResult:
    return await _turn_bridge.handle_image_command_turnrunner(
        turn_runner,
        session_key,
        tool_ctx,
        command,
        model=model,
        svc=svc,
        timeout=timeout,
        tui_output=tui_output,
        deps=default_turn_stream_dependencies(),
        pending_input_provider=pending_input_provider,
    )
