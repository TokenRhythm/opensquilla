"""TUI runtime launch bridge for chat command wiring.

This module owns concrete runtime dependency assembly and OpenTUI bridge
defaults. ``chat_cmd.py`` supplies mode-level CLI parameters; the TUI bridge
decides how frontend, slash-command, and turn-stream callbacks become gateway
or standalone runtime dependencies.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine, Mapping
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import quote, urlsplit, urlunsplit

import typer
from rich.panel import Panel
from rich.text import Text

import opensquilla.cli.tui.adapters.native_bridge as _native_bridge
import opensquilla.cli.tui.adapters.opentui_bridge as _opentui_bridge
from opensquilla.cli.chat import gateway_runtime as _gateway_runtime
from opensquilla.cli.chat.session_context import (
    GatewayRuntimeScope,
    StandaloneRuntimeScope,
)
from opensquilla.cli.chat.turn import TurnResult
from opensquilla.cli.tui import standalone_runtime as _standalone_runtime
from opensquilla.cli.tui.adapters import commands as _commands
from opensquilla.cli.tui.adapters import runtime_helpers as _runtime_helpers
from opensquilla.cli.tui.adapters import slash_bridge as _slash_bridge
from opensquilla.cli.tui.backend.contracts import TuiOutputHandle
from opensquilla.cli.ui import ACCENT, console, error_panel
from opensquilla.engine.commands import Surface

if TYPE_CHECKING:
    from opensquilla.engine.agent_injection import PendingInputProvider

PENDING_QUEUE_MAX_SIZE = 8

GatewayRuntimeDependencies = _gateway_runtime.GatewayRuntimeDependencies
GatewayClientLike = _gateway_runtime.GatewayClientLike
StandaloneRuntimeDependencies = _standalone_runtime.StandaloneRuntimeDependencies


def validate_tui_backend_selection(env: Mapping[str, str] | None = None) -> str:
    from opensquilla.cli.tui.renderers.selection import (  # noqa: PLC0415
        select_renderer_backend_from_env,
    )

    return select_renderer_backend_from_env(env).backend_id


def _runtime_bridge_for_selected_backend() -> Any:
    backend_id = validate_tui_backend_selection()
    if backend_id == "opentui":
        return _opentui_bridge
    return _native_bridge


class GatewayTerminalReplRunner(Protocol):
    async def __call__(
        self,
        *,
        surface: Surface,
        scope: GatewayRuntimeScope,
        dispatch: Callable[[str], Coroutine[Any, Any, bool]] | Callable[[str], Awaitable[bool]],
        abort_active_turn: Callable[[], Awaitable[None]] | None = None,
        steer_active_turn: Callable[[str], Awaitable[bool]] | None = None,
        queue_max_size: int | None = None,
    ) -> None: ...


async def run_concurrent_repl(
    *,
    surface: Surface,
    scope: GatewayRuntimeScope | StandaloneRuntimeScope,
    dispatch: Callable[[str], Coroutine[Any, Any, bool]] | Callable[[str], Awaitable[bool]],
    abort_active_turn: Callable[[], Awaitable[None]] | None = None,
    steer_active_turn: Callable[[str], Awaitable[bool]] | None = None,
    queue_max_size: int | None = None,
) -> None:
    kwargs: dict[str, Any] = {
        "surface": surface,
        "scope": scope,
        "dispatch": dispatch,
        "queue_max_size": (
            PENDING_QUEUE_MAX_SIZE if queue_max_size is None else queue_max_size
        ),
        "abort_active_turn": abort_active_turn,
    }
    # Additive compatibility: third-party/older bridges do not accept the
    # steering callback. Omit it when Gateway steering is not wired.
    if steer_active_turn is not None:
        kwargs["steer_active_turn"] = steer_active_turn
    await _runtime_bridge_for_selected_backend().run_concurrent_repl(
        **kwargs,
    )


def get_tui_output(
    scope: GatewayRuntimeScope | StandaloneRuntimeScope,
) -> TuiOutputHandle | None:
    output = _runtime_bridge_for_selected_backend().get_tui_output(scope)
    return output if isinstance(output, TuiOutputHandle) else None


def clear_current_cancel() -> None:
    _runtime_helpers.clear_current_cancel()


def cli_sender_id() -> str:
    return _standalone_runtime.cli_sender_id()


async def read_standalone_transcript(
    session_manager: Any,
    session_key: str,
) -> list[Any] | None:
    return await _standalone_runtime.read_standalone_transcript(
        session_manager,
        session_key,
    )


def standalone_slash_services_from_runtime(
    svc: Any,
) -> Any:
    return _standalone_runtime.standalone_slash_services_from_runtime(svc)


def renderer_factory_for_selected_backend() -> Any:
    """Pick the stream renderer that matches the active TUI backend."""
    from opensquilla.cli.tui.native.renderer import NativeStreamRenderer
    from opensquilla.cli.tui.opentui.renderer import OpenTuiStreamRenderer

    backend_id = validate_tui_backend_selection()
    return OpenTuiStreamRenderer if backend_id == "opentui" else NativeStreamRenderer


def _turn_stream_dependencies() -> Any:
    from opensquilla.cli.tui import turn_bridge as _turn_bridge

    return _turn_bridge.default_turn_stream_dependencies(
        renderer_factory=renderer_factory_for_selected_backend()
    )


async def stream_response_gateway(
    # This bridge is shared by the runtime and slash adapters, whose client
    # protocols intentionally expose different method subsets.  The concrete
    # GatewayClient satisfies both; the downstream turn bridge is structural.
    client: Any,
    session_key: str,
    message: str,
    elevated_state: dict[str, str | None] | None = None,
    attachments: list[dict] | None = None,
    *,
    tui_output: TuiOutputHandle | None = None,
) -> TurnResult:
    from opensquilla.cli.tui import turn_bridge as _turn_bridge

    return await _turn_bridge.stream_response_gateway(
        client,
        session_key,
        message,
        elevated_state,
        attachments=attachments,
        tui_output=tui_output,
        deps=_turn_stream_dependencies(),
    )


async def handle_gateway_slash_command(
    cmd: str,
    state: Any,
    client: GatewayClientLike,
    elevated_state: dict[str, str | None],
    *,
    tui_output: TuiOutputHandle | None = None,
) -> bool:
    return await _slash_bridge.handle_gateway_slash_command(
        cmd,
        state,
        client,
        elevated_state,
        tui_output=tui_output,
        stream_response=stream_response_gateway,
    )


def _gateway_input_loop_for(
    repl_runner: GatewayTerminalReplRunner,
) -> _gateway_runtime.GatewayRunInputLoop:
    async def _run_gateway_input_loop(
        *,
        scope: GatewayRuntimeScope,
        dispatch: Callable[[str], Coroutine[Any, Any, bool]],
        abort_active_turn: Callable[[], Awaitable[None]] | None = None,
        steer_active_turn: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        await repl_runner(
            surface=Surface.CLI_GATEWAY,
            scope=scope,
            dispatch=dispatch,
            abort_active_turn=abort_active_turn,
            steer_active_turn=steer_active_turn,
        )

    return _run_gateway_input_loop


def _gateway_runtime_notifier(
    output_console: Any,
    error_panel_factory: Callable[[str], Any],
) -> Callable[[_gateway_runtime.GatewayRuntimeNotice], None]:
    def _notify(notice: _gateway_runtime.GatewayRuntimeNotice) -> None:
        if notice.kind == "created":
            output_console.print(f"[dim]Connected to gateway. Session: {notice.session_key}[/dim]")
            return
        if notice.kind == "resumed":
            output_console.print(
                f"[dim]Connected to gateway. Resuming session: {notice.session_key}[/dim]"
            )
            return
        if notice.kind == "resume_model_ignored":
            output_console.print(
                "[yellow]Note: --model is honored only at session creation; "
                "ignored when resuming a session.[/yellow]"
            )
            return
        if notice.kind == "model":
            output_console.print(f"[dim]Model: {notice.model}[/dim]")
            return
        if notice.kind == "welcome":
            output_console.print(
                Panel(
                    f"[bold {ACCENT}]OpenSquilla Chat[/bold {ACCENT}]\n"
                    "[dim]Enter sends. Ctrl+C cancels the current turn or clears "
                    "input. Ctrl+D exits. /help lists commands.[/dim]",
                    title="Gateway",
                    border_style=ACCENT,
                    expand=False,
                )
            )
            return
        if notice.kind == "goodbye":
            output_console.print("[yellow]Goodbye.[/yellow]")
            return
        if notice.kind == "unknown_command":
            output_console.print("[red]Unknown command.[/red] [dim]Use /help.[/dim]")
            return
        if notice.kind == "queued_behind_external":
            output_console.print(
                "[dim]Queued behind a turn running from another client;"
                " your message sends when it finishes.[/dim]"
            )
            return
        if notice.kind == "error":
            output_console.print(error_panel_factory(notice.message or ""))

    return _notify


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
    from opensquilla.cli.tui import turn_bridge as _turn_bridge

    return await _turn_bridge.stream_response_turnrunner(
        turn_runner,
        session_key,
        tool_ctx,
        message,
        model=model,
        svc=svc,
        timeout=timeout,
        tui_output=tui_output,
        deps=_turn_stream_dependencies(),
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
    from opensquilla.cli.tui import turn_bridge as _turn_bridge

    return await _turn_bridge.handle_image_command_turnrunner(
        turn_runner,
        session_key,
        tool_ctx,
        command,
        model=model,
        svc=svc,
        timeout=timeout,
        tui_output=tui_output,
        deps=_turn_stream_dependencies(),
        pending_input_provider=pending_input_provider,
    )


async def run_gateway_chat(
    *,
    model: str | None,
    session_id: str | None,
    stream_response: _gateway_runtime.GatewayStreamResponse | None = None,
    handle_slash_command: _gateway_runtime.GatewayHandleSlashCommand | None = None,
    run_concurrent_repl: GatewayTerminalReplRunner | None = None,
    output_console: Any | None = None,
    error_panel_factory: Callable[[str], Any] | None = None,
) -> _gateway_runtime.SessionExitSummary | None:
    from opensquilla.cli.gateway_client import GatewayRPCError
    from opensquilla.cli.gateway_rpc import rpc_error_exit_code

    repl_runner = (
        globals()["run_concurrent_repl"] if run_concurrent_repl is None else run_concurrent_repl
    )
    active_console = console if output_console is None else output_console
    active_error_panel = error_panel if error_panel_factory is None else error_panel_factory
    active_stream_response = stream_response_gateway if stream_response is None else stream_response
    if handle_slash_command is None:
        if stream_response is None and output_console is None and error_panel_factory is None:
            active_handle_slash_command = handle_gateway_slash_command
        else:

            async def _handle_gateway_slash_command_with_runtime_defaults(
                cmd: str,
                state: Any,
                client: GatewayClientLike,
                elevated_state: dict[str, str | None],
                *,
                tui_output: TuiOutputHandle | None = None,
            ) -> bool:
                return await _slash_bridge.handle_gateway_slash_command(
                    cmd,
                    state,
                    client,
                    elevated_state,
                    tui_output=tui_output,
                    stream_response=cast(
                        _slash_bridge.GatewayStreamResponse,
                        active_stream_response,
                    ),
                    output_console=active_console,
                    error_panel_factory=active_error_panel,
                )

            active_handle_slash_command = _handle_gateway_slash_command_with_runtime_defaults
    else:
        active_handle_slash_command = handle_slash_command

    try:
        summary = await _gateway_runtime.run_gateway_chat(
            model=model,
            session_id=session_id,
            deps=_gateway_runtime.GatewayRuntimeDependencies(
                stream_response=active_stream_response,
                handle_slash_command=active_handle_slash_command,
                run_input_loop=_gateway_input_loop_for(repl_runner),
                get_tui_output=get_tui_output,
                is_exit_command=lambda value: _commands.is_exit_command(
                    value,
                    Surface.CLI_GATEWAY,
                ),
                notify=_gateway_runtime_notifier(active_console, active_error_panel),
            ),
        )
    except _gateway_runtime.SessionExitError as exc:
        _print_session_exit_receipt(active_console, exc.summary)
        raise typer.Exit(code=1) from None
    except GatewayRPCError as exc:
        if (
            session_id
            and (exc.code or "").upper() == "NOT_FOUND"
            and exc.method in {"sessions.bootstrap", "sessions.resolve"}
        ):
            message = (
                f"Session '{session_id}' was not found. Run `opensquilla sessions list` "
                "to find a resumable key, or omit `--session` to create a new session."
            )
        else:
            message = str(exc)
        active_console.print(active_error_panel(message))
        raise typer.Exit(code=rpc_error_exit_code(exc.code)) from None
    if summary is not None:
        _print_session_exit_receipt(active_console, summary)
    return summary


def _gateway_web_session_url(session_key: str) -> str:
    from opensquilla.cli.gateway_rpc import default_gateway_url  # noqa: PLC0415

    parsed = urlsplit(default_gateway_url())
    scheme = "https" if parsed.scheme == "wss" else "http"
    return urlunsplit(
        (
            scheme,
            parsed.netloc,
            "/control/chat",
            f"session={quote(session_key, safe='')}",
            "",
        )
    )


def _print_session_exit_receipt(
    output_console: Any,
    summary: _gateway_runtime.SessionExitSummary,
) -> None:
    """Print once, after the active surface has restored the real terminal."""

    label = summary.title or summary.session_key
    lines = [
        f"Session saved: {label}",
        f"Resume: opensquilla chat --session {summary.session_key}",
        f"Web: {_gateway_web_session_url(summary.session_key)}",
        f"Exit: {summary.reason}",
    ]
    if summary.active or summary.queued:
        lines.append(
            f"Remaining work: active={str(summary.active).lower()}, queued={summary.queued}"
        )
    output_console.print(Text("\n".join(lines)))


async def gateway_chat_runner(model: str | None, session_id: str | None) -> None:
    await run_gateway_chat(
        model=model,
        session_id=session_id,
    )


async def run_standalone_chat(
    *,
    model: str | None,
    session_id: str | None,
    stream_response: _standalone_runtime.StandaloneStreamResponse | None = None,
    image_command_handler: _standalone_runtime.StandaloneImageCommandHandler | None = None,
    run_concurrent_repl: _standalone_runtime.StandaloneRunConcurrentRepl | None = None,
    workspace: str | None = None,
    workspace_strict: bool | None = None,
    timeout: float | None = None,
    output_console: Any | None = None,
    error_panel_factory: Callable[[str], Any] | None = None,
) -> None:
    repl_runner = (
        globals()["run_concurrent_repl"] if run_concurrent_repl is None else run_concurrent_repl
    )
    active_console = console if output_console is None else output_console
    active_error_panel = error_panel if error_panel_factory is None else error_panel_factory
    active_stream_response = (
        stream_response_turnrunner if stream_response is None else stream_response
    )
    active_image_command_handler = (
        handle_image_command_turnrunner if image_command_handler is None else image_command_handler
    )

    def _sync_slash_adapter_io() -> None:
        _slash_bridge.sync_standalone_slash_adapter_io(
            output_console=active_console,
            error_panel_factory=active_error_panel,
        )

    await _standalone_runtime.run_standalone_chat(
        model=model,
        session_id=session_id,
        workspace=workspace,
        workspace_strict=workspace_strict,
        timeout=timeout,
        deps=_standalone_runtime.StandaloneRuntimeDependencies(
            stream_response=active_stream_response,
            image_command_handler=active_image_command_handler,
            run_concurrent_repl=repl_runner,
            slash_services_factory=standalone_slash_services_from_runtime,
            sync_slash_adapter_io=_sync_slash_adapter_io,
            get_tui_output=get_tui_output,
            output_console=active_console,
            error_panel_factory=active_error_panel,
        ),
    )


async def standalone_chat_runner(
    model: str | None,
    session_id: str | None,
    workspace: str | None = None,
    workspace_strict: bool | None = None,
    timeout: float | None = None,
) -> None:
    await run_standalone_chat(
        model=model,
        session_id=session_id,
        workspace=workspace,
        workspace_strict=workspace_strict,
        timeout=timeout,
    )
