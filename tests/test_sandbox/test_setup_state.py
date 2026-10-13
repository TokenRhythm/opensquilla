from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_setup_status_payload_defaults_to_not_setup() -> None:
    from opensquilla.sandbox.setup_state import SandboxSetupState, setup_status_payload

    payload = setup_status_payload(SandboxSetupState.NOT_SETUP, platform="win32")

    assert payload == {
        "state": "not_setup",
        "platform": "win32",
        "message": "Sandbox setup has not been completed.",
        "requiresAdmin": False,
    }


def test_linux_setup_does_not_require_admin() -> None:
    from opensquilla.sandbox.setup_state import SandboxSetupState, setup_status_payload

    payload = setup_status_payload(SandboxSetupState.READY, platform="linux")

    assert payload["state"] == "ready"
    assert payload["requiresAdmin"] is False


async def test_platform_setup_dispatches_windows(monkeypatch) -> None:
    from opensquilla.sandbox import setup_state

    calls = []

    async def fake_windows_setup(config):
        calls.append(config)
        return setup_state.SetupResult(
            state=setup_state.SandboxSetupState.READY,
            platform="win32",
            message="Windows default sandbox is ready.",
            requires_admin=False,
        )

    monkeypatch.setattr(setup_state.sys, "platform", "win32")
    monkeypatch.setattr(setup_state, "_ensure_windows_setup", fake_windows_setup)

    result = await setup_state.ensure_sandbox_setup(SimpleNamespace())

    assert result.state is setup_state.SandboxSetupState.READY
    assert calls


async def test_macos_setup_status_reports_seatbelt_ready(monkeypatch) -> None:
    from opensquilla.sandbox import setup_state

    monkeypatch.setattr(setup_state.sys, "platform", "darwin")
    monkeypatch.setattr(setup_state, "_macos_seatbelt_available", lambda: True)

    result = await setup_state.current_sandbox_setup_status(SimpleNamespace())
    ensured = await setup_state.ensure_sandbox_setup(SimpleNamespace())

    assert result.state is setup_state.SandboxSetupState.READY
    assert result.platform == "darwin"
    assert result.requires_admin is False
    assert result.detail == "sandbox-exec=ready"
    assert ensured.state is setup_state.SandboxSetupState.READY
    assert ensured.detail == "sandbox-exec=ready"


async def test_macos_setup_status_reports_missing_seatbelt(monkeypatch) -> None:
    from opensquilla.sandbox import setup_state

    monkeypatch.setattr(setup_state.sys, "platform", "darwin")
    monkeypatch.setattr(setup_state, "_macos_seatbelt_available", lambda: False)

    result = await setup_state.current_sandbox_setup_status(SimpleNamespace())

    assert result.state is setup_state.SandboxSetupState.UNAVAILABLE
    assert result.platform == "darwin"
    assert result.requires_admin is False
    assert result.detail == "sandbox-exec=missing"


async def test_windows_setup_status_reports_windows_default_ready(monkeypatch) -> None:
    from opensquilla.sandbox import setup_state

    monkeypatch.setattr(setup_state.sys, "platform", "win32")
    monkeypatch.setattr(
        setup_state,
        "_probe_windows_sandbox_support",
        lambda **_kwargs: setup_state.WindowsSetupSupport(
            default_backend_available=True,
            ctypes_available=True,
            token_api_available=True,
            acl_api_available=True,
            setup_ready=True,
            proxy_allowlist_enforced=False,
        ),
    )

    result = await setup_state.current_sandbox_setup_status(SimpleNamespace())

    assert result.state is setup_state.SandboxSetupState.NOT_SETUP
    assert result.requires_admin is True
    assert result.message == "Windows default sandbox setup is required."
    assert "network_boundary=not ready" in str(result.detail)


async def test_ensure_windows_setup_repairs_missing_network_boundary(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.sandbox import setup_state
    from opensquilla.sandbox.backend.windows_default_network import (
        FIREWALL_RULE_VERSION,
        WFP_RULE_VERSION,
        WindowsNetworkSetup,
    )

    marker = tmp_path / "setup_marker.json"
    _network = WindowsNetworkSetup(
        offline_user_sid="S-1-5-21-100-200-300-400",
        allowed_proxy_ports=(48123,),
        allow_local_binding=False,
        firewall_rule_version=FIREWALL_RULE_VERSION,
        wfp_rule_version=WFP_RULE_VERSION,
    )

    monkeypatch.setattr(setup_state.sys, "platform", "win32")
    monkeypatch.setattr(setup_state, "_windows_process_is_admin", lambda: True)
    monkeypatch.setattr(setup_state, "_windows_setup_marker_path", lambda: marker, raising=False)
    probe_calls = iter((False, True))
    monkeypatch.setattr(
        setup_state,
        "_probe_windows_sandbox_support",
        lambda **_kwargs: setup_state.WindowsSetupSupport(
            default_backend_available=True,
            ctypes_available=True,
            token_api_available=True,
            acl_api_available=True,
            setup_ready=True,
            proxy_allowlist_enforced=next(probe_calls),
        ),
    )
    monkeypatch.setattr(
        setup_state,
        "_run_windows_setup_helper_in_process",
        lambda marker_path: marker_path.write_text("{}"),
    )

    result = setup_state._ensure_windows_setup_sync(SimpleNamespace())

    assert result.state is setup_state.SandboxSetupState.READY
    assert result.requires_admin is False
    assert result.detail == "proxy_allowlist=ready"
    assert marker.exists()


async def test_ensure_windows_setup_requires_admin_before_mutating(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.sandbox import setup_state

    marker = tmp_path / "setup_marker.json"
    calls = []

    monkeypatch.setattr(setup_state.sys, "platform", "win32")
    monkeypatch.setattr(setup_state, "_windows_setup_marker_path", lambda: marker, raising=False)
    monkeypatch.setattr(setup_state, "_windows_process_is_admin", lambda: False)
    monkeypatch.setattr(
        setup_state,
        "_probe_windows_sandbox_support",
        lambda **_kwargs: setup_state.WindowsSetupSupport(
            default_backend_available=False,
            ctypes_available=True,
            token_api_available=True,
            acl_api_available=True,
            setup_ready=False,
            proxy_allowlist_enforced=False,
        ),
    )

    def fail_if_called(marker_path):
        calls.append(marker_path)
        raise AssertionError("setup mutation should not run without admin")

    monkeypatch.setattr(setup_state, "_establish_windows_network_setup", fail_if_called)
    helper_calls = []

    def fake_elevated_helper(marker_path):
        helper_calls.append(marker_path)

    monkeypatch.setattr(setup_state, "_run_windows_setup_helper_elevated", fake_elevated_helper)
    monkeypatch.setattr(
        setup_state,
        "_windows_default_setup_result",
        lambda: setup_state.SetupResult(
            state=setup_state.SandboxSetupState.READY,
            platform="win32",
            message="Windows default sandbox is ready.",
            requires_admin=False,
            detail="proxy_allowlist=ready",
        ),
    )

    result = setup_state._ensure_windows_setup_sync(SimpleNamespace())

    assert result.state is setup_state.SandboxSetupState.READY
    assert result.requires_admin is False
    assert helper_calls == [marker]
    assert calls == []


async def test_ensure_windows_setup_delegates_async_and_forwards_repair(monkeypatch) -> None:
    from opensquilla.sandbox import setup_state
    from opensquilla.sandbox.backend import windows_setup_process

    entered = asyncio.Event()
    finish = asyncio.Event()
    calls = []

    async def observe(config, *, repair_identity, on_unavailable):
        calls.append(repair_identity)
        entered.set()
        await finish.wait()
        return setup_state.SetupResult(setup_state.SandboxSetupState.READY, "win32", "ready")

    monkeypatch.setattr(windows_setup_process, "run_windows_setup_process", observe)
    monkeypatch.setattr(setup_state, "_platform_name", lambda: "win32")
    task = asyncio.create_task(setup_state.ensure_sandbox_setup(None, repair_identity=True))
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert not task.done()
    finish.set()
    result = await task
    assert result.state is setup_state.SandboxSetupState.READY
    assert calls == [True]


async def test_launcher_invalidity_cannot_revoke_new_runtime(monkeypatch) -> None:
    from opensquilla.sandbox import setup_runtime, setup_state
    from opensquilla.sandbox.backend import windows_setup_process

    setup_runtime.reset_sandbox_setup_runtime_state()
    ready = setup_state.SetupResult(setup_state.SandboxSetupState.READY, "win32", "ready")
    setup_runtime._LAST_RESULT = ready

    async def observe(config, *, repair_identity, on_unavailable):
        setup_runtime.reset_sandbox_setup_runtime_state()
        setup_runtime._LAST_RESULT = ready
        on_unavailable("old launcher failed")
        return ready

    monkeypatch.setattr(windows_setup_process, "run_windows_setup_process", observe)
    try:
        await setup_state._ensure_windows_setup(None)
        assert setup_runtime._LAST_RESULT is ready
    finally:
        setup_runtime.reset_sandbox_setup_runtime_state()


def test_invalid_setup_is_published_before_helper_mutation(monkeypatch, tmp_path) -> None:
    from opensquilla.sandbox import setup_state

    events = []
    monkeypatch.setattr(
        setup_state,
        "_probe_windows_sandbox_support",
        lambda **kw: setup_state.WindowsSetupSupport(False, True, True, True, False, False),
    )
    monkeypatch.setattr(setup_state, "_windows_setup_marker_path", lambda: tmp_path / "marker")
    monkeypatch.setattr(setup_state, "_windows_process_is_admin", lambda: False)
    monkeypatch.setattr(
        setup_state, "_run_windows_setup_helper_elevated", lambda path: events.append("mutate")
    )
    monkeypatch.setattr(
        setup_state,
        "_windows_default_setup_result",
        lambda: setup_state.SetupResult(setup_state.SandboxSetupState.READY, "win32", "ready"),
    )
    result = setup_state._ensure_windows_setup_sync(
        None, on_unavailable=lambda detail: events.append("unavailable")
    )
    assert result.state is setup_state.SandboxSetupState.READY
    assert events == ["unavailable", "mutate"]


async def test_ensure_windows_setup_reports_elevated_helper_failure(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.sandbox import setup_state

    marker = tmp_path / "setup_marker.json"

    monkeypatch.setattr(setup_state.sys, "platform", "win32")
    monkeypatch.setattr(setup_state, "_windows_setup_marker_path", lambda: marker, raising=False)
    monkeypatch.setattr(setup_state, "_windows_process_is_admin", lambda: False)
    monkeypatch.setattr(
        setup_state,
        "_probe_windows_sandbox_support",
        lambda **_kwargs: setup_state.WindowsSetupSupport(
            default_backend_available=False,
            ctypes_available=True,
            token_api_available=True,
            acl_api_available=True,
            setup_ready=False,
            proxy_allowlist_enforced=False,
        ),
    )
    monkeypatch.setattr(
        setup_state,
        "_run_windows_setup_helper_elevated",
        lambda marker_path: (_ for _ in ()).throw(OSError("elevated_setup_cancelled")),
    )

    result = setup_state._ensure_windows_setup_sync(SimpleNamespace())

    assert result.state is setup_state.SandboxSetupState.FAILED
    assert result.requires_admin is True
    assert result.message == "Windows default sandbox setup failed."
    assert result.detail == "elevated_setup_cancelled"


async def test_windows_setup_status_reports_windows_default_not_setup(
    monkeypatch,
) -> None:
    from opensquilla.sandbox import setup_state

    monkeypatch.setattr(setup_state.sys, "platform", "win32")
    monkeypatch.setattr(
        setup_state,
        "_probe_windows_sandbox_support",
        lambda **_kwargs: setup_state.WindowsSetupSupport(
            default_backend_available=False,
            ctypes_available=True,
            token_api_available=True,
            acl_api_available=True,
            setup_ready=False,
            proxy_allowlist_enforced=False,
        ),
    )

    result = await setup_state.current_sandbox_setup_status(SimpleNamespace())

    assert result.state is setup_state.SandboxSetupState.NOT_SETUP
    assert result.requires_admin is True
    assert "setup=not ready" in str(result.detail)


async def test_windows_setup_repairs_stale_offline_identity(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.sandbox import setup_state
    from opensquilla.sandbox.backend.windows_default_network import (
        FIREWALL_RULE_VERSION,
        WFP_RULE_VERSION,
        WindowsNetworkSetup,
    )

    marker = tmp_path / "setup_marker.json"
    _repaired = WindowsNetworkSetup(
        offline_user_sid="S-1-5-21-100-200-300-400",
        allowed_proxy_ports=(48123,),
        allow_local_binding=False,
        firewall_rule_version=FIREWALL_RULE_VERSION,
        wfp_rule_version=WFP_RULE_VERSION,
        offline_username="OpenSquillaSandbox",
        protected_password="new-protected-password",
    )
    probes = iter(
        (
            setup_state.WindowsSetupSupport(
                default_backend_available=False,
                ctypes_available=True,
                token_api_available=True,
                acl_api_available=True,
                setup_ready=True,
                proxy_allowlist_enforced=True,
                identity_ready=False,
            ),
            setup_state.WindowsSetupSupport(
                default_backend_available=True,
                ctypes_available=True,
                token_api_available=True,
                acl_api_available=True,
                setup_ready=True,
                proxy_allowlist_enforced=True,
                identity_ready=True,
            ),
        )
    )

    monkeypatch.setattr(setup_state.sys, "platform", "win32")
    monkeypatch.setattr(setup_state, "_windows_process_is_admin", lambda: True)
    monkeypatch.setattr(
        setup_state, "_probe_windows_sandbox_support", lambda **_kwargs: next(probes)
    )
    monkeypatch.setattr(setup_state, "_windows_setup_marker_path", lambda: marker)
    monkeypatch.setattr(
        setup_state,
        "_run_windows_setup_helper_in_process",
        lambda marker_path: marker_path.write_text("{}"),
    )

    result = setup_state._ensure_windows_setup_sync(SimpleNamespace())

    assert result.state is setup_state.SandboxSetupState.READY
    assert result.detail == "proxy_allowlist=ready"
    assert marker.exists()


async def test_windows_setup_repairs_stale_identity_through_elevated_helper(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.sandbox import setup_state

    marker = tmp_path / "setup_marker.json"
    probes = iter(
        (
            setup_state.WindowsSetupSupport(
                default_backend_available=False,
                ctypes_available=True,
                token_api_available=True,
                acl_api_available=True,
                setup_ready=True,
                proxy_allowlist_enforced=True,
                identity_ready=False,
            ),
            setup_state.WindowsSetupSupport(
                default_backend_available=True,
                ctypes_available=True,
                token_api_available=True,
                acl_api_available=True,
                setup_ready=True,
                proxy_allowlist_enforced=True,
                identity_ready=True,
            ),
        )
    )
    helper_calls: list[Path] = []

    monkeypatch.setattr(setup_state.sys, "platform", "win32")
    monkeypatch.setattr(setup_state, "_windows_process_is_admin", lambda: False)
    monkeypatch.setattr(
        setup_state, "_probe_windows_sandbox_support", lambda **_kwargs: next(probes)
    )
    monkeypatch.setattr(setup_state, "_windows_setup_marker_path", lambda: marker)
    monkeypatch.setattr(
        setup_state,
        "_run_windows_setup_helper_elevated",
        lambda path: helper_calls.append(path),
    )

    result = setup_state._ensure_windows_setup_sync(SimpleNamespace())

    assert result.state is setup_state.SandboxSetupState.READY
    assert result.detail == "proxy_allowlist=ready"
    assert helper_calls == [marker]


async def test_windows_setup_repairs_unwritable_persistent_storage(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.sandbox import setup_state

    marker = tmp_path / "setup_marker.json"
    probes = iter(
        (
            setup_state.WindowsSetupSupport(
                default_backend_available=False,
                ctypes_available=True,
                token_api_available=True,
                acl_api_available=True,
                setup_ready=True,
                proxy_allowlist_enforced=True,
                identity_ready=True,
                storage_ready=False,
            ),
            setup_state.WindowsSetupSupport(
                default_backend_available=True,
                ctypes_available=True,
                token_api_available=True,
                acl_api_available=True,
                setup_ready=True,
                proxy_allowlist_enforced=True,
                identity_ready=True,
                storage_ready=True,
            ),
        )
    )
    helper_calls: list[Path] = []

    monkeypatch.setattr(setup_state.sys, "platform", "win32")
    monkeypatch.setattr(setup_state, "_windows_process_is_admin", lambda: False)
    monkeypatch.setattr(
        setup_state, "_probe_windows_sandbox_support", lambda **_kwargs: next(probes)
    )
    monkeypatch.setattr(setup_state, "_windows_setup_marker_path", lambda: marker)
    monkeypatch.setattr(
        setup_state,
        "_run_windows_setup_helper_elevated",
        lambda path: helper_calls.append(path),
    )

    result = setup_state._ensure_windows_setup_sync(SimpleNamespace())

    assert result.state is setup_state.SandboxSetupState.READY
    assert helper_calls == [marker]


async def test_ensure_windows_setup_writes_marker_when_windows_checks_are_ready(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.sandbox import setup_state
    from opensquilla.sandbox.backend.windows_default_network import (
        FIREWALL_RULE_VERSION,
        WFP_RULE_VERSION,
        WindowsNetworkSetup,
    )

    marker = tmp_path / "setup_marker.json"
    _network = WindowsNetworkSetup(
        offline_user_sid="S-1-5-21-100-200-300-400",
        allowed_proxy_ports=(48123,),
        allow_local_binding=False,
        firewall_rule_version=FIREWALL_RULE_VERSION,
        wfp_rule_version=WFP_RULE_VERSION,
    )

    def fake_probe(**_kwargs) -> setup_state.WindowsSetupSupport:
        ready = marker.exists()
        return setup_state.WindowsSetupSupport(
            default_backend_available=ready,
            ctypes_available=True,
            token_api_available=True,
            acl_api_available=True,
            setup_ready=ready,
            proxy_allowlist_enforced=ready,
        )

    monkeypatch.setattr(setup_state.sys, "platform", "win32")
    monkeypatch.setattr(setup_state, "_windows_process_is_admin", lambda: True)
    monkeypatch.setattr(setup_state, "_probe_windows_sandbox_support", fake_probe)
    monkeypatch.setattr(setup_state, "_windows_setup_marker_path", lambda: marker, raising=False)
    monkeypatch.setattr(
        setup_state,
        "_run_windows_setup_helper_in_process",
        lambda marker_path: marker_path.write_text("{}"),
    )

    result = setup_state._ensure_windows_setup_sync(SimpleNamespace())

    assert result.state is setup_state.SandboxSetupState.READY
    assert result.requires_admin is False
    assert result.message == "Windows default sandbox is ready."
    assert result.detail == "proxy_allowlist=ready"
    assert marker.exists()


def test_windows_setup_status_reports_network_ready(monkeypatch) -> None:
    from opensquilla.sandbox import setup_state

    monkeypatch.setattr(
        setup_state,
        "_probe_windows_sandbox_support",
        lambda **_kwargs: setup_state.WindowsSetupSupport(
            default_backend_available=True,
            ctypes_available=True,
            token_api_available=True,
            acl_api_available=True,
            setup_ready=True,
            proxy_allowlist_enforced=True,
        ),
    )

    result = setup_state._windows_default_setup_result()

    assert result.state is setup_state.SandboxSetupState.READY
    assert result.detail == "proxy_allowlist=ready"


def test_windows_setup_support_uses_marker_proxy_ports(monkeypatch, tmp_path) -> None:
    from opensquilla.sandbox import setup_state
    from opensquilla.sandbox.backend import windows_default_setup as setup_marker_mod
    from opensquilla.sandbox.backend import windows_default_support as support_mod
    from opensquilla.sandbox.backend.windows_default_network import (
        FIREWALL_RULE_VERSION,
        WFP_RULE_VERSION,
        WindowsNetworkSetup,
    )
    from opensquilla.sandbox.backend.windows_default_setup import write_setup_marker

    marker = tmp_path / "setup_marker.json"
    write_setup_marker(
        marker,
        network=WindowsNetworkSetup(
            offline_user_sid="S-1-5-21-100-200-300-400",
            allowed_proxy_ports=(48123,),
            allow_local_binding=False,
            firewall_rule_version=FIREWALL_RULE_VERSION,
            wfp_rule_version=WFP_RULE_VERSION,
        ),
    )
    monkeypatch.setattr(support_mod.sys, "platform", "win32")
    monkeypatch.setattr(support_mod, "_ctypes_available", lambda: True)
    monkeypatch.setattr(support_mod, "_token_api_available", lambda: True)
    monkeypatch.setattr(support_mod, "_acl_api_available", lambda: True)
    monkeypatch.setattr(support_mod, "_offline_identity_ready", lambda _path: True)
    monkeypatch.setattr(support_mod, "_persistent_storage_ready", lambda _path: True)
    monkeypatch.setattr(support_mod, "default_setup_marker_path", lambda home=None: marker)
    monkeypatch.setattr(setup_marker_mod, "default_setup_marker_path", lambda home=None: marker)

    result = setup_state._probe_windows_sandbox_support()

    assert result.default_backend_available is True
    assert result.proxy_allowlist_enforced is True


@pytest.mark.asyncio
async def test_ensure_windows_setup_records_network_marker(monkeypatch, tmp_path) -> None:
    from opensquilla.sandbox import setup_state as mod
    from opensquilla.sandbox.backend.windows_default_network import (
        FIREWALL_RULE_VERSION,
        WFP_RULE_VERSION,
        WindowsNetworkSetup,
    )

    marker = tmp_path / "setup_marker.json"
    monkeypatch.setattr(mod, "_platform_name", lambda platform=None: "win32")
    monkeypatch.setattr(mod, "_windows_setup_marker_path", lambda: marker)
    monkeypatch.setattr(mod, "_windows_process_is_admin", lambda: True)

    monkeypatch.setattr(
        mod,
        "_probe_windows_sandbox_support",
        lambda **_kwargs: mod.WindowsSetupSupport(
            default_backend_available=False,
            ctypes_available=True,
            token_api_available=True,
            acl_api_available=True,
            setup_ready=False,
            proxy_allowlist_enforced=False,
        ),
    )

    _network = WindowsNetworkSetup(
        offline_user_sid="S-1-5-21-100-200-300-400",
        allowed_proxy_ports=(48123,),
        allow_local_binding=False,
        firewall_rule_version=FIREWALL_RULE_VERSION,
        wfp_rule_version=WFP_RULE_VERSION,
    )
    monkeypatch.setattr(
        mod,
        "_run_windows_setup_helper_in_process",
        lambda marker_path: marker_path.write_text("{}"),
    )
    monkeypatch.setattr(
        mod,
        "_windows_default_setup_result",
        lambda: mod.SetupResult(
            state=mod.SandboxSetupState.READY,
            platform="win32",
            message="Windows default sandbox is ready.",
            detail="proxy_allowlist=ready",
        ),
    )

    result = mod._ensure_windows_setup_sync(config=object())

    assert result.state == mod.SandboxSetupState.READY
