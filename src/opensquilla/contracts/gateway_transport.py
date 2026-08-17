"""Transport limits shared by OpenSquilla's Python Gateway clients."""

from __future__ import annotations

GATEWAY_CLIENT_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
GATEWAY_CLIENT_MAX_QUEUE = 1
ANSWER_GENERATION_RESET_CAPABILITY = "session.answer_generation_reset.v1"

__all__ = [
    "ANSWER_GENERATION_RESET_CAPABILITY",
    "GATEWAY_CLIENT_MAX_MESSAGE_BYTES",
    "GATEWAY_CLIENT_MAX_QUEUE",
]
