"""Typed v4 ``sessions.resolve`` adapter shared by Python clients.

The public clients keep their historical method signatures and transport
implementations.  This module is the only authored place that constructs the
wire method/params pair or validates the result with generated types.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from pydantic import ValidationError

from opensquilla.contracts.generated.v4.sessions_resolve import (
    SessionsResolveParams,
    SessionsResolveResult,
)
from opensquilla.contracts.generated.v4.sessions_resolve_metadata import (
    SESSIONS_RESOLVE_METHOD,
)


class SessionsResolveContractError(ValueError):
    """Raised when an authored client result violates the v4 Contract."""


def sessions_resolve_params_contract_errors(
    params: Any,
) -> tuple[dict[str, Any], ...]:
    """Observe drift without taking ownership of legacy request errors."""

    try:
        SessionsResolveParams.model_validate(
            dict(params) if isinstance(params, Mapping) else params
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


def validate_sessions_resolve_result(payload: Any) -> dict[str, Any]:
    """Validate and return the original mapping, preserving unknown fields."""

    if not isinstance(payload, dict):
        raise SessionsResolveContractError(
            "sessions.resolve result must be a JSON object"
        )
    try:
        SessionsResolveResult.model_validate(payload)
    except ValidationError as exc:
        raise SessionsResolveContractError(
            "sessions.resolve result violated the generated v4 Contract"
        ) from exc
    return payload


SessionsResolveCaller = Callable[[str, dict[str, Any] | None], Awaitable[Any]]


async def call_sessions_resolve(
    caller: SessionsResolveCaller,
    *,
    key: str,
) -> dict[str, Any]:
    """Call an existing v4 client and validate its result at the seam.

    Parameter validation is intentionally observe-only here.  The historical
    clients forwarded malformed values to Gateway, whose ``INVALID_REQUEST``
    code and message are part of the public v4 behaviour; rejecting them
    locally would silently change that error contract.
    """

    params = {"key": key}
    payload = await caller(SESSIONS_RESOLVE_METHOD, params)
    return validate_sessions_resolve_result(payload)


__all__ = [
    "SESSIONS_RESOLVE_METHOD",
    "SessionsResolveCaller",
    "SessionsResolveContractError",
    "call_sessions_resolve",
    "sessions_resolve_params_contract_errors",
    "validate_sessions_resolve_result",
]
