"""Standalone chat runtime for the TurnRunner-backed TUI path.

This module owns standalone session setup and input dispatch. It deliberately
depends on typed callbacks instead of `chat_cmd.py` or raw frontend
objects, so the CLI entrypoint can stay as wiring while future TUI frontends
reuse the same backend loop.
"""

from __future__ import annotations

import getpass
import os
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import uuid4

from opensquilla.cli.chat.session_context import (
    StandaloneRuntimeScope,
    StandaloneSessionContext,
)
from opensquilla.cli.chat.session_state import ChatSessionState
from opensquilla.cli.chat.turn import TurnResult
from opensquilla.cli.tui.adapters import slash_standalone as _standalone_slash_adapter
from opensquilla.cli.tui.adapters.commands import is_exit_command
from opensquilla.cli.tui.backend.contracts import TuiOutputHandle
from opensquilla.cli.ui import console, error_panel
from opensquilla.engine.commands import Surface
from opensquilla.permissions import configured_default_elevated

if TYPE_CHECKING:
    from opensquilla.engine.agent_injection import PendingInputProvider


class StandaloneRunConcurrentRepl(Protocol):
    async def __call__(
        self,
        *,
        surface: Surface,
        scope: StandaloneRuntimeScope,
        dispatch: Callable[[str], Coroutine[Any, Any, bool]],
    ) -> None: ...


class StandaloneStreamResponse(Protocol):
    async def __call__(
        self,
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
    ) -> TurnResult: ...


class StandaloneImageCommandHandler(Protocol):
    async def __call__(
        self,
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
    ) -> TurnResult: ...


@dataclass(frozen=True)
class StandaloneRuntimeDependencies:
    stream_response: StandaloneStreamResponse
    image_command_handler: StandaloneImageCommandHandler
    run_concurrent_repl: StandaloneRunConcurrentRepl
    slash_services_factory: Callable[[object], _standalone_slash_adapter.StandaloneSlashServices]
    sync_slash_adapter_io: Callable[[], None]
    get_tui_output: Callable[[StandaloneRuntimeScope], TuiOutputHandle | None]
    output_console: Any = console
    error_panel_factory: Callable[[str], Any] = error_panel


def cli_sender_id() -> str:
    raw = os.environ.get("USER")
    if raw and raw.strip():
        return raw.strip()
    try:
        return getpass.getuser() or "cli-user"
    except Exception:
        return "cli-user"


async def read_standalone_transcript(
    session_manager: Any,
    session_key: str,
) -> list[Any] | None:
    return await _standalone_slash_adapter._read_standalone_transcript(
        session_manager,
        session_key,
    )


async def flush_before_standalone_rewrite(
    svc: Any,
    session_key: str,
    *,
    operation: str,
) -> bool:
    return await _standalone_slash_adapter._flush_before_standalone_rewrite(
        standalone_slash_services_from_runtime(svc),
        session_key,
        operation=operation,
    )


def standalone_slash_services_from_runtime(
    svc: Any,
) -> _standalone_slash_adapter.StandaloneSlashServices:
    session_manager = getattr(svc, "session_manager", None)
    flush_service = getattr(svc, "flush_service", None)

    create_session = (
        getattr(session_manager, "get_or_create", None)
        if session_manager is not None
        else None
    )
    get_session = (
        getattr(session_manager, "get_session", None)
        if session_manager is not None
        else None
    )
    truncate_session = (
        getattr(session_manager, "truncate", None)
        if session_manager is not None
        else None
    )
    compact_session = (
        getattr(session_manager, "compact", None)
        if session_manager is not None
        else None
    )
    compact_with_result = (
        getattr(session_manager, "compact_with_result", None)
        if session_manager is not None
        else None
    )
    flush_transcript = (
        getattr(flush_service, "execute", None)
        if flush_service is not None
        else None
    )
    create_session_callable = (
        cast(_standalone_slash_adapter.StandaloneCreateSession, create_session)
        if callable(create_session)
        else None
    )
    get_session_callable = (
        cast(_standalone_slash_adapter.StandaloneGetSession, get_session)
        if callable(get_session)
        else None
    )
    truncate_session_callable = (
        cast(_standalone_slash_adapter.StandaloneTruncateSession, truncate_session)
        if callable(truncate_session)
        else None
    )
    compact_session_callable = (
        cast(_standalone_slash_adapter.StandaloneCompactSession, compact_session)
        if callable(compact_session)
        else None
    )
    compact_with_result_callable = (
        cast(_standalone_slash_adapter.CompactWithResult, compact_with_result)
        if callable(compact_with_result)
        else None
    )
    flush_transcript_callable = (
        cast(_standalone_slash_adapter.StandaloneFlushTranscript, flush_transcript)
        if callable(flush_transcript)
        else None
    )

    async def _read_transcript(session_key: str) -> list[Any] | None:
        return await read_standalone_transcript(
            session_manager,
            session_key,
        )

    return _standalone_slash_adapter.StandaloneSlashServices(
        create_session=create_session_callable,
        get_session=get_session_callable,
        read_transcript=_read_transcript if session_manager is not None else None,
        truncate_session=truncate_session_callable,
        compact_session=compact_session_callable,
        compact_with_result=compact_with_result_callable,
        flush_transcript=flush_transcript_callable,
        config=getattr(svc, "config", None),
        provider_selector=getattr(svc, "provider_selector", None),
    )


async def run_standalone_chat(
    *,
    model: str | None,
    session_id: str | None,
    deps: StandaloneRuntimeDependencies,
    workspace: str | None = None,
    workspace_strict: bool | None = None,
    timeout: float | None = None,
) -> None:
    """Run standalone chat without owning a concrete terminal application."""
    from opensquilla.cli.agent_cmd import _resolve_workspace_strict
    from opensquilla.gateway import build_services, build_turn_runner_from_services
    from opensquilla.gateway.routing import build_cli_route_envelope, tool_context_from_envelope

    svc = await build_services()
    session_manager = svc.session_manager
    if session_manager is None:
        raise RuntimeError("standalone chat requires session manager")
    session_key = session_id or f"agent:main:standalone:{uuid4().hex[:8]}"
    await session_manager.get_or_create(session_key, agent_id="main")
    active_workspace = workspace or getattr(svc.config, "workspace_dir", None)
    effective_workspace_strict = _resolve_workspace_strict(
        cli_value=workspace_strict,
        config_value=getattr(svc.config, "workspace_strict", None),
        entrypoint_default=bool(active_workspace),
    )

    def _build_tool_ctx(active_session_key: str) -> object:
        route_envelope = build_cli_route_envelope(
            session_key=active_session_key,
            agent_id="main",
            channel_id="cli:chat",
            sender_id=cli_sender_id(),
            source_name="chat",
        )
        return tool_context_from_envelope(
            route_envelope,
            is_owner=True,
            workspace_dir=active_workspace,
            workspace_strict=effective_workspace_strict,
            default_elevated=configured_default_elevated(svc.config),
        )

    tool_ctx = _build_tool_ctx(session_key)
    state = ChatSessionState(session_key=session_key, model=model)
    turn_runner = build_turn_runner_from_services(svc)
    session_context = StandaloneSessionContext.create(state=state, tool_ctx=tool_ctx)
    # The explicit user request (--model here, /model later) is tracked apart
    # from state.model: after a turn state.model records the model that actually
    # ran (display only), and feeding that back into dispatch as an explicit
    # override would pin the session to the router's first pick from turn 2 on.
    requested_model = model

    def _report_dispatch_failure(exc: Exception) -> None:
        detail = str(exc).strip()
        message = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
        deps.output_console.print(deps.error_panel_factory(message))

    async def _dispatch_input(user_input: str) -> bool:
        nonlocal requested_model
        if user_input is None or is_exit_command(user_input, Surface.CLI_STANDALONE):
            deps.output_console.print("[yellow]Goodbye.[/yellow]")
            return False

        stripped = user_input.strip()
        if not stripped:
            return True

        if stripped.startswith("/"):
            deps.sync_slash_adapter_io()

            def _replace_session(
                *,
                session_key: str,
                tool_ctx: object,
                state: ChatSessionState,
                model: str | None,
            ) -> None:
                session_context.replace_session(
                    session_key=session_key,
                    tool_ctx=tool_ctx,
                    state=state,
                    model=model,
                )

            async def _stream_response(
                turn_runner: object,
                session_key: str,
                tool_context: object,
                message: str,
                *,
                model: str | None = None,
                services: object = None,
                timeout: float | None = None,
                tui_output: TuiOutputHandle | None = None,
                pending_input_provider: PendingInputProvider | None = None,
            ) -> TurnResult:
                return await deps.stream_response(
                    turn_runner,
                    session_key,
                    tool_context,
                    message,
                    model=model,
                    svc=services,
                    timeout=timeout,
                    tui_output=tui_output,
                    pending_input_provider=pending_input_provider,
                )

            async def _image_command_handler(
                turn_runner: object,
                session_key: str,
                tool_context: object,
                command: str,
                *,
                model: str | None = None,
                services: object = None,
                timeout: float | None = None,
                tui_output: TuiOutputHandle | None = None,
                pending_input_provider: PendingInputProvider | None = None,
            ) -> TurnResult:
                return await deps.image_command_handler(
                    turn_runner,
                    session_key,
                    tool_context,
                    command,
                    model=model,
                    svc=services,
                    timeout=timeout,
                    tui_output=tui_output,
                    pending_input_provider=pending_input_provider,
                )

            slash_context = _standalone_slash_adapter.StandaloneSlashContext(
                state=session_context.state,
                session_key=session_context.session_key,
                model=requested_model,
                tool_ctx=session_context.tool_ctx,
                slash_services=deps.slash_services_factory(svc),
                runtime_services=svc,
                turn_runner=turn_runner,
                build_tool_ctx=_build_tool_ctx,
                replace_session=_replace_session,
                timeout=timeout,
                tui_output=deps.get_tui_output(session_context.scope),
                stream_response=_stream_response,
                image_command_handler=_image_command_handler,
            )
            try:
                handled = await _standalone_slash_adapter.handle_standalone_slash_command(
                    stripped,
                    slash_context,
                )
            except Exception as exc:
                _report_dispatch_failure(exc)
                handled = True
            # Only slash handlers (/model) may change the requested model; the
            # session/state bookkeeping keeps whatever the handler left behind,
            # including partial /new progress before a failure.
            requested_model = slash_context.model
            session_context.replace_session(
                session_key=slash_context.session_key,
                tool_ctx=slash_context.tool_ctx,
                state=slash_context.state,
                model=slash_context.state.model,
            )
            return handled

        pending_input_provider = cast(
            "PendingInputProvider | None",
            session_context.scope.get("pending_input_provider"),
        )
        try:
            result = await deps.stream_response(
                turn_runner,
                session_context.session_key,
                session_context.tool_ctx,
                user_input,
                model=requested_model,
                svc=svc,
                timeout=timeout,
                tui_output=deps.get_tui_output(session_context.scope),
                pending_input_provider=pending_input_provider,
            )
        except Exception as exc:
            _report_dispatch_failure(exc)
            return True
        session_context.state.model = result.model_after or session_context.model
        session_context.state.transcript.add("user", user_input)
        session_context.state.transcript.add("assistant", result.text)
        session_context.state.usage.apply(result.usage)
        session_context.sync_from_state()
        return True

    try:
        await deps.run_concurrent_repl(
            surface=Surface.CLI_STANDALONE,
            scope=session_context.scope,
            dispatch=_dispatch_input,
        )
    finally:
        await svc.close()
