from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from opensquilla import workspace_files
from opensquilla.sandbox import filesystem_worker
from opensquilla.sandbox.operation_runtime import SandboxOperationResult
from opensquilla.sandbox.types import SandboxBackendError
from opensquilla.tools.builtin import filesystem
from opensquilla.tools.types import ToolContext, current_tool_context


def _reference(identity: str, relative: str = "data.txt"):
    return {
        "workspaceId": identity,
        "relativePath": relative,
        "name": "data.txt",
        "mime": "text/plain",
    }


def _session(root: Path):
    return SimpleNamespace(
        workspace_id=None,
        execution_workspace={
            "version": 1,
            "id": str(uuid4()),
            "kind": "configured",
            "root": str(root),
        },
    )


@pytest.mark.parametrize(
    "relative", ["../x", "a/../x", "a//x", "a/./x", "/x", "C:/x", "a\\x", "x:stream", "a\nx"]
)
def test_workspace_references_reject_noncanonical_paths(relative):
    with pytest.raises(ValueError, match="canonical relative path"):
        workspace_files.normalize_workspace_files([_reference("project", relative)])


@pytest.mark.parametrize(
    "field,value", [("name", "unsafe\nmarker"), ("mime", "text/plain\nmarker"), ("size", True)]
)
def test_workspace_reference_metadata_has_no_control_or_boolean_size(field, value):
    ref = _reference("project")
    ref[field] = value
    with pytest.raises(ValueError):
        workspace_files.normalize_workspace_files([ref])


def test_workspace_reference_cannot_smuggle_absolute_path_or_authority():
    ref = {**_reference("project"), "path": "/synthetic/private/file", "readAllowed": True}
    with pytest.raises(ValueError, match="invalid workspace file"):
        workspace_files.normalize_workspace_files([ref])


def test_canonical_workspace_file_rejects_linked_parent(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.txt").write_text("outside")
    try:
        (project / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Host cannot create symlinks")
    with pytest.raises(ValueError, match="link or junction"):
        workspace_files.canonical_workspace_file(project, "linked/data.txt")


def test_canonical_workspace_file_rejects_windows_reparse_marker(tmp_path, monkeypatch):
    path = tmp_path / "data.txt"
    path.write_text("data")
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)
    monkeypatch.setattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", marker, raising=False)
    original = Path.lstat

    def metadata(candidate):
        value = original(candidate)
        if candidate == path:
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=marker)
        return value

    monkeypatch.setattr(Path, "lstat", metadata)
    with pytest.raises(ValueError, match="link or junction"):
        workspace_files.canonical_workspace_file(tmp_path, path.name)


@pytest.mark.asyncio
async def test_valid_workspace_file_is_live_and_does_not_copy_material(tmp_path):
    path = tmp_path / "data.txt"
    path.write_text("before")
    session = _session(tmp_path)
    ref = _reference(session.execution_workspace["id"])
    context = ToolContext(workspace_dir=str(tmp_path), run_mode="full")
    first = await workspace_files.validate_workspace_files(
        [ref], session=session, storage=None, tool_context=context
    )
    path.write_text("after")
    second = await workspace_files.validate_workspace_files(
        [ref], session=session, storage=None, tool_context=context
    )
    assert first[0].path == second[0].path == path
    assert path.read_text() == "after"
    assert not (tmp_path / ".opensquilla").exists()


@pytest.mark.asyncio
async def test_workspace_reference_rejects_rebinding_and_missing_file(tmp_path):
    path = tmp_path / "data.txt"
    path.write_text("before")
    session = _session(tmp_path)
    ref = _reference(session.execution_workspace["id"])
    context = ToolContext(workspace_dir=str(tmp_path), run_mode="full")
    session.execution_workspace["id"] = str(uuid4())
    with pytest.raises(ValueError, match="binding changed"):
        await workspace_files.validate_workspace_files(
            [ref], session=session, storage=None, tool_context=context
        )
    ref["workspaceId"] = session.execution_workspace["id"]
    path.unlink()
    with pytest.raises(FileNotFoundError):
        await workspace_files.validate_workspace_files(
            [ref], session=session, storage=None, tool_context=context
        )


@pytest.mark.asyncio
async def test_workspace_probe_uses_executor_without_parsing_or_write_permission(
    tmp_path, monkeypatch
):
    path = tmp_path / "data.docx"
    path.write_bytes(b"deliberately not a document")
    operations = []

    async def execute(operation, **kwargs):
        operations.append(operation)
        payload = {
            "kind": operation.kind,
            **operation.request.to_payload(),
            "permissions": {
                "filesystem": {
                    "profile": {
                        "entries": [{"path": str(tmp_path), "access": "read"}],
                        "deniedReadGlobs": [],
                        "defaultAccess": "deny",
                    }
                },
            },
        }
        return SandboxOperationResult.from_worker_stdout(
            json.dumps(filesystem_worker._run(payload))
        )

    monkeypatch.setattr(filesystem, "_run_sandbox_operation_if_required", execute)
    context = ToolContext(workspace_dir=str(tmp_path), run_mode="safe")
    await workspace_files.probe_file_access(path, context)
    assert [operation.kind for operation in operations] == ["probe_file"]
    assert path.read_bytes() == b"deliberately not a document"
    assert not context.sandbox_mounts
    assert not context.workspace_file_writes


@pytest.mark.asyncio
async def test_workspace_probe_does_not_fallback_after_backend_denial(tmp_path, monkeypatch):
    path = tmp_path / "data.txt"
    path.write_text("data")

    async def deny(*args, **kwargs):
        raise SandboxBackendError("effective executor denied")

    def forbidden(*args, **kwargs):
        pytest.fail("An executor denial must not fall back to host filesystem reads")

    monkeypatch.setattr(filesystem, "_run_sandbox_operation_if_required", deny)
    monkeypatch.setattr(workspace_files.os, "open", forbidden)
    context = ToolContext(workspace_dir=str(tmp_path))
    with pytest.raises(SandboxBackendError, match="denied"):
        await workspace_files.probe_file_access(path, context)
    assert current_tool_context.get() is None


@pytest.mark.asyncio
async def test_workspace_probe_rejects_mismatching_executor_file_identity(tmp_path, monkeypatch):
    path = tmp_path / "data.txt"
    path.write_text("data")

    async def substitute(*args, **kwargs):
        return SandboxOperationResult(message="readable", metadata={"device": -1, "inode": -1})

    monkeypatch.setattr(filesystem, "_run_sandbox_operation_if_required", substitute)
    with pytest.raises(ValueError, match="changed while opening"):
        await workspace_files.probe_file_access(path, ToolContext(workspace_dir=str(tmp_path)))


@pytest.mark.asyncio
async def test_workspace_probe_rejects_host_file_descriptor_substitution(tmp_path, monkeypatch):
    path = tmp_path / "data.txt"
    other = tmp_path / "other.txt"
    path.write_text("data")
    other.write_text("other")
    real_open = os.open
    monkeypatch.setattr(
        workspace_files.os, "open", lambda candidate, flags: real_open(other, flags)
    )
    context = ToolContext(workspace_dir=str(tmp_path), run_mode="full")
    with pytest.raises(ValueError, match="changed while opening"):
        await workspace_files.probe_file_access(path, context)
