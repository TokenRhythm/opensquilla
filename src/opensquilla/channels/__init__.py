"""opensquilla.channels — Channel adapter layer.

Adapters: Terminal, WebSocket, Slack, Feishu, Discord, Telegram.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from opensquilla.channels.types import (
    Attachment,
    Channel,
    ChannelHealth,
    ChannelMeta,
    IncomingMessage,
    ManagedChannel,
    OutgoingMessage,
)

if TYPE_CHECKING:
    from opensquilla.channels.discord import DiscordChannel
    from opensquilla.channels.feishu import FeishuChannel
    from opensquilla.channels.manager import ChannelManager
    from opensquilla.channels.slack import SlackChannel
    from opensquilla.channels.telegram import TelegramChannel, TelegramChannelConfig
    from opensquilla.channels.terminal import TerminalChannel
    from opensquilla.channels.websocket import WebSocketChannel

# Storage and protocol users must not load every adapter or boot the gateway's
# import graph. Keep the public facade, resolving implementations only on use.
_LAZY_EXPORTS = {
    "ChannelManager": "manager",
    "TerminalChannel": "terminal",
    "WebSocketChannel": "websocket",
    "SlackChannel": "slack",
    "FeishuChannel": "feishu",
    "DiscordChannel": "discord",
    "TelegramChannel": "telegram",
    "TelegramChannelConfig": "telegram",
}
for _export in _LAZY_EXPORTS:
    globals().pop(_export, None)
del _export


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    resolved = getattr(import_module(f".{module_name}", __name__), name)
    globals()[name] = resolved
    return resolved


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

__all__ = [
    # Protocol + types
    "Channel",
    "ManagedChannel",
    "ChannelHealth",
    "ChannelMeta",
    "IncomingMessage",
    "OutgoingMessage",
    "Attachment",
    # Manager
    "ChannelManager",
    # Adapters
    "TerminalChannel",
    "WebSocketChannel",
    "SlackChannel",
    "FeishuChannel",
    "DiscordChannel",
    "TelegramChannel",
    "TelegramChannelConfig",
]
