"""Safe canonical context projection for the OpenTUI host."""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from opensquilla.cli.tui.backend.render_summary import sanitize_terminal_text
from opensquilla.cli.tui.opentui.messages import ContextUpdate, ModelRoutingState
from opensquilla.engine.commands import Surface

_MAX_CONTEXT_FIELD = 120


def _display_text(value: object, *, limit: int = _MAX_CONTEXT_FIELD) -> str:
    if value is None:
        return ""
    clean = sanitize_terminal_text(str(value)).replace("\r", " ").replace("\n", " ")
    return " ".join(clean.split()).strip()[:limit]


def _workspace_name(value: object) -> str:
    """Return a useful workspace label without exposing its absolute path."""

    clean = _display_text(value)
    if not clean:
        return ""
    parts = [part for part in re.split(r"[\\/]+", clean.rstrip("\\/")) if part]
    return parts[-1][:72] if parts else ""


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _agent(snapshot: dict[str, Any], session: dict[str, Any]) -> dict[str, str | None]:
    identity = _mapping(snapshot.get("agent_identity"))
    agent_id = _display_text(
        identity.get("agent_id")
        or identity.get("id")
        or session.get("agent_id")
        or "main",
        limit=80,
    )
    name = _display_text(identity.get("name") or agent_id, limit=80) or agent_id
    emoji = _display_text(identity.get("emoji"), limit=16) or None
    return {"id": agent_id or "main", "name": name or "main", "emoji": emoji}


def _queue_label(snapshot: dict[str, Any]) -> str:
    queue = _mapping(snapshot.get("queue"))

    def _count(*keys: str) -> int:
        for key in keys:
            value = queue.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
        return 0

    running = _count("running_count", "runningCount")
    queued = _count("queued_count", "queuedCount")
    if not running and not queued:
        return "idle"
    return f"{running} running · {queued} queued"


def context_update_from_bootstrap(
    snapshot: dict[str, Any] | None,
    *,
    surface: Surface,
    model: str | None = None,
    session_id: str | None = None,
    workspace_label: str | None = None,
    permission: str | None = None,
) -> ContextUpdate:
    """Build one complete, sanitized host context snapshot.

    ``session_id`` is used only to distinguish a resumed unnamed session from
    a brand-new one; the opaque identifier itself is never rendered.
    """

    data = snapshot if isinstance(snapshot, dict) else {}
    session = _mapping(data.get("session"))
    task = _display_text(
        session.get("displayName")
        or session.get("display_name")
        or session.get("title")
    )
    if not task:
        task = "Session" if session_id or session.get("session_key") else "New session"
    resolved_model = _display_text(
        model or session.get("effective_model") or session.get("model") or "default"
    )
    gateway_mode = surface is Surface.CLI_GATEWAY
    return ContextUpdate(
        agent=_agent(data, session),
        task=task,
        surface="Web + TUI" if gateway_mode else "TUI",
        gateway="connected" if gateway_mode else "isolated",
        model=resolved_model or "default",
        permission=_display_text(permission or "normal", limit=32) or "normal",
        workspace=_workspace_name(workspace_label or session.get("workspace")),
        queue=_queue_label(data),
    )


async def send_context_update(
    output: object | None,
    snapshot: dict[str, Any] | None,
    *,
    surface: Surface = Surface.CLI_GATEWAY,
    model: str | None = None,
    session_id: str | None = None,
    workspace_label: str | None = None,
    permission: str | None = None,
) -> None:
    """Send a complete context snapshot when the output supports host frames."""

    send = getattr(output, "send_message", None)
    if not callable(send):
        return
    update = context_update_from_bootstrap(
        snapshot,
        surface=surface,
        model=model,
        session_id=session_id,
        workspace_label=workspace_label,
        permission=permission,
    )
    await send("context.update", asdict(update))
    runtime = _mapping((snapshot or {}).get("runtime"))
    routing = _mapping(runtime.get("model_routing"))
    if routing:
        await send_model_routing_state(output, routing)


async def send_model_routing_state(
    output: object | None,
    snapshot: dict[str, Any],
) -> None:
    """Send one canonical model-strategy snapshot to an IPC-capable host."""

    send = getattr(output, "send_message", None)
    if not callable(send):
        return
    update = ModelRoutingState(
        mode=_display_text(snapshot.get("mode") or "direct", limit=16) or "direct",
        router_enabled=bool(snapshot.get("router_enabled")),
        ensemble_enabled=bool(snapshot.get("ensemble_enabled")),
        selection_mode=_display_text(snapshot.get("selection_mode"), limit=48),
        rollout_phase=_display_text(snapshot.get("rollout_phase") or "observe", limit=16)
        or "observe",
        applies_to=_display_text(
            snapshot.get("applies_to") or "next_accepted_turn",
            limit=32,
        )
        or "next_accepted_turn",
        busy=bool(snapshot.get("busy")),
    )
    await send("model.routing.state", asdict(update))


async def send_context_patch(output: object | None, **fields: object) -> None:
    """Send a sanitized partial context update without clearing other fields."""

    send = getattr(output, "send_message", None)
    if not callable(send):
        return
    payload = {
        key: _workspace_name(value) if key == "workspace" else _display_text(value)
        for key, value in fields.items()
        if value is not None
    }
    if payload:
        await send("context.update", payload)


__all__ = [
    "context_update_from_bootstrap",
    "send_context_patch",
    "send_context_update",
    "send_model_routing_state",
]
