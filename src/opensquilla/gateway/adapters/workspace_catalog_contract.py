"""Generated Contract bindings for persisted project workspace Gateway methods."""

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

WORKSPACE_CATALOG_CONTRACT_METHODS: Final = (
    "workspaces.list",
    "sandbox.path.list",
    "sandbox.path.create-directory",
    "sandbox.path.pick",
)


class WorkspaceCatalogContractError(ValueError):
    """A workspace success payload violated its generated Contract."""


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
            raise WorkspaceCatalogContractError(
                f"{method} result violated the generated v4 Contract"
            ) from exc
        return result

    return validate


def _binding(method: str) -> GatewayContractBinding[Any]:
    if method not in WORKSPACE_CATALOG_CONTRACT_METHODS:
        raise ValueError(f"unsupported workspace Contract method: {method}")
    descriptor = GATEWAY_METHOD_CONTRACTS[method]
    return GatewayContractBinding(
        descriptor=descriptor,
        observe_params=_params_observer(method, descriptor),
        validate_result=_result_validator(method, descriptor),
        result_validation_errors=(WorkspaceCatalogContractError,),
        response_error_message=f"{method} response violated its v4 contract",
        request_mismatch_event=f"{method}.request_contract_mismatch",
        response_violation_event=f"{method}.contract_violation",
    )


_BINDINGS: Final = {
    method: _binding(method) for method in WORKSPACE_CATALOG_CONTRACT_METHODS
}


def register_workspace_catalog_contract[ContextT, ResultT](
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
        raise ValueError(f"unsupported workspace Contract method: {method}") from exc
    return register_gateway_contract_method(
        registry,
        cast(GatewayContractBinding[ResultT], binding),
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


__all__ = [
    "WORKSPACE_CATALOG_CONTRACT_METHODS",
    "WorkspaceCatalogContractError",
    "register_workspace_catalog_contract",
]
