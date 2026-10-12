"""Fail-closed backend used when auto selection finds no real sandbox."""

from __future__ import annotations

from opensquilla.sandbox.backend.base import Backend
from opensquilla.sandbox.types import SandboxBackendError, SandboxRequest, SandboxResult


class UnavailableBackend(Backend):
    """Represents an enabled sandbox whose native backend is not ready."""

    name = "unavailable"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def available(self) -> bool:
        return False

    async def run(self, request: SandboxRequest) -> SandboxResult:
        _ = request
        raise SandboxBackendError(f"sandbox backend unavailable: {self.reason}")


__all__ = ["UnavailableBackend"]
