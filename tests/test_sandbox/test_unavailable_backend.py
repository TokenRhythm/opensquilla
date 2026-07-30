from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.application.approval_queue import ApprovalQueue
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import (
    configure_runtime,
    escalate_unavailable_backend_in_managed_mode,
    gate_action,
    refresh_runtime_backend_after_setup,
    reset_runtime,
)
from opensquilla.sandbox.policy import LevelHints
from opensquilla.sandbox.run_context import RunContext
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.sandbox.types import (
    MountSpec,
    NetworkMode,
    ResourceLimits,
    SandboxBackendError,
    SandboxPolicy,
    SandboxRequest,
    SecurityLevel,
)
from opensquilla.tools.types import ToolContext, current_tool_context


def _request(tmp_path: Path) -> SandboxRequest:
    return SandboxRequest(
        argv=("cmd", "/c", "echo", "ok"),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=SandboxPolicy(
            level=SecurityLevel.STANDARD,
            network=NetworkMode.NONE,
            mounts=(
                MountSpec(
                    host_path=tmp_path,
                    sandbox_path=Path("/workspace"),
                    mode="rw",
                    required=True,
                ),
            ),
            workspace_rw=True,
            tmp_writable=True,
            limits=ResourceLimits(wall_timeout_s=1.0),
            env_allowlist=("PATH",),
            require_approval=False,
        ),
    )


@pytest.mark.asyncio
async def test_unavailable_backend_fails_closed_without_running_command(
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox.backend.unavailable import UnavailableBackend

    backend = UnavailableBackend("no real sandbox backend is available")

    with pytest.raises(SandboxBackendError, match="no real sandbox backend"):
        await backend.run(_request(tmp_path))


@pytest.mark.asyncio
async def test_unavailable_backend_background_failure_remains_escalatable(
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox.backend.unavailable import UnavailableBackend
    from opensquilla.tools.builtin import shell

    backend = UnavailableBackend("native background sandbox is not installed")
    runtime = SimpleNamespace(backend=backend)

    with pytest.raises(SandboxBackendError, match="background sandbox"):
        await shell._spawn_sandboxed_background_process(
            runtime=runtime,
            request=_request(tmp_path),
        )


@pytest.mark.asyncio
async def test_managed_mode_unavailable_backend_requests_exact_host_retry(
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox.backend.unavailable import UnavailableBackend

    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    backend = UnavailableBackend("native sandbox is not installed")
    runtime = SimpleNamespace(
        backend=backend,
        approval_queue=queue,
        settings=SimpleNamespace(run_mode="trusted"),
        effective=SimpleNamespace(sandbox_enabled=True),
    )
    request = replace(_request(tmp_path), run_mode="trusted", session_id="managed-session")
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            run_mode="trusted",
            session_key="managed-session",
            workspace_dir=str(tmp_path),
        )
    )
    try:
        result = await escalate_unavailable_backend_in_managed_mode(
            SandboxBackendError("sandbox backend unavailable"),
            request,
            request.policy,
            runtime=runtime,
        )
        assert result is not None
        assert result.status == "approval_required"
        entry = queue.get(result.approval_id or "")
        assert entry.params["backendRetry"] is True
        assert entry.params["reviewer"] == "auto_review"
        assert entry.params["humanActionable"] is False
    finally:
        current_tool_context.reset(token)
        queue.close()


@pytest.mark.asyncio
async def test_standard_mode_unavailable_backend_does_not_request_host_retry(
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox.backend.unavailable import UnavailableBackend

    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    runtime = SimpleNamespace(
        backend=UnavailableBackend("native sandbox is not installed"),
        approval_queue=queue,
        settings=SimpleNamespace(run_mode="standard"),
        effective=SimpleNamespace(sandbox_enabled=True),
    )
    request = replace(_request(tmp_path), run_mode="standard")
    try:
        result = await escalate_unavailable_backend_in_managed_mode(
            SandboxBackendError("sandbox backend unavailable"),
            request,
            request.policy,
            runtime=runtime,
        )
        assert result is None
        assert queue.list_pending("exec") == []
    finally:
        queue.close()


def test_setup_refresh_promotes_only_backend_and_preserves_runtime_services(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox import integration

    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    settings = SandboxSettings(sandbox=True, backend="auto", run_mode="standard")

    monkeypatch.setattr(
        integration,
        "select_backend",
        lambda _settings: (_ for _ in ()).throw(SandboxBackendError("setup required")),
    )
    runtime = configure_runtime(settings, approval_queue=queue, workspace=tmp_path)
    original_services = (
        runtime.gate,
        runtime.ledger,
        runtime.cache,
        runtime.approval_queue,
        runtime.workspace,
    )
    replacement = SimpleNamespace(name="windows_default")
    monkeypatch.setattr(integration, "select_backend", lambda _settings: replacement)

    try:
        promoted = refresh_runtime_backend_after_setup()

        assert promoted is replacement
        assert integration.get_runtime() is runtime
        assert runtime.backend is replacement
        assert (
            runtime.gate,
            runtime.ledger,
            runtime.cache,
            runtime.approval_queue,
            runtime.workspace,
        ) == original_services
    finally:
        reset_runtime()
        queue.close()


def test_setup_refresh_does_not_replace_an_already_available_backend(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox import integration

    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    original = SimpleNamespace(name="windows_default")
    monkeypatch.setattr(integration, "select_backend", lambda _settings: original)
    runtime = configure_runtime(
        SandboxSettings(sandbox=True, backend="auto", run_mode="standard"),
        approval_queue=queue,
        workspace=tmp_path,
    )
    monkeypatch.setattr(
        integration,
        "select_backend",
        lambda _settings: (_ for _ in ()).throw(AssertionError("must not reselect backend")),
    )

    try:
        assert refresh_runtime_backend_after_setup() is original
        assert runtime.backend is original
    finally:
        reset_runtime()
        queue.close()


@pytest.mark.asyncio
async def test_managed_high_impact_gate_does_not_wait_for_legacy_human_approval(
    tmp_path: Path,
) -> None:
    queue = ApprovalQueue(db_path=str(tmp_path / "managed-gate.sqlite"))
    configure_runtime(
        SandboxSettings(run_mode="trusted", backend="noop", allow_legacy_mode=True),
        approval_queue=queue,
        workspace=tmp_path,
    )
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            run_mode="trusted",
            session_key="managed-high-impact",
            workspace_dir=str(tmp_path),
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(tmp_path),
            ),
        )
    )
    try:
        decision, policy, _request = await gate_action(
            action_kind="code.exec",
            argv=("python", "-c", "from pathlib import Path; Path('x').unlink()"),
            cwd=tmp_path,
            hints=LevelHints(high_impact=True),
        )

        assert not isinstance(decision, dict)
        assert policy.require_approval is False
        assert queue.list_pending("exec") == []
    finally:
        current_tool_context.reset(token)
        reset_runtime()
        queue.close()


def test_auto_backend_failure_includes_windows_setup_diagnostics(monkeypatch) -> None:
    from opensquilla.sandbox import backend as backend_mod
    from opensquilla.sandbox.backend import windows_default_support
    from opensquilla.sandbox.backend.windows_default import (
        WindowsDefaultBackend,
    )
    from opensquilla.sandbox.config import SandboxSettings

    monkeypatch.setattr(backend_mod.sys, "platform", "win32")
    monkeypatch.setattr(WindowsDefaultBackend, "available", lambda self: False)
    monkeypatch.setattr(windows_default_support, "_ctypes_available", lambda: True)
    monkeypatch.setattr(windows_default_support, "_token_api_available", lambda: False)
    monkeypatch.setattr(windows_default_support, "_acl_api_available", lambda: True)

    with pytest.raises(SandboxBackendError) as exc_info:
        backend_mod.select_backend(SandboxSettings(sandbox=True, backend="auto"))

    message = str(exc_info.value)
    assert "no real sandbox backend" in message
    assert "Windows sandbox setup diagnostics" in message
    assert "windows_default" in message
    assert "network boundary" in message


def test_auto_backend_failure_includes_macos_seatbelt_diagnostics(monkeypatch) -> None:
    from opensquilla.sandbox import backend as backend_mod
    from opensquilla.sandbox.backend import seatbelt as seatbelt_mod
    from opensquilla.sandbox.backend.seatbelt import SeatbeltBackend
    from opensquilla.sandbox.config import SandboxSettings

    monkeypatch.setattr(backend_mod.sys, "platform", "darwin")
    monkeypatch.setattr(SeatbeltBackend, "available", lambda self: False)
    monkeypatch.setattr(seatbelt_mod, "_sandbox_exec_binary", lambda binary=None: None)

    with pytest.raises(SandboxBackendError) as exc_info:
        backend_mod.select_backend(SandboxSettings(sandbox=True, backend="auto"))

    message = str(exc_info.value)
    assert "no real sandbox backend" in message
    assert "macOS Seatbelt diagnostics" in message
    assert "sandbox-exec=missing" in message
