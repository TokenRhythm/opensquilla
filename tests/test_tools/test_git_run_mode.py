from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, reset_runtime
from opensquilla.sandbox.permissions import FileSystemPermissionProfile
from opensquilla.sandbox.types import SandboxResult
from opensquilla.tools.builtin import git
from opensquilla.tools.types import ToolContext, current_tool_context


class _FakeProcess:
    returncode = 0

    async def communicate(self) -> tuple[bytes, None]:
        return b"## main\n", None


def test_read_only_git_diff_disables_repository_controlled_helpers() -> None:
    args = git._harden_read_only_git_args(("diff", "--cached"))

    assert args == (
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--cached",
    )


@pytest.mark.asyncio
async def test_git_status_run_mode_full_uses_host_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_runtime()
    calls: list[dict[str, Any]] = []

    async def fake_create_subprocess_exec(*args: str, **kwargs: Any) -> _FakeProcess:
        calls.append({"args": args, "kwargs": kwargs})
        return _FakeProcess()

    monkeypatch.setattr(git.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            workspace_dir=str(tmp_path),
            run_mode="full",
            session_key="agent:main:test",
        )
    )
    try:
        result = await git.git_status()
    finally:
        current_tool_context.reset(token)
        reset_runtime()

    assert result == "## main\n"
    assert calls == [
        {
            "args": ("git", "status", "--short", "--branch"),
            "kwargs": {
                "stdout": git.asyncio.subprocess.PIPE,
                "stderr": git.asyncio.subprocess.STDOUT,
                "cwd": str(tmp_path),
            },
        }
    ]


@pytest.mark.asyncio
async def test_git_uses_runtime_read_only_profile_and_read_only_mounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Any] = []

    async def fake_run_under_backend(request: Any, *, runtime: Any) -> SandboxResult:
        del runtime
        captured.append(request)
        return SandboxResult(
            returncode=0,
            stdout="## main\n",
            stderr="",
            wall_time_s=0.0,
            backend_used="test",
        )

    configure_runtime(
        SandboxSettings(run_mode="trusted", backend="noop", allow_legacy_mode=True),
        workspace=tmp_path,
    )
    monkeypatch.setattr(git, "run_under_backend", fake_run_under_backend)
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            workspace_dir=str(tmp_path),
            run_mode="trusted",
            session_key="restricted-internal-reader",
            sandbox_file_system_profile=FileSystemPermissionProfile.read_only(),
        )
    )
    try:
        result = await git.git_status()
    finally:
        current_tool_context.reset(token)
        reset_runtime()

    assert result == "## main\n"
    assert len(captured) == 1
    request = captured[0]
    assert request.policy.file_system == FileSystemPermissionProfile.read_only()
    assert request.policy.workspace_rw is False
    assert request.policy.tmp_writable is False
    assert all(mount.mode == "ro" for mount in request.policy.mounts)
    assert request.argv[:4] == (
        "git",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
    )


class _GbkProcess:
    """Emits GBK/CP936-encoded Chinese bytes (e.g. a filename in git status)."""

    returncode = 0

    async def communicate(self) -> tuple[bytes, None]:
        # "新建文件" (new file) encoded in GBK — invalid UTF-8, so a naive
        # utf-8/replace decode would mangle it into replacement characters.
        return " M ".encode("ascii") + "新建文件.txt\n".encode("gbk"), None


@pytest.mark.asyncio
async def test_git_host_output_decodes_via_centralized_decoder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The git host fallback must route bytes through the centralized subprocess
    # decoder (not a raw utf-8/replace decode that garbles CJK filenames on
    # Windows, #336 residue). Verify the wiring by spying on the decoder.
    reset_runtime()
    raw = " M ".encode("ascii") + "新建文件.txt\n".encode("gbk")
    seen: list[bytes] = []

    def fake_decode(data: bytes | None, **kwargs: Any) -> str:
        seen.append(bytes(data or b""))
        return "新建文件.txt (decoded)"

    monkeypatch.setattr(
        "opensquilla.subprocess_encoding.decode_subprocess_output", fake_decode
    )

    async def fake_create_subprocess_exec(*args: str, **kwargs: Any) -> _GbkProcess:
        return _GbkProcess()

    monkeypatch.setattr(git.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            workspace_dir=str(tmp_path),
            run_mode="full",
            session_key="agent:main:test",
        )
    )
    try:
        result = await git.git_status()
    finally:
        current_tool_context.reset(token)
        reset_runtime()

    # Git output flowed through the centralized decoder (not a raw .decode()).
    assert seen == [raw]
    assert result == "新建文件.txt (decoded)"
