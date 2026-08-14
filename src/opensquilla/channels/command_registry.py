"""Channel-side slash-command dispatcher — adapter over the unified registry.

``DEFAULT_COMMAND_REGISTRY`` is derived from
:data:`opensquilla.engine.commands.DEFAULT_REGISTRY` rather than holding its
own hard-coded command table. The ``CommandRegistry.match`` and ``dispatch``
API is preserved for existing callers (``gateway/boot.py``,
``gateway/channel_dispatch.py``).

The channel-side slash-intercept-pre-persist invariant
(``channel_dispatch.py:88-100``) stays where it lives; this module only
provides the dispatch lookup table.
"""

from __future__ import annotations

from typing import Any

from opensquilla.channels.admission import has_verified_channel_admin_stamp
from opensquilla.channels.system_messages import ChannelSystemMessageKey, render_channel_message
from opensquilla.channels.types import OutgoingMessage
from opensquilla.engine.commands import DEFAULT_REGISTRY, ExecutionKind, ParamsFactory, Surface
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.scopes import READ_SCOPE, WRITE_SCOPE
from opensquilla.run_mode import normalize_run_mode


class CommandRegistry:
    """Channel-mode dispatcher.

    Matches inbound channel messages against a registered slash-command set
    and forwards the resulting RPC call to the gateway dispatcher. Lookup
    keys are bare command names (without leading slash, lowercased).
    """

    def __init__(self, commands: dict[str, tuple[str, ParamsFactory]]) -> None:
        self._commands = commands

    @property
    def command_names(self) -> set[str]:
        return set(self._commands)

    def match(self, envelope: RouteEnvelope, content: str) -> tuple[str, str, ParamsFactory] | None:
        head = content.strip().split(maxsplit=1)[0] if content.strip() else ""
        if (
            envelope.source_kind is not SourceKind.CHANNEL
            or not head.startswith("/")
            or head == "/"
        ):
            return None
        bare = head[1:].lower()
        command = self._commands.get(bare)
        return (bare, *command) if command else None

    async def dispatch(
        self,
        *,
        envelope: RouteEnvelope,
        message_content: str,
        rpc_dispatcher: Any,
        context_factory: Any,
        config: Any = None,
    ) -> OutgoingMessage | None:
        match = self.match(envelope, message_content)
        if match is None:
            return None
        name, method, params_factory = match
        params = _channel_command_params(
            name=name,
            params_factory=params_factory,
            envelope=envelope,
            message_content=message_content,
        )
        if params is None:
            return OutgoingMessage(
                content=render_channel_message("command_usage_sandbox", config=config),
                reply_to=envelope.thread_id or envelope.channel_id,
                metadata={"command": name, "method": method, "denied": False},
            )
        res = await rpc_dispatcher.dispatch(
            f"channel-command:{name}",
            method,
            params,
            context_factory(envelope),
        )
        reply_to = envelope.thread_id or envelope.channel_id
        sandbox_reply = _format_channel_sandbox_reply(
            name=name,
            method=method,
            res=res,
            reply_to=reply_to,
            config=config,
        )
        if sandbox_reply is not None:
            return sandbox_reply
        compact_reply = _format_channel_compact_reply(
            name=name,
            method=method,
            res=res,
            reply_to=reply_to,
            config=config,
        )
        if compact_reply is not None:
            return compact_reply
        meta_reply = _format_channel_meta_list_reply(
            name=name,
            method=method,
            res=res,
            reply_to=reply_to,
            config=config,
        )
        if meta_reply is not None:
            return meta_reply
        denied = bool(not res.ok and getattr(res.error, "code", "") == "UNAUTHORIZED")
        reason = "" if res.ok else f": {getattr(res.error, 'message', 'command failed')}"
        if res.ok:
            message_key: ChannelSystemMessageKey = "command_completed"
        elif denied:
            message_key = "command_denied"
        else:
            message_key = "command_failed"
        return OutgoingMessage(
            content=render_channel_message(
                message_key, config=config, name=name, reason=reason
            ),
            reply_to=envelope.thread_id or envelope.channel_id,
            metadata={"command": name, "method": method, "denied": denied},
        )


def _channel_command_params(
    *,
    name: str,
    params_factory: ParamsFactory,
    envelope: RouteEnvelope,
    message_content: str,
) -> dict[str, Any] | None:
    params = params_factory(envelope)
    if name != "sandbox":
        return params
    parts = message_content.strip().split()
    if len(parts) < 2:
        return None
    try:
        run_mode = normalize_run_mode(parts[1]).value
    except ValueError:
        return None
    return {**params, "runMode": run_mode}


def _format_channel_sandbox_reply(
    *,
    name: str,
    method: str,
    res: Any,
    reply_to: str | None,
    config: Any = None,
) -> OutgoingMessage | None:
    if name != "sandbox" or method != "sandbox.run_context.set":
        return None
    denied = bool(not res.ok and getattr(res.error, "code", "") == "UNAUTHORIZED")
    metadata = {"command": name, "method": method, "denied": denied}
    if not res.ok:
        error_message = getattr(res.error, "message", "command failed")
        message_key: ChannelSystemMessageKey = (
            "command_sandbox_denied" if denied else "command_sandbox_failed"
        )
        return OutgoingMessage(
            content=render_channel_message(
                message_key, config=config, reason=error_message
            ),
            reply_to=reply_to,
            metadata=metadata,
        )
    payload = res.payload if isinstance(res.payload, dict) else {}
    run_mode = str(payload.get("runMode") or "").strip()
    if run_mode == "safe":
        label = render_channel_message("command_sandbox_safe", config=config)
    elif run_mode == "full":
        label = render_channel_message("command_sandbox_full", config=config)
    else:
        label = run_mode or render_channel_message("command_sandbox_unknown_mode", config=config)
    return OutgoingMessage(
        content=render_channel_message("command_sandbox_updated", config=config, mode=label),
        reply_to=reply_to,
        metadata=metadata,
    )


def _format_channel_compact_reply(
    *,
    name: str,
    method: str,
    res: Any,
    reply_to: str | None,
    config: Any = None,
) -> OutgoingMessage | None:
    if name != "compact" or method != "sessions.contextCompact":
        return None
    denied = bool(not res.ok and getattr(res.error, "code", "") == "UNAUTHORIZED")
    metadata = {"command": name, "method": method, "denied": denied}
    if not res.ok:
        error_message = getattr(res.error, "message", "command failed")
        message_key: ChannelSystemMessageKey = (
            "command_compact_denied" if denied else "command_compact_failed"
        )
        return OutgoingMessage(
            content=render_channel_message(
                message_key, config=config, reason=error_message
            ),
            reply_to=reply_to,
            metadata=metadata,
        )
    payload = res.payload if isinstance(res.payload, dict) else {}
    status = str(payload.get("status") or "").lower()
    compacted = bool(payload.get("compacted"))
    if compacted or status == "completed":
        return OutgoingMessage(
            content=render_channel_message("command_compact_completed", config=config),
            reply_to=reply_to,
            metadata=metadata,
        )
    if status == "skipped" or payload.get("compacted") is False:
        return OutgoingMessage(
            content=render_channel_message("command_compact_skipped", config=config),
            reply_to=reply_to,
            metadata=metadata,
        )
    return OutgoingMessage(
        content=render_channel_message("command_completed", config=config, name="compact"),
        reply_to=reply_to,
        metadata=metadata,
    )


def _format_channel_meta_list_reply(
    *,
    name: str,
    method: str,
    res: Any,
    reply_to: str | None,
    config: Any = None,
) -> OutgoingMessage | None:
    if name != "meta" or method != "meta.list":
        return None
    denied = bool(not res.ok and getattr(res.error, "code", "") == "UNAUTHORIZED")
    metadata = {"command": name, "method": method, "denied": denied}
    if not res.ok:
        error_message = getattr(res.error, "message", "command failed")
        message_key: ChannelSystemMessageKey = (
            "command_meta_denied" if denied else "command_meta_failed"
        )
        return OutgoingMessage(
            content=render_channel_message(
                message_key, config=config, reason=error_message
            ),
            reply_to=reply_to,
            metadata=metadata,
        )
    payload = res.payload if isinstance(res.payload, dict) else {}
    skills = payload.get("skills") if isinstance(payload.get("skills"), list) else []
    if payload.get("disabled") or not skills:
        return OutgoingMessage(
            content=render_channel_message("command_meta_empty", config=config),
            reply_to=reply_to,
            metadata=metadata,
        )
    lines = [render_channel_message("command_meta_heading", config=config)]
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        skill_name = str(skill.get("name") or "")
        description = skill.get("description")
        suffix = f" — {description}" if description else ""
        lines.append(f"- {skill_name}{suffix}")
    return OutgoingMessage(
        content="\n".join(lines),
        reply_to=reply_to,
        metadata=metadata,
    )


def build_channel_rpc_context(
    envelope: RouteEnvelope,
    *,
    gateway_config: Any,
    **handles: Any,
) -> RpcContext:
    sender_id = envelope.sender_id
    # The dispatcher has already checked authenticated ingress and stamped
    # the route. Never recompute operator standing from a raw sender ID here.
    is_operator = has_verified_channel_admin_stamp(envelope)
    principal = Principal(
        role="operator" if is_operator else "viewer",
        scopes=frozenset({READ_SCOPE, WRITE_SCOPE}) if is_operator else frozenset(),
        is_owner=is_operator,
        authenticated=True,
    )
    return RpcContext(
        conn_id=f"channel:{envelope.source_name}:{sender_id or 'unknown'}",
        principal=principal,
        config=gateway_config,
        originating_envelope=envelope,
        **handles,
    )


def _build_default_command_table() -> dict[str, tuple[str, ParamsFactory]]:
    """Project the unified registry's CHANNEL surface into the dispatcher table.

    Inserts both the canonical command name and any declared aliases under
    their bare (slash-stripped, lowercase) form so an alias advertised via
    ``commands.list_for_surface`` actually dispatches when typed by a
    channel user. Skips ``CommandDef`` entries that lack RPC metadata —
    channels require a method + params factory to dispatch.
    """
    table: dict[str, tuple[str, ParamsFactory]] = {}
    for cmd in DEFAULT_REGISTRY.for_surface(Surface.CHANNEL):
        execution = cmd.execution_for(Surface.CHANNEL)
        if (
            execution is None
            or execution.kind is not ExecutionKind.RPC
            or execution.rpc_method is None
            or execution.rpc_params is None
        ):
            continue
        for word in cmd.words():
            table[word.lstrip("/").lower()] = (execution.rpc_method, execution.rpc_params)
    return table


DEFAULT_COMMAND_REGISTRY = CommandRegistry(_build_default_command_table())
