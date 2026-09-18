from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from opensquilla.attachment_refs import make_attachment_ref, write_transcript_material
from opensquilla.attachment_workspace import AttachmentWorkspaceMaterializer
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import TranscriptEntry
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.builtin import filesystem
from opensquilla.tools.types import SafeToolError, ToolContext, current_tool_context


@pytest_asyncio.fixture
async def storage():
    value = SessionStorage(":memory:")
    await value.connect()
    yield value
    await value.close()


async def _parent(storage: SessionStorage, tmp_path: Path, count: int = 1):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    media_root = tmp_path / "media"
    async def full_context(session):
        return ToolContext(
            workspace_dir=str(workspace), artifact_session_id=session.session_id,
            session_key=session.session_key, run_mode="full", workspace_strict=True,
        )

    manager = SessionManager(
        storage, inject_time_prefix=False, media_root=media_root,
        attachment_fork_context_resolver=full_context,
    )
    parent = await manager.create(
        "agent:main:main",
        execution_workspace={
            "version": 1,
            "id": str(uuid4()),
            "kind": "configured",
            "root": str(workspace),
        },
    )
    records = {}
    materializer = AttachmentWorkspaceMaterializer(
        media_root=media_root,
        workspace_dir=workspace,
        working_files=records,
    )
    context = ToolContext(
        workspace_dir=str(workspace),
        artifact_session_id=parent.session_id,
        run_mode="full",
        attachment_working_files=records,
    )
    messages = []
    token = current_tool_context.set(context)
    try:
        for index in range(count):
            payload = f"original {index}\n".encode()
            sha, _, _ = write_transcript_material(
                media_root=media_root,
                session_id=parent.session_id,
                payload=payload,
            )
            ref = make_attachment_ref(
                sha256=sha,
                name=f"input{index}.txt",
                mime="text/plain",
                size=len(payload),
                session_id=parent.session_id,
                source="transcript",
            )
            original = materializer.materialize(ref)
            assert original.rel_path
            await filesystem.edit_file(original.rel_path, "original", "parent edit")
            entry = TranscriptEntry(
                session_id=parent.session_id,
                session_key=parent.session_key,
                role="user",
                content=json.dumps({"text": "Read attachment", "attachments": [ref]}),
                token_count=20,
            )
            await storage.append_transcript_entry(entry)
            messages.append(entry.message_id)
    finally:
        current_tool_context.reset(token)
    parent = await manager.update(parent.session_key, origin={"attachment_working_files": records})
    return manager, parent, workspace, records, messages


@pytest.mark.asyncio
async def test_manager_branch_persists_independent_current_working_bytes(storage, tmp_path):
    manager, parent, workspace, records, _ = await _parent(storage, tmp_path)
    child = await manager.branch(
        parent.session_key, "agent:main:direct:child", fork_transcript=True
    )
    restored = await SessionManager(storage).get_session(child.session_key)
    assert restored is not None
    child_records = restored.origin["attachment_working_files"]
    assert len(child_records) == 1
    key, child_record = next(iter(child_records.items()))
    assert child_record["session_id"] == child.session_id
    assert f"/{child.session_id}/" in key
    assert (workspace / child_record["path"]).read_text() == "parent edit 0\n"
    token = current_tool_context.set(
        ToolContext(
            workspace_dir=str(workspace),
            artifact_session_id=child.session_id,
            run_mode="full",
            attachment_working_files=child_records,
        )
    )
    try:
        await filesystem.edit_file(key, "parent edit", "child edit")
        assert "child edit" in await filesystem.read_file(key)
    finally:
        current_tool_context.reset(token)
    parent_record = next(iter(records.values()))
    assert (workspace / parent_record["path"]).read_text() == "parent edit 0\n"
    assert (workspace / next(iter(records))).read_text() == "original 0\n"


@pytest.mark.asyncio
async def test_manager_prefix_branch_excludes_later_working_files(storage, tmp_path):
    manager, parent, _, records, messages = await _parent(storage, tmp_path, count=2)
    assert len(records) == 2
    child = await manager.branch(
        parent.session_key,
        "agent:main:direct:prefix",
        fork_transcript=True,
        fork_before_message_id=messages[1],
    )
    child_records = child.origin["attachment_working_files"]
    assert len(child_records) == 1
    assert next(iter(child_records)).endswith("input0.txt")


@pytest.mark.asyncio
async def test_skipped_transcript_fork_does_not_copy_working_files(storage, tmp_path):
    manager, parent, workspace, _, _ = await _parent(storage, tmp_path)
    child = await manager.branch(
        parent.session_key,
        "agent:main:direct:small",
        fork_transcript=True,
        max_fork_tokens=0,
    )
    assert child.forked_from_parent is False
    assert not (child.origin or {}).get("attachment_working_files")
    assert not (workspace / ".opensquilla" / "attachments" / child.session_id).exists()


@pytest.mark.asyncio
async def test_preparing_uncommitted_prefix_branch_allocates_no_working_files(storage, tmp_path):
    manager, parent, workspace, records, messages = await _parent(storage, tmp_path, count=2)
    intent = await manager.prepare_prefix_branch(
        parent.session_key,
        "agent:main:direct:uncommitted",
        fork_before_message_id=messages[1],
    )
    assert await storage.get_session(intent.node.session_key) is None
    assert not (workspace / ".opensquilla" / "attachments" / intent.node.session_id).exists()
    assert len(intent.node.origin["attachment_working_files"]) == 1
    for entry in records.values():
        assert "parent edit" in (workspace / entry["path"]).read_text()


@pytest.mark.asyncio
async def test_fork_without_current_policy_reserves_unavailable_working_target(storage, tmp_path):
    _, parent, workspace, records, _ = await _parent(storage, tmp_path)
    manager = SessionManager(storage, media_root=tmp_path / "media")
    child = await manager.branch(
        parent.session_key, "agent:main:direct:no-policy", fork_transcript=True,
    )
    child_records = child.origin["attachment_working_files"]
    assert len(child_records) == 1
    assert not (workspace / next(iter(child_records.values()))["path"]).exists()
    assert (workspace / next(iter(records.values()))["path"]).read_text() == "parent edit 0\n"
    token = current_tool_context.set(ToolContext(
        workspace_dir=str(workspace), artifact_session_id=child.session_id,
        run_mode="full", attachment_working_files=child_records,
    ))
    try:
        with pytest.raises(SafeToolError, match="missing|substituted"):
            await filesystem.read_file(next(iter(child_records)))
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["source_read", "target_write"])
@pytest.mark.parametrize("mode", ["safe", "full"])
async def test_fork_enforces_both_file_policies_before_copy(
    storage, tmp_path, monkeypatch, denial, mode,
):
    from opensquilla.sandbox import filesystem_worker
    from opensquilla.sandbox.operation_runtime import SandboxOperationResult
    from opensquilla.sandbox.permissions import (
        FileSystemAccess,
        FileSystemPermissionEntry,
        FileSystemPermissionProfile,
    )

    manager, parent, workspace, records, _ = await _parent(storage, tmp_path)
    source = workspace / next(iter(records.values()))["path"]
    profile = FileSystemPermissionProfile(entries=(
        FileSystemPermissionEntry(workspace, FileSystemAccess.READ),
        FileSystemPermissionEntry(source, (
            FileSystemAccess.DENY if denial == "source_read" else FileSystemAccess.READ
        )),
    ))

    async def context(session):
        return ToolContext(
            workspace_dir=str(workspace), artifact_session_id=session.session_id,
            run_mode=mode, workspace_strict=True, sandbox_file_system_profile=profile,
        )

    manager._attachment_fork_context_resolver = context
    calls = []

    async def executor(operation, **kwargs):
        calls.append(operation.kind)
        assert operation.kind == "fork_attachment"
        assert operation.file_system_profile == profile
        payload = operation.to_payload()
        payload["_filesystemProfileCache"] = profile
        result = filesystem_worker._run(payload)
        return SandboxOperationResult.from_worker_stdout(json.dumps(result))

    def forbid_bytes(*args):
        pytest.fail("denied fork read working-file bytes")

    monkeypatch.setattr(filesystem, "_run_sandbox_operation_if_required", executor)
    monkeypatch.setattr(
        "opensquilla.attachment_working_files.snapshot_attachment_working_file", forbid_bytes,
    )
    with pytest.raises(PermissionError):
        await manager.branch(
            parent.session_key, "agent:main:direct:denied", fork_transcript=True,
        )
    assert calls == ["fork_attachment"]
    assert not await storage.get_session("agent:main:direct:denied")
    assert list((workspace / ".opensquilla" / "attachments").iterdir()) == [source.parent.parent]
    assert source.read_text() == "parent edit 0\n"


@pytest.mark.asyncio
async def test_safe_fork_never_falls_back_to_host_without_executor(storage, tmp_path, monkeypatch):
    manager, parent, workspace, records, _ = await _parent(storage, tmp_path)

    async def context(session):
        return ToolContext(
            workspace_dir=str(workspace), artifact_session_id=session.session_id,
            run_mode="safe", workspace_strict=True,
        )

    async def no_executor(operation, **kwargs):
        return None

    manager._attachment_fork_context_resolver = context
    monkeypatch.setattr(filesystem, "_run_sandbox_operation_if_required", no_executor)
    with pytest.raises(PermissionError, match="available filesystem sandbox"):
        await manager.branch(
            parent.session_key, "agent:main:direct:no-executor", fork_transcript=True,
        )
    assert len(list((workspace / ".opensquilla" / "attachments").iterdir())) == 1
    assert (workspace / next(iter(records.values()))["path"]).read_text() == "parent edit 0\n"


@pytest.mark.asyncio
async def test_safe_fork_uses_worker_receipt_and_distinct_session_boundaries(
    storage, tmp_path, monkeypatch,
):
    from opensquilla.sandbox import filesystem_worker
    from opensquilla.sandbox.operation_runtime import SandboxOperationResult

    manager, parent, workspace, records, _ = await _parent(storage, tmp_path)

    async def context(session):
        return ToolContext(
            workspace_dir=str(workspace), artifact_session_id=session.session_id,
            run_mode="safe", workspace_strict=True,
        )

    calls = []

    async def executor(operation, **kwargs):
        calls.append(operation)
        payload = operation.to_payload()
        result = filesystem_worker._run(payload)
        return SandboxOperationResult.from_worker_stdout(json.dumps(result))

    manager._attachment_fork_context_resolver = context
    monkeypatch.setattr(filesystem, "_run_sandbox_operation_if_required", executor)
    child = await manager.branch(
        parent.session_key, "agent:main:direct:safe-worker", fork_transcript=True,
    )
    assert len(calls) == 1
    assert calls[0].run_mode == "safe"
    boundary = calls[0].permissions.filesystem
    assert boundary["attachmentSessionRoot"].endswith(child.session_id)
    assert boundary["forkSourceBoundary"]["attachmentSessionRoot"].endswith(parent.session_id)
    child_entry = next(iter(child.origin["attachment_working_files"].values()))
    assert (workspace / child_entry["path"]).read_text() == "parent edit 0\n"
    assert child_entry["sha256"] == next(iter(records.values()))["sha256"]


def test_snapshot_rejects_source_replacement_before_open(tmp_path, monkeypatch):
    import os

    from opensquilla.attachment_working_files import snapshot_attachment_working_file

    source = tmp_path / "working.txt"
    target = tmp_path / "child" / "working.txt"
    source.write_text("approved bytes")
    original_open = os.open

    def replace_before_open(path, flags, *args, **kwargs):
        if Path(path) == source:
            source.rename(tmp_path / "old.txt")
            source.write_text("replacement bytes")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_before_open)
    with pytest.raises(ValueError, match="changed while opening"):
        snapshot_attachment_working_file(source, target)
    assert not target.parent.exists()


def test_snapshot_rejects_non_regular_source_without_open(tmp_path, monkeypatch):
    import os

    from opensquilla.attachment_working_files import snapshot_attachment_working_file

    source = tmp_path / "directory"
    source.mkdir()

    def forbid_open(*args, **kwargs):
        pytest.fail("non-regular source reached open")

    monkeypatch.setattr(os, "open", forbid_open)
    with pytest.raises(ValueError, match="not a regular file"):
        snapshot_attachment_working_file(source, tmp_path / "target")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["reset", "epoch", "metadata"])
async def test_fork_material_settlement_never_restores_state_read_before_copy(
    storage, tmp_path, monkeypatch, change,
):
    import asyncio

    from opensquilla.session.models import SessionIntent

    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(tmp_path / "archives"))
    manager, parent, workspace, _, messages = await _parent(storage, tmp_path, count=2)
    intent = await manager.prepare_prefix_branch(
        parent.session_key, "agent:main:direct:settlement", fork_before_message_id=messages[1],
    )
    child = intent.node
    await storage.upsert_session(child)
    for entry in intent.initial_transcript_entries:
        await storage.append_transcript_entry(entry)
    copying = asyncio.Event()
    release = asyncio.Event()
    copy_files = manager._fork_attachment_working_files

    async def blocked_copy(*args, **kwargs):
        copying.set()
        await release.wait()
        await copy_files(*args, **kwargs)

    monkeypatch.setattr(manager, "_fork_attachment_working_files", blocked_copy)
    pending = asyncio.create_task(manager._copy_fork_materials(
        parent.session_id, child.session_id, child.session_key,
    ))
    try:
        await asyncio.wait_for(copying.wait(), timeout=5)
        if change == "reset":
            await manager.apply_intent(child.session_key, SessionIntent.RESET_SAME_KEY)
        elif change == "epoch":
            await storage.increment_epoch(child.session_key)
        changed = await manager.update(
            child.session_key, label="current state", origin={"settlement_race": change},
        )
    finally:
        release.set()
        await asyncio.wait_for(pending, timeout=5)
    current = await storage.get_session(child.session_key)
    assert current is not None
    assert current.session_id == changed.session_id
    assert current.epoch == changed.epoch
    assert current.label == "current state"
    assert current.origin["settlement_race"] == change
    if change == "metadata":
        records = current.origin["attachment_working_files"]
        assert len(records) == 1
        assert (workspace / next(iter(records.values()))["path"]).read_text() == "parent edit 0\n"
    else:
        assert current.origin == {"settlement_race": change}
        assert current.epoch > child.epoch
        assert not (current.origin or {}).get("attachment_working_files")
