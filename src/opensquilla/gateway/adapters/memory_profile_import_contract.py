"""Generated Contract bindings for model-assisted profile import methods."""

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

MEMORY_PROFILE_IMPORT_CONTRACT_METHODS: Final = (
    "memory.import.info",
    "memory.import.start",
    "memory.import.status",
    "memory.import.retry",
    "memory.import.cancel",
    "memory.import.apply",
    "memory.import.undo",
    "memory.import.discard",
)


class MemoryProfileImportContractError(ValueError):
    """A profile import success payload violated its generated Contract."""


_BINDINGS: Final = generated_contract_bindings(
    MEMORY_PROFILE_IMPORT_CONTRACT_METHODS,
    MemoryProfileImportContractError,
)


def register_memory_profile_import_contract[ContextT, ResultT](
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
        unsupported_contract="profile import",
    )


__all__ = [
    "MEMORY_PROFILE_IMPORT_CONTRACT_METHODS",
    "MemoryProfileImportContractError",
    "register_memory_profile_import_contract",
]
