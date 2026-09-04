"""Generated Contract bindings for persisted project workspace Gateway methods."""

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

WORKSPACE_CATALOG_CONTRACT_METHODS: Final = (
    "workspaces.list",
    "workspaces.open",
    "workspaces.update",
    "workspaces.pin",
    "workspaces.remove",
    "workspaces.history.delete",
    "sandbox.path.list",
    "sandbox.path.create-directory",
    "sandbox.path.pick",
)


class WorkspaceCatalogContractError(ValueError):
    """A workspace success payload violated its generated Contract."""


_BINDINGS: Final = generated_contract_bindings(
    WORKSPACE_CATALOG_CONTRACT_METHODS,
    WorkspaceCatalogContractError,
)


def register_workspace_catalog_contract[ContextT, ResultT](
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
        unsupported_contract="workspace",
    )


__all__ = [
    "WORKSPACE_CATALOG_CONTRACT_METHODS",
    "WorkspaceCatalogContractError",
    "register_workspace_catalog_contract",
]
