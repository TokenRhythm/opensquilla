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
from opensquilla.contracts.generated.v4.sessions_list_metadata import SESSIONS_LIST_SCOPE
from opensquilla.gateway.adapters.contract_method import (
    GatewayContractBinding,
    MethodRegistry,
    StaticGatewayMethodDescriptor,
    register_gateway_contract_method,
)

ErrorFactory = Callable[[str, str], Exception]

_SESSIONS_LIST_DESCRIPTOR = StaticGatewayMethodDescriptor(
    name=SESSIONS_LIST_METHOD,
    scope=SESSIONS_LIST_SCOPE,
)
_SESSIONS_LIST_BINDING = GatewayContractBinding(
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
) -> Callable[[Any, ContextT], Awaitable[dict[str, Any]]]:
    return register_gateway_contract_method(
        registry,
        _SESSIONS_LIST_BINDING,
        implementation,
        internal_error=internal_error,
    )
