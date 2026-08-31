from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.sandbox import integration, setup_runtime
from opensquilla.sandbox.backend.unavailable import UnavailableBackend
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.setup_state import SandboxSetupState, SetupResult


@pytest.fixture(autouse=True)
def reset_runtime():
    setup_runtime.reset_sandbox_setup_runtime_state()
    integration.reset_runtime()
    yield
    setup_runtime.reset_sandbox_setup_runtime_state()
    integration.reset_runtime()


@pytest.mark.parametrize("refresh", [False, True])
async def test_capability_status_never_executes_canaries(monkeypatch, refresh):
    calls = []

    class Backend:
        name = "windows_default"

        def available(self):
            calls.append("available")
            return True

        def operation_domains_supported(self):
            return frozenset({"filesystem"})

        async def run(self, request):
            calls.append("run")
            raise RuntimeError("status must not execute a command")

        async def run_operation(self, operation):
            calls.append("run_operation")
            raise RuntimeError("status must not read or write test files")

    monkeypatch.setattr(integration, "get_runtime", lambda: SimpleNamespace(backend=Backend()))

    async def ready(_config):
        return SetupResult(SandboxSetupState.READY, "win32", "Sandbox initialized.")

    monkeypatch.setattr(setup_runtime, "current_sandbox_setup_runtime_status", ready)
    report = await setup_runtime.current_sandbox_capability_report(
        SimpleNamespace(sandbox=SandboxSettings()), force_refresh=refresh
    )

    assert calls == []
    assert report.available is True
    assert report.probe_version == 0
    assert report.capabilities == frozenset()


def test_deferred_runtime_does_not_check_or_select_backend(monkeypatch):
    def unexpected_selection(_settings):
        raise AssertionError("gateway construction must not initialize the sandbox")

    monkeypatch.setattr(integration, "select_backend", unexpected_selection)
    runtime = integration.configure_runtime(SandboxSettings(run_mode="safe"), defer_backend=True)

    assert isinstance(runtime.backend, UnavailableBackend)
    assert runtime.backend.available() is False


def test_sandbox_settings_no_longer_exposes_auto_setup() -> None:
    assert "auto_setup" not in SandboxSettings.model_fields


async def test_initialized_status_is_cached_without_rechecking_host(monkeypatch):
    result = SetupResult(SandboxSetupState.READY, "win32", "Sandbox initialized.")
    monkeypatch.setattr(setup_runtime, "_LAST_RESULT", result)

    async def unexpected_inspection(_config):
        raise AssertionError("status reads must not repeat host inspection")

    monkeypatch.setattr(
        "opensquilla.sandbox.setup_state.current_sandbox_setup_status", unexpected_inspection
    )
    assert await setup_runtime.current_sandbox_setup_runtime_status(SimpleNamespace()) is result


async def test_missing_runtime_is_not_advertised_as_available(monkeypatch):
    async def ready(_config):
        return SetupResult(SandboxSetupState.READY, "linux", "Platform setup ready.")

    monkeypatch.setattr(setup_runtime, "current_sandbox_setup_runtime_status", ready)
    report = await setup_runtime.current_sandbox_capability_report(SimpleNamespace())
    assert report.available is False


async def test_startup_failure_does_not_install_repair_or_retry(monkeypatch):
    calls = []
    integration.configure_runtime(SandboxSettings(run_mode="safe"), defer_backend=True)

    async def missing():
        calls.append("initialize")
        raise RuntimeError("Missing setup.")

    async def unexpected_setup(_config):
        raise AssertionError("automatic startup must not install or request elevation")

    monkeypatch.setattr(integration, "initialize_runtime_backend", missing)
    monkeypatch.setattr(setup_runtime, "ensure_sandbox_setup", unexpected_setup)
    result = await setup_runtime.initialize_sandbox_runtime(SimpleNamespace())
    assert result.state is SandboxSetupState.FAILED
    assert await setup_runtime.current_sandbox_setup_runtime_status(SimpleNamespace()) is result
    assert await setup_runtime.initialize_sandbox_runtime(SimpleNamespace()) is result
    assert calls == ["initialize"]


async def test_full_access_remains_authorized_when_sandbox_startup_fails(monkeypatch):
    from opensquilla.sandbox.mode_resolver import ModeResolutionError, resolve_mode
    from opensquilla.sandbox.run_mode import RunMode

    setup_runtime.mark_sandbox_startup_pending()

    async def fail():
        raise RuntimeError("sandbox startup failed")

    monkeypatch.setattr(integration, "initialize_runtime_backend", fail)
    await setup_runtime.initialize_sandbox_runtime(SimpleNamespace())
    report = await setup_runtime.current_sandbox_capability_report(SimpleNamespace())
    host = SimpleNamespace(capabilities={"host.execute"})
    assert resolve_mode(RunMode.FULL, host, report).effective_mode is RunMode.FULL
    with pytest.raises(ModeResolutionError, match="sandbox_unavailable"):
        resolve_mode(RunMode.SAFE, host, report)


async def test_late_backend_initialization_cannot_replace_a_new_runtime(monkeypatch):
    import asyncio
    import threading

    from opensquilla.sandbox.types import SandboxBackendError

    entered = threading.Event()
    release = threading.Event()
    backend = SimpleNamespace(name="native")

    def slow_selection(_settings, **_kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return backend

    monkeypatch.setattr(integration, "select_backend", slow_selection)
    original = integration.configure_runtime(SandboxSettings(run_mode="safe"), defer_backend=True)
    task = asyncio.create_task(integration.initialize_runtime_backend())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        replacement = integration.configure_runtime(
            SandboxSettings(run_mode="safe"), defer_backend=True
        )
    finally:
        release.set()
    with pytest.raises(SandboxBackendError, match="replaced"):
        await task
    assert isinstance(original.backend, UnavailableBackend)
    assert isinstance(replacement.backend, UnavailableBackend)


def test_linux_startup_selection_does_not_run_namespace_self_tests(monkeypatch):
    from opensquilla.sandbox import backend

    monkeypatch.setattr(backend.sys, "platform", "linux")
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/bwrap")

    def unexpected_probe(_self):
        raise AssertionError("startup must not run a bwrap test command")

    monkeypatch.setattr(backend.BubblewrapBackend, "available", unexpected_probe)
    selected = backend.select_backend(SandboxSettings(run_mode="safe"), verify_runtime=False)
    assert selected.name == "bubblewrap"


async def test_automatic_initialization_does_not_inspect_runtime_capabilities(monkeypatch):
    setup_runtime.mark_sandbox_startup_pending()

    async def unexpected_status(_config):
        raise AssertionError("startup must not run login or filesystem canaries")

    async def initialize():
        return SimpleNamespace(name="native")

    monkeypatch.setattr(
        "opensquilla.sandbox.setup_state.current_sandbox_setup_status", unexpected_status
    )
    monkeypatch.setattr(integration, "initialize_runtime_backend", initialize)
    result = await setup_runtime.initialize_sandbox_runtime(SimpleNamespace())
    assert result.state is SandboxSetupState.READY


async def test_retired_startup_cannot_overwrite_new_status(monkeypatch):
    import asyncio

    entered = asyncio.Event()
    release = asyncio.Event()
    setup_runtime.mark_sandbox_startup_pending()

    async def initialize():
        entered.set()
        await release.wait()
        raise RuntimeError("old runtime failed")

    monkeypatch.setattr(integration, "initialize_runtime_backend", initialize)
    task = asyncio.create_task(setup_runtime.initialize_sandbox_runtime(SimpleNamespace()))
    await asyncio.wait_for(entered.wait(), 1)
    setup_runtime.reset_sandbox_setup_runtime_state()
    setup_runtime.mark_sandbox_startup_pending()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    status = await setup_runtime.current_sandbox_setup_runtime_status(SimpleNamespace())
    assert status.state is SandboxSetupState.SETTING_UP


def test_windows_passive_selection_never_logs_on_or_opens_state_for_writing(monkeypatch, tmp_path):
    from pathlib import Path

    from opensquilla.sandbox.backend import windows_default_support as support

    root = tmp_path / "profile"
    for name in ("sandbox", "sandbox-secrets", "sandbox-bin"):
        (root / name).mkdir(parents=True)
    marker_path = root / "sandbox" / "setup_marker.json"
    marker = SimpleNamespace(
        network=SimpleNamespace(
            offline_user_sid="S-1-5-21-test",
            offline_username="sandbox-user",
            protected_password="synthetic-protected-password",
            allowed_proxy_ports=(12345,),
        )
    )
    monkeypatch.setattr(support.sys, "platform", "win32")
    for name in ("_ctypes_available", "_token_api_available", "_acl_api_available"):
        monkeypatch.setattr(support, name, lambda: True)
    monkeypatch.setattr(support, "default_setup_marker_path", lambda home=None: marker_path)
    monkeypatch.setattr(support, "read_setup_marker", lambda _path: marker)
    monkeypatch.setattr(support, "setup_marker_is_current", lambda _path: True)
    monkeypatch.setattr(support, "setup_marker_proxy_allowlist_ready", lambda *a, **kw: True)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("passive startup must not log on or open files")

    monkeypatch.setattr(support, "_offline_identity_ready", unexpected)
    with monkeypatch.context() as passive_check:
        passive_check.setattr(Path, "open", unexpected)
        result = support.probe_windows_default_support(verify_runtime=False)
    assert result.default_backend_available is True
    assert result.proxy_allowlist_enforced is True


async def test_cold_status_does_not_probe_an_existing_backend(monkeypatch):
    class Backend:
        name = "windows_default"

        def available(self):
            raise AssertionError("status must not validate the live backend")

    monkeypatch.setattr(integration, "get_runtime", lambda: SimpleNamespace(backend=Backend()))
    report = await setup_runtime.current_sandbox_capability_report(SimpleNamespace())
    assert report.available is True
    assert report.capabilities == frozenset()
