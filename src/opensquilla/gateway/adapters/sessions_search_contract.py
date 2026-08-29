"""Gateway registration Adapter for the generated ``sessions.search`` Contract."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from opensquilla.contracts.adapters.sessions_search_contract import (
    SESSIONS_SEARCH_METHOD,
    SessionsSearchContractError,
    sessions_search_params_contract_errors,
    validate_sessions_search_result,
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

_SESSIONS_SEARCH_DESCRIPTOR = GATEWAY_METHOD_CONTRACTS[SESSIONS_SEARCH_METHOD]
_SESSIONS_SEARCH_BINDING: GatewayContractBinding[dict[str, Any]] = GatewayContractBinding(
    descriptor=_SESSIONS_SEARCH_DESCRIPTOR,
    observe_params=sessions_search_params_contract_errors,
    validate_result=validate_sessions_search_result,
    result_validation_errors=(SessionsSearchContractError,),
    response_error_message="sessions.search response violated its v4 contract",
    request_mismatch_event="sessions.search.request_contract_mismatch",
    response_violation_event="sessions.search.contract_violation",
)


def register_sessions_search_contract[ContextT](
    registry: MethodRegistry[ContextT],
    implementation: Callable[[Any, ContextT], Awaitable[dict[str, Any]]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[dict[str, Any]]]:
    """Register one Contract adapter around the existing implementation."""

    return register_gateway_contract_method(
        registry,
        _SESSIONS_SEARCH_BINDING,
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


__all__ = ["register_sessions_search_contract"]
