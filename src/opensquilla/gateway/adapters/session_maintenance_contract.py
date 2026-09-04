"""Generated Contract bindings for SessionMaintenance Gateway methods."""

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

SESSION_MAINTENANCE_CONTRACT_METHODS: Final = (
    "sessions.reset",
    "sessions.contextCompact",
    "sessions.compact",
)


class SessionMaintenanceContractError(ValueError):
    """A successful maintenance payload violated its generated Contract."""


_BINDINGS: Final = generated_contract_bindings(
    SESSION_MAINTENANCE_CONTRACT_METHODS,
    SessionMaintenanceContractError,
)


def register_session_maintenance_contract[ContextT, ResultT](
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
    "SESSION_MAINTENANCE_CONTRACT_METHODS",
    "SessionMaintenanceContractError",
    "register_session_maintenance_contract",
]
