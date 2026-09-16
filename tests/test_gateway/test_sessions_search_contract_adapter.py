"""Contract adapter tests for the sessions.search Gateway seam."""

from __future__ import annotations

from typing import Any, cast

import pytest
import structlog

from opensquilla.contracts.adapters.sessions_search_contract import (
    SESSIONS_SEARCH_METHOD,
    SessionsSearchContractError,
    call_sessions_search,
    sessions_search_params_contract_errors,
    validate_sessions_search_result,
)
from opensquilla.gateway.adapters.sessions_search_contract import (
    register_sessions_search_contract,
)
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import RpcHandlerError, RpcRegistry


def _result() -> dict[str, Any]:
    return {"sessions": [], "messages": [], "query": "x", "ts": 1000}


def test_observer_accepts_current_and_reports_drift_without_raising() -> None:
    assert sessions_search_params_contract_errors({"query": "x", "limit": 5}) == ()
    assert sessions_search_params_contract_errors({"query": 1})
    assert sessions_search_params_contract_errors(None) == ()


def test_result_validation_preserves_extensions() -> None:
    payload = {**_result(), "future": True}
    assert validate_sessions_search_result(payload) is payload
    with pytest.raises(SessionsSearchContractError):
        validate_sessions_search_result({"sessions": []})


@pytest.mark.asyncio
async def test_gateway_adapter_registers_and_validates_result() -> None:
    registry = RpcRegistry()
    observed: list[Any] = []

    async def implementation(params: Any, _ctx: Any) -> dict[str, Any]:
        observed.append(params)
        return _result()

    handler = register_sessions_search_contract(
        registry,
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )
    assert await handler({"query": "x"}, cast(Any, object())) == _result()
    assert observed == [{"query": "x"}]
    assert registry.get_entry(SESSIONS_SEARCH_METHOD).handler is handler


@pytest.mark.asyncio
async def test_gateway_adapter_maps_invalid_result_to_internal_error() -> None:
    registry = RpcRegistry()

    async def implementation(_params: Any, _ctx: Any) -> dict[str, Any]:
        return {"sessions": []}

    handler = register_sessions_search_contract(
        registry,
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )
    with structlog.testing.capture_logs():
        with pytest.raises(RpcHandlerError) as error:
            await handler({"query": "x"}, cast(Any, object()))
    assert error.value.code == "INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_python_client_adapter_preserves_wire_call() -> None:
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def caller(method: str, params: dict[str, Any] | None) -> Any:
        calls.append((method, params))
        return _result()

    result = await call_sessions_search(caller, query="x", limit=5)
    assert result is not None
    assert calls == [("sessions.search", {"query": "x", "limit": 5})]
