"""Generated Contract registration for PendingInputQueue methods."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final

from opensquilla.gateway.adapters._generated_contract_bindings import (
    generated_contract_bindings,
    register_generated_contract_binding,
)
from opensquilla.gateway.adapters.contract_method import (
    ErrorFactory,
    GuestAllowedChecker,
    MethodRegistry,
)

PENDING_INPUT_QUEUE_CONTRACT_METHODS: Final = (
    "sessions.pending_inputs.enqueue",
    "sessions.pending_inputs.list",
    "sessions.pending_inputs.update",
    "sessions.pending_inputs.reorder",
    "sessions.pending_inputs.cancel",
    "sessions.pending_inputs.dispatch",
    "sessions.pending_inputs.steer",
)


class PendingInputQueueContractError(ValueError):
    """A successful queue response violated its generated Contract."""


_BINDINGS: Final = generated_contract_bindings(
    PENDING_INPUT_QUEUE_CONTRACT_METHODS,
    PendingInputQueueContractError,
)


def register_pending_input_queue_contract[ContextT, ResultT](
    registry: MethodRegistry[ContextT],
    method: str,
    implementation: Callable[[Any, ContextT], Awaitable[ResultT]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[ResultT]]:
    return register_generated_contract_binding(
        registry,
        _BINDINGS,
        method,
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


__all__ = [
    "PENDING_INPUT_QUEUE_CONTRACT_METHODS",
    "PendingInputQueueContractError",
    "register_pending_input_queue_contract",
]
