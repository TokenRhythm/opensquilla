"""Typed v4 ``sessions.search`` Contract adapter shared by Python callers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from pydantic import ValidationError

from opensquilla.contracts.generated.v4.sessions_search import (
    SessionsSearchLegacyNonObjectParams,
    SessionsSearchParams,
    SessionsSearchResult,
)
from opensquilla.contracts.generated.v4.sessions_search_metadata import (
    SESSIONS_SEARCH_METHOD,
)


class SessionsSearchContractError(ValueError):
    """Raised when an authored search result violates the v4 Contract."""


def sessions_search_params_contract_errors(params: Any) -> tuple[dict[str, Any], ...]:
    """Observe request drift without taking ownership of legacy errors."""

    try:
        if isinstance(params, Mapping):
            SessionsSearchParams.model_validate(dict(params))
        elif params is not None:
            SessionsSearchLegacyNonObjectParams.model_validate(params)
    except ValidationError as exc:
        return tuple(
            cast(
                list[dict[str, Any]],
                exc.errors(include_url=False, include_context=False, include_input=False),
            )
        )
    return ()


def validate_sessions_search_result(payload: Any) -> dict[str, Any]:
    """Validate a result while preserving unknown additive fields."""

    if not isinstance(payload, dict):
        raise SessionsSearchContractError("sessions.search result must be a JSON object")
    try:
        SessionsSearchResult.model_validate(payload)
    except ValidationError as exc:
        raise SessionsSearchContractError(
            "sessions.search result violated the generated v4 Contract"
        ) from exc
    return payload


SessionsSearchCaller = Callable[[str, dict[str, Any] | None], Awaitable[Any]]


async def call_sessions_search(
    caller: SessionsSearchCaller,
    *,
    query: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    """Call an existing v4 client and validate its result at the seam."""

    params = {"query": query, "limit": limit}
    payload = await caller(SESSIONS_SEARCH_METHOD, params)
    return validate_sessions_search_result(payload)


__all__ = [
    "SESSIONS_SEARCH_METHOD",
    "SessionsSearchCaller",
    "SessionsSearchContractError",
    "call_sessions_search",
    "sessions_search_params_contract_errors",
    "validate_sessions_search_result",
]
