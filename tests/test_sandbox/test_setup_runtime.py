from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.sandbox.setup_state import SandboxSetupState, SetupResult


@pytest.fixture(autouse=True)
def reset_setup_runtime_state():
    from opensquilla.sandbox.setup_runtime import reset_sandbox_setup_runtime_state

    reset_sandbox_setup_runtime_state()
    yield
    reset_sandbox_setup_runtime_state()


@pytest.mark.asyncio
async def test_status_reports_setting_up_while_setup_is_running(monkeypatch) -> None:
    from opensquilla.sandbox import setup_runtime

    entered = asyncio.Event()
    release = asyncio.Event()
    config = SimpleNamespace()

    async def blocked_setup(setup_config):
        assert setup_config is config
        entered.set()
        await release.wait()
        return SetupResult(
            state=SandboxSetupState.READY,
            platform="linux",
            message="Sandbox setup is ready.",
            requires_admin=False,
        )

    monkeypatch.setattr(setup_runtime, "ensure_sandbox_setup", blocked_setup)
    monkeypatch.setattr("opensquilla.sandbox.integration.initialize_runtime_backend", AsyncMock())

    task = asyncio.create_task(setup_runtime.ensure_sandbox_setup_auto(config))
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    try:
        status = await setup_runtime.current_sandbox_setup_runtime_status(config)

        assert status.state is SandboxSetupState.SETTING_UP
        assert status.platform == "auto"
    finally:
        release.set()

    await task


@pytest.mark.asyncio
async def test_setup_failure_remains_visible_after_setup_finishes(monkeypatch) -> None:
    from opensquilla.sandbox import setup_runtime

    config = SimpleNamespace()

    async def fail_setup(_config):
        raise RuntimeError("setup exploded")

    async def current_probe(_config):
        return SetupResult(
            state=SandboxSetupState.NOT_SETUP,
            platform="linux",
            message="Sandbox setup has not been completed.",
            requires_admin=False,
        )

    monkeypatch.setattr(setup_runtime, "ensure_sandbox_setup", fail_setup)
    monkeypatch.setattr(
        "opensquilla.sandbox.setup_state.current_sandbox_setup_status", current_probe
    )

    result = await setup_runtime.ensure_sandbox_setup_auto(config)
    status = await setup_runtime.current_sandbox_setup_runtime_status(config)

    assert result.state is SandboxSetupState.FAILED
    assert result.detail == "setup exploded"
    assert status is result


@pytest.mark.asyncio
async def test_windows_setup_promotes_runtime_backend_after_setup(monkeypatch) -> None:
    from opensquilla.sandbox import integration, setup_runtime

    config = SimpleNamespace()
    promotions = []

    async def ready_setup(_config):
        return SetupResult(
            state=SandboxSetupState.READY,
            platform="win32",
            message="Windows default sandbox is ready.",
            requires_admin=False,
        )

    monkeypatch.setattr(setup_runtime, "ensure_sandbox_setup", ready_setup)
    monkeypatch.setattr(
        integration,
        "initialize_runtime_backend",
        AsyncMock(side_effect=lambda: promotions.append("promoted")),
        raising=False,
    )

    result = await setup_runtime.ensure_sandbox_setup_auto(config)

    assert result.state is SandboxSetupState.READY
    assert promotions == ["promoted"]


@pytest.mark.asyncio
async def test_ready_setup_is_idempotent_after_a_client_loses_the_response(monkeypatch) -> None:
    from opensquilla.sandbox import integration, setup_runtime

    config = SimpleNamespace()
    setup_calls = 0
    promotions = []

    async def ready_setup(_config):
        nonlocal setup_calls
        setup_calls += 1
        return SetupResult(
            state=SandboxSetupState.READY,
            platform="win32",
            message="Windows default sandbox is ready.",
            requires_admin=False,
        )

    monkeypatch.setattr(setup_runtime, "ensure_sandbox_setup", ready_setup)
    monkeypatch.setattr(
        integration,
        "initialize_runtime_backend",
        AsyncMock(side_effect=lambda: promotions.append("promoted")),
        raising=False,
    )

    first = await setup_runtime.ensure_sandbox_setup_auto(config)
    second = await setup_runtime.ensure_sandbox_setup_auto(config)

    assert second is first
    assert setup_calls == 1
    assert promotions == ["promoted"]


@pytest.mark.asyncio
async def test_windows_setup_reports_failed_when_runtime_cannot_be_promoted(
    monkeypatch,
) -> None:
    from opensquilla.sandbox import integration, setup_runtime

    async def ready_setup(_config):
        return SetupResult(
            state=SandboxSetupState.READY,
            platform="win32",
            message="Windows default sandbox is ready.",
            requires_admin=False,
        )

    monkeypatch.setattr(setup_runtime, "ensure_sandbox_setup", ready_setup)
    monkeypatch.setattr(
        integration,
        "initialize_runtime_backend",
        AsyncMock(side_effect=RuntimeError("backend still unavailable")),
        raising=False,
    )

    result = await setup_runtime.ensure_sandbox_setup_auto(SimpleNamespace())

    assert result.state is SandboxSetupState.FAILED
    assert result.platform == "win32"
    assert result.detail == "backend still unavailable"


@pytest.mark.asyncio
async def test_reset_setup_runtime_state_returns_to_uninitialized_without_probe(
    monkeypatch,
) -> None:
    from opensquilla.sandbox import setup_runtime

    config = SimpleNamespace()

    async def fail_setup(_config):
        raise RuntimeError("setup exploded")

    async def current_probe(_config):
        return SetupResult(
            state=SandboxSetupState.NOT_SETUP,
            platform="linux",
            message="Sandbox setup has not been completed.",
            requires_admin=False,
        )

    monkeypatch.setattr(setup_runtime, "ensure_sandbox_setup", fail_setup)
    monkeypatch.setattr(
        "opensquilla.sandbox.setup_state.current_sandbox_setup_status", current_probe
    )
    monkeypatch.setattr("opensquilla.sandbox.integration.get_runtime", lambda: None)
    await setup_runtime.ensure_sandbox_setup_auto(config)

    setup_runtime.reset_sandbox_setup_runtime_state()
    status = await setup_runtime.current_sandbox_setup_runtime_status(config)

    assert status.state is SandboxSetupState.NOT_SETUP
    assert status.message == "Sandbox is not initialized."
