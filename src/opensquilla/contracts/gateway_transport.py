"""Transport limits shared by OpenSquilla's Python Gateway clients."""

from __future__ import annotations

GATEWAY_CLIENT_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
GATEWAY_CLIENT_MAX_QUEUE = 1
ANSWER_GENERATION_RESET_CAPABILITY = "session.answer_generation_reset.v1"
TURN_COMMITTED_CAPABILITY = "session.turn_committed.v1"
TURN_COMMITTED_EVENT = "session.event.turn_committed"

__all__ = [
    "ANSWER_GENERATION_RESET_CAPABILITY",
    "GATEWAY_CLIENT_MAX_MESSAGE_BYTES",
    "GATEWAY_CLIENT_MAX_QUEUE",
    "TURN_COMMITTED_CAPABILITY",
    "TURN_COMMITTED_EVENT",
]
