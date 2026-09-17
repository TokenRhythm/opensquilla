"""Typed v4 ``sessions.list`` Adapter shared by Python callers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from pydantic import ValidationError

from opensquilla.contracts.generated.v4.sessions_list import (
    SessionsListLegacyNonObjectParams,
    SessionsListParams,
    SessionsListResult,
)
from opensquilla.contracts.generated.v4.sessions_list_metadata import SESSIONS_LIST_METHOD


class SessionsListContractError(ValueError):
    pass


def sessions_list_params_contract_errors(params: Any) -> tuple[dict[str, Any], ...]:
    """Report Contract drift without changing the legacy Implementation input."""
    try:
        if isinstance(params, Mapping):
            SessionsListParams.model_validate(dict(params))
        elif params is not None:
            SessionsListLegacyNonObjectParams.model_validate(params)
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


def validate_sessions_list_result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SessionsListContractError("sessions.list result must be a JSON object")
    try:
        SessionsListResult.model_validate(payload)
    except ValidationError as exc:
        raise SessionsListContractError(
            "sessions.list result violated the generated v4 Contract"
        ) from exc
    return payload


SessionsListCaller = Callable[[str, dict[str, Any] | None], Awaitable[Any]]


async def call_sessions_list(
    caller: SessionsListCaller,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    params = {"limit": limit}
    errors = sessions_list_params_contract_errors(params)
    if errors:
        raise SessionsListContractError(
            "sessions.list client params violated the generated v4 Contract"
        )
    payload = await caller(SESSIONS_LIST_METHOD, params)
    return validate_sessions_list_result(payload)
