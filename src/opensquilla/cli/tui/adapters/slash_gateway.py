"""Gateway slash-command adapter for the chat REPL backend.

This module owns gateway-mode slash command dispatch. It is intentionally
independent from raw frontend and chat application objects: callers pass
typed session state, a gateway client, and an optional TUI output handle.
"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from rich.table import Table

import opensquilla.cli.tui.adapters.input_bridge as _input_bridge
from opensquilla.cli.chat.session_state import ChatSessionState, messages_to_markdown
from opensquilla.cli.chat.turn import TurnResult
from opensquilla.cli.gateway_client import (
    GatewayEventSubscription,
    GatewayRPCError,
    session_history_all,
)
from opensquilla.cli.tui.adapters.commands import render_help_table, render_keys_table
from opensquilla.cli.tui.adapters.slash_common import (
    compact_skipped_line,
    compact_success_line,
    compact_summary_stats,
    compact_token_stats,
    dispatch_theme_command,
    output_supports_host_ui,
    record_turn,
    registry_handler_words,
    resolve_transcript_target,
    save_transcript_markdown,
)
from opensquilla.cli.tui.adapters.slash_common import (
    slash_parts as _slash_parts,
)
from opensquilla.cli.tui.adapters.slash_common import (
    slash_parts_any as _slash_parts_any,
)
from opensquilla.cli.tui.backend.contracts import TuiOutputHandle
from opensquilla.cli.tui.opentui.context import (
    send_context_patch,
    send_context_update,
    send_model_routing_state,
)
from opensquilla.cli.tui.opentui.history import (
    HISTORY_BOOTSTRAP_LIMIT,
    apply_bootstrap_to_state,
    history_replace_from_bootstrap,
    replace_tui_history,
    set_tui_history_loading,
)
from opensquilla.cli.ui import ACCENT, ACCENT_HEADER, console, error_panel
from opensquilla.engine.commands import Surface

_CLI_ALLOWED_FILE_MIMES = _input_bridge.CLI_ALLOWED_FILE_MIMES
_CLI_INLINE_THRESHOLD_BYTES = _input_bridge.CLI_INLINE_THRESHOLD_BYTES
_PATH_REMOTE_GATEWAY_MESSAGE = _input_bridge.PATH_REMOTE_GATEWAY_MESSAGE

# Derived from the engine registry so a new slash command only has to be
# declared once; the dispatch chain below is pinned to this set by tests.
GATEWAY_SLASH_HANDLER_WORDS = registry_handler_words(Surface.CLI_GATEWAY)


class GatewayClientLike(Protocol):
    async def call(self, method: str, params: dict | None = None) -> Any: ...

    async def create_session(
        self,
        agent_id: str = "main",
        model: str | None = None,
        display_name: str | None = None,
    ) -> str: ...

    async def list_sessions(self, limit: int = 50) -> dict[str, Any]: ...

    async def resolve_session(self, key: str) -> dict[str, Any]: ...

    async def bootstrap_session(
        self,
        key: str,
        *,
        limit: int = 200,
    ) -> dict[str, Any]: ...

    async def delete_sessions(self, keys: list[str]) -> dict[str, Any]: ...

    async def reset_session(self, key: str) -> dict[str, Any]: ...

    async def compact_session(self, key: str) -> dict[str, Any]: ...

    async def list_models(
        self,
        provider: str | None = None,
        capabilities: list[str] | None = None,
    ) -> list[dict[str, Any]]: ...

    async def patch_session(self, key: str, **fields: Any) -> dict[str, Any]: ...

    async def usage_status(self) -> dict[str, Any]: ...

    async def upload_file(self, path: Path, mime: str, name: str) -> str: ...

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

    async def abort_session(self, key: str) -> dict[str, Any]: ...

    async def session_history(
        self,
        session_key: str,
        limit: int = 1000,
        *,
        before: str | None = None,
        after: str | None = None,
        include_canonical: bool | None = None,
        include_summaries: bool | None = None,
    ) -> dict[str, Any]: ...

    async def forget_approvals(self, target: str | None = None) -> dict[str, Any]: ...

    async def approvals_snapshot(self) -> dict[str, Any]: ...

    async def set_approval_mode(self, mode: str) -> dict[str, Any]: ...

    async def get_model_routing(self) -> dict[str, Any]: ...

    async def set_model_routing(self, mode: str) -> dict[str, Any]: ...

    async def subscribe_session_events(
        self,
        session_key: str,
        *,
        since_stream_seq: int | None = None,
        event_names: set[str] | frozenset[str] | None = None,
    ) -> GatewayEventSubscription: ...


class _GoalTurnStreamClient(Protocol):
    """The client surface the shared gateway stream renderer needs per turn.

    ``stream_response_gateway`` only pulls ``send_message`` frames and resolves
    approvals/aborts through the client; ``_GoalTurnClient`` implements exactly
    this surface for goal continuation turns replayed from a session
    subscription.
    """

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

    async def abort_session(self, key: str) -> dict[str, Any]: ...


class GatewayStreamResponse(Protocol):
    async def __call__(
        self,
        client: _GoalTurnStreamClient,
        session_key: str,
        message: str,
        elevated_state: dict[str, str | None] | None = None,
        attachments: list[dict] | None = None,
        *,
        tui_output: TuiOutputHandle | None = None,
    ) -> TurnResult: ...


async def stream_response_gateway(
    client: _GoalTurnStreamClient,
    session_key: str,
    message: str,
    elevated_state: dict[str, str | None] | None = None,
    attachments: list[dict] | None = None,
    *,
    tui_output: TuiOutputHandle | None = None,
) -> TurnResult:
    del client, session_key, message, elevated_state, attachments, tui_output
    raise RuntimeError("gateway streaming dependency was not configured")


@dataclass
class GatewaySlashContext:
    state: ChatSessionState
    client: GatewayClientLike
    elevated_state: dict[str, str | None]
    tui_output: TuiOutputHandle | None = None
    stream_response: GatewayStreamResponse | None = None
    # The model the user explicitly requested (--model at launch or the last
    # /model choice). Kept apart from state.model, which tracks the model that
    # last ran and may be a router pick that must not leak into session pins.
    requested_model: str | None = None


def _attachment_label_from_command(command: str, *, fallback: str) -> str:
    """Return only a basename for UI chips; never expose a local path."""

    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    if len(words) < 2:
        return fallback
    raw = words[1].replace("\\", "/").rsplit("/", 1)[-1].strip()
    return raw[:72] or fallback


def _attachment_failure_message(kind: str, label: str) -> str:
    return f"Could not prepare {label}; check the file and retry /{kind}."


async def _add_attachment_chip(
    output: object | None,
    *,
    kind: str,
    label: str,
) -> str | None:
    add = getattr(output, "add_attachment", None)
    if not callable(add):
        return None
    return str(await add(kind=kind, label=label, status="reading"))


async def _update_attachment_chip(
    output: object | None,
    attachment_id: str | None,
    *,
    status: str,
    message: str = "",
) -> None:
    if not attachment_id:
        return
    update = getattr(output, "update_attachment", None)
    if callable(update):
        await update(attachment_id, status=status, message=message)


async def _clear_ready_attachment_chips(output: object | None) -> None:
    clear = getattr(output, "clear_attachments", None)
    if callable(clear):
        await clear(status="ready")


async def _activate_session_from_bootstrap(
    context: GatewaySlashContext,
    session_key: str,
) -> dict[str, Any]:
    """Replace UI + mutable state from one canonical session snapshot."""

    await set_tui_history_loading(context.tui_output, loading=True)
    try:
        snapshot = await context.client.bootstrap_session(
            session_key,
            limit=HISTORY_BOOTSTRAP_LIMIT,
        )
        history = history_replace_from_bootstrap(
            snapshot,
            fallback_session_key=session_key,
        )
        # The host replacement is one frame. Update Python state only once it
        # has landed so a renderer failure cannot leave scope and screen on two
        # keys.
        await replace_tui_history(
            context.tui_output,
            history,
            manage_composer=False,
        )
        apply_bootstrap_to_state(context.state, snapshot, history)
        raw_session = snapshot.get("session")
        session = raw_session if isinstance(raw_session, dict) else {}
        if "model" in session:
            # ``effective_model`` is display state and can be a Router pick.
            # Only the canonical session ``model`` field is the durable pin.
            model_pin = session.get("model")
            context.requested_model = str(model_pin) if model_pin else None
        await send_context_update(
            context.tui_output,
            snapshot,
            model=context.state.model,
            session_id=context.state.session_key,
            permission=context.elevated_state.get("mode"),
        )
        return snapshot
    finally:
        await set_tui_history_loading(context.tui_output, loading=False)


async def handle_gateway_slash_command(
    cmd: str,
    context: GatewaySlashContext,
) -> bool:
    """Handle gateway-mode slash commands.

    Returns ``True`` when the command was handled (including handled failures
    such as a lost gateway connection) and ``False`` for unknown commands so
    the runtime can render its unknown-command notice. Exit commands are owned
    by the runtime loop, which intercepts them before slash dispatch.
    """
    try:
        return await _dispatch_gateway_slash_command(cmd, context)
    except (ConnectionError, OSError) as exc:
        console.print(error_panel(str(exc), title="Gateway command failed"))
        console.print(
            "[dim]Check the gateway with[/dim] [bold]opensquilla gateway status[/bold] "
            "[dim]and start it with[/dim] [bold]opensquilla gateway run[/bold] "
            "[dim]if it is down, then retry the command.[/dim]"
        )
        return True


async def _canonical_session_model_pin(context: GatewaySlashContext) -> str | None:
    """Read the durable model pin owned by the current Gateway session.

    Gateway slash contexts are deliberately short-lived: the runtime creates
    one per command, while ``state.model`` tracks the most recently effective
    model and may therefore contain a Router/Ensemble selection.  Neither is a
    reliable source for the model picker.  ``sessions.resolve.model`` is the
    shared canonical field used by WebUI and every other Gateway client.
    """

    payload = await asyncio.wait_for(
        context.client.resolve_session(context.state.session_key),
        timeout=2.0,
    )
    model = payload.get("model")
    return str(model) if model else None


async def _requested_session_model(context: GatewaySlashContext) -> str | None:
    """Return the explicitly requested model for new sessions, if any.

    ``state.model`` is display bookkeeping: after a routed turn it holds the
    router's pick, and pinning a fresh session to it would silently replace
    routing. The explicit request is the in-context ``/model`` choice or,
    failing that, the model stored on the current session (set from
    ``--model`` at creation or an earlier ``/model`` patch).
    """
    if context.requested_model:
        return context.requested_model
    try:
        return await _canonical_session_model_pin(context)
    except Exception:  # noqa: BLE001 - best-effort read; default to routing
        # The read itself failed (slow/erroring gateway), which is different
        # from "no pin stored". Warn so the user is not surprised that the new
        # session falls back to the router default, and can re-pin explicitly.
        console.print(
            "[yellow]Could not read the current session's model pin; the new "
            "session will use the router default. Re-pin with[/yellow] "
            "[bold]/model <id>[/bold][yellow] if needed.[/yellow]"
        )
        return None


# --------------------------------------------------------------------------- #
# /goal (gateway mode): goals.* RPC surface + watch loop for continuation turns
# --------------------------------------------------------------------------- #

# Goal run statuses that mean "no more turns will be driven" (server FSM).
_GOAL_TERMINAL_STATUSES = frozenset({"complete", "blocked"})
# Plan run statuses that settle the owning goal run (driverKind == "goal").
_GOAL_PLAN_RUN_TERMINAL_STATUSES = frozenset({"completed", "cancelled"})

# How often the watch loop refreshes the goal watcher registration. The server
# evicts watchers that stay silent past ``goal.watcher_ttl_seconds`` (default
# 900s); a 60s heartbeat keeps a long-running single turn from being evicted.
_GOAL_HEARTBEAT_INTERVAL_S = 60.0


def _goal_payload(frame: Mapping[str, Any]) -> dict[str, Any]:
    payload = frame.get("payload")
    return payload if isinstance(payload, dict) else {}


def _goal_turn_id(frame: Mapping[str, Any]) -> str | None:
    """Return the turn identity stamped on a session event frame, if any."""
    event = _goal_payload(frame)
    for key in ("turn_id", "turnId", "task_id", "taskId"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _goal_plan_run(frame: Mapping[str, Any]) -> dict[str, Any] | None:
    """Extract the ``plan_run`` snapshot from a plan run event frame."""
    if str(frame.get("event") or "") not in {
        "session.event.plan_run",
        "session.event.goal_run",
    }:
        return None
    run = _goal_payload(frame).get("plan_run")
    return run if isinstance(run, dict) else None


def _goal_plan_run_terminal(
    frame: Mapping[str, Any],
) -> tuple[str, str | None] | None:
    """Return ``(status, terminal_reason)`` when the frame settles the goal run.

    A plan run only settles its goal when it is a goal driver run reaching a
    terminal state. Anything else returns ``None`` so the watch loop keeps
    consuming frames.
    """
    run = _goal_plan_run(frame)
    if run is None or str(run.get("driverKind") or "") != "goal":
        return None
    status = str(run.get("status") or "")
    if status not in _GOAL_PLAN_RUN_TERMINAL_STATUSES:
        return None
    reason = run.get("terminalReason")
    return status, reason if isinstance(reason, str) and reason else None


def _goal_frame_error(frame: Mapping[str, Any]) -> str | None:
    """Return a user-facing message when the frame is a session error."""
    if str(frame.get("event") or "") != "session.event.error":
        return None
    event = _goal_payload(frame)
    message = event.get("message")
    if isinstance(message, str) and message:
        return message
    error_message = event.get("error_message")
    return (
        error_message
        if isinstance(error_message, str) and error_message
        else "Goal turn failed"
    )


def _goal_reason_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _print_goal_status(payload: dict[str, Any]) -> None:
    """Render a ``goals.status`` payload in the compact ``/status`` style."""
    goal = payload.get("goal")
    if goal is None:
        console.print(f"[{ACCENT}]goal[/] [dim]No active goal[/dim]")
        return
    goal_text = str(goal.get("goalText") or "")
    console.print(f"[{ACCENT}]goal[/] [dim]{goal_text}[/dim]")
    console.print(f"[{ACCENT}]status[/] [dim]{str(goal.get('status') or '')}[/dim]")
    turns = goal.get("turns")
    if isinstance(turns, int):
        console.print(f"[{ACCENT}]turns[/] [dim]{turns}[/dim]")
    idle = goal.get("idleTurns")
    if isinstance(idle, int):
        console.print(f"[{ACCENT}]idle[/] [dim]{idle}[/dim]")
    failure_retries = goal.get("failureRetries")
    if isinstance(failure_retries, int) and failure_retries:
        console.print(f"[{ACCENT}]retries[/] [dim]{failure_retries}[/dim]")
    last_error = _goal_reason_text(goal.get("lastError"))
    if last_error:
        console.print(f"[{ACCENT}]last error[/] [dim]{last_error}[/dim]")
    pause_reason = _goal_reason_text(goal.get("pauseReason"))
    if pause_reason:
        console.print(f"[{ACCENT}]paused[/] [dim]{pause_reason}[/dim]")
    reason = _goal_reason_text(goal.get("blockedReason")) or _goal_reason_text(
        goal.get("terminalReason")
    )
    if reason:
        console.print(f"[{ACCENT}]reason[/] [dim]{reason}[/dim]")
    run = payload.get("planRun")
    if isinstance(run, dict) and str(run.get("driverKind") or "") == "goal":
        console.print(
            f"[{ACCENT}]run[/] [dim]{run.get('runId')}[/dim] "
            f"[dim]({str(run.get('status') or '')})[/dim]"
        )


def _print_goal_terminal_summary(terminal: str, reason: str | None) -> None:
    """Render the one-line outcome after the watch loop stops."""
    if terminal in {"complete", "completed"}:
        headline = "[green]goal complete[/green]"
    elif terminal in {"blocked", "failed", "error"}:
        headline = f"[yellow]goal {terminal}[/yellow]"
    elif terminal in {"cancelled", "cleared", "paused"}:
        headline = f"[{ACCENT}]goal {terminal}[/{ACCENT}]"
    else:
        headline = f"[{ACCENT}]goal {terminal}[/{ACCENT}]"
    if reason:
        console.print(f"{headline} [dim]({reason})[/dim]")
    else:
        console.print(headline)


class _GoalTurnClient:
    """Feed one goal-driven continuation turn through the shared renderer.

    Mirrors the runtime's external-turn projection (``_ExternalTurnClient``):
    the first frame that announced the turn is replayed, then frames are pulled
    from the shared session subscription until the turn reaches its terminal
    frame. A settling goal plan run is intercepted while pulling so the watch
    loop can stop as soon as the server terminalizes the goal.
    """

    def __init__(
        self,
        client: GatewayClientLike,
        subscription: Any,
        first_frame: dict[str, Any],
        *,
        turn_id: str,
    ) -> None:
        self._client = client
        self._subscription = subscription
        self._first_frame = first_frame
        self._turn_id = turn_id
        self.terminal_plan_run: dict[str, Any] | None = None

    async def send_message(
        self,
        session_key: str,
        message: str,
        attachments: list[dict] | None = None,
        elevated: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        del session_key, message, attachments, elevated
        from opensquilla.cli.gateway_client import _advance_gateway_turn_event

        active_task_groups: set[str] = set()
        first: dict[str, Any] | None = self._first_frame
        while True:
            frame = first if first is not None else await self._subscription.get()
            first = None
            plan_terminal = _goal_plan_run_terminal(frame)
            if plan_terminal is not None:
                run = _goal_plan_run(frame)
                if run is not None:
                    self.terminal_plan_run = run
                return
            event_name = str(frame.get("event") or "")
            payload = _goal_payload(frame)
            turn_id = _goal_turn_id(frame)
            if turn_id is not None and turn_id != self._turn_id:
                continue
            normalized, terminal = _advance_gateway_turn_event(
                event_name,
                payload,
                active_task_groups,
            )
            yield normalized
            if terminal:
                return

    async def resolve_approval(
        self,
        approval_id: str,
        approved: bool,
        *,
        choice: str | None = None,
    ) -> Any:
        return await self._client.resolve_approval(approval_id, approved, choice=choice)

    async def abort_session(self, key: str) -> dict[str, Any]:
        # Cancelling the local projection (Ctrl+C / surface exit) must not
        # strand the goal driver: the watcher cleanup pauses the goal instead.
        return await self._client.abort_session(key)


async def _goal_status_after_turn(
    client: GatewayClientLike,
    session_key: str,
) -> tuple[str, str | None] | None:
    """Return ``(terminal_status, reason)`` after a goal turn, else ``None``.

    The driver settles the goal run durably without broadcasting a terminal
    ``plan_run`` frame, so the watcher re-reads canonical status after each
    turn. ``None`` means the goal is still running and the loop keeps watching.
    """
    try:
        payload = await client.call("goals.status", {"sessionKey": session_key})
    except GatewayRPCError:
        return ("error", None)
    goal = payload.get("goal") if isinstance(payload, dict) else None
    if goal is None:
        return ("cleared", None)
    status = str(goal.get("status") or "")
    if status in _GOAL_TERMINAL_STATUSES:
        return (status, _goal_reason_text(goal.get("terminalReason")))
    if status == "paused":
        reason = _goal_reason_text(goal.get("pauseReason")) or _goal_reason_text(
            goal.get("lastError")
        )
        return ("paused", reason)
    if status == "running" and goal.get("nextRetryAtMs") is not None:
        # A transient failure scheduled an automatic retry; keep watching.
        retries = goal.get("failureRetries")
        console.print(
            f"[{ACCENT}]goal[/] [yellow]retry scheduled[/yellow] "
            f"[dim](attempt {retries}, waiting…)[/dim]"
        )
        return None
    return None


async def _goal_watch_cleanup(
    client: GatewayClientLike,
    session_key: str,
) -> None:
    """Release the watcher and pause a still-running goal ("close chat = pause")."""
    try:
        await client.call("goals.unobserve", {"sessionKey": session_key, "watch": False})
    except Exception:  # noqa: BLE001 - cleanup is best-effort
        pass
    try:
        payload = await client.call("goals.status", {"sessionKey": session_key})
    except Exception:  # noqa: BLE001 - cleanup is best-effort
        return
    goal = payload.get("goal") if isinstance(payload, dict) else None
    if goal is None or str(goal.get("status") or "") != "running":
        return
    try:
        await client.call("goals.pause", {"sessionKey": session_key})
    except Exception:  # noqa: BLE001 - cleanup is best-effort
        return
    console.print(f"[{ACCENT}]goal[/] [yellow]paused[/yellow] [dim](no watcher)[/dim]")


async def _run_goal_watch(
    client: GatewayClientLike,
    session_key: str,
    *,
    stream: GatewayStreamResponse,
    elevated_state: dict[str, str | None],
    tui_output: TuiOutputHandle | None,
) -> None:
    """Render gateway-driven goal turns until the goal settles or watching stops.

    Continuation turns arrive as ``session.event.*`` frames on the session
    stream. Every new turn (keyed by its ``turn_id``) is projected through the
    shared gateway stream path, so TUI and plain surfaces see the same live
    output as a normal turn. The loop exits when the goal plan run reaches a
    terminal state, the goal run itself terminalizes (checked after each turn),
    an error surfaces, or the user interrupts (Ctrl+C), which pauses the goal.
    """
    try:
        subscription = await client.subscribe_session_events(session_key)
    except Exception:
        # Never leave the watcher registered when the stream cannot be opened.
        await _goal_watch_cleanup(client, session_key)
        raise
    console.print(f"[{ACCENT}]goal[/] [dim]watching; press Ctrl+C to pause[/dim]")
    terminal: str | None = None
    terminal_reason: str | None = None
    current_turn_id: str | None = None

    async def _heartbeat() -> None:
        """Refresh the watcher registration so a long turn stays observed."""

        try:
            while True:
                await asyncio.sleep(_GOAL_HEARTBEAT_INTERVAL_S)
                try:
                    await client.call(
                        "goals.observe",
                        {"sessionKey": session_key, "watch": True},
                    )
                except Exception:  # noqa: BLE001 - heartbeat is best-effort
                    pass
        except asyncio.CancelledError:
            return

    heartbeat_task = asyncio.create_task(_heartbeat())
    try:
        async for frame in subscription:
            plan_terminal = _goal_plan_run_terminal(frame)
            if plan_terminal is not None:
                terminal, terminal_reason = plan_terminal
                break
            error_message = _goal_frame_error(frame)
            if error_message is not None:
                console.print(error_panel(error_message, title="Goal turn failed"))
                terminal = "failed"
                break
            turn_id = _goal_turn_id(frame)
            if turn_id is None or turn_id == current_turn_id:
                continue
            current_turn_id = turn_id
            turn_client = _GoalTurnClient(
                client,
                subscription,
                frame,
                turn_id=turn_id,
            )
            result = await stream(
                turn_client,
                session_key,
                "",
                elevated_state,
                tui_output=tui_output,
            )
            if turn_client.terminal_plan_run is not None:
                run = turn_client.terminal_plan_run
                terminal = str(run.get("status") or "cancelled")
                terminal_reason = _goal_reason_text(run.get("terminalReason"))
                break
            if result.error:
                console.print(error_panel(str(result.error), title="Goal turn failed"))
                terminal = "failed"
                break
            status = await _goal_status_after_turn(client, session_key)
            if status is not None:
                terminal, terminal_reason = status
                break
    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print(f"\n[{ACCENT}]goal[/] [yellow]watch cancelled[/yellow]")
    finally:
        heartbeat_task.cancel()
        close = getattr(subscription, "close", None)
        if callable(close):
            try:
                await close()
            except Exception:  # noqa: BLE001 - cleanup is best-effort
                pass
        await _goal_watch_cleanup(client, session_key)
        if terminal is not None:
            _print_goal_terminal_summary(terminal, terminal_reason)


async def _handle_goal_command(argument: str, context: GatewaySlashContext) -> bool:
    """Dispatch one gateway-mode ``/goal`` invocation (all subcommands)."""
    client = context.client
    key = context.state.session_key
    word = argument.strip().lower()

    if word in {"", "status"}:
        try:
            payload = await client.call("goals.status", {"sessionKey": key})
        except GatewayRPCError as exc:
            console.print(error_panel(str(exc), title="Goal status failed"))
        else:
            _print_goal_status(payload if isinstance(payload, dict) else {})
        return True

    if word == "clear":
        try:
            before = await client.call("goals.clear", {"sessionKey": key})
        except GatewayRPCError as exc:
            console.print(error_panel(str(exc), title="Goal clear failed"))
            return True
        goal = before.get("goal") if isinstance(before, dict) else None
        if goal is None:
            console.print(f"[{ACCENT}]goal[/] [dim]No active goal to clear[/dim]")
        else:
            console.print(
                f"[{ACCENT}]goal[/] [yellow]cleared[/yellow] "
                f"[dim]{goal.get('goalText')}[/dim]"
            )
        return True

    if word == "pause":
        try:
            payload = await client.call("goals.pause", {"sessionKey": key})
        except GatewayRPCError as exc:
            console.print(error_panel(str(exc), title="Goal pause failed"))
            return True
        goal = payload.get("goal") if isinstance(payload, dict) else None
        label = (
            str(goal.get("goalText") or "")
            if isinstance(goal, dict) and goal.get("goalText")
            else ""
        )
        console.print(
            f"[{ACCENT}]goal[/] [yellow]paused[/yellow]"
            + (f" [dim]{label}[/dim]" if label else "")
        )
        return True

    if word == "resume":
        try:
            await client.call("goals.resume", {"sessionKey": key})
        except GatewayRPCError as exc:
            console.print(error_panel(str(exc), title="Goal resume failed"))
            return True
        # Re-register the watcher before entering the watch loop (mirrors the
        # /goal <description> path) so a stale/expired watcher entry does not
        # stop the continuation driver after the first resumed turn.
        try:
            await client.call("goals.observe", {"sessionKey": key, "watch": True})
        except GatewayRPCError as exc:
            console.print(error_panel(str(exc), title="Goal watch failed"))
            return True
        console.print(f"[{ACCENT}]goal[/] [green]resumed[/green]; watching…")
        await _run_goal_watch(
            client,
            key,
            stream=context.stream_response or stream_response_gateway,
            elevated_state=context.elevated_state,
            tui_output=context.tui_output,
        )
        return True

    if word in {"help", "-h", "--help"}:
        console.print(
            "[red]Usage: /goal [status|clear|pause|resume|<description>][/red]"
        )
        return True

    # /goal <description> → goals.observe + goals.set + watch loop.
    try:
        await client.call("goals.observe", {"sessionKey": key, "watch": True})
    except GatewayRPCError as exc:
        console.print(error_panel(str(exc), title="Goal watch failed"))
        return True
    watch_started = False
    try:
        try:
            payload = await client.call("goals.set", {"sessionKey": key, "message": argument})
        except GatewayRPCError as exc:
            console.print(error_panel(str(exc), title="Goal set failed"))
            return True
        goal = payload.get("goal") if isinstance(payload, dict) else None
        goal_text = (
            str(goal.get("goalText") or argument)
            if isinstance(goal, dict)
            else argument
        )
        console.print(f"[{ACCENT}]goal[/] [green]set[/green]: [bold]{goal_text}[/bold]")
        watch_started = True
        await _run_goal_watch(
            client,
            key,
            stream=context.stream_response or stream_response_gateway,
            elevated_state=context.elevated_state,
            tui_output=context.tui_output,
        )
    finally:
        # The watch loop's own finally unobserves and pauses when it runs.
        # This guard only covers the goals.set failure path before the loop
        # starts: drop the watcher so the driver cannot keep enqueueing turns
        # without a viewer.
        if not watch_started:
            try:
                await client.call(
                    "goals.unobserve",
                    {"sessionKey": key, "watch": False},
                )
            except Exception:  # noqa: BLE001 - cleanup is best-effort
                pass
    return True


async def _dispatch_gateway_slash_command(
    cmd: str,
    context: GatewaySlashContext,
) -> bool:
    state = context.state
    client = context.client
    elevated_state = context.elevated_state
    tui_output = context.tui_output
    stream = context.stream_response or stream_response_gateway

    if cmd == "/help":
        console.print(render_help_table(Surface.CLI_GATEWAY))
        return True

    if cmd in {"/keys", "/shortcuts"}:
        console.print(render_keys_table(opentui=output_supports_host_ui(tui_output)))
        return True

    if _slash_parts(cmd, "/theme"):
        await dispatch_theme_command(cmd, tui_output)
        return True

    if parts := _slash_parts_any(cmd, "/strategy", "/router", "/ensemble"):
        command = parts[0].lower()
        argument = parts[1].strip().lower() if len(parts) > 1 else ""
        allowed_arguments = (
            {"", "direct", "router", "ensemble", "status"}
            if command == "/strategy"
            else {"", "on", "off", "status"}
        )
        if argument not in allowed_arguments:
            usage = (
                "/strategy [direct|router|ensemble|status]"
                if command == "/strategy"
                else f"{command} [on|off|status]"
            )
            console.print(f"[red]Usage: {usage}[/red]")
            return True

        try:
            snapshot = await client.get_model_routing()
        except Exception as exc:
            # Older/read-only Gateways may project the last known strategy in
            # bootstrap but lack the canonical control RPC. Never reinterpret
            # the command as a model prompt or silently fall back to raw config
            # mutation: leave the displayed state read-only and explain why.
            console.print(
                "[yellow]Model routing controls are unavailable on this Gateway; "
                "the displayed strategy is read-only.[/yellow] "
                f"[dim]{exc}[/dim]"
            )
            return True
        if not argument:
            send = getattr(tui_output, "send_message", None)
            if bool(getattr(tui_output, "supports_send_message", False)) and callable(send):
                await send(
                    "model.routing.picker",
                    {
                        "current": snapshot.get("mode", "direct"),
                        "options": ["direct", "router", "ensemble"],
                    },
                )
                return True
            console.print(
                "[yellow]The three-state strategy picker is unavailable on this "
                "surface; showing read-only status.[/yellow]"
            )
            argument = "status"

        try:
            if command == "/strategy" and argument in {"direct", "router", "ensemble"}:
                snapshot = await client.set_model_routing(argument)
            elif argument == "on":
                requested = "router" if command == "/router" else "ensemble"
                snapshot = await client.set_model_routing(requested)
            elif argument == "off":
                snapshot = await client.set_model_routing("direct")
        except Exception as exc:
            # Re-project the canonical pre-write snapshot so a failed control
            # request cannot leave an optimistic "next …" strategy in the host.
            await send_model_routing_state(tui_output, snapshot)
            console.print(
                "[red]Model routing change failed; strategy remains "
                f"{snapshot.get('mode', 'direct')}.[/red] [dim]{exc}[/dim]"
            )
            return True

        mode = str(snapshot.get("mode") or "direct")
        await send_model_routing_state(tui_output, snapshot)
        if argument == "status":
            console.print(f"[dim]strategy[/dim] [bold]{mode}[/bold]")
        else:
            console.print(
                f"[green]strategy:[/green] {mode} "
                "[dim](applies to the next accepted turn)[/dim]"
            )
        return True

    if parts := _slash_parts(cmd, "/new"):
        title = parts[1].strip() if len(parts) > 1 else None
        requested_model = await _requested_session_model(context)
        session_key = await client.create_session(model=requested_model, display_name=title)
        await _activate_session_from_bootstrap(context, session_key)
        label = f" ({title})" if title else ""
        console.print(f"[green]Started new session{label}:[/green] {session_key}")
        return True

    if cmd in {"/status", "/session"}:
        console.print(
            f"[{ACCENT}]session[/] [dim]{state.session_key}[/dim]\n"
            f"[{ACCENT}]model[/] [dim]{state.model or 'default'}[/dim]\n"
            f"[{ACCENT}]permissions[/] [dim]{state.elevated or 'normal'}[/dim]"
        )
        return True

    if parts := _slash_parts(cmd, "/goal"):
        argument = parts[1].strip() if len(parts) > 1 else ""
        return await _handle_goal_command(argument, context)

    if parts := _slash_parts(cmd, "/sessions"):
        limit = 10
        if len(parts) > 1:
            try:
                limit = int(parts[1])
            except ValueError:
                console.print("[red]Usage: /sessions [limit][/red]")
                return True
        payload = await client.list_sessions(limit=limit)
        rows = payload.get("sessions", [])
        send = getattr(tui_output, "send_message", None)
        if bool(getattr(tui_output, "supports_send_message", False)) and callable(send):
            await send(
                "session.pick",
                {
                    "current_key": state.session_key,
                    "sessions": _session_picker_rows(rows),
                },
            )
        else:
            _print_sessions_table(rows)
        return True

    if parts := _slash_parts(cmd, "/resume"):
        if len(parts) == 1 or not parts[1].strip():
            payload = await client.list_sessions(limit=50)
            rows = payload.get("sessions", [])
            send = getattr(tui_output, "send_message", None)
            if bool(getattr(tui_output, "supports_send_message", False)) and callable(send):
                await send(
                    "session.pick",
                    {
                        "current_key": state.session_key,
                        "sessions": _session_picker_rows(rows),
                    },
                )
            else:
                _print_sessions_table(rows)
            return True
        target = cmd.split(maxsplit=1)[1].strip()
        await _activate_session_from_bootstrap(context, target)
        console.print(f"[green]Resumed session:[/green] {state.session_key}")
        return True

    if parts := _slash_parts(cmd, "/delete"):
        if len(parts) == 1 or not parts[1].strip():
            console.print("[red]Usage: /delete <id>[/red]")
            return True
        target = cmd.split(maxsplit=1)[1].strip()
        resolved = await client.resolve_session(target)
        session_key = resolved.get("session_key") or resolved.get("key") or target
        deleting_active = session_key == state.session_key
        payload = await client.delete_sessions([session_key])
        errors = [str(item) for item in payload.get("errors") or []]
        deleted = [str(item) for item in payload.get("deleted") or []]
        if errors:
            console.print(error_panel("\n".join(errors), title="Delete failed"))
        elif deleted:
            console.print(f"[yellow]Deleted session:[/yellow] {deleted[0]}")
            if deleting_active:
                # The REPL must not keep sending turns to a deleted key; move
                # to a fresh session that keeps the user's explicit model pin.
                replacement_model = context.requested_model or resolved.get("model") or None
                new_key = await client.create_session(model=replacement_model)
                await _activate_session_from_bootstrap(context, new_key)
                console.print(
                    "[yellow]The deleted session was active; switched to a new "
                    f"session:[/yellow] {new_key}"
                )
        else:
            console.print(error_panel("No session was deleted.", title="Delete failed"))
        return True

    if cmd in {"/clear", "/reset"}:
        await client.reset_session(state.session_key)
        await _activate_session_from_bootstrap(context, state.session_key)
        console.print(f"[{ACCENT}]cleared[/] [dim]{state.session_key}[/dim]")
        return True

    if cmd in {"/compact", "/cmp"}:
        console.print(f"[{ACCENT}]compacting context...[/]")
        try:
            payload = await client.compact_session(state.session_key)
        except Exception as exc:  # noqa: BLE001 - keep interactive chat alive.
            console.print(f"[red]compact failed: {exc}[/red]")
            return True
        if payload.get("compacted"):
            before = int(payload.get("tokens_before") or 0)
            after = int(payload.get("tokens_after") or 0)
            remaining = int(payload.get("remaining_budget_tokens") or 0)
            source = payload.get("summary_source") or "unknown"
            token_stats = (
                compact_token_stats(before, after, remaining, source)
                if before or after
                else compact_summary_stats(payload.get("summary_len", 0))
            )
            console.print(compact_success_line(token_stats))
        else:
            console.print(compact_skipped_line())
        return True

    if parts := _slash_parts(cmd, "/models"):
        if len(parts) > 1:
            console.print("[red]Usage: /models[/red]")
            return True
        models = await client.list_models()
        _print_models_table(models)
        return True

    if parts := _slash_parts(cmd, "/model"):
        argument = parts[1].strip() if len(parts) > 1 else ""
        normalized = argument.lower()
        if not argument:
            requested_model = await _canonical_session_model_pin(context)
            models = await client.list_models()
            send = getattr(tui_output, "send_message", None)
            if bool(getattr(tui_output, "supports_send_message", False)) and callable(send):
                await send(
                    "model.picker",
                    {
                        # The picker marks the durable session pin, not the
                        # last model projected by a routed/ensemble turn.
                        "current": requested_model,
                        "options": [
                            {
                                "id": str(row.get("id") or ""),
                                "provider": str(row.get("provider") or ""),
                                "context_window": row.get("contextWindow"),
                            }
                            for row in models
                            if str(row.get("id") or "").strip()
                        ],
                    },
                )
                return True
            console.print(
                "[dim]model pin[/dim] "
                f"[bold]{requested_model or 'auto'}[/bold]"
            )
            _print_models_table(models)
            return True
        if normalized == "status":
            requested_model = await _canonical_session_model_pin(context)
            console.print(
                "[dim]model pin[/dim] "
                f"[bold]{requested_model or 'auto'}[/bold]"
                + (
                    " [dim](explicit; overrides Router selection)[/dim]"
                    if requested_model
                    else " [dim](Router/default decides)[/dim]"
                )
            )
            return True
        if normalized in {"auto", "default"}:
            await client.patch_session(state.session_key, model=None)
            state.model = None
            context.requested_model = None
            await send_context_patch(tui_output, model="default")
            console.print(
                "[green]model pin:[/green] auto "
                "[dim](Router/default decides from the next accepted turn)[/dim]"
            )
            return True

        new_model = argument
        await client.patch_session(state.session_key, model=new_model)
        state.model = new_model
        context.requested_model = new_model
        await send_context_patch(tui_output, model=new_model)
        console.print(
            f"[green]model pin:[/green] {new_model} "
            "[dim](explicit; applies to the next accepted turn)[/dim]"
        )
        return True

    if cmd == "/cost":
        console.print(state.usage.render())
        return True

    if cmd == "/usage":
        payload = await client.usage_status()
        console.print(
            "[dim]aggregate usage: "
            f"{payload.get('totalTokens', 0):,} tok · "
            f"${float(payload.get('totalCostUsd', 0.0) or 0.0):.6f}[/dim]"
        )
        return True

    if _slash_parts(cmd, "/save"):
        await _save_gateway_transcript_command(cmd, state, client)
        return True

    if parts := _slash_parts(cmd, "/image"):
        if len(parts) == 1 or not parts[1].strip():
            console.print("[red]Usage: /image <path> [prompt][/red]")
            return True
        label = _attachment_label_from_command(cmd, fallback="image")
        attachment_id = await _add_attachment_chip(
            tui_output,
            kind="image",
            label=label,
        )
        try:
            prompt, attachments = _image_prompt_and_attachments(cmd)
        except ValueError:
            message = _attachment_failure_message("image", label)
            await _update_attachment_chip(
                tui_output,
                attachment_id,
                status="failed",
                message=message,
            )
            console.print(error_panel(message))
            return True
        await _update_attachment_chip(
            tui_output,
            attachment_id,
            status="ready",
        )
        try:
            result = await stream(
                client,
                state.session_key,
                prompt,
                elevated_state,
                attachments=attachments,
                tui_output=tui_output,
            )
        finally:
            await _clear_ready_attachment_chips(tui_output)
        record_turn(state, prompt, result)
        return True

    if parts := _slash_parts(cmd, "/meta"):
        raw_args = parts[1].strip() if len(parts) > 1 else ""
        meta_args = raw_args.split(maxsplit=1)
        name = meta_args[0] if meta_args else ""
        request = meta_args[1].strip() if len(meta_args) > 1 else ""
        if not name:
            payload = await client.call("meta.list", {})
            _print_meta_skills_table(payload)
            return True
        run_result = await client.call("meta.run", {"name": name, "sessionKey": state.session_key})
        if not (isinstance(run_result, dict) and run_result.get("ok")):
            error = ""
            if isinstance(run_result, dict):
                error = str(run_result.get("error") or "")
            console.print(error_panel(error or f"Could not run meta-skill {name!r}."))
            return True
        prompt = f"/meta {name}"
        if request:
            prompt = f"{prompt} {request}"
        result = await stream(
            client,
            state.session_key,
            prompt,
            elevated_state,
            tui_output=tui_output,
        )
        record_turn(state, prompt, result)
        return True

    if parts := _slash_parts(cmd, "/path"):
        if len(parts) == 1 or not parts[1].strip():
            console.print("[red]Usage: /path <path> [prompt][/red]")
            return True
        if not _gateway_client_is_local(client):
            console.print(error_panel(_PATH_REMOTE_GATEWAY_MESSAGE))
            return True
        label = _attachment_label_from_command(cmd, fallback="path")
        attachment_id = await _add_attachment_chip(
            tui_output,
            kind="path",
            label=label,
        )
        try:
            prompt, attachments = path_prompt_and_attachments(cmd)
        except ValueError:
            message = _attachment_failure_message("path", label)
            await _update_attachment_chip(
                tui_output,
                attachment_id,
                status="failed",
                message=message,
            )
            console.print(error_panel(message))
            return True
        await _update_attachment_chip(
            tui_output,
            attachment_id,
            status="ready",
        )
        try:
            result = await stream(
                client,
                state.session_key,
                prompt,
                elevated_state,
                attachments=attachments,
                tui_output=tui_output,
            )
        finally:
            await _clear_ready_attachment_chips(tui_output)
        record_turn(state, prompt, result)
        return True

    if parts := _slash_parts(cmd, "/file"):
        if len(parts) == 1 or not parts[1].strip():
            console.print("[red]Usage: /file <path> [prompt][/red]")
            return True

        label = _attachment_label_from_command(cmd, fallback="file")
        attachment_id = await _add_attachment_chip(
            tui_output,
            kind="file",
            label=label,
        )

        async def _bridge_upload(path: Path, mime: str, name: str) -> str:
            await _update_attachment_chip(
                tui_output,
                attachment_id,
                status="uploading",
            )
            return await client.upload_file(path, mime, name)

        try:
            prompt, attachments = await _async_file_prompt_and_attachments(
                cmd, upload_callable=_bridge_upload
            )
        except ValueError:
            message = _attachment_failure_message("file", label)
            await _update_attachment_chip(
                tui_output,
                attachment_id,
                status="failed",
                message=message,
            )
            console.print(error_panel(message))
            return True
        await _update_attachment_chip(
            tui_output,
            attachment_id,
            status="ready",
        )
        try:
            result = await stream(
                client,
                state.session_key,
                prompt,
                elevated_state,
                attachments=attachments,
                tui_output=tui_output,
            )
        finally:
            await _clear_ready_attachment_chips(tui_output)
        record_turn(state, prompt, result)
        return True

    if _slash_parts_any(cmd, "/permissions", "/elevated"):
        await _handle_elevated_command(cmd, elevated_state, client)
        state.elevated = elevated_state.get("mode")
        await send_context_patch(tui_output, permission=state.elevated or "normal")
        return True

    if cmd == "/forget" or cmd.startswith("/forget "):
        await _handle_forget_command(cmd, client)
        return True

    if cmd == "/approvals" or cmd.startswith("/approvals "):
        await _handle_approvals_command(cmd, client)
        return True

    return False


def _session_picker_rows(rows: Any) -> list[dict[str, Any]]:
    """Normalize Gateway rows without letting one malformed count close the picker."""

    if not isinstance(rows, list):
        return []
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = row.get("key") or row.get("session_key")
        if not key:
            continue
        raw_count = row.get("message_count") or row.get("entry_count") or 0
        try:
            message_count = max(0, int(raw_count))
        except (TypeError, ValueError):
            message_count = 0
        normalized.append(
            {
                "key": str(key),
                "title": str(
                    row.get("display_name")
                    or row.get("displayName")
                    or row.get("title")
                    or ""
                ),
                "status": str(row.get("status") or ""),
                "model": str(row.get("model") or ""),
                "message_count": message_count,
            }
        )
    return normalized


def _print_sessions_table(rows: list[dict[str, Any]]) -> None:
    table = Table(title="Sessions", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("Key")
    table.add_column("Status")
    table.add_column("Model")
    table.add_column("Messages", justify="right")
    for row in rows:
        table.add_row(
            str(row.get("key") or row.get("session_key") or ""),
            str(row.get("status") or ""),
            str(row.get("model") or ""),
            str(row.get("message_count") or row.get("entry_count") or 0),
        )
    console.print(table)


def _print_meta_skills_table(payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("disabled"):
        console.print("[dim]meta-skills are disabled.[/dim]")
        return
    skills = payload.get("skills")
    rows = (
        [skill for skill in skills if isinstance(skill, dict)] if isinstance(skills, list) else []
    )
    if not rows:
        console.print("[dim]No meta-skills available.[/dim]")
        return
    table = Table(title="Meta-skills", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("Name")
    table.add_column("Description")
    for row in rows:
        table.add_row(
            str(row.get("name") or ""),
            str(row.get("description") or ""),
        )
    console.print(table)


def _print_models_table(rows: list[dict[str, Any]]) -> None:
    table = Table(title="Models", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("ID")
    table.add_column("Provider")
    table.add_column("Context", justify="right")
    table.add_column("Capabilities")
    for row in rows:
        table.add_row(
            str(row.get("id") or ""),
            str(row.get("provider") or ""),
            str(row.get("contextWindow") or ""),
            ", ".join(str(v) for v in row.get("capabilities") or []),
        )
    console.print(table)


async def _save_gateway_transcript_command(
    cmd: str, state: ChatSessionState, client: GatewayClientLike
) -> None:
    target = resolve_transcript_target(cmd, state.session_key)
    try:
        history = await session_history_all(client.session_history, state.session_key)
    except GatewayRPCError as exc:
        console.print(error_panel(str(exc), title="Could not save transcript"))
        return
    messages = history.get("messages") or []
    markdown = messages_to_markdown(messages) if isinstance(messages, list) else ""
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


def _image_prompt_and_attachments(command: str) -> tuple[str, list[dict[str, str]]]:
    return _input_bridge.image_prompt_and_attachments(command, output_console=console)


def _gateway_client_is_local(client: object) -> bool:
    return _input_bridge.gateway_client_is_local(client)


def _parse_path_command(command: str) -> tuple[Path, str]:
    return _input_bridge.parse_path_command(command)


def _path_strategy_hint(path: Path) -> str:
    return _input_bridge.path_strategy_hint(path)


def path_prompt_and_attachments(command: str) -> tuple[str, list[dict[str, Any]]]:
    return _input_bridge.path_prompt_and_attachments(command)


def _path_prompt_and_attachments(command: str) -> tuple[str, list[dict[str, Any]]]:
    return path_prompt_and_attachments(command)


def _file_prompt_and_attachments(
    command: str,
    *,
    upload_callable: Any | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    return _input_bridge.file_prompt_and_attachments(command, upload_callable=upload_callable)


async def _async_file_prompt_and_attachments(
    command: str,
    *,
    upload_callable: Any | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    return await _input_bridge.async_file_prompt_and_attachments(
        command, upload_callable=upload_callable
    )


async def _forget_server_approvals(
    client: GatewayClientLike | None, target: str | None = None
) -> bool:
    """Compatibility no-op for the removed intent approval cache."""
    if client is not None:
        try:
            await client.forget_approvals(target)
            return True
        except Exception as exc:
            console.print(
                f"[red]Failed to clear server-side approvals:[/red] {type(exc).__name__}: {exc}"
            )
            console.print(
                "[red]The gateway is likely running older code. "
                "Restart it with[/red] [bold]pkill -f 'opensquilla gateway' "
                "&& opensquilla gateway run[/bold][red] and retry.[/red]"
            )
            return False

    _ = target
    return True


async def _handle_approvals_command(cmd: str, client: GatewayClientLike | None = None) -> None:
    """Diagnostic view / reset for the approval queue."""
    parts = cmd.split()
    arg = parts[1].lower() if len(parts) > 1 else "status"

    if client is None:
        from opensquilla.gateway.approval_queue import get_approval_queue

        queue = get_approval_queue()
        if arg == "reset":
            queue.set_settings(mode="prompt")
            console.print(f"[{ACCENT}]Approval mode reset to prompt.[/]")
            return
        console.print(f"[{ACCENT}]mode:[/] {queue.get_settings().mode}")
        return

    if arg == "reset":
        try:
            await client.set_approval_mode("prompt")
            await client.forget_approvals()
            console.print(f"[{ACCENT}]Approval mode reset to prompt.[/]")
        except Exception as exc:
            console.print(f"[red]Failed to reset approvals:[/red] {type(exc).__name__}: {exc}")
            console.print("[red]Restart the gateway if this is an older build.[/red]")
        return

    try:
        snap = await client.approvals_snapshot()
    except Exception as exc:
        console.print(f"[red]Failed to query approvals:[/red] {type(exc).__name__}: {exc}")
        console.print("[red]Older gateway? Restart it.[/red]")
        return
    console.print(f"[{ACCENT}]mode:[/] {snap.get('mode')}")


async def _handle_forget_command(cmd: str, client: GatewayClientLike | None = None) -> None:
    """Compatibility no-op for removed approval cache."""
    parts = cmd.split(maxsplit=1)
    if len(parts) < 2:
        if await _forget_server_approvals(client):
            console.print(f"[{ACCENT}]Approval cache is inactive.[/]")
        return
    target = parts[1].strip()
    if await _forget_server_approvals(client, target):
        console.print(f"[{ACCENT}]Approval cache is inactive for[/] {target}.")


async def _handle_elevated_command(
    cmd: str,
    state: dict[str, str | None],
    client: GatewayClientLike | None = None,
) -> None:
    """Interpret ``/permissions`` / ``/elevated`` and mutate state in place."""
    parts = cmd.split()
    arg = parts[1].lower() if len(parts) > 1 else "status"
    if arg == "status":
        current = state["mode"] or "off (session override cleared; configured default applies)"
        console.print(f"[{ACCENT}]permissions:[/] {current}")
        return

    known = {"off": None, "on": "on", "bypass": "bypass", "full": "full"}
    if arg not in known:
        console.print(f"[red]Unknown permissions mode:[/red] {arg}")
        console.print("Usage: /permissions on | off | bypass | full | status")
        return

    state["mode"] = known[arg]
    cleared = await _forget_server_approvals(client)
    queue_mode_reset_warning = ""
    if arg == "off":
        if client is not None:
            try:
                await client.set_approval_mode("prompt")
            except Exception as exc:
                queue_mode_reset_warning = (
                    f" [bold red]WARNING: queue mode not reset "
                    f"({type(exc).__name__}: {exc}).[/bold red]"
                )
        else:
            from opensquilla.gateway.approval_queue import get_approval_queue

            get_approval_queue().set_settings(mode="prompt")
    cache_suffix = (
        ""
        if cleared
        else " [bold red]WARNING: legacy approval cache status not confirmed "
        "(see error above).[/bold red]"
    )

    if arg == "off":
        console.print(
            f"[{ACCENT}]permissions: off[/] - exec runs inside the sandbox. "
            f"Queue mode reset to prompt.{cache_suffix}{queue_mode_reset_warning}"
        )
    elif arg == "on":
        console.print(
            f"[yellow]permissions: on[/yellow] - compatibility alias for Safe mode; "
            f"approvals still apply. "
            f"{cache_suffix}"
        )
    elif arg == "bypass":
        console.print(
            f"[red]permissions: bypass[/red] - compatibility alias for Safe mode "
            f"with fewer prompts; host access is not granted.{cache_suffix}"
        )
    else:
        console.print(
            f"[red]permissions: full[/red] - exec on host, approvals skipped, "
            f"sensitive paths bypassed. Trusted operators only.{cache_suffix}"
        )
