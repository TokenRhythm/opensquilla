"""Generated Contract bindings for session subscription and routing controls."""

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

SESSION_CONTROL_CONTRACT_METHODS: Final = (
    "sessions.subscribe",
    "sessions.unsubscribe",
    "sessions.routing.get",
    "sessions.routing.set",
)


class SessionControlContractError(ValueError):
    """A session control success payload violated its generated Contract."""


_BINDINGS: Final = generated_contract_bindings(
    SESSION_CONTROL_CONTRACT_METHODS,
    SessionControlContractError,
)


def register_session_control_contract[ContextT, ResultT](
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
        unsupported_contract="session control",
    )


__all__ = [
    "SESSION_CONTROL_CONTRACT_METHODS",
    "SessionControlContractError",
    "register_session_control_contract",
]
