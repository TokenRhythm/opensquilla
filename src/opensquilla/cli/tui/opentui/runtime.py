"""Chat runtime adapter for the OpenTUI footer surface."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import asdict, dataclass
from typing import Any

from opensquilla.cli.tui.adapters.runtime_helpers import (
    ChatAbortTurn,
    ChatRuntimeContext,
    ChatRuntimeScope,
    classify_chat_input,
    clear_current_cancel,
    default_tui_plugin_manager,
    surface_task_name,
)
from opensquilla.cli.tui.backend.contracts import (
    TuiOutputHandle,
    TuiRuntimeConfig,
    TuiRuntimeHooks,
    TuiSurface,
)
from opensquilla.cli.tui.backend.output_binding import TuiOutputBinding
from opensquilla.cli.tui.backend.runtime import run_tui_runtime
from opensquilla.cli.tui.backend.state import TuiRuntimeState
from opensquilla.cli.tui.opentui.context import context_update_from_bootstrap
from opensquilla.cli.tui.opentui.messages import (
    ContextUpdate,
    HistoryReplace,
    ModelText,
    NoticeWrite,
    PromptEcho,
)
from opensquilla.cli.tui.opentui.notice_capture import capture_stdout_as_notices, real_stderr
from opensquilla.cli.tui.opentui.surface import open_opentui_surface
from opensquilla.engine.commands import Surface

# The one notice the backend runtime emits when the input surface dies (see
# cli/tui/backend/runtime.py) — it carries the only copy of a sidecar crash
# reason, so it must survive host teardown.
_SURFACE_ERROR_MARKER = "Input surface error"

# Strong references to in-flight notice tasks: the event loop only holds weak
# references to scheduled tasks, so a notice could otherwise be garbage
# collected mid-send and silently vanish.
_notice_tasks: set[asyncio.Task[None]] = set()


@dataclass
class OpenTuiChatRuntimeContext(ChatRuntimeContext):
    """Typed OpenTUI-chat adapter state with a legacy scope mirror."""

    @property
    def workspace_dir(self) -> str | None:
        value = self.scope.get("workspace_dir")
        if isinstance(value, str) and value:
            return value
        tool_ctx = self.scope.get("tool_ctx")
        ctx_workspace = getattr(tool_ctx, "workspace_dir", None)
        if isinstance(ctx_workspace, str) and ctx_workspace:
            return ctx_workspace
        return None

    @property
    def history_replace(self) -> HistoryReplace | None:
        value = self.scope.get("history_replace")
        return value if isinstance(value, HistoryReplace) else None

    @property
    def workspace_label(self) -> str | None:
        value = self.scope.get("workspace_label")
        return value if isinstance(value, str) and value else None

    @property
    def context_update(self) -> ContextUpdate:
        snapshot = self.scope.get("bootstrap")
        bootstrap = snapshot if isinstance(snapshot, dict) else None
        state = self.scope.get("state")
        permission = getattr(state, "elevated", None)
        return context_update_from_bootstrap(
            bootstrap,
            surface=self.surface,
            model=self.model,
            session_id=self.session_id,
            workspace_label=self.workspace_label or self.workspace_dir,
            permission=permission,
        )


def get_tui_output(scope: MutableMapping[str, Any]) -> TuiOutputHandle | None:
    """Return the active typed TUI output handle from an OpenTUI runtime scope."""
    return TuiOutputBinding(scope).get()


def opentui_notice(scope: MutableMapping[str, Any], payload: str) -> None:
    """Render a runtime notice (Cancelled/Goodbye/Queue full/...) as a styled host
    notice.

    Runtime notices are Rich markup. Printing them through the shared console lets
    the active stdout capture forward them to the host as severity-colored notice
    lines — the same path slash-command notices take — instead of dumping raw
    ``[yellow]...[/yellow]`` markup into the scrollback. ``scope`` is retained for
    call-site compatibility; delivery is handled by the installed capture.
    """
    del scope  # delivery is via the active stdout capture, not the bound scope
    from opensquilla.cli.ui import console  # noqa: PLC0415 - keep import light

    with contextlib.suppress(Exception):
        console.print(payload)


def forward_console_notice(
    scope: MutableMapping[str, Any],
    line: str,
    *,
    pending_tasks: set[asyncio.Task[None]] | None = None,
) -> None:
    """Ship one captured console line to the host as a styled notice.

    Mirrors :func:`opentui_notice`'s loop-scheduling so it is safe to call from
    the synchronous ``console.print`` path inside a running turn. When the host
    bridge is gone (teardown, sidecar crash) the line falls back to the real
    terminal stderr so shutdown and crash diagnostics are never silently
    dropped. ``pending_tasks`` collects the scheduled sends so the runtime can
    drain them before the event loop goes away.
    """
    output = get_tui_output(scope)
    send = getattr(output, "send_message", None) if output is not None else None
    if send is None:
        _write_to_real_stderr(line)
        return

    async def _write() -> None:
        try:
            await send("notice.write", asdict(NoticeWrite(text=line)))
        except Exception:
            _write_to_real_stderr(line)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _write_to_real_stderr(line)
        return
    task = loop.create_task(_write())
    _notice_tasks.add(task)
    task.add_done_callback(_notice_tasks.discard)
    if pending_tasks is not None:
        # Prune the caller's set as sends complete too, or a long session pins
        # one dead task per captured console line until teardown; the exit-path
        # gather only needs whatever is genuinely still in flight.
        pending_tasks.add(task)
        task.add_done_callback(pending_tasks.discard)


def _write_to_real_stderr(line: str) -> None:
    with contextlib.suppress(Exception):
        stream = real_stderr()
        stream.write(line + "\n")
        stream.flush()


def _reprint_surface_error(payload: str) -> None:
    # By the time this runs the capture has been torn down and sys.stderr is
    # the real terminal again; render the Rich markup to plain text so the
    # crash reason survives even when the styled notice raced a dying host.
    from rich.text import Text  # noqa: PLC0415 - keep import light

    with contextlib.suppress(Exception):
        sys.stderr.write(Text.from_markup(payload).plain + "\n")
        sys.stderr.flush()


async def echo_opentui_user_input(tui_surface: TuiSurface, text: str) -> None:
    """Echo accepted user input as a structured prompt block."""
    if not text.strip():
        return
    send = getattr(tui_surface, "send_message", None)
    if send is not None:
        from opensquilla.cli.tui.backend.input_identity import (
            current_tui_client_message_id,
        )

        await send(
            "prompt.echo",
            asdict(
                PromptEcho(
                    text=text,
                    client_message_id=current_tui_client_message_id(),
                )
            ),
        )


async def echo_opentui_queued_turn_start(tui_surface: TuiSurface) -> None:
    """Render a queue marker as a model.text line."""
    send = getattr(tui_surface, "send_message", None)
    if send is not None:
        await send("model.text", asdict(ModelText(text="running queued input")))


async def run_opentui_chat_runtime(
    *,
    surface: Surface,
    scope: ChatRuntimeScope,
    dispatch: Callable[[str], Awaitable[bool]],
    queue_max_size: int,
    abort_active_turn: ChatAbortTurn | None = None,
    steer_active_turn: Callable[[str], Awaitable[bool]] | None = None,
) -> None:
    """Compose the OpenTUI footer adapter with the TUI backend runtime."""
    context = OpenTuiChatRuntimeContext(
        surface=surface,
        scope=scope,
        plugin_manager=default_tui_plugin_manager(),
        abort_active_turn=abort_active_turn,
    )

    def _surface_factory():
        kwargs: dict[str, Any] = {
            "surface": surface,
            "model": context.model,
            "session_id": context.session_id,
            "context_update": context.context_update,
        }
        if context.workspace_dir is not None:
            kwargs["workspace_dir"] = context.workspace_dir
        if context.workspace_label is not None:
            kwargs["workspace_label"] = context.workspace_label
        if context.history_replace is not None:
            kwargs["history_replace"] = context.history_replace
        return open_opentui_surface(**kwargs)

    notice_tasks: set[asyncio.Task[None]] = set()
    surface_errors: list[str] = []

    def _notice(payload: str) -> None:
        # The surface-error notice carries the only copy of the sidecar crash
        # reason; remember it so it can be re-printed to the real stderr once
        # the capture (and the possibly-dead host) is out of the way.
        if _SURFACE_ERROR_MARKER in payload:
            surface_errors.append(payload)
        opentui_notice(scope, payload)

    runtime_state = TuiRuntimeState()
    scope["pending_input_provider"] = runtime_state

    def _forward_notice(line: str) -> None:
        forward_console_notice(scope, line, pending_tasks=notice_tasks)

    try:
        # Capture slash-command/runtime console output so it renders inside the
        # host frame as styled notices instead of bleeding raw onto the terminal.
        with capture_stdout_as_notices(_forward_notice):
            await run_tui_runtime(
                dispatch=dispatch,
                surface_factory=_surface_factory,
                config=TuiRuntimeConfig(
                    task_name=surface_task_name(surface),
                    queue_max_size=queue_max_size,
                    concurrent_input_during_turn=True,
                    classify_input=lambda user_input: classify_chat_input(
                        user_input,
                        surface=surface,
                    ),
                    state=runtime_state,
                ),
                hooks=TuiRuntimeHooks(
                    on_user_input_echo=echo_opentui_user_input,
                    on_queued_turn_start=echo_opentui_queued_turn_start,
                    clear_current_cancel=clear_current_cancel,
                    notice=_notice,
                    on_cancel_active_turn=context.abort_turn,
                    on_steer_active_turn=(
                        steer_active_turn
                        if steer_active_turn is not None
                        else TuiRuntimeHooks().on_steer_active_turn
                    ),
                    expose_surface=context.expose_surface,
                    clear_exposed_surface=context.clear_output,
                ),
            )
    finally:
        for payload in surface_errors:
            _reprint_surface_error(payload)
        if scope.get("pending_input_provider") is runtime_state:
            scope.pop("pending_input_provider", None)
        if notice_tasks:
            # Exit-path notices (Goodbye, surface errors) are scheduled with no
            # further await before teardown; drive them here so each one is
            # delivered or hits the stderr fallback instead of dying with the
            # event loop.
            await asyncio.gather(*notice_tasks, return_exceptions=True)
