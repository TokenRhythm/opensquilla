from __future__ import annotations

import json
from pathlib import Path

import pytest

from opensquilla.engine.types import ToolCall
from opensquilla.gateway.approval_queue import reset_approval_queue
from opensquilla.sandbox.backend.seatbelt import _validate_request
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, reset_runtime
from opensquilla.sandbox.permissions import (
    FileSystemAccess,
    FileSystemPermissionEntry,
    FileSystemPermissionProfile,
)
from opensquilla.sandbox.types import (
    DenialReason,
    DenialResult,
    SandboxBackendError,
    SandboxResult,
    SecurityLevel,
    SuggestedNextStep,
)
from opensquilla.tools.builtin import shell
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import CallerKind, ToolContext, ToolSpec, current_tool_context


@pytest.fixture
def execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    reset_runtime()
    reset_approval_queue()
    configure_runtime(
        SandboxSettings(backend="noop", allow_legacy_mode=True, security_grading=False),
        workspace=tmp_path,
    )
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.CLI,
        run_mode="safe",
        session_key="agent:main:workdir-test",
        workspace_dir=str(tmp_path),
    )
    token = current_tool_context.set(ctx)
    calls: list[str | Path | None] = []

    async def backend(request, **_kwargs):
        calls.append(request.cwd)
        # Exercise the existing backend rejection without requiring macOS or a process.
        _validate_request(request)
        return SandboxResult(
            returncode=0, stdout="ran", stderr="", wall_time_s=0, backend_used="test"
        )

    async def host(_command, *, cwd, **_kwargs):
        calls.append(cwd)
        return "exit_code=0\nran"

    monkeypatch.setattr(shell, "_run_backend_with_managed_network", backend)
    monkeypatch.setattr(shell, "_run_host_shell_command", host)
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="exec_command", description="Run a command",
            parameters={"command": {"type": "string"}, "workdir": {"type": "string"}},
            required=["command"],
        ),
        shell.exec_command,
    )
    yield ctx, calls, build_tool_handler(registry, ctx)
    current_tool_context.reset(token)
    reset_runtime()
    reset_approval_queue()


@pytest.mark.parametrize("execution_mode", ["sandbox", "full", "host"])
@pytest.mark.parametrize("kind", ["missing", "file", "file-child"])
async def test_invalid_explicit_workdir_is_correctable_without_execution_or_path_disclosure(
    tmp_path: Path, execution, monkeypatch: pytest.MonkeyPatch,
    execution_mode: str, kind: str,
) -> None:
    ctx, calls, handler = execution
    if execution_mode == "full":
        ctx.run_mode = "full"
    elif execution_mode == "host":
        monkeypatch.setenv("OPENSQUILLA_SANDBOX_DISABLED_FULL_HOST", "off")
        configure_runtime(SandboxSettings(sandbox=False), workspace=tmp_path)
    target = tmp_path / "private-target"
    if kind != "missing":
        target.write_text("private contents", encoding="utf-8")
    if kind == "file-child":
        target /= "nested"
    result = await handler(ToolCall(
        tool_use_id="invalid-cwd", tool_name="exec_command",
        arguments={"command": "echo ran", "workdir": str(target)},
    ))
    payload = json.loads(result.content)
    assert result.is_error
    assert payload["error_class"] == "RetryableToolInputError"
    assert payload["retry_allowed"] is True
    assert "workdir" in payload["user_message"]
    assert "existing directory" in payload["user_message"]
    assert str(tmp_path) not in result.content
    assert target.name not in result.content
    assert calls == []


@pytest.mark.parametrize("workdir", [None, ".", "child"])
async def test_valid_workdir_keeps_requested_directory_and_execution(
    tmp_path: Path, execution, workdir: str | None,
) -> None:
    _, calls, _ = execution
    (tmp_path / "child").mkdir()
    result = await shell.exec_command("echo ran", workdir=workdir)
    assert result == "exit_code=0\nran"
    assert calls == [(tmp_path / (workdir or ".")).resolve()]


async def test_genuine_backend_failure_keeps_nonretryable_sandbox_error(
    tmp_path: Path, execution, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, handler = execution

    async def unavailable(*_args, **_kwargs):
        raise SandboxBackendError("private backend failure details")

    monkeypatch.setattr(shell, "_run_backend_with_managed_network", unavailable)
    result = await handler(ToolCall(
        tool_use_id="backend-failure", tool_name="exec_command",
        arguments={"command": "echo ran", "workdir": str(tmp_path)},
    ))
    payload = json.loads(result.content)
    assert payload["error_class"] == "SandboxBackendError"
    assert payload["retry_allowed"] is False
    assert "private backend" not in result.content


@pytest.mark.parametrize("exists", [False, True])
async def test_denied_workdir_is_not_validated_or_disclosed(
    tmp_path: Path, execution, monkeypatch: pytest.MonkeyPatch, exists: bool,
) -> None:
    ctx, calls, _ = execution
    target = tmp_path / "denied"
    if exists:
        target.mkdir()
    ctx.sandbox_file_system_profile = FileSystemPermissionProfile(
        entries=(FileSystemPermissionEntry(target, FileSystemAccess.DENY),),
        default_access=FileSystemAccess.WRITE,
    )

    def no_validation(*_args):
        raise AssertionError("denied paths must not reach workdir diagnostics")

    monkeypatch.setattr(shell, "_validate_explicit_workdir", no_validation)
    payload = json.loads(await shell.exec_command("echo ran", workdir=str(target)))
    assert payload["status"] == "blocked"
    assert payload["reason"] == "denied_read"
    assert calls == []


@pytest.mark.parametrize("gate", ["action", "elevation"])
async def test_execution_authorization_precedes_workdir_diagnostics(
    tmp_path: Path, execution, monkeypatch: pytest.MonkeyPatch, gate: str,
) -> None:
    _, calls, _ = execution

    def no_validation(*_args):
        raise AssertionError("execution denial must precede workdir diagnostics")

    async def deny_action(**_kwargs):
        return DenialResult(
            reason=DenialReason.RUNTIME_UNCONFIGURED,
            suggested_next_step=SuggestedNextStep.ASK_USER,
            level=SecurityLevel.STANDARD,
            action_fingerprint="synthetic",
            message="synthetic denial",
            retryable=False,
        ), None, None

    monkeypatch.setattr(shell, "_validate_explicit_workdir", no_validation)
    if gate == "action":
        monkeypatch.setattr(shell, "gate_action", deny_action)
    else:
        monkeypatch.setattr(
            shell, "_gate_shell_elevation",
            lambda *_args, **_kwargs: {"status": "denied", "reason": "synthetic"},
        )
    payload = json.loads(await shell.exec_command(
        "echo ran", workdir=str(tmp_path / "missing"),
        sandbox_permissions="require_escalated" if gate == "elevation" else "use_default",
    ))
    assert payload["status"] == "denied"
    assert calls == []


async def test_directory_removed_after_validation_cannot_start_or_trigger_host_fallback(
    tmp_path: Path, execution, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, calls, handler = execution
    target = tmp_path / "removed"
    target.mkdir()

    async def racing_backend(request, **_kwargs):
        target.rmdir()
        _validate_request(request)
        raise AssertionError("backend must still reject the removed workdir")

    monkeypatch.setattr(shell, "_run_backend_with_managed_network", racing_backend)
    result = await handler(ToolCall(
        tool_use_id="removed-cwd", tool_name="exec_command",
        arguments={"command": "echo ran", "workdir": str(target)},
    ))
    payload = json.loads(result.content)
    assert payload["error_class"] == "SandboxBackendError"
    assert payload["retry_allowed"] is False
    assert str(target) not in result.content
    assert calls == []


def test_permission_error_is_not_reclassified_as_invalid_workdir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied_stat(*_args, **_kwargs):
        raise PermissionError("private host path")

    monkeypatch.setattr(Path, "stat", denied_stat)
    with pytest.raises(PermissionError):
        shell._validate_explicit_workdir("restricted", "restricted")
