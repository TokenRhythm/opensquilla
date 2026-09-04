"""Transport-neutral turn admission, cancellation, and steering use cases.

The Gateway owns v4 aliases, authentication, guest projection, and ``RpcContext``.
This Module supplies the single application entry point used by the canonical
WebChat methods and their session-oriented compatibility names.  The mature
durable ingress and runtime cancellation state machines remain behind the Port.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

from opensquilla.session_key import canonicalize_session_key

type TurnAdmissionSurface = Literal["webchat", "session"]
type TurnSteerMode = Literal["durable", "legacy"]


@dataclass(frozen=True, slots=True)
class PendingInputGuard:
    pending_input_id: str
    request_fingerprint: str
    expected_revision: int
    source_scope: str | None = None


@dataclass(frozen=True, slots=True)
class AdmitTurn:
    session_key: str
    message: str
    surface: TurnAdmissionSurface
    attributes: Mapping[str, Any]
    pending_input: PendingInputGuard | None = None


@dataclass(frozen=True, slots=True)
class CancelTurn:
    session_key: str
    surface: TurnAdmissionSurface
    task_id: str | None
    task_scoped: bool
    source: str | None
    attributes: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SteerTurn:
    session_key: str
    message: str
    mode: TurnSteerMode
    attributes: Mapping[str, Any]
    pending_input: PendingInputGuard | None = None


class TurnAdmissionRuntimePort(Protocol):
    """Existing durable ingress/runtime state machines, hidden from callers."""

    async def admit(self, command: AdmitTurn) -> Mapping[str, Any]: ...

    async def cancel(self, command: CancelTurn) -> Mapping[str, Any]: ...

    async def steer(self, command: SteerTurn) -> Mapping[str, Any]: ...


class TurnAdmission:
    """One application implementation for canonical and legacy turn commands."""

    def __init__(self, runtime: TurnAdmissionRuntimePort) -> None:
        self._runtime = runtime

    async def admit(self, command: AdmitTurn) -> Mapping[str, Any]:
        key = self._session_key(command.session_key)
        if not isinstance(command.message, str):
            raise ValueError("message must be a string")
        self._validate_pending_guard(command.pending_input)
        return await self._runtime.admit(replace(command, session_key=key))

    async def cancel(self, command: CancelTurn) -> Mapping[str, Any]:
        key = self._session_key(command.session_key)
        task_id = command.task_id.strip() if isinstance(command.task_id, str) else None
        if task_id == "":
            task_id = None
        if command.task_scoped and task_id is None:
            # Never broaden an exact-task cancellation into a session-wide one.
            return {"aborted": False, "key": key, "reason": "task_id_required"}
        return await self._runtime.cancel(
            replace(command, session_key=key, task_id=task_id)
        )

    async def steer(self, command: SteerTurn) -> Mapping[str, Any]:
        key = self._session_key(command.session_key)
        if not isinstance(command.message, str):
            raise ValueError("message must be a string")
        if not command.message.strip():
            raise ValueError("message must not be blank")
        self._validate_pending_guard(command.pending_input, require_source=True)
        return await self._runtime.steer(replace(command, session_key=key))

    @staticmethod
    def _validate_pending_guard(
        guard: PendingInputGuard | None,
        *,
        require_source: bool = False,
    ) -> None:
        if guard is None:
            return
        if not guard.pending_input_id.strip() or not guard.request_fingerprint.strip():
            raise ValueError("pending input identity must be non-empty")
        if guard.expected_revision < 1:
            raise ValueError("pending input revision must be positive")
        if require_source and not (guard.source_scope or "").strip():
            raise ValueError("pending input source scope must be non-empty")

    @staticmethod
    def _session_key(value: str) -> str:
        key = canonicalize_session_key(value)
        if not key:
            raise ValueError("session_key must be non-empty")
        return key


__all__ = [
    "AdmitTurn",
    "CancelTurn",
    "PendingInputGuard",
    "SteerTurn",
    "TurnAdmission",
    "TurnAdmissionRuntimePort",
    "TurnAdmissionSurface",
    "TurnSteerMode",
]
