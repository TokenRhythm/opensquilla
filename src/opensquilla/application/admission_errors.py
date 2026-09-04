"""Admission failures retain whether a durable input was accepted."""

from __future__ import annotations

from collections.abc import Mapping


class AdmissionError(RuntimeError):
    def __init__(
        self,
        kind: str,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
        retryable: bool | None = None,
        retry_after_ms: int | None = None,
        accepted: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.details = details
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms
        self.accepted = accepted


class AdmissionUnavailableError(RuntimeError):
    """An input cannot be accepted with the available durable services."""


class AdmissionQueueFullError(RuntimeError):
    def __init__(self, session_key: str, max_pending: int) -> None:
        self.session_key = session_key
        self.max_pending = max_pending
        super().__init__("The pending task queue is full")


class AdmissionShuttingDownError(RuntimeError):
    def __init__(self, session_key: str) -> None:
        self.session_key = session_key
        super().__init__("The runtime is shutting down")
