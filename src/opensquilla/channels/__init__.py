"""opensquilla.channels — Channel adapter layer.

Adapters: Terminal, WebSocket, Slack, Feishu, Discord, Telegram.
"""

from opensquilla.channels.discord import DiscordChannel
from opensquilla.channels.feishu import FeishuChannel
from opensquilla.channels.manager import ChannelManager
from opensquilla.channels.slack import SlackChannel
from opensquilla.channels.telegram import TelegramChannel, TelegramChannelConfig
from opensquilla.channels.terminal import TerminalChannel
from opensquilla.channels.types import (
    Attachment,
    Channel,
    ChannelHealth,
    ChannelMeta,
    IncomingMessage,
    ManagedChannel,
    OutgoingMessage,
)
from opensquilla.channels.websocket import WebSocketChannel

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
