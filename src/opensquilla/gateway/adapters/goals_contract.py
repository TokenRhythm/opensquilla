"""Gateway registration adapters for the typed Goal query/command seam."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from opensquilla.contracts.adapters.goals_contract import (
    GOALS_SET_METHOD,
    GOALS_STATUS_METHOD,
    GoalsContractError,
    goals_set_params_contract_errors,
    goals_status_params_contract_errors,
    validate_goals_set_result,
    validate_goals_status_result,
)
from opensquilla.contracts.generated.v4.gateway_contract_registry import (
    GATEWAY_METHOD_CONTRACTS,
)
from opensquilla.gateway.adapters.contract_method import (
    GatewayContractBinding,
    GuestAllowedChecker,
    MethodRegistry,
    register_gateway_contract_method,
)

ErrorFactory = Callable[[str, str], Exception]

_GOALS_STATUS_BINDING: GatewayContractBinding[dict[str, Any]] = GatewayContractBinding(
    descriptor=GATEWAY_METHOD_CONTRACTS[GOALS_STATUS_METHOD],
    observe_params=goals_status_params_contract_errors,
    validate_result=validate_goals_status_result,
    result_validation_errors=(GoalsContractError,),
    response_error_message="goals.status response violated its v4 contract",
    request_mismatch_event="goals.status.request_contract_mismatch",
    response_violation_event="goals.status.contract_violation",
)

_GOALS_SET_BINDING: GatewayContractBinding[dict[str, Any]] = GatewayContractBinding(
    descriptor=GATEWAY_METHOD_CONTRACTS[GOALS_SET_METHOD],
    observe_params=goals_set_params_contract_errors,
    validate_result=validate_goals_set_result,
    result_validation_errors=(GoalsContractError,),
    response_error_message="goals.set response violated its v4 contract",
    request_mismatch_event="goals.set.request_contract_mismatch",
    response_violation_event="goals.set.contract_violation",
)


def register_goals_status_contract[ContextT](
    registry: MethodRegistry[ContextT],
    implementation: Callable[[Any, ContextT], Awaitable[dict[str, Any]]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[dict[str, Any]]]:
    """Register one validated wrapper around the existing status handler."""

    return register_gateway_contract_method(
        registry,
        _GOALS_STATUS_BINDING,
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


def register_goals_set_contract[ContextT](
    registry: MethodRegistry[ContextT],
    implementation: Callable[[Any, ContextT], Awaitable[dict[str, Any]]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[dict[str, Any]]]:
    """Register one validated wrapper around the existing set handler."""

    return register_gateway_contract_method(
        registry,
        _GOALS_SET_BINDING,
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


__all__ = ["register_goals_set_contract", "register_goals_status_contract"]
