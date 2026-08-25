from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from opensquilla.git_runtime import GitCapability, GitCapabilityState
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, reset_runtime
from opensquilla.sandbox.permissions import FileSystemPermissionProfile
from opensquilla.sandbox.types import SandboxResult
from opensquilla.tools.builtin import git
from opensquilla.tools.types import ToolContext, current_tool_context


class _FakeProcess:
    returncode = 0
    pid = os.getpid()

    async def communicate(self) -> tuple[bytes, None]:
        return b"## main\n", None


class _NotRepositoryProcess(_FakeProcess):
    returncode = 128

    async def communicate(self) -> tuple[bytes, None]:
        return b"fatal: not a git repository (or any parent directory): .git\n", None


@pytest.fixture(autouse=True)
def _resolved_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        git,
        "resolve_git_capability",
        lambda **_kwargs: GitCapability(
            state=GitCapabilityState.AVAILABLE,
            executable=Path("/resolved/git"),
            source="test",
        ),
    )


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
async def test_git_unavailable_is_reported_without_launching_a_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        git,
        "resolve_git_capability",
        lambda **_kwargs: GitCapability(
            state=GitCapabilityState.UNAVAILABLE,
            reason="git_not_found",
        ),
    )
    monkeypatch.setattr(
        git,
        "create_owned_subprocess_exec",
        lambda *_args, **_kwargs: pytest.fail("unavailable Git must not be launched"),
    )

    with pytest.raises(RuntimeError, match=r"^GIT_UNAVAILABLE:"):
        await git._run_git("status")


@pytest.mark.asyncio
async def test_git_non_repository_failure_has_stable_error_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_runtime()

    async def fake_create_subprocess_exec(
        *args: str, **kwargs: Any
    ) -> _NotRepositoryProcess:
        del args, kwargs
        return _NotRepositoryProcess()

    monkeypatch.setattr(git, "create_owned_subprocess_exec", fake_create_subprocess_exec)
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            workspace_dir=str(tmp_path),
            run_mode="full",
            session_key="agent:main:test",
        )
    )
    try:
        with pytest.raises(RuntimeError, match=r"^GIT_NOT_REPOSITORY:"):
            await git.git_status()
    finally:
        current_tool_context.reset(token)
        reset_runtime()


@pytest.mark.asyncio
async def test_git_status_run_mode_full_uses_host_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_runtime()
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    monkeypatch.setenv("GCM_INTERACTIVE", "Always")
    calls: list[dict[str, Any]] = []

    async def fake_create_subprocess_exec(*args: str, **kwargs: Any) -> _FakeProcess:
        calls.append({"args": args, "kwargs": kwargs})
        return _FakeProcess()

    monkeypatch.setattr(git, "create_owned_subprocess_exec", fake_create_subprocess_exec)
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
    assert len(calls) == 1
    assert calls[0]["args"] == (
        str(Path("/resolved/git")),
        "status",
        "--short",
        "--branch",
    )
    kwargs = calls[0]["kwargs"]
    assert kwargs["stdout"] is git.asyncio.subprocess.PIPE
    assert kwargs["stderr"] is git.asyncio.subprocess.STDOUT
    assert kwargs["cwd"] == str(tmp_path)
    environment = kwargs["env"]
    assert environment["LC_ALL"] == "C"
    assert environment["LANG"] == "C"
    assert environment["GIT_TERMINAL_PROMPT"] == "1"
    assert environment["GCM_INTERACTIVE"] == "Always"


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
    assert request.env == {"LC_ALL": "C", "LANG": "C"}
    assert request.argv[:4] == (
        str(Path("/resolved/git")),
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
    )


class _GbkProcess:
    """Emits GBK/CP936-encoded Chinese bytes (e.g. a filename in git status)."""

    returncode = 0
    pid = os.getpid()

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

    monkeypatch.setattr(git, "create_owned_subprocess_exec", fake_create_subprocess_exec)
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
