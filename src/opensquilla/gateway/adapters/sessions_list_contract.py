"""Gateway registration Adapter for the generated ``sessions.list`` Contract."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

import structlog

from opensquilla.contracts.adapters.sessions_list_contract import (
    SESSIONS_LIST_METHOD,
    SessionsListContractError,
    sessions_list_params_contract_errors,
    validate_sessions_list_result,
)
from opensquilla.contracts.generated.v4.sessions_list_metadata import SESSIONS_LIST_SCOPE

log = structlog.get_logger(__name__)

Implementation = Callable[[Any, Any], Awaitable[dict[str, Any]]]
ErrorFactory = Callable[[str, str], Exception]


class MethodRegistry(Protocol):
    """Minimal registration port required by this Gateway Adapter."""

    def method(self, name: str, scope: str) -> Callable[[Implementation], Implementation]: ...


def register_sessions_list_contract(
    registry: MethodRegistry,
    implementation: Implementation,
    *,
    internal_error: ErrorFactory,
) -> Implementation:
    @registry.method(SESSIONS_LIST_METHOD, scope=SESSIONS_LIST_SCOPE)
    async def handle_sessions_list(params: Any, ctx: Any) -> dict[str, Any]:
        request_errors = sessions_list_params_contract_errors(params)
        if request_errors:
            log.warning(
                "sessions.list.request_contract_mismatch",
                params_type=type(params).__name__,
                errors=request_errors,
            )
        result = await implementation(params, ctx)
        try:
            return validate_sessions_list_result(result)
        except SessionsListContractError as exc:
            log.error(
                "sessions.list.contract_violation",
                error=str(exc),
            )
            raise internal_error(
                "INTERNAL_ERROR",
                "sessions.list response violated its v4 contract",
            ) from exc

    return cast(Implementation, handle_sessions_list)
