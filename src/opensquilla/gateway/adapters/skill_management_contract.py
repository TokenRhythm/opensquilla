"""Generated Contract registration for SkillManagement methods."""

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

SKILL_MANAGEMENT_CONTRACT_METHODS: Final = (
    "skills.reload",
    "skills.install",
    "skills.install.cancel",
    "skills.deps.install",
    "skills.uninstall",
)


class SkillManagementContractError(ValueError):
    """A successful SkillManagement response violated its generated Contract."""


_BINDINGS: Final = generated_contract_bindings(
    SKILL_MANAGEMENT_CONTRACT_METHODS,
    SkillManagementContractError,
)


def register_skill_management_contract[ContextT, ResultT](
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
    "SKILL_MANAGEMENT_CONTRACT_METHODS",
    "SkillManagementContractError",
    "register_skill_management_contract",
]
