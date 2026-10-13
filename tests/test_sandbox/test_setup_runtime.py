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
            platform="linux",
            message="Sandbox setup is ready.",
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
async def test_windows_explicit_setup_revalidates_each_new_request(monkeypatch) -> None:
    from opensquilla.sandbox import integration, setup_runtime

    monkeypatch.setattr(setup_runtime.sys, "platform", "win32")
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

    await setup_runtime.initialize_sandbox_runtime(config)
    first = await setup_runtime.ensure_sandbox_setup_auto(config)
    second = await setup_runtime.ensure_sandbox_setup_auto(config)

    assert first.state is SandboxSetupState.READY
    assert second.state is SandboxSetupState.READY
    assert setup_calls == 2
    assert promotions == ["promoted", "promoted", "promoted"]


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


@pytest.mark.asyncio
async def test_request_returns_before_setup_and_duplicate_clients_share_work(monkeypatch):
    from opensquilla.sandbox import setup_runtime

    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def setup(_config):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return SetupResult(SandboxSetupState.READY, "win32", "ready")

    monkeypatch.setattr(setup_runtime, "ensure_sandbox_setup", setup)
    monkeypatch.setattr("opensquilla.sandbox.integration.initialize_runtime_backend", AsyncMock())
    config = SimpleNamespace()
    first = await setup_runtime.request_sandbox_setup(config)
    second = await setup_runtime.request_sandbox_setup(config)
    assert first.state is second.state is SandboxSetupState.SETTING_UP
    await asyncio.wait_for(entered.wait(), 1)
    waiter = asyncio.create_task(setup_runtime.ensure_sandbox_setup_auto(config))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert calls == 1
    assert not setup_runtime._SETUP_TASK.done()
    release.set()
    result = await setup_runtime._SETUP_TASK
    assert result.state is SandboxSetupState.READY


@pytest.mark.asyncio
async def test_check_preserves_capability_until_real_invalidity_is_reported(monkeypatch):
    from opensquilla.sandbox import integration, setup_runtime

    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(
        integration,
        "get_runtime",
        lambda: SimpleNamespace(backend=SimpleNamespace(name="windows_default")),
    )
    monkeypatch.setattr(
        setup_runtime,
        "_LAST_RESULT",
        SetupResult(SandboxSetupState.READY, "win32", "ready"),
    )

    async def setup(_config):
        entered.set()
        await release.wait()
        return SetupResult(SandboxSetupState.FAILED, "win32", "failed")

    monkeypatch.setattr(setup_runtime, "ensure_sandbox_setup", setup)
    config = SimpleNamespace()
    await setup_runtime.request_sandbox_setup(config)
    await asyncio.wait_for(entered.wait(), 1)
    assert (
        await setup_runtime.current_sandbox_setup_runtime_status(config)
    ).state is SandboxSetupState.SETTING_UP
    assert (await setup_runtime.current_sandbox_capability_report(config)).available
    setup_runtime.mark_sandbox_capability_unavailable(
        "identity invalid",
        generation=setup_runtime.sandbox_setup_generation(),
    )
    assert not (await setup_runtime.current_sandbox_capability_report(config)).available
    release.set()
    await setup_runtime._SETUP_TASK


@pytest.mark.asyncio
async def test_shutdown_fences_late_result_and_rejects_new_preparation(monkeypatch):
    from opensquilla.sandbox import setup_runtime

    entered = asyncio.Event()
    config = SimpleNamespace()

    async def setup(_config):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # A late process result must not revive a retired runtime.
            return SetupResult(SandboxSetupState.READY, "win32", "late ready")

    monkeypatch.setattr(setup_runtime, "ensure_sandbox_setup", setup)
    old_generation = setup_runtime.sandbox_setup_generation()
    await setup_runtime.request_sandbox_setup(config)
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(setup_runtime.shutdown_sandbox_setup_runtime(), 1)
    assert setup_runtime._LAST_RESULT is None
    setup_runtime.mark_sandbox_capability_unavailable("old failure", generation=old_generation)
    assert setup_runtime._LAST_RESULT is None
    assert (await setup_runtime.request_sandbox_setup(config)).state is SandboxSetupState.FAILED
    assert setup_runtime._SETUP_TASK is None


@pytest.mark.asyncio
async def test_first_setup_not_available_and_failed_attempt_can_retry(monkeypatch):
    from opensquilla.sandbox import setup_runtime

    config = SimpleNamespace()
    setup = AsyncMock(return_value=SetupResult(SandboxSetupState.FAILED, "win32", "failed"))
    monkeypatch.setattr(setup_runtime, "ensure_sandbox_setup", setup)
    assert (await setup_runtime.request_sandbox_setup(config)).state is SandboxSetupState.SETTING_UP
    assert not (await setup_runtime.current_sandbox_capability_report(config)).available
    await setup_runtime._SETUP_TASK
    await setup_runtime.request_sandbox_setup(config, repair_identity=True)
    await setup_runtime._SETUP_TASK
    assert setup.call_count == 2
    setup.assert_awaited_with(config, repair_identity=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed_invalid", [False, True])
@pytest.mark.parametrize(
    "detail", ["windows_setup_launcher_spawn_failed", "windows_setup_operation_timeout"]
)
async def test_observer_failure_keeps_committed_capability_unless_check_invalidates_it(
    monkeypatch,
    confirmed_invalid,
    detail,
):
    from opensquilla.sandbox import integration, setup_runtime

    config = SimpleNamespace()
    ready = SetupResult(SandboxSetupState.READY, "win32", "ready")
    monkeypatch.setattr(setup_runtime, "_LAST_RESULT", ready)
    monkeypatch.setattr(
        integration,
        "get_runtime",
        lambda: SimpleNamespace(backend=SimpleNamespace(name="windows_default")),
    )

    async def failed_check(_config):
        if confirmed_invalid:
            setup_runtime.mark_sandbox_capability_unavailable(
                "identity invalid",
                generation=setup_runtime.sandbox_setup_generation(),
            )
        return SetupResult(SandboxSetupState.FAILED, "win32", "check failed", detail=detail)

    monkeypatch.setattr(setup_runtime, "ensure_sandbox_setup", failed_check)
    result = await setup_runtime.ensure_sandbox_setup_auto(config)
    assert result.state is SandboxSetupState.FAILED
    assert (await setup_runtime.current_sandbox_setup_runtime_status(config)).detail == detail
    assert (
        await setup_runtime.current_sandbox_capability_report(config)
    ).available is not confirmed_invalid
