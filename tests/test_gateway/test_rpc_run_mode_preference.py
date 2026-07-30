from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError
from opensquilla.gateway.scopes import METHOD_SCOPES, READ_SCOPE, WRITE_SCOPE
from opensquilla.session.storage import SessionStorage


def _principal(*, owner: bool) -> Principal:
    return Principal(
        role="operator",
        scopes=frozenset({"operator.admin" if owner else "operator.write"}),
        is_owner=owner,
        authenticated=True,
    )


def _config(mode: str = "full") -> SimpleNamespace:
    return SimpleNamespace(
        sandbox=SimpleNamespace(run_mode=mode),
        permissions=SimpleNamespace(default_mode="off"),
    )


def _ctx(storage: SessionStorage, *, owner: bool = True) -> RpcContext:
    return RpcContext(
        conn_id="run-mode-preference-test",
        principal=_principal(owner=owner),
        session_manager=SimpleNamespace(storage=storage),
        config=_config(),
    )


def test_run_mode_preference_scope_contract() -> None:
    assert METHOD_SCOPES["sandbox.run_mode.preference.get"] == READ_SCOPE
    assert METHOD_SCOPES["sandbox.run_mode.preference.set"] == WRITE_SCOPE


@pytest.mark.asyncio
async def test_run_mode_preference_get_uses_configured_fallback() -> None:
    from opensquilla.gateway import rpc_sandbox

    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        payload = await rpc_sandbox._handle_run_mode_preference_get({}, _ctx(storage))
    finally:
        await storage.close()

    assert payload == {"runMode": "full", "source": "config"}


@pytest.mark.asyncio
async def test_run_mode_preference_set_persists_before_broadcast(
    monkeypatch,
) -> None:
    from opensquilla.gateway import rpc_sandbox

    storage = SessionStorage(":memory:")
    await storage.connect()
    observed: list[tuple[str, dict[str, str], str | None]] = []

    class _Registry:
        async def broadcast(self, event: str, payload: dict[str, str]) -> None:
            observed.append(
                (
                    event,
                    payload,
                    await storage.get_runtime_preference("sandbox.run_mode"),
                )
            )

    monkeypatch.setattr(rpc_sandbox, "_run_mode_preference_registry", lambda: _Registry())
    try:
        payload = await rpc_sandbox._handle_run_mode_preference_set(
            {"runMode": "full"},
            _ctx(storage),
        )
    finally:
        await storage.close()

    assert payload == {"runMode": "full", "source": "preference"}
    assert observed == [
        (
            "sandbox.run_mode.preference.changed",
            {"runMode": "full", "source": "preference"},
            "full",
        )
    ]


@pytest.mark.asyncio
async def test_run_mode_preference_get_coerces_full_for_non_owner() -> None:
    from opensquilla.gateway import rpc_sandbox

    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        await storage.set_runtime_preference("sandbox.run_mode", "full")
        payload = await rpc_sandbox._handle_run_mode_preference_get(
            {},
            _ctx(storage, owner=False),
        )
    finally:
        await storage.close()

    assert payload == {"runMode": "trusted", "source": "preference"}


@pytest.mark.asyncio
async def test_run_mode_preference_set_requires_owner() -> None:
    from opensquilla.gateway import rpc_sandbox

    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        with pytest.raises(RpcHandlerError) as excinfo:
            await rpc_sandbox._handle_run_mode_preference_set(
                {"runMode": "standard"},
                _ctx(storage, owner=False),
            )
    finally:
        await storage.close()

    assert excinfo.value.code == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_run_mode_preference_set_requires_sandbox_setup(
    monkeypatch,
) -> None:
    from opensquilla.gateway import rpc_sandbox
    from opensquilla.sandbox.setup_state import SandboxSetupState, SetupResult

    async def fake_status(config):
        return SetupResult(
            state=SandboxSetupState.NOT_SETUP,
            platform="darwin",
            message="Sandbox setup has not been completed.",
            requires_admin=False,
        )

    monkeypatch.setattr(rpc_sandbox, "current_sandbox_setup_status", fake_status)
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        with pytest.raises(RpcHandlerError) as excinfo:
            await rpc_sandbox._handle_run_mode_preference_set(
                {"runMode": "trusted"},
                _ctx(storage),
            )
    finally:
        await storage.close()

    assert excinfo.value.code == "SANDBOX_SETUP_REQUIRED"
