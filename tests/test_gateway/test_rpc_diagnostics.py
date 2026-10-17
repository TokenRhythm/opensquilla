from __future__ import annotations

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.diagnostics import DiagnosticsState
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.scopes import ADMIN_SCOPE, METHOD_SCOPES, READ_SCOPE


@pytest.mark.asyncio
async def test_diagnostics_status_is_read_scoped_and_reports_standard_default(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_TURN_CALL_LOG", raising=False)
    state = DiagnosticsState.from_config(GatewayConfig(diagnostics_enabled=True))
    ctx = RpcContext(
        conn_id="test",
        config=GatewayConfig(diagnostics_enabled=True),
        diagnostics_state=state,
    )

    assert METHOD_SCOPES["diagnostics.status"] == READ_SCOPE
    response = await get_dispatcher().dispatch("req-1", "diagnostics.status", {}, ctx)

    assert response.ok is True
    assert response.payload["enabled"] is True
    assert response.payload["detail"] == "standard"
    assert response.payload["raw_turn_call"]["enabled"] is False
    assert response.payload["raw_turn_call"]["source"] == "off"


@pytest.mark.asyncio
async def test_diagnostics_set_requires_admin_and_enables_runtime_raw(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_TURN_CALL_LOG", raising=False)
    state = DiagnosticsState.from_config(GatewayConfig())
    ctx = RpcContext(conn_id="test", config=GatewayConfig(), diagnostics_state=state)

    assert METHOD_SCOPES["diagnostics.set"] == ADMIN_SCOPE
    response = await get_dispatcher().dispatch(
        "req-1",
        "diagnostics.set",
        {"enabled": True, "raw": True},
        ctx,
    )

    assert response.ok is True
    assert response.payload["enabled"] is True
    assert response.payload["detail"] == "raw"
    assert response.payload["raw_turn_call"] == {
        "enabled": True,
        "source": "runtime",
        "env_override": False,
    }
    assert response.payload["applies_to"] == "next_turn"


@pytest.mark.asyncio
async def test_diagnostics_status_read_scope_but_set_requires_admin(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_TURN_CALL_LOG", raising=False)
    read_principal = Principal(
        role="operator",
        scopes=frozenset({READ_SCOPE}),
        is_owner=False,
        authenticated=True,
    )
    ctx = RpcContext(
        conn_id="test",
        principal=read_principal,
        config=GatewayConfig(),
        diagnostics_state=DiagnosticsState.from_config(GatewayConfig()),
    )

    status = await get_dispatcher().dispatch("req-1", "diagnostics.status", {}, ctx)
    setter = await get_dispatcher().dispatch(
        "req-2",
        "diagnostics.set",
        {"enabled": True, "raw": True},
        ctx,
    )

    assert status.ok is True
    assert setter.ok is False
    assert setter.error is not None
    assert setter.error.code == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_diagnostics_set_attaches_fallback_state_to_context(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_TURN_CALL_LOG", raising=False)
    ctx = RpcContext(conn_id="test", config=GatewayConfig())

    setter = await get_dispatcher().dispatch(
        "req-1",
        "diagnostics.set",
        {"enabled": True, "raw": True},
        ctx,
    )
    status = await get_dispatcher().dispatch("req-2", "diagnostics.status", {}, ctx)

    assert setter.ok is True
    assert status.ok is True
    assert status.payload["detail"] == "raw"
    assert status.payload["raw_turn_call"]["source"] == "runtime"


@pytest.mark.asyncio
async def test_diagnostics_off_clears_runtime_state_but_reports_env_forced_raw(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG", "1")
    state = DiagnosticsState.from_config(GatewayConfig())
    state.set_runtime(enabled=True, raw=True)
    ctx = RpcContext(conn_id="test", config=GatewayConfig(), diagnostics_state=state)

    response = await get_dispatcher().dispatch(
        "req-1",
        "diagnostics.set",
        {"enabled": False},
        ctx,
    )

    assert response.ok is True
    assert response.payload["enabled"] is False
    assert response.payload["detail"] == "off"
    assert response.payload["runtime"]["enabled"] is False
    assert response.payload["runtime"]["raw"] is False
    assert response.payload["raw_turn_call"] == {
        "enabled": True,
        "source": "env",
        "env_override": True,
    }
    assert response.payload["warning"] == "OPENSQUILLA_TURN_CALL_LOG still forces raw capture"

    second = await get_dispatcher().dispatch(
        "req-2",
        "diagnostics.set",
        {"enabled": False},
        ctx,
    )
    assert second.ok is True
    assert second.payload["runtime"]["raw"] is False
    assert second.payload["raw_turn_call"]["source"] == "env"
