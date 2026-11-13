"""Generated Contract bindings for session JEV decisions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final, cast

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

JEV_CONTRACT_METHODS: Final = ("jev.get", "jev.configure", "jev.respond", "jev.preview")


class JevContractError(ValueError):
    """A JEV success payload violated its generated Contract."""


_BINDINGS: Final = generated_contract_bindings(JEV_CONTRACT_METHODS, JevContractError)


def validate_jev_params(method: str, params: Any) -> dict[str, Any]:
    return cast(dict[str, Any], validate_generated_contract_params(method, params))


def register_jev_contract[ContextT, ResultT](
    registry: MethodRegistry[ContextT],
    method: str,
    implementation: Callable[[Any, ContextT], Awaitable[ResultT]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[ResultT]]:
    return cast(
        Callable[[Any, ContextT], Awaitable[ResultT]],
        register_generated_contract_binding(
            registry, _BINDINGS, method, implementation,
            internal_error=internal_error, guest_allowed_checker=guest_allowed_checker,
            unsupported_contract="JEV routing",
        ),
    )
