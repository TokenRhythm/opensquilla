"""REPL slash-command bridge for chat command wiring.

This module owns the concrete gateway/standalone slash adapter assembly so
``chat_cmd.py`` can stay as a CLI entrypoint instead of knowing raw slash
adapter contexts and IO globals.
"""

from __future__ import annotations

from typing import Any, cast

from opensquilla.cli.chat.session_state import ChatSessionState
from opensquilla.cli.tui.adapters import slash_gateway as _gateway_slash_adapter
from opensquilla.cli.tui.adapters import slash_standalone as _standalone_slash_adapter
from opensquilla.cli.tui.adapters.slash_gateway import (
    GatewayClientLike,
    GatewaySlashContext,
    GatewayStreamResponse,
)
from opensquilla.cli.tui.backend.contracts import TuiOutputHandle
from opensquilla.cli.ui import console, error_panel

GATEWAY_SLASH_HANDLER_WORDS = _gateway_slash_adapter.GATEWAY_SLASH_HANDLER_WORDS
STANDALONE_SLASH_HANDLER_WORDS = _standalone_slash_adapter.STANDALONE_SLASH_HANDLER_WORDS


def sync_gateway_slash_adapter_io(
    *,
    output_console: Any | None = None,
    error_panel_factory: Any | None = None,
) -> None:
    _gateway_slash_adapter.console = console if output_console is None else output_console
    _gateway_slash_adapter.error_panel = (
        error_panel if error_panel_factory is None else error_panel_factory
    )


def sync_standalone_slash_adapter_io(
    *,
    output_console: Any | None = None,
    error_panel_factory: Any | None = None,
) -> None:
    _standalone_slash_adapter.console = (
        console if output_console is None else output_console
    )
    _standalone_slash_adapter.error_panel = (
        error_panel if error_panel_factory is None else error_panel_factory
    )


def resolve_compaction_provider(
    provider_selector: Any,
    model_override: str | None = None,
) -> Any | None:
    return _standalone_slash_adapter._resolve_compaction_provider(
        provider_selector,
        model_override,
    )


async def flush_before_standalone_rewrite(
    slash_services: _standalone_slash_adapter.StandaloneSlashServices,
    session_key: str,
    *,
    operation: str,
    output_console: Any | None = None,
    error_panel_factory: Any | None = None,
) -> bool:
    sync_standalone_slash_adapter_io(
        output_console=output_console,
        error_panel_factory=error_panel_factory,
    )
    return await _standalone_slash_adapter._flush_before_standalone_rewrite(
        slash_services,
        session_key,
        operation=operation,
    )


async def handle_gateway_slash_command(
    cmd: str,
    state: ChatSessionState,
    # Kept permissive: callers annotate clients with their own structural
    # protocols (chat runtime vs TUI adapter); the concrete GatewayClient
    # satisfies the adapter's full contract.
    client: Any,
    elevated_state: dict[str, str | None],
    *,
    tui_output: TuiOutputHandle | None = None,
    stream_response: GatewayStreamResponse | None = None,
    requested_model: str | None = None,
    output_console: Any | None = None,
    error_panel_factory: Any | None = None,
) -> bool:
    sync_gateway_slash_adapter_io(
        output_console=output_console,
        error_panel_factory=error_panel_factory,
    )
    return await _gateway_slash_adapter.handle_gateway_slash_command(
        cmd,
        GatewaySlashContext(
            state=state,
            client=cast(GatewayClientLike, client),
            elevated_state=elevated_state,
            tui_output=tui_output,
            stream_response=stream_response,
            requested_model=requested_model,
        ),
    )


def print_sessions_table(
    rows: list[dict[str, Any]],
    *,
    output_console: Any | None = None,
    error_panel_factory: Any | None = None,
) -> None:
    sync_gateway_slash_adapter_io(
        output_console=output_console,
        error_panel_factory=error_panel_factory,
    )
    _gateway_slash_adapter._print_sessions_table(rows)


def print_models_table(
    rows: list[dict[str, Any]],
    *,
    output_console: Any | None = None,
    error_panel_factory: Any | None = None,
) -> None:
    sync_gateway_slash_adapter_io(
        output_console=output_console,
        error_panel_factory=error_panel_factory,
    )
    _gateway_slash_adapter._print_models_table(rows)


def save_transcript_command(
    cmd: str,
    state: ChatSessionState,
    *,
    output_console: Any | None = None,
    error_panel_factory: Any | None = None,
) -> None:
    sync_standalone_slash_adapter_io(
        output_console=output_console,
        error_panel_factory=error_panel_factory,
    )
    _standalone_slash_adapter._save_state_transcript_command(cmd, state)


async def save_gateway_transcript_command(
    cmd: str,
    state: ChatSessionState,
    client: object,
    *,
    output_console: Any | None = None,
    error_panel_factory: Any | None = None,
) -> None:
    sync_gateway_slash_adapter_io(
        output_console=output_console,
        error_panel_factory=error_panel_factory,
    )
    await _gateway_slash_adapter._save_gateway_transcript_command(
        cmd,
        state,
        cast(GatewayClientLike, client),
    )


async def forget_server_approvals(
    client: object | None,
    target: str | None = None,
    *,
    output_console: Any | None = None,
    error_panel_factory: Any | None = None,
) -> bool:
    sync_gateway_slash_adapter_io(
        output_console=output_console,
        error_panel_factory=error_panel_factory,
    )
    return await _gateway_slash_adapter._forget_server_approvals(
        cast("GatewayClientLike | None", client),
        target,
    )


async def handle_approvals_command(
    cmd: str,
    client: object | None = None,
    *,
    output_console: Any | None = None,
    error_panel_factory: Any | None = None,
) -> None:
    sync_gateway_slash_adapter_io(
        output_console=output_console,
        error_panel_factory=error_panel_factory,
    )
    await _gateway_slash_adapter._handle_approvals_command(
        cmd,
        cast("GatewayClientLike | None", client),
    )


async def handle_forget_command(
    cmd: str,
    client: object | None = None,
    *,
    output_console: Any | None = None,
    error_panel_factory: Any | None = None,
) -> None:
    sync_gateway_slash_adapter_io(
        output_console=output_console,
        error_panel_factory=error_panel_factory,
    )
    await _gateway_slash_adapter._handle_forget_command(
        cmd,
        cast("GatewayClientLike | None", client),
    )


async def handle_elevated_command(
    cmd: str,
    state: dict[str, str | None],
    client: object | None = None,
    *,
    output_console: Any | None = None,
    error_panel_factory: Any | None = None,
) -> None:
    sync_gateway_slash_adapter_io(
        output_console=output_console,
        error_panel_factory=error_panel_factory,
    )
    await _gateway_slash_adapter._handle_elevated_command(
        cmd,
        state,
        cast("GatewayClientLike | None", client),
    )
