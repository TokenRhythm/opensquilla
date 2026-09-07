"""Private mechanics shared by generated Gateway Contract adapters.

Domain adapters still own their method inventory, violation error, and public
registration function.  This module only centralizes the identical descriptor
lookup, drift observation, result validation, and registry hand-off.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any, cast

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
    RegisteredHandler,
    register_gateway_contract_method,
)


def _validation_errors(exc: ValidationError) -> tuple[dict[str, Any], ...]:
    return tuple(
        cast(
            list[dict[str, Any]],
            exc.errors(include_url=False, include_context=False, include_input=False),
        )
    )


def _observe_request(
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
            descriptor.params_model.model_validate({} if params is None else params)
        except ValidationError as exc:
            return _validation_errors(exc)
        return ()

    return observe


def _validate_result(
    method: str,
    descriptor: GatewayMethodContract,
    violation_error: type[ValueError],
) -> Callable[[Any], Any]:
    def validate(result: Any) -> Any:
        try:
            descriptor.result_model.model_validate(result)
        except ValidationError as exc:
            raise violation_error(f"{method} result violated the generated v4 Contract") from exc
        return result

    return validate


def generated_contract_bindings(
    methods: Iterable[str],
    violation_error: type[ValueError],
) -> dict[str, GatewayContractBinding[Any]]:
    """Build one binding per domain-owned method from generated descriptors."""

    bindings: dict[str, GatewayContractBinding[Any]] = {}
    for method in methods:
        descriptor: GatewayMethodContract = GATEWAY_METHOD_CONTRACTS[method]
        bindings[method] = GatewayContractBinding(
            descriptor=descriptor,
            observe_params=_observe_request(method, descriptor),
            validate_result=_validate_result(method, descriptor, violation_error),
            result_validation_errors=(violation_error,),
            response_error_message=f"{method} response violated its v4 contract",
            request_mismatch_event=f"{method}.request_contract_mismatch",
            response_violation_event=f"{method}.contract_violation",
        )
    return bindings


def register_generated_contract_binding[ContextT, ResultT](
    registry: MethodRegistry[ContextT],
    bindings: Mapping[str, GatewayContractBinding[Any]],
    method: str,
    implementation: Callable[[Any, ContextT], Awaitable[ResultT]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
    unsupported_contract: str | None = None,
) -> RegisteredHandler[ContextT, ResultT]:
    """Register one domain binding while preserving its unsupported-method error."""

    try:
        binding = bindings[method]
    except KeyError as exc:
        if unsupported_contract is None:
            raise
        raise ValueError(f"unsupported {unsupported_contract} Contract method: {method}") from exc
    registered: RegisteredHandler[ContextT, ResultT] = register_gateway_contract_method(
        registry,
        cast(GatewayContractBinding[ResultT], binding),
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )
    return registered
