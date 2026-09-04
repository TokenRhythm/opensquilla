"""Semantic input and durable acceptance identity shared by turn use cases."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class IncomingTurnSource:
    caller_kind: str = "web"
    channel_kind: str = "web"
    channel_id: str | None = None
    sender_id: str | None = None
    source_kind: str | None = None
    source_name: str | None = None
    elevated: str | None = None
    run_mode: str | None = None
    is_web: bool = True
    client_message_id: str | None = None
    surface_id: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryCapturePolicy:
    no_memory_capture: bool = False
    input_provenance: Mapping[str, Any] | None = None
    run_kind: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentTurnContext:
    document_id: str
    head_revision_id: str


@dataclass(frozen=True, slots=True)
class PlanAdmissionContext:
    revision_id: str | None = None
    context_revision_id: str | None = None
    run_driver_kind: str | None = None
    run_driver_id: str | None = None
    required_collaboration_mode: str | None = None
    required_collaboration_revision: int | None = None
    expected_collaboration_revision: int | None = None
    expected_active_revision_id: str | None = None
    require_idle: bool = False
    atomic_mode_update: bool = False


@dataclass(frozen=True)
class TurnRequestIdentity:
    source_scope: str
    request_session_key: str
    client_request_id: str
    request_fingerprint: str


async def complete_durable_ingress[T](awaitable: Awaitable[T]) -> T:
    """Finish an ingress commit/activation pair even if its caller is cancelled.

    Once queue admission has been reserved, cancellation must not split the
    durable acceptance transaction from runtime activation.  The inner task is
    shielded and repeated cancellation requests are deferred until that small
    critical section settles.  Returning its result intentionally consumes the
    caller cancellation: a disconnected transport may drop the response, while
    its stable request id makes a later replay safe.
    """

    task = asyncio.ensure_future(awaitable)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()
