from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, get_runtime, reset_runtime
from opensquilla.sandbox.network_runtime import NetworkPolicyRequest, NetworkProtocol
from opensquilla.sandbox.run_context import (
    DomainGrant,
    PackageBundleGrant,
    RunContext,
    TemporaryGrant,
)
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


def test_shell_tools_publish_structured_elevation_fields() -> None:
    from opensquilla.tools.builtin import shell  # noqa: F401
    from opensquilla.tools.registry import get_default_registry

    for tool_name in ("exec_command", "background_process"):
        registered = get_default_registry().get(tool_name)
        assert registered is not None
        params = registered.spec.parameters
        assert params["sandbox_permissions"]["enum"] == [
            "use_default",
            "require_escalated",
        ]
        assert "justification" in params
        assert "prefix_rule" in params


def test_code_exec_publishes_structured_elevation_fields() -> None:
    from opensquilla.tools.builtin import code_exec  # noqa: F401
    from opensquilla.tools.registry import get_default_registry

    registered = get_default_registry().get("execute_code")
    assert registered is not None
    params = registered.spec.parameters
    assert params["sandbox_permissions"]["enum"] == [
        "use_default",
        "require_escalated",
    ]
    assert "justification" in params
    assert "prefix_rule" in params


def test_default_tool_contract_does_not_advertise_experimental_codex_permissions() -> None:
    from opensquilla.tools.builtin import code_exec, filesystem, patch, shell  # noqa: F401
    from opensquilla.tools.registry import get_default_registry

    experimental = {
        "additional_permissions",
        "with_additional_permissions",
        "request_permissions",
        "shell_zsh_fork",
        "unified_exec_zsh_fork",
    }
    for tool_name in (
        "exec_command",
        "background_process",
        "execute_code",
        "write_file",
        "edit_file",
        "apply_patch",
    ):
        registered = get_default_registry().get(tool_name)
        assert registered is not None
        params = registered.spec.parameters
        assert params["sandbox_permissions"]["enum"] == [
            "use_default",
            "require_escalated",
        ]
        assert experimental.isdisjoint(params)


@pytest.mark.asyncio
async def test_code_exec_exact_elevation_runs_host_once(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import code_exec, shell

    code = "print('approved host code')"
    host_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class _FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"approved\n", b""

        def kill(self) -> None:
            raise AssertionError("approved code should not time out")

    async def _fake_create_subprocess_exec(*args: object, **kwargs: object) -> _FakeProcess:
        host_calls.append((args, kwargs))
        return _FakeProcess()

    monkeypatch.setattr(code_exec.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(code_exec, "_resolve_python_bin", lambda *, sandbox_enabled: sys.executable)
    monkeypatch.setattr(shell, "_host_execution_allowed", lambda: False)
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="code-approved",
            run_mode="standard",
        )
    )
    try:
        requested = json.loads(
            await code_exec.execute_code(
                code,
                sandbox_permissions="require_escalated",
                justification="Run the exact short Python calculation requested by the user.",
            )
        )
        approval_id = requested["approval_id"]
        entry = get_approval_queue().get(approval_id)
        action = entry.params["action"]
        assert requested["status"] == "approval_required"
        assert action["argv"] == ["execute_code"]
        assert action["cwd"] == str(managed_runtime.resolve())
        assert action["content_digest"] == hashlib.sha256(code.encode()).hexdigest()
        assert action["content_length"] == len(code)
        assert code not in json.dumps(entry.params)
        assert host_calls == []

        get_approval_queue().resolve(approval_id, True)
        result = json.loads(
            await code_exec.execute_code(
                code,
                sandbox_permissions="require_escalated",
                justification="Run the exact short Python calculation requested by the user.",
                approval_id=approval_id,
            )
        )
    finally:
        current_tool_context.reset(token)

    assert result["exit_code"] == 0
    assert result["stdout"] == "approved\n"
    assert len(host_calls) == 1
    assert host_calls[0][0][-2:] == ("-c", code)
    assert host_calls[0][1]["cwd"] == str(managed_runtime.resolve())
    assert get_approval_queue().get(approval_id).consumed is True


@pytest.mark.asyncio
async def test_code_exec_changed_code_cannot_reuse_elevation(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import code_exec, shell

    async def _host_must_not_run(*args: object, **kwargs: object) -> object:
        raise AssertionError("changed code must not execute on the host")

    monkeypatch.setattr(code_exec.asyncio, "create_subprocess_exec", _host_must_not_run)
    monkeypatch.setattr(code_exec, "_resolve_python_bin", lambda *, sandbox_enabled: sys.executable)
    monkeypatch.setattr(shell, "_host_execution_allowed", lambda: False)
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="code-changed",
            run_mode="standard",
        )
    )
    try:
        requested = json.loads(
            await code_exec.execute_code(
                "print('safe')",
                sandbox_permissions="require_escalated",
                justification="Run the exact short Python calculation requested by the user.",
            )
        )
        get_approval_queue().resolve(requested["approval_id"], True)
        changed = json.loads(
            await code_exec.execute_code(
                "import shutil\nshutil.rmtree('/')",
                sandbox_permissions="require_escalated",
                justification="Run the exact short Python calculation requested by the user.",
                approval_id=requested["approval_id"],
            )
        )
    finally:
        current_tool_context.reset(token)

    assert changed["status"] == "approval_action_mismatch"


@pytest.mark.asyncio
async def test_code_exec_credential_path_reaches_elevation_review(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import code_exec, shell

    monkeypatch.setattr(shell, "_host_execution_allowed", lambda: False)
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="code-sensitive",
            run_mode="standard",
        )
    )
    try:
        result = json.loads(
            await code_exec.execute_code(
                "from pathlib import Path\nprint(Path('.env').read_text())",
                sandbox_permissions="require_escalated",
                justification="Read a secret file.",
            )
        )
    finally:
        current_tool_context.reset(token)

    assert result["status"] == "approval_required"
    entry = get_approval_queue().get(result["approval_id"])
    assert entry.params["action"]["sandbox_permissions"] == "require_escalated"
    assert len(get_approval_queue().list_pending("exec")) == 1


@pytest.mark.asyncio
async def test_trusted_shell_outside_write_requires_explicit_elevation(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import shell

    target = managed_runtime.parent / "outside-default" / "probe.txt"

    async def _host_must_not_run(*args, **kwargs):
        raise AssertionError("default sandbox intent must not auto-run on the host")

    monkeypatch.setattr(shell, "_run_host_shell_command", _host_must_not_run)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="shell-default",
            run_mode="trusted",
        )
    )
    try:
        result = await shell.exec_command(
            f"touch {shlex.quote(str(target))}",
            workdir=str(managed_runtime),
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result)
    assert payload["status"] == "elevation_required"
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_shell_exact_elevation_grant_runs_host_once(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import shell

    target = managed_runtime.parent / "outside-approved" / "probe.txt"
    command = f"touch {shlex.quote(str(target))}"
    host_calls: list[tuple[str, str | None]] = []

    async def _fake_host(command, *, cwd, **kwargs):
        host_calls.append((command, cwd))
        return "exit_code=0\ncreated\n"

    monkeypatch.setattr(shell, "_run_host_shell_command", _fake_host)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="shell-approved",
            run_mode="trusted",
        )
    )
    try:
        requested = json.loads(
            await shell.exec_command(
                command,
                workdir=str(managed_runtime),
                sandbox_permissions="require_escalated",
                justification="Create the one fixed file requested by the user.",
            )
        )
        assert requested["status"] == "approval_required"
        approval_id = requested["approval_id"]
        pending = get_approval_queue().get(approval_id)
        assert pending.params["humanActionable"] is False
        assert host_calls == []

        get_approval_queue().resolve(approval_id, True)
        result = await shell.exec_command(
            command,
            workdir=str(managed_runtime),
            sandbox_permissions="require_escalated",
            justification="Create the one fixed file requested by the user.",
            approval_id=approval_id,
        )
        replay = json.loads(
            await shell.exec_command(
                command,
                workdir=str(managed_runtime),
                sandbox_permissions="require_escalated",
                justification="Create the one fixed file requested by the user.",
                approval_id=approval_id,
            )
        )
    finally:
        current_tool_context.reset(token)

    assert result == "exit_code=0\ncreated\n"
    assert host_calls == [(command, str(managed_runtime.resolve()))]
    assert get_approval_queue().get(approval_id).consumed is True
    assert replay["status"] == "approval_invalid"
    assert replay["message"] == "approval already consumed"


@pytest.mark.asyncio
async def test_shell_create_then_delete_uses_two_one_shot_elevation_audits(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import shell

    outside = managed_runtime.parent / "outside-create-delete"
    target = outside / "probe.txt"
    create = f"mkdir -p {outside} && printf test > {target}"
    delete = f"rm {target} && rmdir {outside}"
    host_calls: list[str] = []

    async def _fake_host(command: str, **kwargs: object) -> str:
        host_calls.append(command)
        if command == create:
            outside.mkdir()
            target.write_text("test", encoding="utf-8")
            return "exit_code=0\ncreated\n"
        if command == delete:
            target.unlink()
            outside.rmdir()
            return "exit_code=0\ndeleted\n"
        raise AssertionError(f"unexpected elevated command: {command}")

    monkeypatch.setattr(shell, "_run_host_shell_command", _fake_host)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="shell-create-delete",
            run_mode="trusted",
        )
    )
    kwargs = {"workdir": str(managed_runtime)}
    try:
        create_preflight = json.loads(await shell.exec_command(create, **kwargs))
        create_pending = json.loads(
            await shell.exec_command(
                create,
                sandbox_permissions="require_escalated",
                justification="Create the exact requested probe.",
                **kwargs,
            )
        )
        create_id = str(create_pending["approval_id"])
        get_approval_queue().resolve(create_id, True)
        create_result = await shell.exec_command(
            create,
            sandbox_permissions="require_escalated",
            justification="Create the exact requested probe.",
            approval_id=create_id,
            **kwargs,
        )
        create_replay = json.loads(
            await shell.exec_command(
                create,
                sandbox_permissions="require_escalated",
                justification="Create the exact requested probe.",
                approval_id=create_id,
                **kwargs,
            )
        )

        delete_preflight = json.loads(await shell.exec_command(delete, **kwargs))
        delete_pending = json.loads(
            await shell.exec_command(
                delete,
                sandbox_permissions="require_escalated",
                justification="Delete the exact probe and its empty directory.",
                **kwargs,
            )
        )
        delete_id = str(delete_pending["approval_id"])
        get_approval_queue().resolve(delete_id, True)
        delete_result = await shell.exec_command(
            delete,
            sandbox_permissions="require_escalated",
            justification="Delete the exact probe and its empty directory.",
            approval_id=delete_id,
            **kwargs,
        )
        delete_replay = json.loads(
            await shell.exec_command(
                delete,
                sandbox_permissions="require_escalated",
                justification="Delete the exact probe and its empty directory.",
                approval_id=delete_id,
                **kwargs,
            )
        )
    finally:
        current_tool_context.reset(token)
        target.unlink(missing_ok=True)
        if outside.exists():
            outside.rmdir()

    create_entry = get_approval_queue().get(create_id)
    delete_entry = get_approval_queue().get(delete_id)
    assert create_preflight["status"] == "elevation_required"
    assert delete_preflight["status"] == "elevation_required"
    assert create_pending["status"] == "approval_required"
    assert delete_pending["status"] == "approval_required"
    assert create_id != delete_id
    assert create_entry.params["fingerprint"] != delete_entry.params["fingerprint"]
    assert create_entry.consumed is True
    assert delete_entry.consumed is True
    assert create_result == "exit_code=0\ncreated\n"
    assert delete_result == "exit_code=0\ndeleted\n"
    assert host_calls == [create, delete]
    assert create_replay["status"] == "approval_invalid"
    assert delete_replay["status"] == "approval_invalid"
    assert not target.exists()
    assert not outside.exists()


@pytest.mark.asyncio
async def test_shell_changed_command_cannot_consume_elevation_grant(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import shell

    target = managed_runtime.parent / "outside-changed" / "probe.txt"
    original = f"touch {shlex.quote(str(target))}"
    changed = f"rm -rf {target.parent}"
    host_calls: list[str] = []

    async def _fake_host(command, **kwargs):
        host_calls.append(command)
        return "exit_code=0\n"

    monkeypatch.setattr(shell, "_run_host_shell_command", _fake_host)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="shell-changed",
            run_mode="trusted",
        )
    )
    try:
        requested = json.loads(
            await shell.exec_command(
                original,
                workdir=str(managed_runtime),
                sandbox_permissions="require_escalated",
                justification="Create the requested fixed file.",
            )
        )
        approval_id = requested["approval_id"]
        get_approval_queue().resolve(approval_id, True)

        result = json.loads(
            await shell.exec_command(
                changed,
                workdir=str(managed_runtime),
                sandbox_permissions="require_escalated",
                justification="Create the requested fixed file.",
                approval_id=approval_id,
            )
        )
    finally:
        current_tool_context.reset(token)

    assert result["status"] == "approval_action_mismatch"
    assert host_calls == []


@pytest.mark.asyncio
async def test_background_process_uses_the_same_exact_elevation_contract(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import shell

    original = (
        f"touch {shlex.quote(str(managed_runtime.parent / 'background-one.txt'))}"
    )
    changed = (
        f"touch {shlex.quote(str(managed_runtime.parent / 'background-two.txt'))}"
    )

    async def _host_spawn_must_not_run(*args, **kwargs):
        raise AssertionError("a changed background action must not spawn")

    monkeypatch.setattr(shell.asyncio, "create_subprocess_shell", _host_spawn_must_not_run)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="background-changed",
            run_mode="trusted",
        )
    )
    try:
        requested = json.loads(
            await shell.background_process(
                original,
                workdir=str(managed_runtime),
                sandbox_permissions="require_escalated",
                justification="Create the requested background probe.",
            )
        )
        approval_id = requested["approval_id"]
        get_approval_queue().resolve(approval_id, True)
        result = json.loads(
            await shell.background_process(
                changed,
                workdir=str(managed_runtime),
                sandbox_permissions="require_escalated",
                justification="Create the requested background probe.",
                approval_id=approval_id,
            )
        )
    finally:
        current_tool_context.reset(token)

    assert result["status"] == "approval_action_mismatch"


@pytest.fixture
def managed_runtime(tmp_path: Path) -> Iterator[Path]:
    reset_approval_queue()
    configure_runtime(
        SandboxSettings(
            run_mode="standard",
            backend="noop",
            allow_legacy_mode=True,
            network_default="proxy_allowlist",
            exclude_slash_tmp=True,
            exclude_tmpdir_env_var=True,
        ),
        workspace=tmp_path,
    )
    try:
        yield tmp_path
    finally:
        reset_runtime()
        reset_approval_queue()


@pytest.fixture
def standard_runtime_no_preflight(tmp_path: Path) -> Iterator[Path]:
    reset_approval_queue()
    configure_runtime(
        SandboxSettings(
            run_mode="standard",
            backend="noop",
            allow_legacy_mode=True,
            network_default="none",
        ),
        workspace=tmp_path,
    )
    try:
        yield tmp_path
    finally:
        reset_runtime()
        reset_approval_queue()


@pytest.mark.asyncio
async def test_shell_backend_request_preserves_resolved_run_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.types import SandboxRequest
    from opensquilla.tools.builtin import shell

    reset_approval_queue()
    configure_runtime(
        SandboxSettings(
            run_mode="trusted",
            backend="noop",
            allow_legacy_mode=True,
            network_default="none",
        ),
        workspace=tmp_path,
    )
    backend_calls: list[SandboxRequest] = []

    async def _fake_run_under_backend(request, *, runtime=None):
        backend_calls.append(request)
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="", backend_notes=())

    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(tmp_path),
            ),
        )
    )
    try:
        result = await shell.exec_command("echo ok", workdir=str(tmp_path))
    finally:
        current_tool_context.reset(token)
        reset_runtime()
        reset_approval_queue()

    assert "ok" in result
    assert backend_calls
    assert backend_calls[0].run_mode == "trusted"


@pytest.mark.asyncio
async def test_trusted_windows_shell_receives_managed_proxy_without_network_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.types import NetworkMode, SandboxRequest
    from opensquilla.tools.builtin import shell

    reset_approval_queue()
    configure_runtime(
        SandboxSettings(
            run_mode="trusted",
            backend="noop",
            allow_legacy_mode=True,
            network_default="proxy_allowlist",
        ),
        workspace=tmp_path,
    )
    runtime = get_runtime()
    assert runtime is not None
    runtime.backend = SimpleNamespace(name="windows_default")
    backend_calls: list[SandboxRequest] = []

    async def _fake_run_under_backend(request, *, runtime=None):
        backend_calls.append(request)
        return SimpleNamespace(
            returncode=0,
            stdout=request.env["HTTP_PROXY"],
            stderr="",
            backend_notes=(),
        )

    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )
    async def _fake_preflight_subprocess_managed_network(request, runtime, **kwargs):
        return None

    monkeypatch.setattr(
        shell,
        "preflight_subprocess_managed_network",
        _fake_preflight_subprocess_managed_network,
    )
    async def _fake_cleanup() -> None:
        return None

    async def _fake_prepare_subprocess_managed_network_proxy(request, *, runtime=None):
        from dataclasses import replace

        proxy_policy = replace(request.policy, network=NetworkMode.PROXY_ALLOWLIST)
        proxy_request = request.with_policy(proxy_policy)
        proxy_request.env["HTTP_PROXY"] = "http://127.0.0.1:48123"
        proxy_request.env["HTTPS_PROXY"] = "http://127.0.0.1:48123"
        proxy_request.env["NO_PROXY"] = ""
        return SimpleNamespace(request=proxy_request, cleanup=_fake_cleanup)

    monkeypatch.setattr(
        shell,
        "prepare_subprocess_managed_network_proxy",
        _fake_prepare_subprocess_managed_network_proxy,
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(tmp_path),
            ),
        )
    )
    try:
        result = await shell.exec_command(
            "powershell -NoProfile -Command \"Write-Output $env:HTTP_PROXY\"",
            workdir=str(tmp_path),
        )
    finally:
        current_tool_context.reset(token)
        reset_runtime()
        reset_approval_queue()

    assert result.startswith("exit_code=0")
    assert backend_calls
    assert backend_calls[0].policy.network is NetworkMode.PROXY_ALLOWLIST
    assert backend_calls[0].env["HTTP_PROXY"].startswith("http://127.0.0.1:")


@pytest.mark.asyncio
async def test_trusted_linux_shell_receives_managed_proxy_without_network_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.types import NetworkMode, SandboxRequest
    from opensquilla.tools.builtin import shell

    reset_approval_queue()
    configure_runtime(
        SandboxSettings(
            run_mode="trusted",
            backend="noop",
            allow_legacy_mode=True,
            network_default="proxy_allowlist",
        ),
        workspace=tmp_path,
    )
    runtime = get_runtime()
    assert runtime is not None
    runtime.backend = SimpleNamespace(name="bubblewrap")
    backend_calls: list[SandboxRequest] = []

    async def _fake_run_under_backend(request, *, runtime=None):
        backend_calls.append(request)
        return SimpleNamespace(
            returncode=0,
            stdout=(
                request.env["HTTP_PROXY"]
                + "\n"
                + request.env["OPENSQUILLA_SANDBOX_NETWORK"]
                + "\n"
            ),
            stderr="",
            backend_notes=(),
        )

    async def _windows_ready_should_not_run(runtime):
        raise AssertionError("linux managed-network preflight used windows readiness")

    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )
    monkeypatch.setattr(
        "opensquilla.sandbox.integration._windows_proxy_allowlist_ready_or_repaired",
        _windows_ready_should_not_run,
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(tmp_path),
            ),
        )
    )
    try:
        result = await shell.exec_command(
            "sh -lc 'printf \"HTTP_PROXY=%s\\nOPENSQUILLA_SANDBOX_NETWORK=%s\\n\" "
            "\"$HTTP_PROXY\" \"$OPENSQUILLA_SANDBOX_NETWORK\"'",
            workdir=str(tmp_path),
        )
    finally:
        current_tool_context.reset(token)
        reset_runtime()
        reset_approval_queue()

    assert result.startswith("exit_code=0")
    assert backend_calls
    assert backend_calls[0].policy.network is NetworkMode.PROXY_ALLOWLIST
    assert backend_calls[0].env["HTTP_PROXY"].startswith("http://127.0.0.1:")
    assert backend_calls[0].env["OPENSQUILLA_SANDBOX_NETWORK"] == "proxy_allowlist"


@pytest.mark.asyncio
async def test_trusted_windows_code_exec_receives_managed_proxy_without_network_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.types import NetworkMode, NetworkProxySpec
    from opensquilla.tools.builtin import code_exec, shell

    reset_approval_queue()
    configure_runtime(
        SandboxSettings(
            run_mode="trusted",
            backend="noop",
            allow_legacy_mode=True,
            network_default="proxy_allowlist",
        ),
        workspace=tmp_path,
    )
    runtime = get_runtime()
    assert runtime is not None
    runtime.backend = SimpleNamespace(name="windows_default")
    seen: dict[str, object] = {}
    prepare_calls: list[object] = []

    async def _fake_preflight_subprocess_managed_network(request, runtime):
        return None

    async def _fake_cleanup() -> None:
        return None

    async def _fake_prepare_subprocess_managed_network_proxy(request, *, runtime=None):
        from dataclasses import replace

        prepare_calls.append(request)
        proxy_policy = replace(
            request.policy,
            network=NetworkMode.PROXY_ALLOWLIST,
            network_proxy=NetworkProxySpec(host="127.0.0.1", port=48123),
        )
        proxy_request = request.with_policy(proxy_policy)
        proxy_request.env["HTTP_PROXY"] = "http://127.0.0.1:48123"
        proxy_request.env["HTTPS_PROXY"] = "http://127.0.0.1:48123"
        return SimpleNamespace(request=proxy_request, cleanup=_fake_cleanup)

    async def _fake_run_under_backend(request, *, runtime=None):
        seen["policy"] = request.policy
        seen["env"] = request.env
        return SimpleNamespace(
            returncode=0,
            stdout="ok\n",
            stderr="",
            timed_out=False,
            backend_notes=(),
        )

    monkeypatch.setattr(code_exec, "_resolve_python_bin", lambda *, sandbox_enabled: sys.executable)
    monkeypatch.setattr(shell, "_host_execution_allowed", lambda: False)
    monkeypatch.setattr(
        code_exec,
        "preflight_subprocess_managed_network",
        _fake_preflight_subprocess_managed_network,
    )
    monkeypatch.setattr(
        code_exec,
        "prepare_subprocess_managed_network_proxy",
        _fake_prepare_subprocess_managed_network_proxy,
    )
    monkeypatch.setattr(code_exec, "run_under_backend", _fake_run_under_backend)

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(tmp_path),
            ),
        )
    )
    try:
        result = json.loads(await code_exec.execute_code("print('no network token here')"))
    finally:
        current_tool_context.reset(token)
        reset_runtime()
        reset_approval_queue()

    assert result["exit_code"] == 0, result
    assert prepare_calls
    assert prepare_calls[0].policy.network is NetworkMode.PROXY_ALLOWLIST
    assert seen["policy"].network is NetworkMode.PROXY_ALLOWLIST
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["HTTP_PROXY"] == "http://127.0.0.1:48123"


def test_trusted_windows_tools_collapse_host_network_to_managed_proxy(
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox.types import (
        NetworkMode,
        ResourceLimits,
        SandboxPolicy,
        SecurityLevel,
    )
    from opensquilla.tools.builtin import code_exec, shell

    policy = SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.HOST,
        mounts=(),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(),
        env_allowlist=("PATH",),
        require_approval=False,
    )
    runtime = SimpleNamespace(
        backend=SimpleNamespace(name="windows_default"),
        settings=SimpleNamespace(network_default="proxy_allowlist"),
    )
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(run_mode=RunMode.TRUSTED),
        )
    )
    try:
        shell_policy = shell._trusted_windows_managed_network_policy(policy, runtime)
        code_policy = code_exec._trusted_windows_managed_network_policy(policy, runtime)
    finally:
        current_tool_context.reset(token)

    assert shell_policy.network is NetworkMode.PROXY_ALLOWLIST
    assert code_policy.network is NetworkMode.PROXY_ALLOWLIST


def test_windows_direct_powershell_argv_injects_proxy_defaults() -> None:
    from opensquilla.tools.builtin import shell

    argv = shell._windows_direct_powershell_argv(
        "Invoke-WebRequest -UseBasicParsing https://example.com"
    )
    command = argv[-1]

    assert "$PSDefaultParameterValues['Invoke-WebRequest:Proxy']" in command
    assert "$PSDefaultParameterValues['Invoke-RestMethod:Proxy']" in command
    assert "[System.Net.WebRequest]::DefaultWebProxy" in command
    assert "Invoke-WebRequest -UseBasicParsing https://example.com" in command
    assert "System.Net.Sockets.TcpClient" not in command
    assert "Invoke-OpenSquillaProxyNetworkFallback" not in command
    assert ";;" not in command


def test_windows_shell_host_handles_invoke_webrequest_status_via_managed_proxy(
    tmp_path: Path,
) -> None:
    from opensquilla.tools.builtin import shell

    with _SingleResponseHttpProxy(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nexample"
    ) as proxy_port:
        env = {
            **os.environ,
            "HTTP_PROXY": f"http://127.0.0.1:{proxy_port}",
            "HTTPS_PROXY": f"http://127.0.0.1:{proxy_port}",
            "NO_PROXY": "",
        }
        result = subprocess.run(
            (
                sys.executable,
                "-c",
                shell._WINDOWS_SANDBOX_SHELL_HOST_CODE,
                "powershell.exe",
                (
                    "Invoke-WebRequest -UseBasicParsing http://example.test "
                    "| Select-Object -ExpandProperty StatusCode"
                ),
                str(tmp_path),
                str(tmp_path),
            ),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "200"


def test_windows_shell_host_handles_try_wrapped_invoke_webrequest_status_via_managed_proxy(
    tmp_path: Path,
) -> None:
    from opensquilla.tools.builtin import shell

    with _SingleResponseHttpProxy(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nexample"
    ) as proxy_port:
        env = {
            **os.environ,
            "HTTP_PROXY": f"http://127.0.0.1:{proxy_port}",
            "HTTPS_PROXY": f"http://127.0.0.1:{proxy_port}",
            "NO_PROXY": "",
        }
        result = subprocess.run(
            (
                sys.executable,
                "-c",
                shell._WINDOWS_SANDBOX_SHELL_HOST_CODE,
                "missing-powershell.exe",
                (
                    "try { Invoke-WebRequest -UseBasicParsing http://example.test "
                    "-TimeoutSec 15 | Select-Object -ExpandProperty StatusCode } "
                    "catch { Write-Output ($_.Exception.GetType().Name + ': ' + "
                    "$_.Exception.Message) }"
                ),
                str(tmp_path),
                str(tmp_path),
            ),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "200"


def test_windows_shell_host_handles_assigned_invoke_webrequest_status_via_managed_proxy(
    tmp_path: Path,
) -> None:
    from opensquilla.tools.builtin import shell

    with _SingleResponseHttpProxy(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nexample"
    ) as proxy_port:
        env = {
            **os.environ,
            "HTTP_PROXY": f"http://127.0.0.1:{proxy_port}",
            "HTTPS_PROXY": f"http://127.0.0.1:{proxy_port}",
            "NO_PROXY": "",
        }
        result = subprocess.run(
            (
                sys.executable,
                "-c",
                shell._WINDOWS_SANDBOX_SHELL_HOST_CODE,
                "missing-powershell.exe",
                (
                    "try { $r = Invoke-WebRequest -UseBasicParsing http://example.test "
                    "-TimeoutSec 15; Write-Output ('StatusCode=' + $r.StatusCode) } "
                    "catch { Write-Output ($_.Exception.GetType().Name + ': ' + "
                    "$_.Exception.Message) }"
                ),
                str(tmp_path),
                str(tmp_path),
            ),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "StatusCode=200"


def test_windows_shell_host_handles_assigned_curl_head_status_via_managed_proxy(
    tmp_path: Path,
) -> None:
    from opensquilla.tools.builtin import shell

    with _SingleResponseHttpProxy(
        b"HTTP/1.1 200 OK\r\nX-Test: yes\r\n\r\n"
    ) as proxy_port:
        env = {
            **os.environ,
            "HTTP_PROXY": f"http://127.0.0.1:{proxy_port}",
            "HTTPS_PROXY": f"http://127.0.0.1:{proxy_port}",
            "NO_PROXY": "",
        }
        result = subprocess.run(
            (
                sys.executable,
                "-c",
                shell._WINDOWS_SANDBOX_SHELL_HOST_CODE,
                "missing-powershell.exe",
                (
                    "try { $r = curl.exe -I http://example.test 2>&1 | "
                    "Select-String 'HTTP/'; Write-Output $r } "
                    "catch { Write-Output ('ERROR: ' + $_.Exception.Message) }"
                ),
                str(tmp_path),
                str(tmp_path),
            ),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "HTTP/1.1 200 OK"


def test_windows_shell_host_handles_curl_head_via_managed_proxy(
    tmp_path: Path,
) -> None:
    from opensquilla.tools.builtin import shell

    with _SingleResponseHttpProxy(
        b"HTTP/1.1 204 No Content\r\nX-Test: yes\r\n\r\n"
    ) as proxy_port:
        env = {
            **os.environ,
            "HTTP_PROXY": f"http://127.0.0.1:{proxy_port}",
            "HTTPS_PROXY": f"http://127.0.0.1:{proxy_port}",
            "NO_PROXY": "",
        }
        result = subprocess.run(
            (
                sys.executable,
                "-c",
                shell._WINDOWS_SANDBOX_SHELL_HOST_CODE,
                "powershell.exe",
                "curl.exe -I http://example.test",
                str(tmp_path),
                str(tmp_path),
            ),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    assert "HTTP/1.1 204 No Content" in result.stdout
    assert "X-Test: yes" in result.stdout


def test_windows_shell_host_handles_remove_item_then_test_path_without_powershell(
    tmp_path: Path,
) -> None:
    from opensquilla.tools.builtin import shell

    target = tmp_path / "delete-me.txt"
    target.write_text("x", encoding="utf-8")

    result = subprocess.run(
        (
            sys.executable,
            "-c",
            shell._WINDOWS_SANDBOX_SHELL_HOST_CODE,
            "missing-powershell.exe",
            f"Remove-Item '{target}' -Force; Test-Path '{target}'",
            str(tmp_path),
            str(tmp_path),
        ),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"
    assert not target.exists()


@pytest.mark.skipif(
    not sys.platform.startswith("win") or shutil.which("powershell.exe") is None,
    reason="requires native Windows PowerShell",
)
def test_windows_shell_host_leaves_output_expressions_to_powershell(
    tmp_path: Path,
) -> None:
    from opensquilla.tools.builtin import shell

    result = subprocess.run(
        (
            sys.executable,
            "-c",
            shell._WINDOWS_SANDBOX_SHELL_HOST_CODE,
            "powershell.exe",
            "Write-Output ('HTTP_PROXY=' + $env:HTTP_PROXY)",
            str(tmp_path),
            str(tmp_path),
        ),
        env={**os.environ, "HTTP_PROXY": "http://127.0.0.1:48123"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "HTTP_PROXY=http://127.0.0.1:48123"


class _SingleResponseHttpProxy:
    def __init__(self, response: bytes) -> None:
        self._response = response
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(1)
        self._socket.settimeout(5)
        self.port = int(self._socket.getsockname()[1])
        self._thread = threading.Thread(target=self._serve_once, daemon=True)

    def __enter__(self) -> int:
        self._thread.start()
        return self.port

    def __exit__(self, *args: object) -> None:
        self._thread.join(timeout=5)
        self._socket.close()

    def _serve_once(self) -> None:
        try:
            conn, _addr = self._socket.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            conn.sendall(self._response)


def test_windows_direct_powershell_argv_does_not_install_socket_fallbacks() -> None:
    from opensquilla.tools.builtin import shell

    argv = shell._windows_direct_powershell_argv(
        "curl.exe -I https://example.com"
    )
    command = argv[-1]

    assert "function Invoke-WebRequest" not in command
    assert "function curl.exe" not in command
    assert "Invoke-OpenSquillaProxyNetworkFallback" not in command
    assert "System.Net.Sockets.TcpClient" not in command


@pytest.mark.asyncio
async def test_code_exec_backend_request_preserves_resolved_run_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.types import SandboxRequest
    from opensquilla.tools.builtin import code_exec, shell

    reset_approval_queue()
    configure_runtime(
        SandboxSettings(
            run_mode="trusted",
            backend="noop",
            allow_legacy_mode=True,
            network_default="none",
        ),
        workspace=tmp_path,
    )
    backend_calls: list[SandboxRequest] = []

    async def _fake_run_under_backend(request, *, runtime=None):
        backend_calls.append(request)
        return SimpleNamespace(
            returncode=0,
            stdout="ok\n",
            stderr="",
            timed_out=False,
            backend_notes=(),
        )

    monkeypatch.setattr(code_exec, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(shell, "_host_execution_allowed", lambda: False)

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(tmp_path),
            ),
        )
    )
    try:
        result = json.loads(await code_exec.execute_code("print('ok')"))
    finally:
        current_tool_context.reset(token)
        reset_runtime()
        reset_approval_queue()

    assert result["stdout"] == "ok\n"
    assert backend_calls
    assert backend_calls[0].run_mode == "trusted"


@pytest.mark.asyncio
async def test_shell_network_command_passes_network_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    from opensquilla.tools.builtin import shell

    calls: list[dict[str, object]] = []

    class _Runtime:
        effective = SimpleNamespace(sandbox_enabled=True)

    async def _fake_gate_action(**kwargs):
        calls.append(kwargs)
        policy = SimpleNamespace()
        request = SimpleNamespace(cwd="/tmp", action_kind="shell.exec", policy=policy)
        return object(), policy, request

    async def _fake_run_under_backend(request, *, runtime=None):
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="", backend_notes=())

    monkeypatch.setattr(shell, "get_runtime", lambda: _Runtime())
    monkeypatch.setattr(shell, "gate_action", _fake_gate_action)
    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(shell, "_host_execution_allowed", lambda: False)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    result = await shell.exec_command("curl https://example.com")

    assert "ok" in result
    assert len(calls) == 1
    hints = calls[0]["hints"]
    assert hints.needs_network is True
    assert hints.high_impact is False


@pytest.mark.asyncio
async def test_powershell_invoke_webrequest_passes_network_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import shell

    calls: list[dict[str, object]] = []

    class _Runtime:
        effective = SimpleNamespace(sandbox_enabled=True)

    async def _fake_gate_action(**kwargs):
        calls.append(kwargs)
        policy = SimpleNamespace()
        request = SimpleNamespace(cwd="/tmp", action_kind="shell.exec", policy=policy)
        return object(), policy, request

    async def _fake_run_under_backend(request, *, runtime=None):
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="", backend_notes=())

    monkeypatch.setattr(shell, "get_runtime", lambda: _Runtime())
    monkeypatch.setattr(shell, "gate_action", _fake_gate_action)
    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(shell, "_host_execution_allowed", lambda: False)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    result = await shell.exec_command(
        'powershell -NoProfile -Command "Invoke-WebRequest -UseBasicParsing https://example.com"'
    )

    assert "ok" in result
    assert len(calls) == 1
    hints = calls[0]["hints"]
    assert hints.needs_network is True
    assert hints.high_impact is False


@pytest.mark.asyncio
async def test_shell_url_text_does_not_pass_network_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import shell

    calls: list[dict[str, object]] = []

    class _Runtime:
        effective = SimpleNamespace(sandbox_enabled=True)

    async def _fake_gate_action(**kwargs):
        calls.append(kwargs)
        policy = SimpleNamespace()
        request = SimpleNamespace(cwd="/tmp", action_kind="shell.exec", policy=policy)
        return object(), policy, request

    async def _fake_run_under_backend(request, *, runtime=None):
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="", backend_notes=())

    monkeypatch.setattr(shell, "get_runtime", lambda: _Runtime())
    monkeypatch.setattr(shell, "gate_action", _fake_gate_action)
    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(shell, "_host_execution_allowed", lambda: False)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    result = await shell.exec_command("echo https://example.com")

    assert "ok" in result
    assert len(calls) == 1
    hints = calls[0]["hints"]
    assert hints.needs_network is False
    assert hints.high_impact is False


@pytest.mark.asyncio
async def test_code_with_url_literal_passes_network_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import code_exec, shell

    calls: list[dict[str, object]] = []

    class _Runtime:
        effective = SimpleNamespace(sandbox_enabled=True)
        workspace = tmp_path

    async def _fake_gate_action(**kwargs):
        calls.append(kwargs)
        policy = SimpleNamespace()
        request = SimpleNamespace(cwd="/tmp", action_kind="code.exec", policy=policy)
        return object(), policy, request

    async def _fake_run_under_backend(request, *, runtime=None):
        return SimpleNamespace(
            returncode=0,
            stdout="ok\n",
            stderr="",
            timed_out=False,
            backend_notes=(),
        )

    monkeypatch.setattr(code_exec, "get_runtime", lambda: _Runtime())
    monkeypatch.setattr(code_exec, "gate_action", _fake_gate_action)
    monkeypatch.setattr(code_exec, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(code_exec, "_resolve_python_bin", lambda *, sandbox_enabled: sys.executable)
    monkeypatch.setattr(shell, "_host_execution_allowed", lambda: False)

    result = json.loads(
        await code_exec.execute_code('import requests\nrequests.get("https://example.com")')
    )

    assert result["stdout"] == "ok\n"
    assert len(calls) == 1
    hints = calls[0]["hints"]
    assert hints.needs_network is True
    assert hints.high_impact is False


@pytest.mark.asyncio
async def test_code_plain_url_literal_does_not_pass_network_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import code_exec, shell

    calls: list[dict[str, object]] = []

    class _Runtime:
        effective = SimpleNamespace(sandbox_enabled=True)
        workspace = tmp_path

    async def _fake_gate_action(**kwargs):
        calls.append(kwargs)
        policy = SimpleNamespace()
        request = SimpleNamespace(cwd="/tmp", action_kind="code.exec", policy=policy)
        return object(), policy, request

    async def _fake_run_under_backend(request, *, runtime=None):
        return SimpleNamespace(
            returncode=0,
            stdout="ok\n",
            stderr="",
            timed_out=False,
            backend_notes=(),
        )

    monkeypatch.setattr(code_exec, "get_runtime", lambda: _Runtime())
    monkeypatch.setattr(code_exec, "gate_action", _fake_gate_action)
    monkeypatch.setattr(code_exec, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(code_exec, "_resolve_python_bin", lambda *, sandbox_enabled: sys.executable)
    monkeypatch.setattr(shell, "_host_execution_allowed", lambda: False)

    result = json.loads(await code_exec.execute_code('print("https://example.com")'))

    assert result["stdout"] == "ok\n"
    assert len(calls) == 1
    hints = calls[0]["hints"]
    assert hints.needs_network is False
    assert hints.high_impact is False


@pytest.mark.asyncio
async def test_shell_unknown_explicit_url_runs_with_managed_proxy(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import shell

    backend_requests: list[object] = []

    async def _fake_run_under_backend(request, *, runtime=None):
        backend_requests.append(request)
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="", backend_notes=())

    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="standard",
            sandbox_run_context=RunContext(run_mode=RunMode.STANDARD),
        )
    )
    try:
        result = await shell.exec_command(
            "curl https://unknown.test/path",
            workdir=str(managed_runtime),
        )
    finally:
        current_tool_context.reset(token)

    assert "exit_code=0" in result
    assert "ok" in result
    assert len(backend_requests) == 1
    request = backend_requests[0]
    assert request.policy.network.value == "proxy_allowlist"
    assert request.policy.network_proxy is not None
    assert request.env["HTTP_PROXY"].startswith("http://127.0.0.1:")
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_windows_proxy_allowlist_runtime_skips_platform_network_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox import integration as integration_mod
    from opensquilla.sandbox.types import (
        NetworkMode,
        NetworkProxySpec,
        ResourceLimits,
        SandboxPolicy,
        SandboxRequest,
        SecurityLevel,
    )

    events = []

    async def fake_prepare_boundary(request, runtime):
        events.append(("prepare", request.policy.network_proxy.port))
        return "ctx"

    async def fake_cleanup_boundary(ctx):
        events.append(("cleanup", ctx))

    class Backend:
        name = "windows_default"

        async def run(self, request):
            events.append(("run", request.policy.network_proxy.port))
            return SimpleNamespace(returncode=0, stdout="ok", stderr="", backend_notes=())

    policy = SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.PROXY_ALLOWLIST,
        network_proxy=NetworkProxySpec(host="127.0.0.1", port=48123),
        mounts=(),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(wall_timeout_s=30),
        env_allowlist=("PATH",),
        require_approval=False,
    )
    request = SandboxRequest(
        argv=("python", "-m", "pip", "install", "humanize"),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=policy,
        env={"PATH": "x"},
    )
    runtime = SimpleNamespace(backend=Backend())

    monkeypatch.setattr(
        integration_mod,
        "_prepare_platform_network_boundary",
        fake_prepare_boundary,
    )
    monkeypatch.setattr(
        integration_mod,
        "_cleanup_platform_network_boundary",
        fake_cleanup_boundary,
    )

    result = await integration_mod.run_under_backend(request, runtime=runtime)

    assert result.stdout == "ok"
    assert events == [("run", 48123)]


@pytest.mark.asyncio
async def test_windows_unready_proxy_allowlist_blocks_network_workarounds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox import integration as integration_mod
    from opensquilla.sandbox.types import (
        NetworkMode,
        ResourceLimits,
        SandboxPolicy,
        SandboxRequest,
        SecurityLevel,
    )

    class _Ledger:
        async def record_denial(self, *args: object, **kwargs: object) -> None:
            return None

    policy = SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.PROXY_ALLOWLIST,
        mounts=(),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(wall_timeout_s=30),
        env_allowlist=("PATH",),
        require_approval=False,
    )
    request = SandboxRequest(
        argv=("powershell", "-Command", "python -m pip install humanize"),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=policy,
        env={"PATH": r"C:\Windows\System32"},
    )
    runtime = SimpleNamespace(
        backend=SimpleNamespace(name="windows_default"),
        workspace=tmp_path,
        ledger=_Ledger(),
    )

    monkeypatch.setattr(
        integration_mod,
        "_windows_proxy_allowlist_enforced",
        lambda runtime: False,
    )

    result = await integration_mod.preflight_subprocess_managed_network(
        request,
        runtime,
    )

    assert result is not None
    assert not isinstance(result, dict)
    assert result.retryable is False
    assert "Windows sandbox managed network is unavailable" in result.message
    assert "PROXY_ALLOWLIST" in result.message
    assert "Do not retry with http_request" in result.message
    assert "Do not retry with web_fetch" in result.message
    assert "Do not retry with offline wheel downloads" in result.message
    assert "Do not retry with host Python" in result.message


@pytest.mark.asyncio
async def test_windows_proxy_allowlist_preflight_does_not_repair_during_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox import integration as integration_mod
    from opensquilla.sandbox.types import (
        NetworkMode,
        ResourceLimits,
        SandboxPolicy,
        SandboxRequest,
        SecurityLevel,
    )

    class _Ledger:
        def __init__(self) -> None:
            self.denials: list[tuple[object, ...]] = []

        async def record_denial(self, *args: object, **kwargs: object) -> None:
            self.denials.append(args)

    policy = SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.PROXY_ALLOWLIST,
        mounts=(),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(wall_timeout_s=30),
        env_allowlist=("PATH",),
        require_approval=False,
    )
    request = SandboxRequest(
        argv=("powershell", "-Command", "curl -I https://example.com"),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=policy,
        env={"PATH": r"C:\Windows\System32"},
    )
    runtime = SimpleNamespace(
        backend=SimpleNamespace(name="windows_default"),
        workspace=tmp_path,
        settings=SandboxSettings(run_mode="trusted", backend="windows_default"),
        ledger=_Ledger(),
    )
    ledger = _Ledger()
    runtime.ledger = ledger

    monkeypatch.setattr(
        integration_mod,
        "_windows_proxy_allowlist_enforced",
        lambda runtime: False,
    )

    async def _fake_ensure_windows_proxy_allowlist_setup(runtime):
        pytest.fail("command preflight must not repair Windows setup or trigger elevation")

    monkeypatch.setattr(
        integration_mod,
        "_ensure_windows_proxy_allowlist_setup",
        _fake_ensure_windows_proxy_allowlist_setup,
        raising=False,
    )
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(tmp_path),
            ),
        )
    )
    try:
        result = await integration_mod.preflight_subprocess_managed_network(
            request,
            runtime,
        )
    finally:
        current_tool_context.reset(token)

    assert result is not None
    assert not isinstance(result, dict)
    assert result.retryable is False
    assert "Windows sandbox managed network is unavailable" in result.message
    assert ledger.denials


@pytest.mark.asyncio
async def test_windows_ready_proxy_allowlist_preflight_continues_to_proxy_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox import integration as integration_mod
    from opensquilla.sandbox.types import (
        NetworkMode,
        ResourceLimits,
        SandboxPolicy,
        SandboxRequest,
        SecurityLevel,
    )

    class _Ledger:
        async def record_denial(self, *args: object, **kwargs: object) -> None:
            pytest.fail("ready Windows network backend should not record denial")

    policy = SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.PROXY_ALLOWLIST,
        mounts=(),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(wall_timeout_s=30),
        env_allowlist=("PATH",),
        require_approval=False,
    )
    request = SandboxRequest(
        argv=("powershell", "-Command", "curl -I https://example.com"),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=policy,
        env={"PATH": r"C:\Windows\System32"},
    )
    runtime = SimpleNamespace(
        backend=SimpleNamespace(name="windows_default"),
        workspace=tmp_path,
        ledger=_Ledger(),
    )

    monkeypatch.setattr(
        integration_mod,
        "_windows_proxy_allowlist_enforced",
        lambda runtime: True,
    )
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(tmp_path),
            ),
        )
    )
    try:
        result = await integration_mod.preflight_subprocess_managed_network(
            request,
            runtime,
        )
    finally:
        current_tool_context.reset(token)

    assert result is None


@pytest.mark.asyncio
async def test_shell_package_install_queues_bundle_approval_before_proxy_run(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import shell

    async def _fail_run_under_backend(request, *, runtime=None):
        pytest.fail("package bundle approval should run before proxy execution")

    monkeypatch.setattr(shell, "run_under_backend", _fail_run_under_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="standard",
            sandbox_run_context=RunContext(run_mode=RunMode.STANDARD),
        )
    )
    try:
        payload = json.loads(
            await shell.exec_command(
                "pip install requests",
                workdir=str(managed_runtime),
            )
        )
    finally:
        current_tool_context.reset(token)

    assert payload["status"] == "approval_required"
    assert payload["approvalKind"] == "sandbox_network"
    assert payload["bundle_id"] == "python-package-install"
    pending = get_approval_queue().list_pending("exec")
    assert len(pending) == 1
    assert pending[0]["params"]["bundle_id"] == "python-package-install"


@pytest.mark.asyncio
async def test_uv_pip_install_queues_bundle_approval_before_proxy_run(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import shell

    async def _fail_run_under_backend(request, *, runtime=None):
        pytest.fail("uv pip package bundle approval should run before proxy execution")

    monkeypatch.setattr(shell, "run_under_backend", _fail_run_under_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="standard",
            sandbox_run_context=RunContext(run_mode=RunMode.STANDARD),
        )
    )
    try:
        payload = json.loads(
            await shell.exec_command(
                "uv pip install --no-cache-dir httpx[http2] pendulum",
                workdir=str(managed_runtime),
            )
        )
    finally:
        current_tool_context.reset(token)

    assert payload["status"] == "approval_required"
    assert payload["approvalKind"] == "sandbox_network"
    assert payload["bundle_id"] == "python-package-install"
    pending = get_approval_queue().list_pending("exec")
    assert len(pending) == 1
    assert pending[0]["params"]["bundle_id"] == "python-package-install"


@pytest.mark.asyncio
async def test_poetry_install_queues_python_bundle_before_proxy_run(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox import integration as integration_mod
    from opensquilla.tools.builtin import shell

    profile_calls: list[tuple[str, ...]] = []

    async def _fail_run_under_backend(request, *, runtime=None):
        pytest.fail("poetry package bundle approval should run before proxy execution")

    def _fake_capability_profile_for_command(argv):
        profile_calls.append(tuple(argv))
        return SimpleNamespace(package_bundles=("python-package-install",))

    monkeypatch.setattr(
        integration_mod,
        "capability_profile_for_command",
        _fake_capability_profile_for_command,
    )
    monkeypatch.setattr(shell, "run_under_backend", _fail_run_under_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="standard",
            sandbox_run_context=RunContext(run_mode=RunMode.STANDARD),
        )
    )
    try:
        payload = json.loads(
            await shell.exec_command(
                "poetry install",
                workdir=str(managed_runtime),
            )
        )
    finally:
        current_tool_context.reset(token)

    assert profile_calls == [("sh", "-lc", "poetry install")]
    assert payload["status"] == "approval_required"
    assert payload["approvalKind"] == "sandbox_network"
    assert payload["bundle_id"] == "python-package-install"
    pending = get_approval_queue().list_pending("exec")
    assert len(pending) == 1
    assert pending[0]["params"]["bundle_id"] == "python-package-install"


@pytest.mark.asyncio
async def test_composer_install_queues_php_bundle_before_proxy_run(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox import integration as integration_mod
    from opensquilla.tools.builtin import shell

    profile_calls: list[tuple[str, ...]] = []

    async def _fail_run_under_backend(request, *, runtime=None):
        pytest.fail("composer package bundle approval should run before proxy execution")

    def _fake_capability_profile_for_command(argv):
        profile_calls.append(tuple(argv))
        return SimpleNamespace(package_bundles=("php-package-install",))

    monkeypatch.setattr(
        integration_mod,
        "capability_profile_for_command",
        _fake_capability_profile_for_command,
    )
    monkeypatch.setattr(shell, "run_under_backend", _fail_run_under_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="standard",
            sandbox_run_context=RunContext(run_mode=RunMode.STANDARD),
        )
    )
    try:
        payload = json.loads(
            await shell.exec_command(
                "composer install",
                workdir=str(managed_runtime),
            )
        )
    finally:
        current_tool_context.reset(token)

    assert profile_calls == [("sh", "-lc", "composer install")]
    assert payload["status"] == "approval_required"
    assert payload["approvalKind"] == "sandbox_network"
    assert payload["bundle_id"] == "php-package-install"
    pending = get_approval_queue().list_pending("exec")
    assert len(pending) == 1
    assert pending[0]["params"]["bundle_id"] == "php-package-install"


@pytest.mark.asyncio
async def test_trusted_uv_pip_install_receives_managed_proxy_without_prompt(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox import integration as integration_mod
    from opensquilla.tools.builtin import shell

    class _FakeProxyServer:
        host = "127.0.0.1"
        port = 48123

        def __init__(self, *args, **kwargs) -> None:
            return None

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    seen: dict[str, object] = {}

    async def _fake_run_under_backend(request, *, runtime=None):
        managed = await integration_mod.prepare_subprocess_managed_network_proxy(
            request,
            runtime=runtime,
        )
        try:
            seen["env"] = managed.request.env
            seen["policy"] = managed.request.policy
            return SimpleNamespace(returncode=0, stdout="installed\n", stderr="", backend_notes=())
        finally:
            await managed.cleanup()

    monkeypatch.setattr(integration_mod, "SandboxProxyServer", _FakeProxyServer)
    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(managed_runtime),
            ),
        )
    )
    try:
        result = await shell.exec_command(
            "uv pip install --no-cache-dir httpx[http2] pendulum",
            workdir=str(managed_runtime),
        )
    finally:
        current_tool_context.reset(token)

    assert "installed" in result
    assert get_approval_queue().list_pending("exec") == []
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["HTTP_PROXY"] == "http://127.0.0.1:48123"
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"]
    assert env["npm_config_proxy"] == env["HTTP_PROXY"]
    assert env["NODE_USE_ENV_PROXY"] == "1"
    assert env["CODEX_NETWORK_PROXY_ACTIVE"] == "1"
    assert env["CODEX_NETWORK_ALLOW_LOCAL_BINDING"] == "0"
    assert env["OPENSQUILLA_SANDBOX_NETWORK"] == "proxy_allowlist"
    assert "GIT_CONFIG_KEY_0" not in env
    assert "GIT_CONFIG_VALUE_0" not in env


@pytest.mark.asyncio
async def test_trusted_python_explicit_url_uses_managed_proxy_before_execution(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.types import NetworkMode, SandboxRequest
    from opensquilla.tools.builtin import shell

    backend_calls: list[SandboxRequest] = []

    async def _fake_run_under_backend(request, *, runtime=None):
        backend_calls.append(request)
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="", backend_notes=())

    async def _fake_cleanup() -> None:
        return None

    async def _fake_prepare_subprocess_managed_network_proxy(request, *, runtime=None):
        return SimpleNamespace(request=request, cleanup=_fake_cleanup)

    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(
        shell,
        "prepare_subprocess_managed_network_proxy",
        _fake_prepare_subprocess_managed_network_proxy,
    )
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(managed_runtime),
            ),
        )
    )
    try:
        result = await shell.exec_command(
            "python - <<'PY'\n"
            "import urllib.request\n"
            "urllib.request.urlopen('https://example.com')\n"
            "PY",
            workdir=str(managed_runtime),
        )
    finally:
        current_tool_context.reset(token)

    assert "ok" in result
    assert len(backend_calls) == 1
    assert backend_calls[0].policy.network is NetworkMode.PROXY_ALLOWLIST


@pytest.mark.asyncio
async def test_trusted_python_caught_network_error_still_uses_proxy_before_execution(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.types import NetworkMode, SandboxRequest
    from opensquilla.tools.builtin import shell

    backend_calls: list[SandboxRequest] = []

    async def _fake_run_under_backend(request, *, runtime=None):
        backend_calls.append(request)
        return SimpleNamespace(returncode=0, stdout="handled\n", stderr="", backend_notes=())

    async def _fake_cleanup() -> None:
        return None

    async def _fake_prepare_subprocess_managed_network_proxy(request, *, runtime=None):
        return SimpleNamespace(request=request, cleanup=_fake_cleanup)

    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(
        shell,
        "prepare_subprocess_managed_network_proxy",
        _fake_prepare_subprocess_managed_network_proxy,
    )
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(managed_runtime),
            ),
        )
    )
    try:
        result = await shell.exec_command(
            "python - <<'PY'\n"
            "import urllib.request\n"
            "try:\n"
            "    urllib.request.urlopen('https://httpbin.org/get', timeout=10)\n"
            "except Exception as e:\n"
            "    print(type(e).__name__ + ': ' + str(e))\n"
            "PY",
            workdir=str(managed_runtime),
        )
    finally:
        current_tool_context.reset(token)

    assert "handled" in result
    assert len(backend_calls) == 1
    assert backend_calls[0].policy.network is NetworkMode.PROXY_ALLOWLIST


@pytest.mark.asyncio
async def test_trusted_npm_view_uses_managed_proxy_before_execution(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.types import NetworkMode, SandboxRequest
    from opensquilla.tools.builtin import shell

    backend_calls: list[SandboxRequest] = []

    async def _fake_run_under_backend(request, *, runtime=None):
        backend_calls.append(request)
        return SimpleNamespace(returncode=0, stdout="4.17.21\n", stderr="", backend_notes=())

    async def _fake_cleanup() -> None:
        return None

    async def _fake_prepare_subprocess_managed_network_proxy(request, *, runtime=None):
        return SimpleNamespace(request=request, cleanup=_fake_cleanup)

    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(
        shell,
        "prepare_subprocess_managed_network_proxy",
        _fake_prepare_subprocess_managed_network_proxy,
    )
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(managed_runtime),
            ),
        )
    )
    try:
        result = await shell.exec_command("npm view lodash version", workdir=str(managed_runtime))
    finally:
        current_tool_context.reset(token)

    assert "4.17.21" in result
    assert len(backend_calls) == 1
    assert backend_calls[0].policy.network is NetworkMode.PROXY_ALLOWLIST


@pytest.mark.asyncio
async def test_trusted_unknown_install_uses_managed_proxy_without_redundant_retry(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.types import SandboxRequest
    from opensquilla.tools.builtin import shell

    backend_calls: list[SandboxRequest] = []
    cleanup_calls = 0

    async def _fake_run_under_backend(request, *, runtime=None):
        backend_calls.append(request)
        if len(backend_calls) == 1:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="curl: (6) Could not resolve host: pypi.org\n",
                backend_notes=(),
            )
        return SimpleNamespace(
            returncode=0,
            stdout="installed\n",
            stderr="",
            backend_notes=(),
        )

    async def _fake_prepare_subprocess_managed_network_proxy(request, *, runtime=None):
        managed_env = dict(request.env)
        managed_env["HTTP_PROXY"] = "http://127.0.0.1:48123"
        managed_env["HTTPS_PROXY"] = managed_env["HTTP_PROXY"]
        managed_request = SandboxRequest(
            argv=request.argv,
            cwd=request.cwd,
            action_kind=request.action_kind,
            policy=request.policy,
            stdin=request.stdin,
            env=managed_env,
            reason=request.reason,
        )

        async def _cleanup() -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1

        return SimpleNamespace(request=managed_request, cleanup=_cleanup)

    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(
        shell,
        "prepare_subprocess_managed_network_proxy",
        _fake_prepare_subprocess_managed_network_proxy,
    )
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(managed_runtime),
            ),
        )
    )
    try:
        result = await shell.exec_command("pip install demo", workdir=str(managed_runtime))
    finally:
        current_tool_context.reset(token)

    assert "Could not resolve host: pypi.org" in result
    assert len(backend_calls) == 1
    assert backend_calls[0].env["HTTP_PROXY"] == "http://127.0.0.1:48123"
    assert cleanup_calls == 1


@pytest.mark.asyncio
async def test_standard_network_failure_does_not_return_package_bundle_recovery_approval(
    standard_runtime_no_preflight: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import shell

    backend_calls = 0

    async def _fake_run_under_backend(request, *, runtime=None):
        nonlocal backend_calls
        backend_calls += 1
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="curl: (6) Could not resolve host: pypi.org\n",
            backend_notes=(),
        )

    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(standard_runtime_no_preflight),
            session_key="s1",
            run_mode="standard",
            sandbox_run_context=RunContext(run_mode=RunMode.STANDARD),
        )
    )
    try:
        result = await shell.exec_command(
            "pip install demo",
            workdir=str(standard_runtime_no_preflight),
        )
    finally:
        current_tool_context.reset(token)

    assert backend_calls == 1
    assert "exit_code=1" in result
    assert "Could not resolve host: pypi.org" in result
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_standard_network_failure_does_not_retry_explicit_url(
    standard_runtime_no_preflight: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import shell

    backend_calls = 0

    async def _fake_run_under_backend(request, *, runtime=None):
        nonlocal backend_calls
        backend_calls += 1
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="curl: (6) Could not resolve host: unknown.test\n",
            backend_notes=(),
        )

    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(standard_runtime_no_preflight),
            session_key="s1",
            run_mode="standard",
            sandbox_run_context=RunContext(run_mode=RunMode.STANDARD),
        )
    )
    try:
        result = await shell.exec_command(
            "curl https://unknown.test/path",
            workdir=str(standard_runtime_no_preflight),
        )
    finally:
        current_tool_context.reset(token)

    assert backend_calls == 1
    assert "exit_code=1" in result
    assert "Could not resolve host: unknown.test" in result
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_standard_network_failure_does_not_retry_with_approved_bundle(
    standard_runtime_no_preflight: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.types import NetworkMode, SandboxRequest
    from opensquilla.tools.builtin import shell

    backend_calls: list[SandboxRequest] = []
    cleanup_calls = 0

    async def _fake_run_under_backend(request, *, runtime=None):
        backend_calls.append(request)
        if len(backend_calls) == 1:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="curl: (6) Could not resolve host: pypi.org\n",
                backend_notes=(),
            )
        return SimpleNamespace(returncode=0, stdout="installed\n", stderr="", backend_notes=())

    async def _fake_prepare_subprocess_managed_network_proxy(request, *, runtime=None):
        managed_env = dict(request.env)
        managed_env["HTTP_PROXY"] = "http://127.0.0.1:48123"
        managed_env["HTTPS_PROXY"] = managed_env["HTTP_PROXY"]
        managed_request = SandboxRequest(
            argv=request.argv,
            cwd=request.cwd,
            action_kind=request.action_kind,
            policy=request.policy,
            stdin=request.stdin,
            env=managed_env,
            reason=request.reason,
        )

        async def _cleanup() -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1

        return SimpleNamespace(request=managed_request, cleanup=_cleanup)

    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(
        shell,
        "prepare_subprocess_managed_network_proxy",
        _fake_prepare_subprocess_managed_network_proxy,
    )
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(standard_runtime_no_preflight),
            session_key="s1",
            run_mode="standard",
            sandbox_run_context=RunContext(
                run_mode=RunMode.STANDARD,
                workspace=str(standard_runtime_no_preflight),
                bundles=(PackageBundleGrant(bundle_id="python-package-install"),),
            ),
        )
    )
    try:
        result = await shell.exec_command(
            "pip install demo",
            workdir=str(standard_runtime_no_preflight),
        )
    finally:
        current_tool_context.reset(token)

    assert "Could not resolve host: pypi.org" in result
    assert len(backend_calls) == 1
    assert cleanup_calls == 0
    assert backend_calls[0].policy.network is NetworkMode.NONE
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_standard_network_failure_does_not_consume_allow_once_grant(
    standard_runtime_no_preflight: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox import integration as integration_mod
    from opensquilla.sandbox.types import (
        NetworkMode,
        ResourceLimits,
        SandboxPolicy,
        SandboxRequest,
        SecurityLevel,
    )
    from opensquilla.tools.builtin import shell

    command = "curl https://unknown.test/path"
    workdir = standard_runtime_no_preflight.resolve(strict=False)
    base_policy = SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.NONE,
        mounts=(),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(),
        env_allowlist=("PATH",),
        require_approval=False,
    )
    approval_policy = SandboxPolicy(
        level=base_policy.level,
        network=NetworkMode.PROXY_ALLOWLIST,
        mounts=base_policy.mounts,
        workspace_rw=base_policy.workspace_rw,
        tmp_writable=base_policy.tmp_writable,
        limits=base_policy.limits,
        env_allowlist=base_policy.env_allowlist,
        require_approval=base_policy.require_approval,
        description=base_policy.description,
        network_proxy=base_policy.network_proxy,
    )
    approval_request = SandboxRequest(
        argv=("sh", "-lc", command),
        cwd=workdir,
        action_kind="shell.exec",
        policy=approval_policy,
        env=dict(os.environ),
    )

    class _FakeProxyServer:
        host = "127.0.0.1"
        port = 48123

        def __init__(self, decide, *args, **kwargs) -> None:
            self._decide = decide

        async def start(self) -> None:
            decision = await self._decide.decide(
                NetworkPolicyRequest(
                    protocol=NetworkProtocol.HTTPS_CONNECT,
                    host="unknown.test",
                    port=443,
                    method="CONNECT",
                )
            )
            assert decision.status == "allow"

        async def stop(self) -> None:
            return None

    async def _fake_gate_action(**kwargs):
        request = SimpleNamespace(
            cwd=workdir,
            action_kind="shell.exec",
            policy=base_policy,
            reason="",
        )
        return object(), base_policy, request

    backend_calls: list[SandboxRequest] = []

    async def _fake_run_under_backend(request, *, runtime=None):
        backend_calls.append(request)
        if len(backend_calls) == 1:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="curl: (6) Could not resolve host: unknown.test\n",
                backend_notes=(),
            )
        return SimpleNamespace(returncode=0, stdout="downloaded\n", stderr="", backend_notes=())

    monkeypatch.setattr(integration_mod, "SandboxProxyServer", _FakeProxyServer)
    monkeypatch.setattr(shell, "gate_action", _fake_gate_action)
    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    grant = TemporaryGrant(
        kind="domain",
        value="unknown.test",
        fingerprint=integration_mod.action_fingerprint(approval_request),
    )
    run_context = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(standard_runtime_no_preflight),
        temporary_grants=(grant,),
    )
    tool_context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.CLI,
        workspace_dir=str(standard_runtime_no_preflight),
        session_key="s1",
        run_mode="standard",
        sandbox_run_context=run_context,
    )
    token = current_tool_context.set(tool_context)
    try:
        result = await shell.exec_command(command, workdir=str(standard_runtime_no_preflight))
    finally:
        current_tool_context.reset(token)

    assert "Could not resolve host: unknown.test" in result
    assert len(backend_calls) == 1
    assert backend_calls[0].policy.network is NetworkMode.NONE
    assert isinstance(tool_context.sandbox_run_context, RunContext)
    assert tool_context.sandbox_run_context.temporary_grants == (grant,)
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_trusted_hostless_private_network_failure_does_not_retry(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import shell

    backend_calls = 0

    async def _fake_run_under_backend(request, *, runtime=None):
        nonlocal backend_calls
        backend_calls += 1
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Network is unreachable\n",
            backend_notes=(),
        )

    async def _fake_preflight_subprocess_managed_network(request, runtime):
        return None

    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(shell, "_sensitive_shell_block", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        shell,
        "preflight_subprocess_managed_network",
        _fake_preflight_subprocess_managed_network,
    )
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(managed_runtime),
            ),
        )
    )
    try:
        result = await shell.exec_command(
            "curl http://127.0.0.1:8000/",
            workdir=str(managed_runtime),
        )
    finally:
        current_tool_context.reset(token)

    assert backend_calls == 1
    assert "Network is unreachable" in result


@pytest.mark.asyncio
async def test_trusted_metadata_target_is_not_auto_retried(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import shell

    backend_calls = 0

    async def _fake_run_under_backend(request, *, runtime=None):
        nonlocal backend_calls
        backend_calls += 1
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Network is unreachable\n",
            backend_notes=(),
        )

    async def _fake_preflight_subprocess_managed_network(request, runtime):
        return None

    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(shell, "_sensitive_shell_block", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        shell,
        "preflight_subprocess_managed_network",
        _fake_preflight_subprocess_managed_network,
    )
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(managed_runtime),
            ),
        )
    )
    try:
        result = await shell.exec_command(
            "curl http://169.254.169.254/latest/meta-data/",
            workdir=str(managed_runtime),
        )
    finally:
        current_tool_context.reset(token)

    assert backend_calls == 1
    assert "exit_code=1" in result
    assert "Network is unreachable" in result
    assert "approval_required" not in result


@pytest.mark.asyncio
async def test_trusted_network_failure_does_not_retry_after_managed_proxy_execution(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox import integration as integration_mod
    from opensquilla.sandbox.types import NetworkMode
    from opensquilla.tools.builtin import shell

    class _FakeProxyServer:
        host = "127.0.0.1"
        port = 48123

        def __init__(self, *args, **kwargs) -> None:
            return None

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    async def _fake_gate_action(**kwargs):
        _, policy, request = await integration_mod.gate_action(**kwargs)
        policy = policy.__class__(
            level=policy.level,
            network=NetworkMode.NONE,
            mounts=policy.mounts,
            workspace_rw=policy.workspace_rw,
            tmp_writable=policy.tmp_writable,
            limits=policy.limits,
            env_allowlist=policy.env_allowlist,
            require_approval=policy.require_approval,
            description=policy.description,
            network_proxy=policy.network_proxy,
        )
        request = SimpleNamespace(
            cwd=request.cwd,
            action_kind=request.action_kind,
            policy=policy,
            reason=getattr(request, "reason", ""),
        )
        return object(), policy, request

    backend_calls = []

    async def _fake_run_under_backend(request, *, runtime=None):
        backend_calls.append(request)
        if len(backend_calls) == 1:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="curl: (6) Could not resolve host: pypi.org\n",
                backend_notes=(),
            )
        return SimpleNamespace(returncode=0, stdout="installed\n", stderr="", backend_notes=())

    monkeypatch.setattr(integration_mod, "SandboxProxyServer", _FakeProxyServer)
    monkeypatch.setattr(shell, "gate_action", _fake_gate_action)
    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(managed_runtime),
            ),
        )
    )
    try:
        result = await shell.exec_command("pip install demo", workdir=str(managed_runtime))
    finally:
        current_tool_context.reset(token)

    assert "Could not resolve host: pypi.org" in result
    assert len(backend_calls) == 1
    assert backend_calls[0].policy.network is NetworkMode.PROXY_ALLOWLIST
    assert backend_calls[0].policy.network_proxy is not None


@pytest.mark.asyncio
async def test_trusted_normal_user_path_denial_requests_broader_retry_review(
    managed_runtime: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.types import SandboxRequest
    from opensquilla.tools.builtin import shell

    outside = tmp_path_factory.mktemp("outside-project")
    backend_calls: list[SandboxRequest] = []

    async def _fake_run_under_backend(request, *, runtime=None):
        backend_calls.append(request)
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="",
            backend_notes=(f"mount denied: {outside}",),
        )

    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(shell, "_sensitive_shell_block", lambda *args, **kwargs: None)
    monkeypatch.setattr(shell, "_sandbox_workdir_access_envelope", lambda *args, **kwargs: None)
    monkeypatch.setattr(shell, "_sandbox_read_path_access_envelope", lambda *args, **kwargs: None)
    monkeypatch.setattr(shell, "_sandbox_write_path_access_envelope", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(managed_runtime),
            ),
        )
    )
    try:
        result = await shell.exec_command("python -m pip install -e .", workdir=str(outside))
    finally:
        current_tool_context.reset(token)

    assert json.loads(result)["status"] == "approval_required"
    assert len(backend_calls) == 1


@pytest.mark.asyncio
async def test_trusted_read_path_denial_requests_broader_retry_review(
    managed_runtime: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.types import SandboxRequest
    from opensquilla.tools.builtin import shell

    outside = tmp_path_factory.mktemp("outside-read")
    target = outside / "data.txt"
    target.write_text("data\n", encoding="utf-8")
    backend_calls: list[SandboxRequest] = []

    async def _fake_run_under_backend(request, *, runtime=None):
        backend_calls.append(request)
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="",
            backend_notes=(f"filesystem.read.denied: {target}",),
        )

    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(shell, "_sensitive_shell_block", lambda *args, **kwargs: None)
    monkeypatch.setattr(shell, "_sandbox_read_path_access_envelope", lambda *args, **kwargs: None)
    monkeypatch.setattr(shell, "_sandbox_write_path_access_envelope", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(managed_runtime),
            ),
        )
    )
    try:
        result = await shell.exec_command("pip install demo", workdir=str(managed_runtime))
    finally:
        current_tool_context.reset(token)

    assert json.loads(result)["status"] == "approval_required"
    assert len(backend_calls) == 1


@pytest.mark.asyncio
async def test_trusted_execve_path_denial_requests_broader_retry_review(
    managed_runtime: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.types import SandboxRequest
    from opensquilla.tools.builtin import shell

    outside = tmp_path_factory.mktemp("outside-exec")
    target = outside / "tool"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    backend_calls: list[SandboxRequest] = []

    async def _fake_run_under_backend(request, *, runtime=None):
        backend_calls.append(request)
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="",
            backend_notes=(f"execve.denied: {target}",),
        )

    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(shell, "_sensitive_shell_block", lambda *args, **kwargs: None)
    monkeypatch.setattr(shell, "_sandbox_read_path_access_envelope", lambda *args, **kwargs: None)
    monkeypatch.setattr(shell, "_sandbox_write_path_access_envelope", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(managed_runtime),
            ),
        )
    )
    try:
        result = await shell.exec_command("pip install demo", workdir=str(managed_runtime))
    finally:
        current_tool_context.reset(token)

    assert json.loads(result)["status"] == "approval_required"
    assert len(backend_calls) == 1


@pytest.mark.asyncio
async def test_trusted_managed_network_denial_requests_broader_retry_review(
    managed_runtime: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.types import SandboxRequest
    from opensquilla.tools.builtin import shell

    outside = tmp_path_factory.mktemp("outside-path-network")
    backend_calls: list[SandboxRequest] = []
    cleanup_calls = 0

    async def _fake_run_under_backend(request, *, runtime=None):
        backend_calls.append(request)
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="",
            backend_notes=(f"mount denied: {outside}",),
        )

    async def _fake_prepare_subprocess_managed_network_proxy(request, *, runtime=None):
        managed_env = dict(request.env)
        managed_env["HTTP_PROXY"] = "http://127.0.0.1:48123"
        managed_env["HTTPS_PROXY"] = managed_env["HTTP_PROXY"]
        managed_request = SandboxRequest(
            argv=request.argv,
            cwd=request.cwd,
            action_kind=request.action_kind,
            policy=request.policy,
            stdin=request.stdin,
            env=managed_env,
            reason=request.reason,
        )

        async def _cleanup() -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1

        return SimpleNamespace(request=managed_request, cleanup=_cleanup)

    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(
        shell,
        "prepare_subprocess_managed_network_proxy",
        _fake_prepare_subprocess_managed_network_proxy,
    )
    monkeypatch.setattr(shell, "_sensitive_shell_block", lambda *args, **kwargs: None)
    monkeypatch.setattr(shell, "_sandbox_workdir_access_envelope", lambda *args, **kwargs: None)
    monkeypatch.setattr(shell, "_sandbox_read_path_access_envelope", lambda *args, **kwargs: None)
    monkeypatch.setattr(shell, "_sandbox_write_path_access_envelope", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(managed_runtime),
            ),
        )
    )
    try:
        result = await shell.exec_command("pip install demo", workdir=str(outside))
    finally:
        current_tool_context.reset(token)

    assert json.loads(result)["status"] == "approval_required"
    assert len(backend_calls) == 1
    assert cleanup_calls == 1
    assert backend_calls[0].env["HTTP_PROXY"] == "http://127.0.0.1:48123"


@pytest.mark.asyncio
async def test_trusted_sensitive_path_denial_does_not_auto_retry(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.types import (
        DenialReason,
        DenialResult,
        SandboxRequest,
        SecurityLevel,
        SuggestedNextStep,
    )
    from opensquilla.tools.builtin import shell

    backend_calls: list[SandboxRequest] = []

    async def _fake_run_under_backend(request, *, runtime=None):
        backend_calls.append(request)
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="",
            backend_notes=("mount denied: /etc/passwd",),
        )

    async def _fake_escalate_backend_denial(*args, **kwargs):
        return DenialResult(
            reason=DenialReason.SEATBELT_DENIED,
            suggested_next_step=SuggestedNextStep.ASK_USER,
            level=SecurityLevel.STANDARD,
            action_fingerprint="test",
            message="denied",
            retryable=False,
        )

    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(shell, "escalate_backend_denial", _fake_escalate_backend_denial)
    monkeypatch.setattr(shell, "_sensitive_shell_block", lambda *args, **kwargs: None)
    monkeypatch.setattr(shell, "_sandbox_read_path_access_envelope", lambda *args, **kwargs: None)
    monkeypatch.setattr(shell, "_sandbox_write_path_access_envelope", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(managed_runtime),
            ),
        )
    )
    try:
        result = await shell.exec_command("cat /etc/passwd", workdir=str(managed_runtime))
    finally:
        current_tool_context.reset(token)

    assert json.loads(result)["status"] == "denied"
    assert len(backend_calls) == 1


@pytest.mark.asyncio
async def test_trusted_successful_network_failure_text_does_not_retry(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import shell

    backend_calls = 0

    async def _fake_run_under_backend(request, *, runtime=None):
        nonlocal backend_calls
        backend_calls += 1
        return SimpleNamespace(
            returncode=0,
            stdout="Could not resolve host: pypi.org\n",
            stderr="",
            backend_notes=(),
        )

    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(managed_runtime),
            ),
        )
    )
    try:
        result = await shell.exec_command("pip install demo", workdir=str(managed_runtime))
    finally:
        current_tool_context.reset(token)

    assert backend_calls == 1
    assert "Could not resolve host: pypi.org" in result


@pytest.mark.asyncio
async def test_timeout_wrapped_node_install_queues_bundle_approval_before_proxy_run(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import shell

    async def _fail_run_under_backend(request, *, runtime=None):
        pytest.fail("node package bundle approval should run before proxy execution")

    monkeypatch.setattr(shell, "run_under_backend", _fail_run_under_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="standard",
            sandbox_run_context=RunContext(run_mode=RunMode.STANDARD),
        )
    )
    try:
        payload = json.loads(
            await shell.exec_command(
                "timeout 30 npm install lodash",
                workdir=str(managed_runtime),
            )
        )
    finally:
        current_tool_context.reset(token)

    assert payload["status"] == "approval_required"
    assert payload["approvalKind"] == "sandbox_network"
    assert payload["bundle_id"] == "node-package-install"
    pending = get_approval_queue().list_pending("exec")
    assert len(pending) == 1
    assert pending[0]["params"]["bundle_id"] == "node-package-install"


@pytest.mark.asyncio
async def test_subprocess_network_approval_uses_session_workspace_for_external_cwd(
    managed_runtime: Path,
) -> None:
    from opensquilla.sandbox import integration as integration_mod
    from opensquilla.sandbox.types import (
        NetworkMode,
        ResourceLimits,
        SandboxPolicy,
        SandboxRequest,
        SecurityLevel,
    )

    external = managed_runtime.parent / f"{managed_runtime.name}-external"
    external.mkdir()
    runtime = get_runtime()
    assert runtime is not None
    request = SandboxRequest(
        argv=("sh", "-lc", "curl https://unknown.test/path"),
        cwd=external,
        action_kind="shell.exec",
        policy=SandboxPolicy(
            level=SecurityLevel.STANDARD,
            network=NetworkMode.PROXY_ALLOWLIST,
            mounts=(),
            workspace_rw=True,
            tmp_writable=True,
            limits=ResourceLimits(),
            env_allowlist=("PATH",),
            require_approval=False,
        ),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="standard",
            sandbox_run_context=RunContext(
                run_mode=RunMode.STANDARD,
                workspace=str(managed_runtime),
            ),
        )
    )
    try:
        payload = await integration_mod.preflight_subprocess_managed_network(
            request,
            runtime,
        )
    finally:
        current_tool_context.reset(token)

    assert payload is None
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_subprocess_network_once_grant_consumes_from_session_workspace(
    managed_runtime: Path,
) -> None:
    from opensquilla.sandbox import integration as integration_mod
    from opensquilla.sandbox.types import (
        NetworkMode,
        ResourceLimits,
        SandboxPolicy,
        SandboxRequest,
        SecurityLevel,
    )

    external = managed_runtime.parent / f"{managed_runtime.name}-external"
    external.mkdir()
    runtime = get_runtime()
    assert runtime is not None
    request = SandboxRequest(
        argv=("sh", "-lc", "curl https://unknown.test/path"),
        cwd=external,
        action_kind="shell.exec",
        policy=SandboxPolicy(
            level=SecurityLevel.STANDARD,
            network=NetworkMode.PROXY_ALLOWLIST,
            mounts=(),
            workspace_rw=True,
            tmp_writable=True,
            limits=ResourceLimits(),
            env_allowlist=("PATH",),
            require_approval=False,
        ),
    )
    grant = TemporaryGrant(
        kind="domain",
        value="unknown.test",
        fingerprint=integration_mod.action_fingerprint(request),
    )
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.CLI,
        workspace_dir=str(managed_runtime),
        session_key="s1",
        run_mode="standard",
        sandbox_run_context=RunContext(
            run_mode=RunMode.STANDARD,
            workspace=str(managed_runtime),
            temporary_grants=(grant,),
        ),
    )

    token = current_tool_context.set(ctx)
    try:
        payload = await integration_mod.preflight_subprocess_managed_network(
            request,
            runtime,
        )
    finally:
        current_tool_context.reset(token)

    assert payload is None
    assert isinstance(ctx.sandbox_run_context, RunContext)
    assert ctx.sandbox_run_context.temporary_grants == (grant,)


@pytest.mark.asyncio
async def test_background_shell_network_spawn_receives_managed_proxy(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox import integration as integration_mod
    from opensquilla.tools.builtin import shell

    class _FakeProxyServer:
        host = "127.0.0.1"
        port = 48123

        def __init__(self, *args, **kwargs) -> None:
            return None

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    class _FakeStream:
        async def read(self, size: int) -> bytes:
            return b""

    class _FakeProcess:
        stdout = _FakeStream()
        stdin = None
        returncode = 0

        async def wait(self) -> int:
            return 0

    seen: dict[str, object] = {}

    async def _fake_spawn(*, runtime: object, request: object) -> object:
        seen["policy"] = request.policy
        seen["env"] = request.env
        assert request.policy.network_proxy is not None
        return shell._SpawnedBackgroundProcess(process=_FakeProcess())  # type: ignore[arg-type]

    monkeypatch.setattr(shell, "_spawn_sandboxed_background_process", _fake_spawn)
    monkeypatch.setattr(shell, "_host_execution_allowed", lambda: False)
    monkeypatch.setattr(integration_mod, "SandboxProxyServer", _FakeProxyServer)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="standard",
            sandbox_run_context=RunContext(
                run_mode=RunMode.STANDARD,
                domains=(DomainGrant(domain="example.com"),),
            ),
        )
    )
    try:
        result = await shell.background_process(
            "curl https://example.com",
            workdir=str(managed_runtime),
            timeout=5,
        )
        session_id = result.splitlines()[0].split("=", 1)[1]
        session = shell._bg_sessions[session_id]
        assert session.collector_task is not None
        await session.collector_task
    finally:
        current_tool_context.reset(token)

    assert "policy" in seen
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["HTTP_PROXY"].startswith("http://127.0.0.1:")
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"]
    assert "127.0.0.1" in env["NO_PROXY"]
    assert env["PIP_PROXY"] == env["HTTP_PROXY"]


@pytest.mark.asyncio
async def test_code_network_subprocess_receives_managed_proxy_env(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox import integration as integration_mod
    from opensquilla.tools.builtin import code_exec, shell

    class _FakeProxyServer:
        host = "127.0.0.1"
        port = 48123

        def __init__(self, *args, **kwargs) -> None:
            return None

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(code_exec, "_resolve_python_bin", lambda *, sandbox_enabled: sys.executable)
    monkeypatch.setattr(shell, "_host_execution_allowed", lambda: False)
    monkeypatch.setattr(integration_mod, "SandboxProxyServer", _FakeProxyServer)
    seen: dict[str, object] = {}

    async def _fake_run_under_backend(request, *, runtime=None):
        managed = await integration_mod.prepare_subprocess_managed_network_proxy(
            request,
            runtime=runtime,
        )
        try:
            seen["env"] = managed.request.env
            seen["policy"] = managed.request.policy
            return SimpleNamespace(
                returncode=0,
                stdout="ok\n",
                stderr="",
                timed_out=False,
                backend_notes=(),
            )
        finally:
            await managed.cleanup()

    monkeypatch.setattr(code_exec, "run_under_backend", _fake_run_under_backend)

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="standard",
            sandbox_run_context=RunContext(
                run_mode=RunMode.STANDARD,
                domains=(DomainGrant(domain="example.com"),),
            ),
        )
    )
    try:
        result = json.loads(
            await code_exec.execute_code(
                "\n".join(
                    (
                        "import os, socket",
                        "url = 'https://example.com/path'",
                        "socket.gethostname()",
                        "print(os.environ.get('HTTP_PROXY', ''))",
                        "print(os.environ.get('HTTPS_PROXY', ''))",
                        "print(os.environ.get('NO_PROXY', '<missing>'))",
                    )
                )
            )
        )
    finally:
        current_tool_context.reset(token)

    assert result["exit_code"] == 0, result
    assert result["stdout"] == "ok\n"
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["HTTP_PROXY"].startswith("http://127.0.0.1:")
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"]
    assert "127.0.0.1" in env["NO_PROXY"]
    assert env["npm_config_https_proxy"] == env["HTTP_PROXY"]


@pytest.mark.asyncio
async def test_code_exec_prepares_managed_proxy_before_backend_run(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dataclasses

    from opensquilla.sandbox.types import NetworkProxySpec
    from opensquilla.tools.builtin import code_exec, shell

    class _Managed:
        def __init__(self, request) -> None:
            self.request = request
            self.cleaned = False

        async def cleanup(self) -> None:
            self.cleaned = True

    monkeypatch.setattr(code_exec, "_resolve_python_bin", lambda *, sandbox_enabled: sys.executable)
    monkeypatch.setattr(shell, "_host_execution_allowed", lambda: False)
    seen: dict[str, object] = {}
    managed_objects: list[_Managed] = []

    async def _fake_preflight(request, runtime):
        return None

    async def _fake_prepare(request, *, runtime=None):
        policy = dataclasses.replace(
            request.policy,
            network_proxy=NetworkProxySpec(host="127.0.0.1", port=48123),
        )
        managed = _Managed(
            request.with_policy(policy).with_policy(policy)
        )
        managed.request.env["HTTP_PROXY"] = "http://127.0.0.1:48123"
        managed.request.env["HTTPS_PROXY"] = "http://127.0.0.1:48123"
        managed.request.env["NO_PROXY"] = ""
        managed_objects.append(managed)
        return managed

    async def _fake_run_under_backend(request, *, runtime=None):
        seen["env"] = request.env
        seen["policy"] = request.policy
        return SimpleNamespace(
            returncode=0,
            stdout="ok\n",
            stderr="",
            timed_out=False,
            backend_notes=(),
        )

    monkeypatch.setattr(code_exec, "preflight_subprocess_managed_network", _fake_preflight)
    monkeypatch.setattr(
        code_exec,
        "prepare_subprocess_managed_network_proxy",
        _fake_prepare,
        raising=False,
    )
    monkeypatch.setattr(code_exec, "run_under_backend", _fake_run_under_backend)

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                domains=(DomainGrant(domain="example.com"),),
            ),
        )
    )
    try:
        result = json.loads(
            await code_exec.execute_code(
                "import urllib.request\nurllib.request.urlopen('https://example.com')"
            )
        )
    finally:
        current_tool_context.reset(token)

    assert result["exit_code"] == 0, result
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["HTTP_PROXY"] == "http://127.0.0.1:48123"
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"]
    assert env["NO_PROXY"] == ""
    assert managed_objects
    assert managed_objects[0].cleaned


@pytest.mark.asyncio
async def test_trusted_code_exec_path_denial_escalates_without_retry(
    managed_runtime: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.types import (
        DenialReason,
        DenialResult,
        SandboxRequest,
        SecurityLevel,
        SuggestedNextStep,
    )
    from opensquilla.tools.builtin import code_exec, shell

    outside = tmp_path_factory.mktemp("outside-code")
    backend_calls: list[SandboxRequest] = []

    async def _fake_run_under_backend(request, *, runtime=None):
        backend_calls.append(request)
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="",
            timed_out=False,
            backend_notes=(f"filesystem.read.denied: {outside}",),
        )

    async def _fake_escalate_backend_denial(*args, **kwargs):
        return DenialResult(
            reason=DenialReason.SEATBELT_DENIED,
            suggested_next_step=SuggestedNextStep.ASK_USER,
            level=SecurityLevel.STANDARD,
            action_fingerprint="test",
            message="denied",
            retryable=False,
        )

    monkeypatch.setattr(code_exec, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(code_exec, "escalate_backend_denial", _fake_escalate_backend_denial)
    monkeypatch.setattr(code_exec, "_resolve_python_bin", lambda *, sandbox_enabled: sys.executable)
    monkeypatch.setattr(shell, "_host_execution_allowed", lambda: False)

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(
                run_mode=RunMode.TRUSTED,
                workspace=str(managed_runtime),
            ),
        )
    )
    try:
        result = json.loads(await code_exec.execute_code("print('ok')"))
    finally:
        current_tool_context.reset(token)

    assert result["status"] == "denied"
    assert len(backend_calls) == 1


@pytest.mark.asyncio
async def test_code_unknown_explicit_url_runs_with_managed_proxy(
    managed_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox import integration as integration_mod
    from opensquilla.sandbox.types import SandboxRequest
    from opensquilla.tools.builtin import code_exec, shell

    class _FakeProxyServer:
        host = "127.0.0.1"
        port = 48123

        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(integration_mod, "SandboxProxyServer", _FakeProxyServer)
    seen: dict[str, object] = {}

    async def _fake_run_under_backend(request, *, runtime=None):
        managed = await integration_mod.prepare_subprocess_managed_network_proxy(
            request,
            runtime=runtime,
        )
        try:
            seen["request"] = managed.request
            return SimpleNamespace(
                returncode=0,
                stdout="ok\n",
                stderr="",
                backend_notes=(),
                timed_out=False,
            )
        finally:
            await managed.cleanup()

    monkeypatch.setattr(code_exec, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(code_exec, "_resolve_python_bin", lambda *, sandbox_enabled: sys.executable)
    monkeypatch.setattr(shell, "_host_execution_allowed", lambda: False)

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(managed_runtime),
            session_key="s1",
            run_mode="standard",
            sandbox_run_context=RunContext(run_mode=RunMode.STANDARD),
        )
    )
    try:
        payload = json.loads(
            await code_exec.execute_code(
                "import urllib.request\n"
                "import os\n"
                "print(os.environ.get('HTTP_PROXY'))\n"
                "print(os.environ.get('HTTPS_PROXY'))\n"
                "print(os.environ.get('NO_PROXY'))\n"
                "print('https://unknown.test/path')\n"
            )
        )
    finally:
        current_tool_context.reset(token)

    assert payload["stdout"] == "ok\n"
    request = seen["request"]
    assert isinstance(request, SandboxRequest)
    assert request.policy.network.value == "proxy_allowlist"
    assert request.policy.network_proxy is not None
    assert request.env["HTTP_PROXY"] == "http://127.0.0.1:48123"
    assert request.env["HTTPS_PROXY"] == "http://127.0.0.1:48123"
    assert "127.0.0.1" in request.env["NO_PROXY"]
    assert get_approval_queue().list_pending("exec") == []
