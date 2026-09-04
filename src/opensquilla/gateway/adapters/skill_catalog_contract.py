"""Generated Contract registration for SkillCatalog read methods."""

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

SKILL_CATALOG_CONTRACT_METHODS: Final = (
    "skills.list",
    "skills.get",
    "skills.search",
)


class SkillCatalogContractError(ValueError):
    """A successful SkillCatalog response violated its generated Contract."""


_BINDINGS: Final = generated_contract_bindings(
    SKILL_CATALOG_CONTRACT_METHODS,
    SkillCatalogContractError,
)


def register_skill_catalog_contract[ContextT, ResultT](
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
    "SKILL_CATALOG_CONTRACT_METHODS",
    "SkillCatalogContractError",
    "register_skill_catalog_contract",
]
