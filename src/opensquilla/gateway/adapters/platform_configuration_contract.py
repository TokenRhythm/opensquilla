"""Generated Contract bindings for Platform configuration Gateway methods."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any, Final

from opensquilla.gateway.adapters._generated_contract_bindings import (
    generated_contract_bindings,
    register_generated_contract_binding,
    validate_generated_contract_params,
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
    "models.capacity.resolve",
    "providers.status",
    "models.routing.get",
    "models.routing.set",
    "models.routing.resetRecommended",
    "models.recommendations.get",
    "models.recommendations.apply",
    "models.recommendations.acknowledge",
)


class PlatformConfigurationContractError(ValueError):
    """A Platform configuration success payload violated its generated Contract."""


_BINDINGS: Final = generated_contract_bindings(
    PLATFORM_CONFIGURATION_CONTRACT_METHODS,
    PlatformConfigurationContractError,
)


def validate_reset_recommended_params(params: Any) -> dict[str, Any]:
    return validate_generated_contract_params(
        "models.routing.resetRecommended", params, strict=True,
    )


def validate_recommendation_params(method: str, params: Any) -> dict[str, Any]:
    # Generated scalar fields are already strict. Enum fields accept their
    # JSON strings here and become plain domain strings at this boundary.
    values = validate_generated_contract_params(method, params)
    return {key: value.value if isinstance(value, Enum) else value for key, value in values.items()}


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
    "validate_reset_recommended_params",
    "validate_recommendation_params",
]
