"""Transport limits shared by OpenSquilla's Python Gateway clients."""

from __future__ import annotations

GATEWAY_CLIENT_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
GATEWAY_CLIENT_MAX_QUEUE = 1

__all__ = ["GATEWAY_CLIENT_MAX_MESSAGE_BYTES", "GATEWAY_CLIENT_MAX_QUEUE"]
