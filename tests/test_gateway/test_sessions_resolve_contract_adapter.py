"""Contract adapter tests for the sessions.resolve Gateway seam."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
import structlog
from starlette.testclient import TestClient

from opensquilla.contracts.adapters.sessions_resolve_contract import (
    SESSIONS_RESOLVE_METHOD,
    SessionsResolveContractError,
    call_sessions_resolve,
    sessions_resolve_params_contract_errors,
    validate_sessions_resolve_result,
)
from opensquilla.contracts.generated.v4.sessions_resolve_metadata import (
    SESSIONS_RESOLVE_SCOPE,
)
from opensquilla.gateway.adapters.sessions_resolve_contract import (
    register_sessions_resolve_contract,
)
from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, RpcRegistry, get_dispatcher


def _result() -> dict[str, Any]:
    return {
        "session_key": "agent:main:webchat:default",
        "session_id": "default",
        "status": "idle",
        "agent_id": "main",
        "model": None,
        "workspaceId": None,
        "projectWorkspaceDeferred": False,
        "created_at": 1000,
        "updated_at": 2000,
    }


@pytest.mark.parametrize("params", [{"key": "abc"}, {"key": "abc", "future": True}])
def test_request_observer_accepts_current_params(params: Any) -> None:
    assert sessions_resolve_params_contract_errors(params) == ()


@pytest.mark.parametrize("params", [None, {}, {"key": 1}, []])
def test_request_observer_reports_drift_without_raising(params: Any) -> None:
    assert sessions_resolve_params_contract_errors(params)


def test_result_validation_preserves_identity_and_extensions() -> None:
    payload = {**_result(), "future": {"enabled": True}}
    assert validate_sessions_resolve_result(payload) is payload


def test_result_validation_rejects_missing_identity() -> None:
    with pytest.raises(SessionsResolveContractError):
        validate_sessions_resolve_result({"session_key": "only-key"})


@pytest.mark.asyncio
async def test_gateway_adapter_registers_one_generic_handler() -> None:
    registry = RpcRegistry()
    expected = _result()
    observed: list[Any] = []

    async def implementation(params: Any, _ctx: Any) -> dict[str, Any]:
        observed.append(params)
        return expected

    handler = register_sessions_resolve_contract(
        registry,
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )
    params = {"key": "default", "future": True}
    result = await handler(params, cast(Any, object()))

    assert observed == [params]
    assert result is expected
    entry = registry.get_entry(SESSIONS_RESOLVE_METHOD)
    assert entry is not None
    assert entry.handler is handler
    assert entry.required_scope == SESSIONS_RESOLVE_SCOPE


@pytest.mark.asyncio
async def test_gateway_adapter_only_observes_invalid_params() -> None:
    registry = RpcRegistry()

    async def implementation(_params: Any, _ctx: Any) -> dict[str, Any]:
        raise ValueError("params.key is required")

    handler = register_sessions_resolve_contract(
        registry,
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(ValueError, match="params.key is required"):
            await handler(None, cast(Any, object()))

    assert [
        entry
        for entry in logs
        if entry.get("event") == "sessions.resolve.request_contract_mismatch"
    ]


@pytest.mark.asyncio
async def test_gateway_adapter_maps_invalid_result_to_declared_error() -> None:
    registry = RpcRegistry()

    async def implementation(_params: Any, _ctx: Any) -> dict[str, Any]:
        return {"session_key": "incomplete"}

    handler = register_sessions_resolve_contract(
        registry,
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )

    with pytest.raises(RpcHandlerError) as error:
        await handler({"key": "abc"}, cast(Any, object()))

    assert error.value.code == "INTERNAL_ERROR"
    assert error.value.message == "sessions.resolve response violated its v4 contract"


@pytest.mark.asyncio
async def test_python_client_adapter_calls_unmodified_v4_gateway() -> None:
    expected = _result()
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def caller(method: str, params: dict[str, Any] | None) -> Any:
        calls.append((method, params))
        return expected

    result = await call_sessions_resolve(caller, key="abc")

    assert calls == [("sessions.resolve", {"key": "abc"})]
    assert result is expected


@pytest.mark.asyncio
async def test_python_client_adapter_preserves_gateway_owned_invalid_param_errors() -> None:
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def caller(method: str, params: dict[str, Any] | None) -> Any:
        calls.append((method, params))
        return _result()

    # The public signature is ``str``; this deliberately exercises the legacy
    # runtime path to ensure the Contract observer does not replace Gateway's
    # INVALID_REQUEST response with a new client-local exception.
    result = await call_sessions_resolve(caller, key=cast(Any, None))

    assert calls == [("sessions.resolve", {"key": None})]
    assert result is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [None, {}, [], "legacy", 1, True])
async def test_legacy_invalid_params_keep_the_same_wire_error(params: Any) -> None:
    response = await get_dispatcher().dispatch(
        "resolve-invalid",
        SESSIONS_RESOLVE_METHOD,
        params,
        RpcContext(conn_id="resolve-invalid"),
    )

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "INVALID_REQUEST"
    assert response.error.message == "params.key is required"


def test_legacy_v4_frame_reaches_new_gateway_over_real_websocket() -> None:
    """An old WebUI/CLI frame works without a client-side Contract upgrade."""

    class Storage:
        async def get_session(self, key: str) -> Any | None:
            if key != "agent:main:webchat:default":
                return None
            return SimpleNamespace(
                session_key=key,
                session_id="session-default",
                status="idle",
                agent_id="main",
                model=None,
                workspace_id=None,
                created_at=1000,
                updated_at=2000,
            )

        async def list_sessions(self, *, limit: int = 100) -> list[Any]:
            del limit
            return []

    app = create_gateway_app(
        GatewayConfig(ws_writer_queue_enabled=False),
        session_manager=SimpleNamespace(storage=Storage()),
    )
    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51011),
    ) as client:
        with client.websocket_connect("/ws") as websocket:
            challenge = websocket.receive_json()
            assert challenge["event"] == "connect.challenge"
            websocket.send_json(
                {
                    "type": "req",
                    "id": "connect",
                    "method": "connect",
                    "params": {"minProtocol": 1, "role": "operator", "auth": {}},
                }
            )
            assert websocket.receive_json()["type"] == "hello-ok"
            websocket.send_json(
                {
                    "type": "req",
                    "id": "legacy-resolve",
                    "method": "sessions.resolve",
                    "params": {"key": "webchat:default"},
                }
            )
            response: dict[str, Any] | None = None
            for _ in range(4):
                frame = websocket.receive_json()
                if frame.get("type") == "res" and frame.get("id") == "legacy-resolve":
                    response = cast(dict[str, Any], frame)
                    break

    assert response is not None
    assert response["ok"] is True
    assert response["payload"] == {
        "session_key": "agent:main:webchat:default",
        "session_id": "session-default",
        "status": "idle",
        "agent_id": "main",
        "model": None,
        "workspaceId": None,
        "projectWorkspaceDeferred": False,
        "created_at": 1000,
        "updated_at": 2000,
    }
