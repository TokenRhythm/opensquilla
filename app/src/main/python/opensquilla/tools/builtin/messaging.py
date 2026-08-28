"""Channel messaging tool: send, react, delete messages via channel adapters."""

from __future__ import annotations

import json
from typing import Any, cast

from opensquilla.tools.registry import tool
from opensquilla.tools.types import ToolError

_VALID_ACTIONS = ("send", "react", "delete")

# Setter-injected channel adapters (gateway boot calls register_channel)
_channels: dict[str, object] = {}


def register_channel(name: str, channel: object) -> None:
    """Inject a channel adapter (called from gateway boot)."""
    _channels[name] = channel


def unregister_channel(name: str) -> None:
    """Remove a channel adapter from messaging tool routing."""
    _channels.pop(name, None)


def _outgoing_metadata(channel: str, target: str, thread_id: str | None) -> dict[str, str]:
    """Build adapter-recognized target metadata for the public message tool."""
    if channel == "telegram":
        metadata = {"chat_id": target}
        if thread_id:
            metadata["thread_id"] = thread_id
        return metadata
    if channel == "matrix":
        return {"room_id": target}
    if channel == "slack":
        return {"thread_ts": thread_id} if thread_id else {}
    if channel == "wecom":
        return {"touser": target}

    metadata = {"recipient": target}
    if thread_id:
        metadata["thread_id"] = thread_id
    return metadata


def _delete_message_id(channel: str, target: str, message_id: str) -> str:
    if channel == "telegram" and "|" not in message_id:
        return f"{target}|{message_id}"
    return message_id


def _reply_to_target(channel: str, target: str, thread_id: str | None) -> str | None:
    if channel in {"telegram", "matrix", "wecom"}:
        return target
    if channel == "slack":
        return thread_id
    return thread_id or target


@tool(
    name="message",
    description=(
        "Send, react to, or delete messages through channel adapters. "
        "Distinct from sessions_send (agent-to-agent); this delivers to users via channels."
    ),
    params={
        "channel": {
            "type": "string",
            "description": 'Channel ID (e.g. "telegram", "discord", "slack")',
        },
        "target": {
            "type": "string",
            "description": "Delivery target (chat ID, user ID, channel name)",
        },
        "text": {
            "type": "string",
            "description": "Message text (required for send)",
        },
        "action": {
            "type": "string",
            "description": "Action: send, react, delete",
            "default": "send",
        },
        "thread_id": {
            "type": "string",
            "description": "Thread/topic ID for threaded replies",
        },
        "message_id": {
            "type": "string",
            "description": "Message ID (required for react and delete)",
        },
        "reaction": {
            "type": "string",
            "description": "Emoji reaction (required for react)",
        },
    },
    required=["channel", "target"],
)
async def message(
    channel: str,
    target: str,
    text: str | None = None,
    action: str = "send",
    thread_id: str | None = None,
    message_id: str | None = None,
    reaction: str | None = None,
) -> str:
    # Validate action
    if action not in _VALID_ACTIONS:
        raise ToolError(f"Invalid action: {action}. Must be send|react|delete")

    # Validate action-specific params
    if action == "send" and not text:
        raise ToolError("'text' is required for send action")
    if action == "react" and (not message_id or not reaction):
        raise ToolError("'message_id' and 'reaction' required for react")
    if action == "delete" and not message_id:
        raise ToolError("'message_id' required for delete")

    # Resolve channel adapter from injected registry
    adapter = _channels.get(channel)
    if adapter is None:
        if not _channels:
            raise ToolError("No channels configured")
        raise ToolError(f"Unknown channel: {channel}. Available: {', '.join(_channels)}")
    channel_adapter = cast(Any, adapter)

    # Dispatch action via OutgoingMessage protocol
    try:
        from opensquilla.channels.types import OutgoingMessage

        if action == "send":
            msg = OutgoingMessage(
                content=text or "",
                metadata=_outgoing_metadata(channel, target, thread_id),
                reply_to=_reply_to_target(channel, target, thread_id),
            )
            await channel_adapter.send(msg)
            return json.dumps(
                {
                    "status": "sent",
                    "channel": channel,
                    "target": target,
                }
            )
        elif action == "react":
            if hasattr(channel_adapter, "react"):
                await channel_adapter.react(target, message_id, reaction)
            else:
                raise ToolError(f"Channel '{channel}' does not support reactions")
            return json.dumps(
                {
                    "status": "reacted",
                    "channel": channel,
                    "target": target,
                    "message_id": message_id,
                    "reaction": reaction,
                }
            )
        else:  # delete
            if hasattr(channel_adapter, "delete"):
                await channel_adapter.delete(_delete_message_id(channel, target, message_id or ""))
            else:
                raise ToolError(f"Channel '{channel}' does not support delete")
            return json.dumps(
                {
                    "status": "deleted",
                    "channel": channel,
                    "target": target,
                    "message_id": message_id,
                }
            )
    except ToolError:
        raise
    except NotImplementedError as exc:
        raise ToolError(f"Delivery failed: {exc}") from exc
    except Exception as exc:
        raise ToolError(f"Delivery failed: {exc}") from exc
