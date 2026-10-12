"""Bound ordinary process output while preserving authenticated helper status."""

from __future__ import annotations

import json
import secrets

PIPE_READ_SIZE = 65_536
HELPER_ERROR_MESSAGE_LIMIT = 4096
HELPER_CONTROL_FRAME_LIMIT = 65_536
HELPER_ERROR_PREFIX = b"OPENSQUILLA_WINDOWS_DEFAULT_HELPER_ERROR "
HELPER_TIMEOUT_PREFIX = b"\nOPENSQUILLA_WINDOWS_DEFAULT_HELPER_TIMEOUT "


class BoundedOutput:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    def feed(self, chunk: bytes | bytearray) -> None:
        remaining = self.limit - len(self.data)
        self.data.extend(chunk[:remaining])
        self.truncated |= len(chunk) > remaining


class HelperStderr(BoundedOutput):
    def __init__(self, limit: int, nonce: str) -> None:
        super().__init__(limit)
        self.nonce = nonce
        self.pending = bytearray()
        self.timed_out = False
        self.helper_error: str | None = None

    def feed(self, chunk: bytes | bytearray) -> None:
        self.pending.extend(chunk)
        prefixes = (HELPER_ERROR_PREFIX, HELPER_TIMEOUT_PREFIX)
        while self.pending:
            positions = [(self.pending.find(prefix), prefix) for prefix in prefixes]
            positions = [(index, prefix) for index, prefix in positions if index >= 0]
            if not positions:
                keep = max(map(len, prefixes)) - 1
                self._retain_prefix(max(0, len(self.pending) - keep))
                return
            index, prefix = min(positions)
            self._retain_prefix(index)
            end = self.pending.find(b"\n", len(prefix))
            if end < 0 and len(self.pending) <= HELPER_CONTROL_FRAME_LIMIT:
                return
            if end < 0 or end + 1 > HELPER_CONTROL_FRAME_LIMIT:
                # An oversized candidate is user output. Continue searching so
                # a later genuine frame cannot be hidden by an unbounded line.
                self._retain_prefix(1)
                continue
            frame = bytes(self.pending[:end + 1])
            try:
                payload = json.loads(frame[len(prefix):])
            except (ValueError, RecursionError):
                payload = None
            nonce = payload.get("nonce") if isinstance(payload, dict) else None
            trusted = (
                bool(self.nonce) and isinstance(nonce, str) and nonce.isascii()
                and secrets.compare_digest(nonce, self.nonce)
            )
            if trusted and prefix == HELPER_TIMEOUT_PREFIX and payload.get("timed_out") is True:
                self.timed_out = True
                del self.pending[:end + 1]
            else:
                if trusted and prefix == HELPER_ERROR_PREFIX:
                    message = payload.get("message")
                    if isinstance(message, str) and message.strip() and self.helper_error is None:
                        self.helper_error = message.strip()
                self._retain_prefix(end + 1)

    def _retain_prefix(self, count: int) -> None:
        super().feed(self.pending[:count])
        del self.pending[:count]

    def finish(self) -> None:
        self._retain_prefix(len(self.pending))
