"""Generated Contract bindings for read-only profile migration methods."""

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

MIGRATION_OPERATIONS_CONTRACT_METHODS: Final = (
    "migration.sources.list",
    "migration.sources.preview",
)


class MigrationOperationsContractError(ValueError):
    """A migration success payload violated its generated Contract."""


_BINDINGS: Final = generated_contract_bindings(
    MIGRATION_OPERATIONS_CONTRACT_METHODS,
    MigrationOperationsContractError,
)


def register_migration_operations_contract[ContextT, ResultT](
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
        unsupported_contract="migration",
    )


__all__ = [
    "MIGRATION_OPERATIONS_CONTRACT_METHODS",
    "MigrationOperationsContractError",
    "register_migration_operations_contract",
]
