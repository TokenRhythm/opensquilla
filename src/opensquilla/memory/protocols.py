"""Explicit capability surfaces used by memory runtime integrations."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from opensquilla.provider.types import ChatConfig, Message
from opensquilla.tool_boundary import ToolCall, ToolResult


class ProviderCompletionCapability(Protocol):
    async def complete(self, *, messages: list[Message], max_tokens: int) -> Any: ...


class ProviderChatCapability(Protocol):
    def chat(
        self,
        messages: list[Message],
        tools: Any = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]: ...


MemoryProviderCapability = ProviderCompletionCapability | ProviderChatCapability
MemoryToolHandler = Callable[[ToolCall], Awaitable[ToolResult]]
