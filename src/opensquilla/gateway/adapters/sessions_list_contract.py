"""Gateway registration Adapter for the generated ``sessions.list`` Contract."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from opensquilla.contracts.adapters.sessions_list_contract import (
    SESSIONS_LIST_METHOD,
    SessionsListContractError,
    sessions_list_params_contract_errors,
    validate_sessions_list_result,
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

_SESSIONS_LIST_DESCRIPTOR = GATEWAY_METHOD_CONTRACTS[SESSIONS_LIST_METHOD]
_SESSIONS_LIST_BINDING: GatewayContractBinding[dict[str, Any]] = GatewayContractBinding(
    descriptor=_SESSIONS_LIST_DESCRIPTOR,
    observe_params=sessions_list_params_contract_errors,
    validate_result=validate_sessions_list_result,
    result_validation_errors=(SessionsListContractError,),
    response_error_message="sessions.list response violated its v4 contract",
    request_mismatch_event="sessions.list.request_contract_mismatch",
    response_violation_event="sessions.list.contract_violation",
)


def register_sessions_list_contract[ContextT](
    registry: MethodRegistry[ContextT],
    implementation: Callable[[Any, ContextT], Awaitable[dict[str, Any]]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[dict[str, Any]]]:
    return register_gateway_contract_method(
        registry,
        _SESSIONS_LIST_BINDING,
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )
