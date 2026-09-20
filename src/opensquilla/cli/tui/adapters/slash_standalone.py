"""Standalone slash-command adapter for the chat REPL backend.

This module owns TurnRunner-backed slash command dispatch. It stays independent
from raw frontend and chat application objects: callers pass typed session
state, service handles, and optional stream callbacks.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

import opensquilla.cli.tui.adapters.input_bridge as _input_bridge
from opensquilla.cli.chat.session_state import ChatSessionState
from opensquilla.cli.chat.turn import TurnResult
from opensquilla.cli.tui.adapters.commands import render_help_table, render_keys_table
from opensquilla.cli.tui.adapters.slash_common import (
    compact_success_line,
    compact_summary_stats,
    compact_token_stats,
    compact_unapplied_line,
    dispatch_theme_command,
    output_supports_host_ui,
    record_turn,
    registry_handler_words,
    resolve_transcript_target,
    save_transcript_markdown,
    transcript_messages_to_markdown,
)
from opensquilla.cli.tui.adapters.slash_common import (
    slash_parts as _slash_parts,
)
from opensquilla.cli.tui.backend.contracts import TuiOutputHandle
from opensquilla.cli.tui.opentui.context import send_model_routing_state
from opensquilla.cli.ui import ACCENT, console, error_panel
from opensquilla.engine.commands import Surface
from opensquilla.observability.network_policy import (
    provider_request_correlation_disabled,
)
from opensquilla.provider.types import (
    ProviderRequestCorrelation,
)
from opensquilla.session.compaction import (
    build_compaction_config_from_provider,
    call_compact_with_optional_config,
    effective_protected_recent_messages,
)
from opensquilla.session.compaction_lifecycle import (
    durable_receipt_allows_destructive_compaction,
    new_compaction_id,
)

if TYPE_CHECKING:
    from opensquilla.engine.agent_injection import PendingInputProvider

# Derived from the engine registry so a new slash command only has to be
# declared once; the dispatch chain below is pinned to this set by tests.
STANDALONE_SLASH_HANDLER_WORDS = registry_handler_words(Surface.CLI_STANDALONE)


class StandaloneStreamResponse(Protocol):
    async def __call__(
        self,
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
    ) -> TurnResult: ...


class StandaloneImageCommandHandler(Protocol):
    async def __call__(
        self,
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
    ) -> TurnResult: ...


class StandaloneSessionReplacer(Protocol):
    def __call__(
        self,
        *,
        session_key: str,
        tool_ctx: object,
        state: ChatSessionState,
        model: str | None,
    ) -> Awaitable[None] | None: ...


class CompactWithResult(Protocol):
    def __call__(
        self,
        session_key: str,
        context_window_tokens: int,
        compaction_config: object | None,
    ) -> Awaitable[Any]: ...


class StandaloneCreateSession(Protocol):
    def __call__(self, session_key: str, *, agent_id: str = "main") -> Awaitable[Any]: ...


class StandaloneReadTranscript(Protocol):
    def __call__(self, session_key: str) -> Awaitable[Any] | Any: ...


class StandaloneGetSession(Protocol):
    def __call__(self, session_key: str) -> Awaitable[Any] | Any: ...


class StandaloneTruncateSession(Protocol):
    def __call__(self, session_key: str, *, max_messages: int = 0) -> Awaitable[None]: ...


class StandaloneCompactSession(Protocol):
    def __call__(
        self,
        session_key: str,
        context_window_tokens: int,
        config: object | None = None,
    ) -> Awaitable[str]: ...


class StandaloneCheckpointTranscript(Protocol):
    def __call__(
        self,
        session_key: str,
        transcript: object,
        **kwargs: Any,
    ) -> Awaitable[Any]: ...


class StandaloneGetSessionRouting(Protocol):
    def __call__(self, session_key: str) -> Awaitable[dict[str, Any]] | dict[str, Any]:
        pass


class StandaloneSetSessionRouting(Protocol):
    def __call__(
        self,
        session_key: str,
        mode: str,
        *,
        expected_revision: int,
    ) -> Awaitable[dict[str, Any]] | dict[str, Any]:
        pass


@dataclass
class StandaloneSlashServices:
    create_session: StandaloneCreateSession | None = None
    get_session: StandaloneGetSession | None = None
    read_transcript: StandaloneReadTranscript | None = None
    truncate_session: StandaloneTruncateSession | None = None
    compact_session: StandaloneCompactSession | None = None
    compact_with_result: CompactWithResult | None = None
    checkpoint_transcript: StandaloneCheckpointTranscript | None = None
    get_session_routing: StandaloneGetSessionRouting | None = None
    set_session_routing: StandaloneSetSessionRouting | None = None
    config: object | None = None
    provider_selector: object | None = None


@dataclass
class StandaloneSlashContext:
    state: ChatSessionState
    session_key: str
    # The model the user explicitly requested (--model at launch or the last
    # /model choice). The runtime keeps this apart from state.model, which
    # tracks the model that last ran (display only) and may be a router pick.
    model: str | None
    tool_ctx: object
    slash_services: StandaloneSlashServices
    turn_runner: object
    build_tool_ctx: Callable[[str], object]
    replace_session: StandaloneSessionReplacer
    runtime_services: object = None
    timeout: float | None = None
    tui_output: TuiOutputHandle | None = None
    stream_response: StandaloneStreamResponse | None = None
    image_command_handler: StandaloneImageCommandHandler | None = None


async def _maybe_await(value: Awaitable[Any] | Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def stream_response_turnrunner(
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
    del (
        turn_runner,
        session_key,
        tool_context,
        message,
        model,
        services,
        timeout,
        tui_output,
        pending_input_provider,
    )
    raise RuntimeError("standalone streaming dependency was not configured")


async def handle_image_command_turnrunner(
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
    del (
        turn_runner,
        session_key,
        tool_context,
        command,
        model,
        services,
        timeout,
        tui_output,
        pending_input_provider,
    )
    raise RuntimeError("standalone image dependency was not configured")


def _resolve_compaction_provider(
    provider_selector: Any,
    model_override: str | None = None,
) -> Any | None:
    if provider_selector is None:
        return None
    selector = provider_selector
    clone = getattr(provider_selector, "clone", None)
    if callable(clone):
        try:
            selector = clone()
        except Exception:  # noqa: BLE001
            selector = provider_selector
    if model_override and selector is not provider_selector:
        override = getattr(selector, "override_model", None)
        if callable(override):
            try:
                override(model_override)
            except Exception:  # noqa: BLE001
                pass
    resolver = getattr(selector, "resolve", None)
    if not callable(resolver):
        return None
    try:
        return resolver()
    except Exception:  # noqa: BLE001
        return None


def _coerce_transcript_result(result: Any) -> list[Any] | None:
    if result is None:
        return []
    if isinstance(result, str | bytes) or not isinstance(result, Iterable):
        return None
    return list(result)


async def _read_standalone_transcript_handle(
    read_transcript: StandaloneReadTranscript | None,
    session_key: str,
) -> list[Any] | None:
    if read_transcript is None:
        return []
    try:
        result = read_transcript(session_key)
        if inspect.isawaitable(result):
            result = await result
    except KeyError:
        return []
    except Exception:  # noqa: BLE001
        return None
    return _coerce_transcript_result(result)


async def _read_standalone_transcript(
    session_manager: Any,
    session_key: str,
) -> list[Any] | None:
    """Read the durable transcript before a destructive standalone command."""
    if session_manager is None:
        return []
    for method_name in ("get_transcript", "read_transcript"):
        reader = getattr(session_manager, method_name, None)
        if not callable(reader):
            continue
        try:
            result = reader(session_key)
            if inspect.isawaitable(result):
                result = await result
        except KeyError:
            return []
        except Exception:  # noqa: BLE001
            return None
        return _coerce_transcript_result(result)
    return None


async def _checkpoint_before_standalone_rewrite(
    slash_services: StandaloneSlashServices,
    session_key: str,
    *,
    operation: str,
) -> bool:
    """Preserve the removed transcript before a standalone reset."""
    transcript = await _read_standalone_transcript_handle(
        slash_services.read_transcript, session_key,
    )
    if transcript is None:
        console.print(f"[yellow]{operation} aborted: could not inspect the transcript.[/yellow]")
        return False
    if not transcript:
        return True
    checkpoint = slash_services.checkpoint_transcript
    if checkpoint is None or slash_services.get_session is None:
        console.print(
            f"[yellow]{operation} aborted: transcript checkpoint is unavailable.[/yellow]"
        )
        return False
    try:
        session = await _maybe_await(slash_services.get_session(session_key))
        if session is None:
            raise RuntimeError("session is unavailable")
        receipt = await checkpoint(
            session_key,
            transcript,
            source="standalone_reset",
            expected_session_id=session.session_id,
            expected_session_epoch=getattr(session, "epoch", None),
        )
        if not durable_receipt_allows_destructive_compaction(receipt):
            raise RuntimeError("transcript checkpoint failed")
    except Exception:
        console.print(
            f"[yellow]{operation} aborted: transcript checkpoint could not be saved.[/yellow]"
        )
        return False
    return True


async def _standalone_maintenance_correlation(
    slash_services: StandaloneSlashServices,
    session_key: str,
    *,
    call_kind: str,
    turn_id: str | None = None,
) -> ProviderRequestCorrelation | None:
    get_session = slash_services.get_session
    if get_session is None or provider_request_correlation_disabled(
        config=slash_services.config,
    ):
        return None
    try:
        session = get_session(session_key)
        if inspect.isawaitable(session):
            session = await session
    except Exception:  # noqa: BLE001 - observability must not block maintenance
        return None
    session_id = getattr(session, "session_id", None)
    if not isinstance(session_id, str) or not session_id:
        return None
    return ProviderRequestCorrelation(
        session_id=session_id,
        turn_id=turn_id or f"maintenance_{uuid4().hex}",
        execution_id=uuid4().hex,
        call_kind=call_kind,
    )


def _save_state_transcript_command(cmd: str, state: ChatSessionState) -> None:
    """Export only the in-memory turns (legacy path with no durable handle)."""
    target = resolve_transcript_target(cmd, state.session_key)
    save_transcript_markdown(
        target,
        state.transcript.to_markdown(),
        output_console=console,
        error_panel_factory=error_panel,
    )


async def _save_transcript_command(
    cmd: str,
    state: ChatSessionState,
    read_transcript: StandaloneReadTranscript | None = None,
) -> None:
    """Export the durable transcript, falling back to the in-memory turns.

    ``state.transcript`` only records turns dispatched by this process, so a
    resumed session would export an empty file without the durable read.
    """
    target = resolve_transcript_target(cmd, state.session_key)
    markdown = ""
    if read_transcript is not None:
        durable = await _read_standalone_transcript_handle(read_transcript, state.session_key)
        if durable:
            markdown = transcript_messages_to_markdown(durable)
    if not markdown.strip():
        markdown = state.transcript.to_markdown()
    save_transcript_markdown(
        target,
        markdown,
        output_console=console,
        error_panel_factory=error_panel,
    )


def _image_prompt_from_command(command: str) -> str:
    return _input_bridge.image_prompt_from_command(command)


def _path_prompt_and_attachments(command: str) -> tuple[str, list[dict[str, Any]]]:
    return _input_bridge.path_prompt_and_attachments(command)


async def _replace_with_new_session(
    context: StandaloneSlashContext,
    *,
    title: str | None = None,
) -> str:
    session_key = f"agent:main:standalone:{uuid4().hex[:8]}"
    create_session = context.slash_services.create_session
    if create_session is None:
        raise RuntimeError("standalone chat requires session manager")
    await create_session(session_key, agent_id="main")
    if context.slash_services.get_session_routing is not None:
        # Materialize the global default at session creation time.  Without
        # this read a global change between /new and the first message would
        # incorrectly change the new session's initial routing mode.
        await _maybe_await(context.slash_services.get_session_routing(session_key))
    state = ChatSessionState(session_key=session_key, model=context.model)
    tool_ctx = context.build_tool_ctx(session_key)

    context.session_key = session_key
    context.tool_ctx = tool_ctx
    context.state = state
    await _maybe_await(
        context.replace_session(
            session_key=session_key,
            tool_ctx=tool_ctx,
            state=state,
            model=context.model,
        )
    )
    label = f" ({title})" if title else ""
    console.print(f"[green]Started new session{label}:[/green] {session_key}")
    return session_key


async def _compact_standalone_context(context: StandaloneSlashContext) -> None:
    slash_services = context.slash_services
    compact_session = slash_services.compact_session
    compact_with_result = slash_services.compact_with_result
    if compact_session is None and compact_with_result is None:
        console.print("[yellow]No session manager available.[/yellow]")
        return

    compaction_id = new_compaction_id()
    compaction_correlation = await _standalone_maintenance_correlation(
        slash_services,
        context.session_key,
        call_kind="auxiliary.compaction",
        turn_id=compaction_id,
    )
    console.print(f"[{ACCENT}]compacting context...[/]")
    config = slash_services.config
    session = None
    if slash_services.get_session is not None:
        try:
            session = await _maybe_await(
                slash_services.get_session(context.session_key)
            )
        except Exception:  # noqa: BLE001 - target fallback remains isolated
            session = None
    if session is None:
        session = SimpleNamespace(
            session_key=context.session_key,
            model=context.model,
            model_override=None,
            model_provider=None,
            provider_override=None,
        )
    from opensquilla.gateway.compaction_target import (
        build_gateway_compaction_budget,
        resolve_gateway_compaction_target,
        resolve_gateway_consumer_budget,
    )

    gateway_context = SimpleNamespace(
        config=config,
        provider_selector=slash_services.provider_selector,
    )
    consumer_budget = resolve_gateway_consumer_budget(
        gateway_context,
        session,
    )
    target = resolve_gateway_compaction_target(
        gateway_context,
        session,
    )
    compaction_provider = target.provider
    if compaction_provider is None and not target.blocked_reason:
        compaction_provider = _resolve_compaction_provider(
            slash_services.provider_selector,
            context.model,
        )
    compaction_config = build_compaction_config_from_provider(
        compaction_provider,
        model_override=target.model or context.model,
        compaction_config=getattr(config, "compaction", None),
        compaction_plan=target.plan,
    )
    prepare_envelope = getattr(context.turn_runner, "prepare_manual_compaction_envelope", None)
    consumer_agent = None
    if callable(prepare_envelope) and consumer_budget.provider is not None:
        from opensquilla.tools.types import ToolContext

        consumer_agent = prepare_envelope(
            session,
            provider=consumer_budget.provider,
            context_window_tokens=(
                consumer_budget.physical_context_window_tokens
                or consumer_budget.context_window_tokens
            ),
            max_output_tokens=consumer_budget.max_output_tokens,
            context_window_known=consumer_budget.context_window_known,
            provider_request_max_chars=consumer_budget.provider_request_max_chars,
            workspace_dir=getattr(context.tool_ctx, "workspace_dir", None),
            caller_tool_context=(
                context.tool_ctx if isinstance(context.tool_ctx, ToolContext) else None
            ),
        )
    resolved_budget = build_gateway_compaction_budget(
        consumer_budget,
        consumer_agent=consumer_agent,
        trigger_ratio=float(getattr(config, "preflight_compact_ratio", 0.85)),
        retained_tail_messages=effective_protected_recent_messages(compaction_config),
        summary_output_tokens=(target.plan.primary.max_output_tokens if target.plan else 1024),
    )
    context_window = resolved_budget.history_capacity_tokens
    consumer_admission = resolved_budget.consumer_admission
    consumer_admission_fingerprint = resolved_budget.consumer_admission_fingerprint
    compaction_config.budget = resolved_budget
    unapplied_reason = None
    try:
        if compact_with_result is not None:
            compact_kwargs: dict[str, Any] = {}
            try:
                parameters = tuple(
                    inspect.signature(compact_with_result).parameters.values()
                )
            except (TypeError, ValueError):
                parameters = ()
            if compaction_correlation is not None:
                if any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    or parameter.name == "provider_request_correlation"
                    for parameter in parameters
                ):
                    compact_kwargs["provider_request_correlation"] = (
                        compaction_correlation
                    )
            if any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                or parameter.name == "compaction_id"
                for parameter in parameters
            ):
                compact_kwargs["compaction_id"] = compaction_id
            if any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                or parameter.name == "trigger_reason"
                for parameter in parameters
            ):
                compact_kwargs["trigger_reason"] = "manual"
            if any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                or parameter.name == "context_window_chars"
                for parameter in parameters
            ):
                compact_kwargs["context_window_chars"] = (
                    resolved_budget.history_capacity_chars
                )
            if any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                or parameter.name == "consumer_admission"
                for parameter in parameters
            ):
                compact_kwargs["consumer_admission"] = consumer_admission
            if any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                or parameter.name == "consumer_admission_fingerprint"
                for parameter in parameters
            ):
                compact_kwargs["consumer_admission_fingerprint"] = (
                    consumer_admission_fingerprint
                )
            result = await compact_with_result(
                context.session_key,
                context_window,
                compaction_config,
                **compact_kwargs,
            )
            summary = getattr(result, "summary", "") or ""
            unapplied_reason = getattr(result, "skip_reason", None)
            token_stats = compact_token_stats(
                getattr(result, "tokens_before", 0),
                getattr(result, "tokens_after", 0),
                getattr(result, "remaining_budget_tokens", 0),
                getattr(result, "summary_source", "unknown"),
            )
        else:
            if compact_session is None:
                console.print("[yellow]No session manager available.[/yellow]")
                return
            summary = await call_compact_with_optional_config(
                compact_session,
                context.session_key,
                context_window,
                compaction_config,
                provider_request_correlation=compaction_correlation,
            )
            token_stats = compact_summary_stats(len(summary))
    except Exception as exc:  # noqa: BLE001 - keep chat command recoverable.
        console.print(f"[red]compact failed: {exc}[/red]")
        return

    if summary:
        console.print(compact_success_line(token_stats))
    else:
        console.print(compact_unapplied_line(
            reason=unapplied_reason, compaction_id=compaction_id,
        ))


async def handle_standalone_slash_command(
    cmd: str,
    context: StandaloneSlashContext,
) -> bool:
    """Handle standalone-mode slash commands.

    Always returns ``True``: every slash input is either executed or answered
    with the "Unknown command" notice here, so the caller's dispatch loop keeps
    running. Exit commands are owned by the runtime loop, which intercepts them
    before slash dispatch. (The gateway twin instead returns ``False`` for
    unknown commands and lets its runtime render the notice.)
    """

    state = context.state
    stream = context.stream_response or stream_response_turnrunner
    image_handler = context.image_command_handler or handle_image_command_turnrunner

    if cmd == "/help":
        console.print(render_help_table(Surface.CLI_STANDALONE))
        return True

    if cmd in {"/keys", "/shortcuts"}:
        console.print(render_keys_table(opentui=output_supports_host_ui(context.tui_output)))
        return True

    if _slash_parts(cmd, "/theme"):
        await dispatch_theme_command(cmd, context.tui_output)
        return True

    if parts := _slash_parts(cmd, "/routing"):
        argument = parts[1].strip().lower() if len(parts) > 1 else ""
        if argument not in {"", "direct", "router", "ensemble"}:
            console.print("[red]Usage: /routing [direct|router|ensemble][/red]")
            return True
        get_routing = context.slash_services.get_session_routing
        set_routing = context.slash_services.set_session_routing
        if get_routing is None or set_routing is None:
            console.print("[yellow]Session routing service is unavailable.[/yellow]")
            return True
        try:
            snapshot = await _maybe_await(get_routing(context.session_key))
        except Exception as exc:  # noqa: BLE001 - keep the TUI recoverable.
            console.print(f"[red]Could not read session routing.[/red] [dim]{exc}[/dim]")
            return True

        if not argument:
            send = getattr(context.tui_output, "send_message", None)
            if bool(
                getattr(context.tui_output, "supports_send_message", False)
            ) and callable(send):
                await send(
                    "model.routing.picker",
                    {
                        "current": snapshot.get("mode", "direct"),
                        "options": ["direct", "router", "ensemble"],
                        "command": "/routing",
                        "title": "session model routing",
                    },
                )
                return True
            console.print(
                "[dim]session routing[/dim] "
                f"[bold]{snapshot.get('mode', 'direct')}[/bold]"
            )
            return True

        try:
            snapshot = await _maybe_await(
                set_routing(
                    context.session_key,
                    argument,
                    expected_revision=int(snapshot.get("revision") or 0),
                )
            )
        except Exception as exc:  # noqa: BLE001 - keep the TUI recoverable.
            console.print(f"[red]Session routing change failed.[/red] [dim]{exc}[/dim]")
            return True

        mode = str(snapshot.get("mode") or argument)
        await send_model_routing_state(
            context.tui_output,
            {
                **snapshot,
                "mode": mode,
                "router_enabled": mode == "router",
                "ensemble_enabled": mode == "ensemble",
                "applies_to": snapshot.get("appliesTo", "next_accepted_turn"),
            },
        )
        console.print(
            f"[green]session routing:[/green] {mode} "
            "[dim](applies to the next accepted turn)[/dim]"
        )
        return True

    if (
        _slash_parts(cmd, "/strategy")
        or _slash_parts(cmd, "/router")
        or _slash_parts(cmd, "/ensemble")
    ):
        console.print(
            "[yellow]Model strategy controls require Gateway mode; "
            "restart without --standalone.[/yellow]"
        )
        return True

    if parts := _slash_parts(cmd, "/new"):
        title = parts[1].strip() if len(parts) > 1 else None
        await _replace_with_new_session(context, title=title)
        return True

    if cmd in {"/status", "/session"}:
        console.print(
            f"[{ACCENT}]session[/] [dim]{state.session_key}[/dim]\n"
            f"[{ACCENT}]model[/] [dim]{state.model or 'default'}[/dim]"
        )
        return True

    if cmd == "/models":
        console.print("[yellow]/models requires gateway mode.[/yellow]")
        return True

    if _slash_parts(cmd, "/meta"):
        console.print("[yellow]/meta requires gateway mode.[/yellow]")
        return True

    if parts := _slash_parts(cmd, "/model"):
        if len(parts) == 1:
            console.print(f"[dim]model pin[/dim] [bold]{context.model or 'auto'}[/bold]")
        else:
            new_model = parts[1].strip()
            normalized = new_model.lower()
            if normalized == "status":
                console.print(
                    f"[dim]model pin[/dim] [bold]{context.model or 'auto'}[/bold]"
                )
            elif normalized in {"auto", "default"}:
                context.model = None
                state.model = None
                console.print("[green]model pin:[/green] auto")
            else:
                context.model = new_model
                state.model = new_model
                console.print(f"[green]model pin:[/green] {new_model}")
        return True

    if cmd == "/cost":
        console.print(state.usage.render())
        return True

    if cmd in {"/clear", "/reset"}:
        truncate_session = context.slash_services.truncate_session
        if truncate_session is not None:
            safe_to_reset = await _checkpoint_before_standalone_rewrite(
                context.slash_services, context.session_key, operation="Reset",
            )
            if not safe_to_reset:
                return True
            await truncate_session(context.session_key, max_messages=0)
        state.transcript.clear()
        state.usage.reset()
        console.print(f"[{ACCENT}]cleared[/] [dim]{state.session_key}[/dim]")
        return True

    if cmd in {"/compact", "/cmp"}:
        await _compact_standalone_context(context)
        return True

    if _slash_parts(cmd, "/save"):
        await _save_transcript_command(cmd, state, context.slash_services.read_transcript)
        return True

    if parts := _slash_parts(cmd, "/image"):
        if len(parts) == 1 or not parts[1].strip():
            console.print("[red]Usage: /image <path> [prompt][/red]")
            return True
        result = await image_handler(
            context.turn_runner,
            context.session_key,
            context.tool_ctx,
            cmd,
            model=context.model,
            services=context.runtime_services,
            timeout=context.timeout,
            tui_output=context.tui_output,
        )
        record_turn(state, _image_prompt_from_command(cmd), result)
        return True

    if parts := _slash_parts(cmd, "/path"):
        if len(parts) == 1 or not parts[1].strip():
            console.print("[red]Usage: /path <path> [prompt][/red]")
            return True
        try:
            prompt, attachments = _path_prompt_and_attachments(cmd)
        except ValueError as exc:
            console.print(error_panel(str(exc)))
            return True
        if attachments:
            console.print(error_panel("/path must not create attachments."))
            return True
        result = await stream(
            context.turn_runner,
            context.session_key,
            context.tool_ctx,
            prompt,
            model=context.model,
            services=context.runtime_services,
            timeout=context.timeout,
            tui_output=context.tui_output,
        )
        record_turn(state, prompt, result)
        return True

    console.print("[red]Unknown command.[/red] [dim]Use /help.[/dim]")
    return True
