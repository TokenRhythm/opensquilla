from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from opensquilla.paths import native_io_path
from opensquilla.sandbox.integration import reset_runtime
from opensquilla.tools.builtin import code_exec, filesystem, shell
from opensquilla.tools.path_policy import resolve_bound_workspace
from opensquilla.tools.types import (
    CallerKind,
    InteractionMode,
    ToolContext,
    WorkspaceAccessError,
    current_tool_context,
)


@contextmanager
def _bound_context(workspace: Path | None, *, scratch: Path | None = None):
    context = ToolContext(
        is_owner=True, caller_kind=CallerKind.CLI, run_mode="full",
        workspace_dir=str(workspace) if workspace is not None else None,
        scratch_dir=str(scratch) if scratch is not None else None,
    )
    token = current_tool_context.set(context)
    try:
        yield context
    finally:
        current_tool_context.reset(token)


@pytest.fixture(autouse=True)
def _without_runtime():
    reset_runtime()
    yield
    reset_runtime()


async def test_file_shell_and_python_share_bound_root_and_workdir_is_per_call(tmp_path):
    workspace = tmp_path / "项目 空格"
    workspace.mkdir()
    subdir = workspace / "sub dir"
    subdir.mkdir()
    original_cwd = Path.cwd()
    def write_command(value: str, name: str) -> str:
        if os.name == "nt":
            return f"Set-Content -LiteralPath {name} -Value {value} -Encoding utf8"
        return f"echo {value} > {name}"

    with _bound_context(workspace) as context:
        await shell.exec_command(write_command("from-shell", "shell.txt"), timeout=10)
        result = json.loads(await code_exec.execute_code(
            "from pathlib import Path\n"
            "assert 'from-shell' in Path('shell.txt').read_text()\n"
            "Path('python.txt').write_text('from-python', encoding='utf-8')\n"
            "print(Path.cwd())",
        ))
        assert result["exit_code"] == 0, result
        assert str(workspace) in result["stdout"]
        assert "from-python" in await filesystem.read_file("python.txt")

        await shell.exec_command(
            write_command("override", "scoped.txt"), workdir="sub dir", timeout=10,
        )
        await shell.exec_command(write_command("default", "scoped.txt"), timeout=10)
        assert "override" in (subdir / "scoped.txt").read_text(encoding="utf-8")
        assert "default" in (workspace / "scoped.txt").read_text(encoding="utf-8")
        assert context.workspace_dir == str(workspace)

    assert Path.cwd() == original_cwd
    assert not (tmp_path / "shell.txt").exists()
    assert not (tmp_path / "python.txt").exists()


async def _use_bound_root(tool_name: str):
    if tool_name == "read":
        return await filesystem.read_file("marker.txt")
    if tool_name == "write":
        return await filesystem.write_file("marker.txt", "unexpected")
    if tool_name == "list":
        return await filesystem.list_dir(".")
    if tool_name == "shell":
        return await shell.exec_command("echo unexpected > marker.txt", timeout=10)
    return await code_exec.execute_code("from pathlib import Path\nPath('marker.txt').touch()")


@pytest.mark.parametrize("tool_name", ["read", "write", "list", "shell", "python"])
async def test_missing_bound_root_is_not_recreated_or_replaced_by_temp(
    tmp_path, monkeypatch, tool_name,
):
    workspace = tmp_path / "missing-project"

    def reject_temp(*args, **kwargs):
        raise AssertionError("A bound project must never fall back to a temporary directory")

    monkeypatch.setattr(code_exec.tempfile, "mkdtemp", reject_temp)
    with _bound_context(workspace):
        with pytest.raises(WorkspaceAccessError, match="workspace_unavailable"):
            await _use_bound_root(tool_name)
    assert not workspace.exists()


@pytest.mark.parametrize("tool_name", ["read", "write", "list", "shell", "python"])
async def test_retargeted_bound_root_is_rejected(tmp_path, tool_name):
    workspace = tmp_path / "project"
    workspace.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (other / "marker.txt").write_text("preserve", encoding="utf-8")
    with _bound_context(workspace):
        workspace.rmdir()
        try:
            if os.name == "nt":
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(workspace), str(other)],
                    check=True, capture_output=True,
                )
            else:
                workspace.symlink_to(other, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"Directory symlinks are unavailable: {exc}")
        try:
            with pytest.raises(WorkspaceAccessError, match="workspace_binding_changed"):
                await _use_bound_root(tool_name)
        finally:
            if os.name == "nt":
                workspace.rmdir()
            else:
                workspace.unlink()
    assert (other / "marker.txt").read_text(encoding="utf-8") == "preserve"


def test_bound_root_checks_path_meaning_without_pinning_directory_identity(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    assert resolve_bound_workspace(str(workspace)) == workspace
    workspace.rmdir()
    workspace.mkdir()
    assert resolve_bound_workspace(str(workspace)) == workspace
    assert resolve_bound_workspace(str(native_io_path(workspace))) == native_io_path(workspace)
    if os.name == "nt":
        assert resolve_bound_workspace(str(workspace).upper()) == workspace


async def test_unbound_internal_python_job_retains_temporary_workspace():
    with _bound_context(None) as context:
        context.interaction_mode = InteractionMode.UNATTENDED
        result = json.loads(await code_exec.execute_code(
            "from pathlib import Path\nprint(Path.cwd())\nPath('scratch.txt').touch()",
        ))
    assert result["exit_code"] == 0, result
    scratch = Path(result["stdout"].strip())
    assert scratch.name.startswith("opensquilla_exec_")
    assert not scratch.exists()


async def test_explicit_scratch_write_does_not_need_a_project_root(tmp_path):
    scratch = tmp_path / "scratch"
    with _bound_context(None, scratch=scratch) as context:
        context.interaction_mode = InteractionMode.UNATTENDED
        result = json.loads(await filesystem.write_scratch("experiment.txt", "scratch-only"))
    assert result["status"] == "written"
    assert (scratch / "experiment.txt").read_text(encoding="utf-8") == "scratch-only"
