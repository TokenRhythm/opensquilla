from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from opensquilla.sandbox.backend import (
    bubblewrap,
    linux_payload,
    noop,
    seatbelt,
    windows_default,
)
from opensquilla.sandbox.types import (
    NetworkMode,
    ResourceLimits,
    SandboxPolicy,
    SandboxRequest,
    SecurityLevel,
)
from opensquilla.skills.runtime_env import MEDIA_FONTS_DIR_ENV, PAPER_FONTS_ENV
from opensquilla.tools.builtin import shell
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


@pytest.fixture(autouse=True)
def _owner_context():
    token = current_tool_context.set(
        ToolContext(is_owner=True, caller_kind=CallerKind.CLI, session_key="agent:main:test")
    )
    yield
    current_tool_context.reset(token)


def _managed_env(base: Any) -> dict[str, str]:
    return {
        **dict(base),
        "PATH": os.pathsep.join(("/managed", "/system")),
        MEDIA_FONTS_DIR_ENV: "/managed/media-fonts",
        PAPER_FONTS_ENV: "/managed/paper-fonts",
    }


@pytest.mark.asyncio
async def test_exec_command_passes_managed_environment_to_host_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    async def fake_run(command: str, **kwargs: object) -> str:
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return "exit_code=0\nok\n"

    monkeypatch.setattr(shell, "get_runtime", lambda: None)
    monkeypatch.setattr(shell, "managed_skill_env", _managed_env)
    monkeypatch.setattr(shell, "_run_host_shell_command", fake_run)

    result = await shell.exec_command("echo ok", workdir=str(tmp_path))

    assert result == "exit_code=0\nok\n"
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PATH"].split(os.pathsep)[:2] == ["/managed", "/system"]
    assert env[MEDIA_FONTS_DIR_ENV] == "/managed/media-fonts"
    assert env[PAPER_FONTS_ENV] == "/managed/paper-fonts"


@pytest.mark.asyncio
async def test_full_host_exec_passes_managed_environment_to_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    async def fake_run(command: str, **kwargs: object) -> str:
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return "exit_code=0\nok\n"

    monkeypatch.setattr(shell, "get_runtime", lambda: None)
    monkeypatch.setattr(shell, "full_host_access_active", lambda: True)
    monkeypatch.setattr(shell, "managed_skill_env", _managed_env)
    monkeypatch.setattr(shell, "_run_host_shell_command", fake_run)

    result = await shell.exec_command("echo ok", workdir=str(tmp_path))

    assert result == "exit_code=0\nok\n"
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PATH"].split(os.pathsep)[:2] == ["/managed", "/system"]
    assert env[MEDIA_FONTS_DIR_ENV] == "/managed/media-fonts"
    assert env[PAPER_FONTS_ENV] == "/managed/paper-fonts"


@pytest.mark.asyncio
async def test_background_process_passes_managed_environment_to_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class _Stream:
        async def read(self, _size: int) -> bytes:
            return b""

    class _Process:
        pid = 12345
        stdin = None
        stdout = _Stream()
        returncode: int | None = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    async def fake_spawn(command: str, **kwargs: object) -> _Process:
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return _Process()

    monkeypatch.setattr(shell, "get_runtime", lambda: None)
    monkeypatch.setattr(shell, "managed_skill_env", _managed_env)
    monkeypatch.setattr(shell, "_create_host_shell_subprocess", fake_spawn)

    result = await shell.background_process("echo ok", workdir=str(tmp_path))
    session_id = result.splitlines()[0].split("=", 1)[1]
    session = shell._bg_sessions[session_id]
    assert session.collector_task is not None
    await session.collector_task

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PATH"].split(os.pathsep)[:2] == ["/managed", "/system"]
    assert env[MEDIA_FONTS_DIR_ENV] == "/managed/media-fonts"
    assert env[PAPER_FONTS_ENV] == "/managed/paper-fonts"
    shell._bg_sessions.pop(session_id, None)


def test_sandbox_policy_mounts_managed_toolchains_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    toolchain = tmp_path / "toolchain"
    external = tmp_path / "external"
    toolchain.mkdir()
    external.mkdir()
    monkeypatch.setattr(
        shell,
        "managed_toolchain_readonly_paths",
        lambda: (toolchain, external),
    )
    policy = SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.NONE,
        mounts=(),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(),
        env_allowlist=("PATH",),
        require_approval=False,
    )

    result = shell._policy_with_managed_toolchain_mounts(policy)

    assert {(mount.host_path, mount.mode) for mount in result.mounts} == {
        (toolchain, "ro"),
        (external, "ro"),
    }
    assert all(mount.required is False for mount in result.mounts)
    assert MEDIA_FONTS_DIR_ENV in result.env_allowlist
    assert PAPER_FONTS_ENV in result.env_allowlist


def test_sandbox_backends_preserve_both_managed_font_environments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(shell, "managed_toolchain_readonly_paths", lambda: ())
    policy = shell._policy_with_managed_toolchain_mounts(
        SandboxPolicy(
            level=SecurityLevel.STANDARD,
            network=NetworkMode.NONE,
            mounts=(),
            workspace_rw=True,
            tmp_writable=True,
            limits=ResourceLimits(),
            env_allowlist=("PATH",),
            require_approval=False,
        )
    )
    managed = {
        "PATH": "/managed:/system",
        MEDIA_FONTS_DIR_ENV: "/managed/media-fonts",
        PAPER_FONTS_ENV: "/managed/paper-fonts",
        "SHOULD_BE_FILTERED": "secret",
    }
    request = SandboxRequest(
        argv=("env",),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=policy,
        env=managed,
    )

    filtered_environments = (
        bubblewrap._direct_bwrap_env(policy, managed),
        linux_payload._env_payload(policy, managed),
        noop._filtered_request_env(request),
        seatbelt.seatbelt_env_for_policy(policy, managed, tmp_dir=None),
        windows_default._allowed_env(request),
    )

    for env in filtered_environments:
        assert env[MEDIA_FONTS_DIR_ENV] == "/managed/media-fonts"
        assert env[PAPER_FONTS_ENV] == "/managed/paper-fonts"
        assert "SHOULD_BE_FILTERED" not in env
