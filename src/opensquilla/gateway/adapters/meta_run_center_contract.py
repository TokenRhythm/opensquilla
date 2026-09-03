"""Generated Contract bindings for Meta run recovery and setup methods."""

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

META_RUN_CENTER_CONTRACT_METHODS: Final = (
    "meta.drafts.list",
    "meta.drafts.discard",
    "meta.run",
    "meta.runs.confirm_preflight",
    "meta.runs.recovery",
    "meta.runs.replay",
    "meta.setup.plan",
    "meta.setup.install",
    "meta.setup.status",
)


class MetaRunCenterContractError(ValueError):
    """A Meta run success payload violated its generated Contract."""


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
            raise MetaRunCenterContractError(
                f"{method} result violated the generated v4 Contract"
            ) from exc
        return result

    return validate


def _binding(method: str) -> GatewayContractBinding[Any]:
    if method not in META_RUN_CENTER_CONTRACT_METHODS:
        raise ValueError(f"unsupported Meta run Contract method: {method}")
    descriptor = GATEWAY_METHOD_CONTRACTS[method]
    return GatewayContractBinding(
        descriptor=descriptor,
        observe_params=_params_observer(method, descriptor),
        validate_result=_result_validator(method, descriptor),
        result_validation_errors=(MetaRunCenterContractError,),
        response_error_message=f"{method} response violated its v4 contract",
        request_mismatch_event=f"{method}.request_contract_mismatch",
        response_violation_event=f"{method}.contract_violation",
    )


_BINDINGS: Final = {
    method: _binding(method) for method in META_RUN_CENTER_CONTRACT_METHODS
}


def register_meta_run_center_contract[ContextT, ResultT](
    registry: MethodRegistry[ContextT],
    method: str,
    implementation: Callable[[Any, ContextT], Awaitable[ResultT]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[ResultT]]:
    try:
        binding = _BINDINGS[method]
    except KeyError as exc:
        raise ValueError(f"unsupported Meta run Contract method: {method}") from exc
    return register_gateway_contract_method(
        registry,
        cast(GatewayContractBinding[ResultT], binding),
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


__all__ = [
    "META_RUN_CENTER_CONTRACT_METHODS",
    "MetaRunCenterContractError",
    "register_meta_run_center_contract",
]
