from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.sandbox.capability_service import (
    REQUIRED_SAFE_CAPABILITIES,
    WINDOWS_REQUIRED_SAFE_CAPABILITIES,
    CapabilityReport,
)
from opensquilla.sandbox.setup_state import SandboxSetupState, SetupResult


@pytest.fixture(autouse=True)
def reset_setup_runtime_state():
    from opensquilla.sandbox.setup_runtime import reset_sandbox_setup_runtime_state

    reset_sandbox_setup_runtime_state()
    yield
    reset_sandbox_setup_runtime_state()


class _CapabilityBackend:
    name = "fake_native"

    def __init__(
        self,
        *,
        transport_error_for: str | None = None,
        result_overrides: dict[str, tuple[int, str, str]] | None = None,
        worker_read_message: str = "worker-ok",
    ) -> None:
        self.transport_error_for = transport_error_for
        self.result_overrides = result_overrides or {}
        self.worker_read_message = worker_read_message
        self.process_actions: list[str] = []
        self.operation_paths: list[str] = []

    def available(self) -> bool:
        return True

    def operation_domains_supported(self) -> frozenset[str]:
        return frozenset({"filesystem"})

    async def run(self, request: object):
        from opensquilla.sandbox.types import SandboxResult

        action_kind = str(getattr(request, "action_kind", ""))
        argv_text = " ".join(str(item) for item in getattr(request, "argv", ()))
        if "opensquilla-deny-write-ok" in argv_text:
            action_kind = "capability.probe.deny-write"
        elif "opensquilla-deny-read-ok" in argv_text:
            action_kind = "capability.probe.deny-read"
        self.process_actions.append(action_kind)
        if self.transport_error_for == action_kind:
            raise RuntimeError(f"transport failed for {action_kind}")
        defaults = {
            "capability.probe": (0, "opensquilla-safe-probe", ""),
            "capability.probe.deny-write": (
                0,
                "opensquilla-deny-write-ok",
                "",
            ),
            "capability.probe.deny-read": (
                0,
                "opensquilla-deny-read-ok",
                "",
            ),
        }
        returncode, stdout, stderr = self.result_overrides.get(
            action_kind,
            defaults[action_kind],
        )
        return SandboxResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            wall_time_s=0.0,
            backend_used=self.name,
        )

    async def run_operation(self, operation: object):
        from opensquilla.sandbox.operation_runtime import SandboxOperationResult

        path = Path(str(getattr(getattr(operation, "request", None), "path", "")))
        self.operation_paths.append(path.name)
        if path.name in {"must-remain.txt", "authority.txt"}:
            action = (
                "capability.probe.deny-write"
                if path.name == "must-remain.txt"
                else "capability.probe.deny-read"
            )
            if self.transport_error_for == action:
                raise RuntimeError(f"transport failed for {action}")
            raise PermissionError(f"legacy broker denial for {action}")
        return SandboxOperationResult(message=self.worker_read_message)


async def _live_report_for_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    backend: _CapabilityBackend,
    *,
    platform: str = "linux",
) -> CapabilityReport:
    from opensquilla.sandbox import setup_runtime
    from opensquilla.sandbox.config import SandboxSettings

    async def current_probe(_config: object) -> SetupResult:
        return SetupResult(
            state=SandboxSetupState.READY,
            platform=platform,
            message="ready",
        )

    monkeypatch.setattr(setup_runtime, "current_sandbox_setup_status", current_probe)
    monkeypatch.setattr(
        "opensquilla.sandbox.integration.get_runtime",
        lambda: SimpleNamespace(backend=backend),
    )
    config = SimpleNamespace(
        sandbox=SandboxSettings(),
        state_dir=str(tmp_path),
    )
    return await setup_runtime.current_sandbox_capability_report(
        config,
        force_refresh=True,
    )


def test_live_capability_budget_covers_native_windows_canary_startup() -> None:
    from opensquilla.sandbox import setup_runtime

    assert setup_runtime._CAPABILITY_PROBE_TIMEOUT_SECONDS == 30.0
    assert (
        setup_runtime._CAPABILITY_CACHE_TTL_SECONDS
        > setup_runtime._CAPABILITY_PROBE_TIMEOUT_SECONDS
    )


def test_windows_available_report_requires_current_readiness_capabilities() -> None:
    missing_readiness = CapabilityReport.available_for(
        backend="windows_default",
        platform="win32",
        capabilities=REQUIRED_SAFE_CAPABILITIES,
    )
    ready = CapabilityReport.available_for(
        backend="windows_default",
        platform="win32",
        capabilities=REQUIRED_SAFE_CAPABILITIES | WINDOWS_REQUIRED_SAFE_CAPABILITIES,
    )

    assert missing_readiness.available is False
    assert ready.available is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell redirection regression")
def test_native_write_denial_canary_suppresses_expected_redirection_error(
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox import setup_runtime

    target = tmp_path / "missing-parent" / "protected.txt"
    marker = "opensquilla-deny-write-ok"
    argv = setup_runtime._native_denial_canary_argv(
        target,
        operation="write",
        marker=marker,
    )

    result = subprocess.run(
        argv,
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert setup_runtime._exact_process_result(result, marker)
    assert result.stderr == ""


@pytest.mark.skipif(os.name != "nt", reason="Windows command-line parsing regression")
@pytest.mark.parametrize(
    ("operation", "marker", "unexpected_exit"),
    [
        ("write", "opensquilla-deny-write-ok", 41),
        ("read", "opensquilla-deny-read-ok", 42),
    ],
)
@pytest.mark.parametrize("target_name", ["ordinary.txt", "ordinary target.txt"])
def test_native_denial_canary_never_marks_an_allowed_windows_target_as_denied(
    tmp_path: Path,
    operation: str,
    marker: str,
    unexpected_exit: int,
    target_name: str,
) -> None:
    from opensquilla.sandbox import setup_runtime

    target = tmp_path / target_name
    target.write_text("unchanged", encoding="utf-8")
    launcher = tmp_path / "opensquilla-capability-canary.cmd"
    launcher.write_text(
        setup_runtime._WINDOWS_DENIAL_CANARY_SCRIPT,
        encoding="ascii",
        newline="",
    )
    argv = setup_runtime._native_denial_canary_argv(
        target,
        operation=operation,
        marker=marker,
        windows_launcher=launcher,
    )

    result = subprocess.run(
        argv,
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert setup_runtime._exact_process_result(result, marker) is False
    assert result.returncode == unexpected_exit
    assert result.stdout.strip() == f"unexpected-{operation}"


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL denial regression")
def test_native_denial_canary_preserves_a_windows_write_failure(
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox import setup_runtime

    target = tmp_path / "protected.txt"
    target.write_text("unchanged", encoding="utf-8")
    target.chmod(0o444)
    launcher = tmp_path / "opensquilla-capability-canary.cmd"
    launcher.write_text(
        setup_runtime._WINDOWS_DENIAL_CANARY_SCRIPT,
        encoding="ascii",
        newline="",
    )
    argv = setup_runtime._native_denial_canary_argv(
        target,
        operation="write",
        marker="opensquilla-deny-write-ok",
        windows_launcher=launcher,
    )

    try:
        result = subprocess.run(
            argv,
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        target.chmod(0o666)

    assert setup_runtime._exact_process_result(result, "opensquilla-deny-write-ok")
    assert target.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action_kind",
    ["capability.probe.deny-write", "capability.probe.deny-read"],
)
async def test_protected_canary_transport_errors_fail_capability_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    action_kind: str,
) -> None:
    report = await _live_report_for_backend(
        monkeypatch,
        tmp_path,
        _CapabilityBackend(transport_error_for=action_kind),
    )

    assert report.available is False
    assert report.code == "probe_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action_kind", "result"),
    [
        (
            "capability.probe.deny-write",
            (0, "opensquilla-deny-write-ok extra", ""),
        ),
        (
            "capability.probe.deny-read",
            (1, "opensquilla-deny-read-ok", ""),
        ),
    ],
)
async def test_protected_canary_requires_the_exact_native_process_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    action_kind: str,
    result: tuple[int, str, str],
) -> None:
    report = await _live_report_for_backend(
        monkeypatch,
        tmp_path,
        _CapabilityBackend(result_overrides={action_kind: result}),
    )

    assert report.available is False
    assert report.code == "capability_missing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("support_overrides", "missing_capability"),
    [
        ({"identity_ready": False}, "windowsIdentity"),
        ({"storage_ready": False}, "windowsStorage"),
        ({"proxy_allowlist_enforced": False}, "windowsProxyWfp"),
    ],
)
async def test_windows_capability_requires_current_setup_readiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    support_overrides: dict[str, bool],
    missing_capability: str,
) -> None:
    from opensquilla.sandbox.backend import windows_default_support

    support = {
        "identity_ready": True,
        "storage_ready": True,
        "proxy_allowlist_enforced": True,
    }
    support.update(support_overrides)
    monkeypatch.setattr(
        windows_default_support,
        "probe_windows_default_support",
        lambda: SimpleNamespace(**support),
    )
    backend = _CapabilityBackend()
    backend.name = "windows_default"

    report = await _live_report_for_backend(
        monkeypatch,
        tmp_path,
        backend,
        platform="win32",
    )

    assert report.available is False
    assert report.code == "capability_missing"
    assert missing_capability not in report.capabilities


@pytest.mark.asyncio
async def test_windows_capability_includes_current_setup_readiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox.backend import windows_default_support

    monkeypatch.setattr(
        windows_default_support,
        "probe_windows_default_support",
        lambda: SimpleNamespace(
            identity_ready=True,
            storage_ready=True,
            proxy_allowlist_enforced=True,
        ),
    )
    backend = _CapabilityBackend()
    backend.name = "windows_default"

    report = await _live_report_for_backend(
        monkeypatch,
        tmp_path,
        backend,
        platform="win32",
    )

    assert report.available is True
    assert {
        "windowsIdentity",
        "windowsStorage",
        "windowsProxyWfp",
    }.issubset(report.capabilities)


@pytest.mark.asyncio
async def test_status_reports_setting_up_while_auto_setup_is_running(monkeypatch) -> None:
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
async def test_auto_setup_failure_remains_visible_after_setup_finishes(monkeypatch) -> None:
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
    monkeypatch.setattr(setup_runtime, "current_sandbox_setup_status", current_probe)

    result = await setup_runtime.ensure_sandbox_setup_auto(config)
    status = await setup_runtime.current_sandbox_setup_runtime_status(config)

    assert result.state is SandboxSetupState.FAILED
    assert result.detail == "setup exploded"
    assert status is result


@pytest.mark.asyncio
async def test_windows_auto_setup_promotes_runtime_backend_after_setup(monkeypatch) -> None:
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
        "refresh_runtime_backend_after_setup",
        lambda: promotions.append("promoted"),
        raising=False,
    )

    result = await setup_runtime.ensure_sandbox_setup_auto(config)

    assert result.state is SandboxSetupState.READY
    assert promotions == ["promoted"]


@pytest.mark.asyncio
async def test_windows_auto_setup_reports_failed_when_runtime_cannot_be_promoted(
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
        "refresh_runtime_backend_after_setup",
        lambda: (_ for _ in ()).throw(RuntimeError("backend still unavailable")),
        raising=False,
    )

    result = await setup_runtime.ensure_sandbox_setup_auto(SimpleNamespace())

    assert result.state is SandboxSetupState.FAILED
    assert result.platform == "win32"
    assert result.detail == "backend still unavailable"


@pytest.mark.asyncio
async def test_reset_setup_runtime_state_delegates_to_current_probe_again(monkeypatch) -> None:
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
    monkeypatch.setattr(setup_runtime, "current_sandbox_setup_status", current_probe)
    await setup_runtime.ensure_sandbox_setup_auto(config)

    setup_runtime.reset_sandbox_setup_runtime_state()
    status = await setup_runtime.current_sandbox_setup_runtime_status(config)

    assert status.state is SandboxSetupState.NOT_SETUP
    assert status.message == "Sandbox setup has not been completed."


@pytest.mark.asyncio
async def test_capability_report_uses_live_setup_and_configured_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox import setup_runtime

    config = SimpleNamespace(sandbox=SimpleNamespace(backend="windows_default"))

    async def current_probe(_config: object) -> SetupResult:
        return SetupResult(
            state=SandboxSetupState.READY,
            platform="win32",
            message="ready",
        )

    monkeypatch.setattr(setup_runtime, "current_sandbox_setup_status", current_probe)
    expected = CapabilityReport.available_for(
        backend="windows_default",
        platform="win32",
        reason="probe",
        capabilities=REQUIRED_SAFE_CAPABILITIES | WINDOWS_REQUIRED_SAFE_CAPABILITIES,
    )

    async def live_probe(*_args: object, **_kwargs: object) -> CapabilityReport:
        return expected

    monkeypatch.setattr(setup_runtime, "_probe_runtime_capabilities", live_probe)
    monkeypatch.setattr(
        "opensquilla.sandbox.integration.get_runtime",
        lambda: SimpleNamespace(
            backend=SimpleNamespace(name="windows_default"),
        ),
    )
    setup_runtime.reset_sandbox_setup_runtime_state()

    report = await setup_runtime.current_sandbox_capability_report(config)

    assert report.available is True
    assert report.backend == "windows_default"
    assert report.code == "ready"


@pytest.mark.asyncio
async def test_capability_report_force_refresh_bypasses_cached_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox import setup_runtime

    config = SimpleNamespace(sandbox=SimpleNamespace(backend="windows_default"))

    async def current_probe(_config: object) -> SetupResult:
        return SetupResult(
            state=SandboxSetupState.READY,
            platform="win32",
            message="ready",
        )

    calls = 0

    async def live_probe(*_args: object, **_kwargs: object) -> CapabilityReport:
        nonlocal calls
        calls += 1
        return CapabilityReport.available_for(
            backend="windows_default",
            platform="win32",
            reason=f"probe-{calls}",
        )

    monkeypatch.setattr(setup_runtime, "current_sandbox_setup_status", current_probe)
    monkeypatch.setattr(setup_runtime, "_probe_runtime_capabilities", live_probe)
    monkeypatch.setattr(
        "opensquilla.sandbox.integration.get_runtime",
        lambda: SimpleNamespace(
            backend=SimpleNamespace(name="windows_default"),
        ),
    )

    first = await setup_runtime.current_sandbox_capability_report(config)
    cached = await setup_runtime.current_sandbox_capability_report(config)
    refreshed = await setup_runtime.current_sandbox_capability_report(
        config,
        force_refresh=True,
    )

    assert first.reason == cached.reason == "probe-1"
    assert refreshed.reason == "probe-2"
    assert calls == 2


@pytest.mark.asyncio
async def test_concurrent_force_refreshes_share_one_live_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox import setup_runtime

    config = SimpleNamespace(sandbox=SimpleNamespace(backend="windows_default"))

    async def current_probe(_config: object) -> SetupResult:
        return SetupResult(
            state=SandboxSetupState.READY,
            platform="win32",
            message="ready",
        )

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def live_probe(*_args: object, **_kwargs: object) -> CapabilityReport:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return CapabilityReport.available_for(
            backend="windows_default",
            platform="win32",
            reason=f"probe-{calls}",
        )

    monkeypatch.setattr(setup_runtime, "current_sandbox_setup_status", current_probe)
    monkeypatch.setattr(setup_runtime, "_probe_runtime_capabilities", live_probe)
    monkeypatch.setattr(
        "opensquilla.sandbox.integration.get_runtime",
        lambda: SimpleNamespace(
            backend=SimpleNamespace(name="windows_default"),
        ),
    )
    setup_runtime.reset_sandbox_setup_runtime_state()

    first = asyncio.create_task(
        setup_runtime.current_sandbox_capability_report(config, force_refresh=True)
    )
    await entered.wait()
    second = asyncio.create_task(
        setup_runtime.current_sandbox_capability_report(config, force_refresh=True)
    )
    await asyncio.sleep(0)
    release.set()

    first_report, second_report = await asyncio.gather(first, second)

    assert calls == 1
    assert first_report.reason == second_report.reason == "probe-1"


@pytest.mark.asyncio
async def test_failed_capability_report_expires_before_successful_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox import setup_runtime

    config = SimpleNamespace(sandbox=SimpleNamespace(backend="windows_default"))

    async def current_probe(_config: object) -> SetupResult:
        return SetupResult(
            state=SandboxSetupState.READY,
            platform="win32",
            message="ready",
        )

    clock = [100.0]
    calls = 0

    async def live_probe(*_args: object, **_kwargs: object) -> CapabilityReport:
        nonlocal calls
        calls += 1
        if calls == 1:
            return CapabilityReport(
                available=False,
                backend="windows_default",
                platform="win32",
                code="probe_timeout",
                reason="timed out",
                setup_supported=True,
                restart_required=False,
                probe_version=1,
                capabilities=frozenset(),
            )
        return CapabilityReport.available_for(
            backend="windows_default",
            platform="win32",
            reason="ready",
            capabilities=(
                REQUIRED_SAFE_CAPABILITIES | WINDOWS_REQUIRED_SAFE_CAPABILITIES
            ),
        )

    monkeypatch.setattr(setup_runtime, "current_sandbox_setup_status", current_probe)
    monkeypatch.setattr(setup_runtime, "_probe_runtime_capabilities", live_probe)
    monkeypatch.setattr(setup_runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "opensquilla.sandbox.integration.get_runtime",
        lambda: SimpleNamespace(backend=SimpleNamespace(name="windows_default")),
    )

    first = await setup_runtime.current_sandbox_capability_report(config)
    clock[0] += 11.0
    second = await setup_runtime.current_sandbox_capability_report(config)
    clock[0] += 11.0
    cached = await setup_runtime.current_sandbox_capability_report(config)

    assert first.available is False
    assert second.available is cached.available is True
    assert calls == 2


@pytest.mark.asyncio
async def test_live_capability_probe_scopes_file_profile_to_canary_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox import file_policy, setup_runtime
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.permissions import FileSystemPermissionProfile

    captured: dict[str, object] = {}

    def compile_profile(*args: object, **kwargs: object) -> FileSystemPermissionProfile:
        captured["stored_policy"] = args[0]
        captured.update(kwargs)
        return FileSystemPermissionProfile(entries=())

    monkeypatch.setattr(file_policy, "compile_safe_file_profile", compile_profile)
    setup = SetupResult(
        state=SandboxSetupState.READY,
        platform="linux",
        message="ready",
    )
    config = SimpleNamespace(
        sandbox=SandboxSettings(),
        state_dir=str(tmp_path),
    )

    backend = _CapabilityBackend()
    report = await setup_runtime._probe_runtime_capabilities(
        config,
        setup=setup,
        backend=backend.name,
        backend_object=backend,
    )

    assert report.available is True
    assert captured["home"] == captured["writable_roots"][0]
    assert captured["env"]["USERPROFILE"] == str(captured["home"])
    assert captured["env"]["HOME"] == str(captured["home"])
    stored_policy = captured["stored_policy"]
    deny_paths = getattr(getattr(stored_policy, "files"), "custom_deny_write_paths")
    assert len(deny_paths) == 1
    assert Path(deny_paths[0]).name == "must-remain.txt"
    assert Path(deny_paths[0]).is_relative_to(Path(captured["home"]))
    assert backend.process_actions == [
        "capability.probe",
        "capability.probe.deny-write",
        "capability.probe.deny-read",
    ]
    assert backend.operation_paths == ["worker.txt", "worker.txt"]


@pytest.mark.asyncio
async def test_live_capability_probe_accepts_numbered_filesystem_worker_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend = _CapabilityBackend(worker_read_message="1\tworker-ok")

    report = await _live_report_for_backend(monkeypatch, tmp_path, backend)

    assert report.available is True
    assert "filesystem-worker" in report.capabilities
