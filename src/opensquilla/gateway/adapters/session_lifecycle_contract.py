"""Generated Contract bindings for Session lifecycle Gateway handlers.

The generated descriptors own method identity, authorization metadata, and
wire validation.  This Adapter observes request drift without changing legacy
malformed-input behavior and fails closed on invalid success payloads.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final, cast

from pydantic import ValidationError

from opensquilla.contracts.generated.v4.gateway_contract_registry import (
    GATEWAY_METHOD_CONTRACTS,
    GatewayMethodContract,
)
from opensquilla.gateway.adapters.contract_method import (
    ErrorFactory,
    GatewayContractBinding,
    GuestAllowedChecker,
    MethodRegistry,
    register_gateway_contract_method,
)

SESSION_LIFECYCLE_CONTRACT_METHODS: Final = (
    "sessions.create",
    "sessions.fork",
    "sessions.forkThroughTurn",
    "sessions.rename",
    "sessions.delete",
)


class SessionLifecycleContractError(ValueError):
    """A Session lifecycle success payload violated its generated Contract."""


def _validation_errors(exc: ValidationError) -> tuple[dict[str, Any], ...]:
    return tuple(
        cast(
            list[dict[str, Any]],
            exc.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            ),
        )
    )


def _params_observer(
    method: str,
    descriptor: GatewayMethodContract,
) -> Callable[[Any], tuple[dict[str, Any], ...]]:
    def observe(params: Any) -> tuple[dict[str, Any], ...]:
        try:
            descriptor.request_model.model_validate(
                {
                    "type": "req",
                    "id": "contract-observer",
                    "method": method,
                    "params": params,
                }
            )
        except ValidationError as exc:
            return _validation_errors(exc)
        return ()

    return observe


def _result_validator(
    method: str,
    descriptor: GatewayMethodContract,
) -> Callable[[Any], Any]:
    def validate(result: Any) -> Any:
        try:
            descriptor.result_model.model_validate(result)
        except ValidationError as exc:
            raise SessionLifecycleContractError(
                f"{method} result violated the generated v4 Contract"
            ) from exc
        return result

    return validate


def _binding(method: str) -> GatewayContractBinding[Any]:
    if method not in SESSION_LIFECYCLE_CONTRACT_METHODS:
        raise ValueError(f"unsupported Session lifecycle Contract method: {method}")
    descriptor = GATEWAY_METHOD_CONTRACTS[method]
    return GatewayContractBinding(
        descriptor=descriptor,
        observe_params=_params_observer(method, descriptor),
        validate_result=_result_validator(method, descriptor),
        result_validation_errors=(SessionLifecycleContractError,),
        response_error_message=f"{method} response violated its v4 contract",
        request_mismatch_event=f"{method}.request_contract_mismatch",
        response_violation_event=f"{method}.contract_violation",
    )


_BINDINGS: Final = {
    method: _binding(method)
    for method in SESSION_LIFECYCLE_CONTRACT_METHODS
}


def register_session_lifecycle_contract[ContextT, ResultT](
    registry: MethodRegistry[ContextT],
    method: str,
    implementation: Callable[[Any, ContextT], Awaitable[ResultT]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[ResultT]]:
    """Register one generated Contract around one lifecycle implementation."""

    try:
        binding = _BINDINGS[method]
    except KeyError as exc:
        raise ValueError(f"unsupported Session lifecycle Contract method: {method}") from exc
    return register_gateway_contract_method(
        registry,
        cast(GatewayContractBinding[ResultT], binding),
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


__all__ = [
    "SESSION_LIFECYCLE_CONTRACT_METHODS",
    "SessionLifecycleContractError",
    "register_session_lifecycle_contract",
]
