"""Generated Contract bindings for Platform configuration Gateway methods."""

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

PLATFORM_CONFIGURATION_CONTRACT_METHODS: Final = (
    "config.get",
    "config.effective",
    "config.patch",
    "config.patch.safe",
    "models.list",
    "providers.status",
    "models.routing.get",
    "models.routing.set",
)


class PlatformConfigurationContractError(ValueError):
    """A Platform configuration success payload violated its generated Contract."""


_BINDINGS: Final = generated_contract_bindings(
    PLATFORM_CONFIGURATION_CONTRACT_METHODS,
    PlatformConfigurationContractError,
)


def register_platform_configuration_contract[ContextT, ResultT](
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
        unsupported_contract="Platform configuration",
    )


__all__ = [
    "PLATFORM_CONFIGURATION_CONTRACT_METHODS",
    "PlatformConfigurationContractError",
    "register_platform_configuration_contract",
]
