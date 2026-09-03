"""Transport-neutral manual-compaction use case.

The public Module exposes business commands instead of v4 method names.  The
Gateway Adapter terminates ``RpcContext`` and owns legacy wire aliases, while
the runtime Port preserves the existing compaction state machine. Session
reset has its own coordinator and fine-grained Ports in ``session_reset``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

from opensquilla.session_key import canonicalize_session_key


@dataclass(frozen=True, slots=True)
class CompactSession:
    session_key: str
    wait: bool = True
    context_window_tokens: int | None = None
    instructions: str | None = None


class SessionMaintenanceRuntimePort(Protocol):
    """Runtime boundary for the existing compaction state machine."""

    async def compact(self, command: CompactSession) -> Mapping[str, Any]: ...


class SessionMaintenance:
    """Stable application interface shared by canonical and legacy wire names."""

    def __init__(self, runtime: SessionMaintenanceRuntimePort) -> None:
        self._runtime = runtime

    async def compact(self, command: CompactSession) -> Mapping[str, Any]:
        key = canonicalize_session_key(command.session_key)
        if not key:
            raise ValueError("session_key must be non-empty")
        if command.context_window_tokens is not None and command.context_window_tokens <= 0:
            raise ValueError("context_window_tokens must be positive")
        if command.instructions is not None and not isinstance(command.instructions, str):
            raise ValueError("instructions must be a string when provided")
        return await self._runtime.compact(replace(command, session_key=key))


__all__ = [
    "CompactSession",
    "SessionMaintenance",
    "SessionMaintenanceRuntimePort",
]
