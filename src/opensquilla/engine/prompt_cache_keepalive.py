"""Storage-free handoff for session prompt-cache keepalive candidates.

The engine captures only a provider-visible prefix that has already appeared
at the start of a successful real request.  The gateway may later replay that
prefix as an auxiliary provider call; the candidate itself never mutates
session history and contains no persistence behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opensquilla.provider import ChatConfig, Message


@dataclass(frozen=True, slots=True)
class PromptCacheKeepaliveCandidate:
    """Immutable-enough snapshot of one proven provider request prefix."""

    session_key: str
    provider: Any
    provider_id: str
    model: str
    messages: tuple[Message, ...]
    tools: tuple[Any, ...]
    config: ChatConfig


__all__ = ["PromptCacheKeepaliveCandidate"]
