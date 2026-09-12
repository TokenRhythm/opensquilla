"""Generated Contract bindings for Session lifecycle Gateway handlers.

The generated descriptors own method identity, authorization metadata, and
wire validation.  This Adapter observes request drift without changing legacy
malformed-input behavior and fails closed on invalid success payloads.
"""

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

SESSION_LIFECYCLE_CONTRACT_METHODS: Final = (
    "sessions.create",
    "sessions.fork",
    "sessions.forkThroughTurn",
    "sessions.rename",
    "sessions.delete",
)


class SessionLifecycleContractError(ValueError):
    """A Session lifecycle success payload violated its generated Contract."""


_BINDINGS: Final = generated_contract_bindings(
    SESSION_LIFECYCLE_CONTRACT_METHODS,
    SessionLifecycleContractError,
)


def register_session_lifecycle_contract[ContextT, ResultT](
    registry: MethodRegistry[ContextT],
    method: str,
    implementation: Callable[[Any, ContextT], Awaitable[ResultT]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[ResultT]]:
    """Register one generated Contract around one lifecycle implementation."""

    return register_generated_contract_binding(
        registry,
        _BINDINGS,
        method,
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
        unsupported_contract="Session lifecycle",
    )


__all__ = [
    "SESSION_LIFECYCLE_CONTRACT_METHODS",
    "SessionLifecycleContractError",
    "register_session_lifecycle_contract",
]
