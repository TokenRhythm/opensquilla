"""Gateway slash-command adapter for the chat REPL backend.

This module owns gateway-mode slash command dispatch. It is intentionally
independent from raw frontend and chat application objects: callers pass
typed session state, a gateway client, and an optional TUI output handle.
"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from rich.table import Table

import opensquilla.cli.tui.adapters.input_bridge as _input_bridge
from opensquilla.cli.chat.session_state import ChatSessionState, messages_to_markdown
from opensquilla.cli.chat.turn import TurnResult
from opensquilla.cli.gateway_client import GatewayRPCError, session_history_all
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


class GatewayStreamResponse(Protocol):
    async def __call__(
        self,
        client: GatewayClientLike,
        session_key: str,
        message: str,
        elevated_state: dict[str, str | None] | None = None,
        attachments: list[dict] | None = None,
        *,
        tui_output: TuiOutputHandle | None = None,
    ) -> TurnResult: ...


async def stream_response_gateway(
    client: GatewayClientLike,
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
        name = parts[1].strip() if len(parts) > 1 else ""
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
            f"[yellow]permissions: on[/yellow] - legacy alias for Managed Execution; "
            f"approvals still apply. "
            f"{cache_suffix}"
        )
    elif arg == "bypass":
        console.print(
            f"[red]permissions: bypass[/red] - legacy alias for Managed Execution "
            f"with fewer prompts; host access is not granted.{cache_suffix}"
        )
    else:
        console.print(
            f"[red]permissions: full[/red] - exec on host, approvals skipped, "
            f"sensitive paths bypassed. Trusted operators only.{cache_suffix}"
        )
