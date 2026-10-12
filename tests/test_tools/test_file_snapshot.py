from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.attachment_refs import transcript_material_path
from opensquilla.engine.agent import Agent
from opensquilla.engine.types import AgentConfig
from opensquilla.file_snapshot import build_file_edit_snapshot, retain_file_snapshot
from opensquilla.sandbox import filesystem_worker
from opensquilla.sandbox.operation_runtime import SandboxOperationResult
from opensquilla.tool_boundary import ToolCall, ToolResult
from opensquilla.tools.builtin import filesystem
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import (
    ToolContext,
    ToolSpec,
    current_file_snapshot,
    current_tool_context,
)


def _handler(tmp_path: Path, **context_fields):
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="write_file", description="Write", parameters={
            "path": {"type": "string"}, "content": {"type": "string"},
        }, required=["path", "content"]),
        filesystem.write_file,
    )
    registry.register(
        ToolSpec(name="edit_file", description="Edit", parameters={
            "path": {"type": "string"}, "old_text": {"type": "string"},
            "new_text": {"type": "string"}, "edits": {"type": "array"},
        }, required=["path"]),
        filesystem.edit_file,
    )
    return build_tool_handler(registry, ToolContext(
        is_owner=True, workspace_dir=str(tmp_path), run_mode="full",
        session_key="snapshot-test", **context_fields,
    ))


@pytest.mark.parametrize(
    "content", ["", "正文 🦐\n" * 200, "正文 🦐\n" * 4000],
    ids=["empty", "inline-unicode", "large-unicode"],
)
@pytest.mark.parametrize("full_host", [True, False], ids=["full-host", "ordinary"])
async def test_write_retains_complete_successful_content(
    tmp_path: Path, content: str, full_host: bool, monkeypatch,
) -> None:
    monkeypatch.setattr(filesystem, "full_host_access_active", lambda: full_host)
    if not full_host:
        monkeypatch.setattr(filesystem, "_gate_out_of_workspace_write", AsyncMock(
            return_value=(None, False, []),
        ))
        monkeypatch.setattr(filesystem, "_run_sandbox_operation_if_required", AsyncMock(
            return_value=None,
        ))
    path = tmp_path / "source.html"
    media_root = tmp_path / "media"
    result = await _handler(
        tmp_path, artifact_media_root=str(media_root), artifact_session_id="write-session",
    )(ToolCall(
        tool_use_id="write", tool_name="write_file",
        arguments={"path": str(path), "content": content},
    ))
    assert not result.is_error, result.content
    assert path.read_text(encoding="utf-8") == content
    snapshot = result.file_snapshot
    assert snapshot["kind"] == "write"
    assert snapshot["path"] == str(path)
    if len(content.encode("utf-8")) <= 4096:
        assert snapshot == {"kind": "write", "path": str(path), "content": content}
    else:
        assert snapshot["content"] == ""
        assert transcript_material_path(
            media_root, "write-session", snapshot["sha256_ref"],
        ).read_bytes().decode("utf-8") == content
    assert len(result.content) < 1000
    path.write_text("later", encoding="utf-8")
    if "sha256_ref" in snapshot:
        assert transcript_material_path(
            media_root, "write-session", snapshot["sha256_ref"],
        ).read_bytes().decode("utf-8") == content
    else:
        assert snapshot["content"] == content
    assert current_file_snapshot.get() is None


@pytest.mark.parametrize("full_host", [True, False], ids=["full-host", "ordinary"])
async def test_edit_snapshots_actual_file_context_and_batch_changes(
    tmp_path: Path, full_host: bool, monkeypatch,
) -> None:
    monkeypatch.setattr(filesystem, "full_host_access_active", lambda: full_host)
    if not full_host:
        monkeypatch.setattr(filesystem, "_gate_out_of_workspace_write", AsyncMock(
            return_value=(None, False, []),
        ))
        monkeypatch.setattr(filesystem, "_filesystem_operation_workspace", lambda: None)
    path = tmp_path / "source.txt"
    original = "context before\nfirst\nbetween\nsecond\ncontext after\n"
    path.write_text(original, encoding="utf-8")
    result = await _handler(tmp_path)(ToolCall(
        tool_use_id="edit", tool_name="edit_file", arguments={
            "path": str(path), "edits": [
                {"old_text": "first", "new_text": "first changed"},
                {"old_text": "second", "new_text": "second changed"},
            ],
        },
    ))
    assert not result.is_error, result.content
    diff = result.file_snapshot["content"]
    assert " context before\n" in diff
    assert "-first\n+first changed\n" in diff
    assert "-second\n+second changed\n" in diff
    assert path.read_text(encoding="utf-8") == original.replace("first", "first changed").replace(
        "second", "second changed",
    )
    path.unlink()
    assert result.file_snapshot["content"] == diff


@pytest.mark.parametrize("tool_name", ["write_file", "edit_file"])
async def test_failed_mutation_has_no_success_snapshot(tmp_path: Path, tool_name: str) -> None:
    path = tmp_path / "source.txt"
    path.write_text("original", encoding="utf-8")
    args = {"path": str(path), "content": "invalid \udc80"} if tool_name == "write_file" else {
        "path": str(path), "old_text": "not present", "new_text": "changed",
    }
    result = await _handler(tmp_path)(ToolCall(
        tool_use_id="failure", tool_name=tool_name, arguments=args,
    ))
    assert result.is_error
    assert result.file_snapshot is None
    assert current_file_snapshot.get() is None


async def test_parallel_dispatch_isolates_snapshots(tmp_path: Path) -> None:
    registry = ToolRegistry()
    ready = asyncio.Event()
    count = 0

    async def write(label: str) -> str:
        nonlocal count
        count += 1
        if count == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 5)
        return await filesystem.write_file(str(tmp_path / label), label * 6000)

    registry.register(ToolSpec(
        name="write", description="Write", parameters={"label": {"type": "string"}},
        required=["label"],
    ), write)
    handler = build_tool_handler(registry, ToolContext(
        is_owner=True, workspace_dir=str(tmp_path), run_mode="full",
        artifact_media_root=str(tmp_path / "media"), artifact_session_id="parallel",
    ))
    results = await asyncio.gather(*(
        handler(ToolCall(tool_use_id=label, tool_name="write", arguments={"label": label}))
        for label in ("a", "b")
    ))
    for label, result in zip(("a", "b"), results, strict=True):
        assert not result.is_error, result.content
        assert result.file_snapshot["kind"] == "write"
        assert result.file_snapshot["path"] == str(tmp_path / label)
        assert result.file_snapshot["content"] == ""
        assert transcript_material_path(
            tmp_path / "media", "parallel", result.file_snapshot["sha256_ref"],
        ).read_bytes().decode("utf-8") == label * 6000
    assert current_file_snapshot.get() is None


async def test_pending_approval_never_carries_snapshot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(filesystem, "_prepare_attachment_edit_path", AsyncMock(return_value=(
        tmp_path / "source.txt", json.dumps({
            "status": "approval_required", "approval_id": "pending", "message": "Approval",
        }),
    )))
    result = await _handler(tmp_path)(ToolCall(
        tool_use_id="write", tool_name="write_file",
        arguments={"path": "source.txt", "content": "never written"},
    ))
    assert result.file_snapshot is None
    assert not (tmp_path / "source.txt").exists()


@pytest.mark.parametrize("operation", ["write", "edit"])
async def test_sandbox_snapshot_uses_applied_content_without_later_host_read(
    tmp_path: Path, monkeypatch, operation: str,
):
    path = tmp_path / "source.txt"
    path.write_text("before\n", encoding="utf-8")
    if operation == "edit":
        worker_result = filesystem_worker._edit_text({
            "path": str(path), "oldText": "before", "newText": "after",
        })
    else:
        worker_result = filesystem_worker._write_text({"path": str(path), "content": "after\n"})
    result = SandboxOperationResult.from_worker_stdout(json.dumps(worker_result))
    path.write_text("subsequent change", encoding="utf-8")
    monkeypatch.setattr(filesystem, "full_host_access_active", lambda: False)
    monkeypatch.setattr(filesystem, "_prepare_attachment_edit_path", AsyncMock(
        return_value=(path, None),
    ))
    monkeypatch.setattr(filesystem, "_gate_out_of_workspace_write", AsyncMock(
        return_value=(None, False, []),
    ))
    monkeypatch.setattr(filesystem, "_filesystem_operation_workspace", lambda: tmp_path)
    monkeypatch.setattr(filesystem, "_run_sandbox_operation_if_required", AsyncMock(
        return_value=result,
    ))
    snapshot: dict = {}
    token = current_file_snapshot.set(snapshot)
    context_token = current_tool_context.set(ToolContext(
        is_owner=True, workspace_dir=str(tmp_path),
    ))
    try:
        if operation == "edit":
            await filesystem.edit_file(str(path), "before", "after")
        else:
            await filesystem.write_file(str(path), "after\n")
    finally:
        current_file_snapshot.reset(token)
        current_tool_context.reset(context_token)
    if operation == "edit":
        assert "-before\n+after\n" in snapshot["content"]
    else:
        assert snapshot == {"kind": "write", "path": str(path), "content": "after\n"}
    assert "subsequent" not in snapshot["content"]


def test_edit_diff_preserves_final_newline_change() -> None:
    snapshot = build_file_edit_snapshot("source.txt", "before", "before\n")
    assert "-before\n\\ No newline at end of file\n+before\n" in snapshot["content"]


async def test_edit_display_diff_does_not_block_the_event_loop(tmp_path, monkeypatch):
    """The mutation is complete while slow display capture runs elsewhere."""
    path = tmp_path / "source.txt"
    path.write_text("before\n", encoding="utf-8")
    entered = threading.Event()
    release = threading.Event()
    main_thread = threading.get_ident()
    capture_threads = []

    def slow_capture(path, original, updated):
        capture_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(5), "display capture was not released by the event loop"
        return build_file_edit_snapshot(path, original, updated)

    monkeypatch.setattr(filesystem, "full_host_access_active", lambda: True)
    monkeypatch.setattr(filesystem, "build_file_edit_snapshot", slow_capture)
    edit = asyncio.create_task(_handler(tmp_path)(ToolCall(
        tool_use_id="edit", tool_name="edit_file",
        arguments={"path": str(path), "old_text": "before", "new_text": "after"},
    )))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), 4)
        # This callback must execute before diff capture can complete.
        await asyncio.sleep(0)
        assert not edit.done()
        assert path.read_text(encoding="utf-8") == "after\n"
        assert capture_threads == [capture_threads[0]] and capture_threads[0] != main_thread
    finally:
        release.set()
    result = await asyncio.wait_for(edit, 5)
    assert not result.is_error
    assert "-before\n+after\n" in result.file_snapshot["content"]


@pytest.mark.parametrize("limit", ["input", "lines", "output", "failure"])
async def test_display_capture_limits_never_fail_a_successful_edit(tmp_path, monkeypatch, limit):
    from opensquilla import file_snapshot

    path = tmp_path / "source.txt"
    path.write_text("before\ncontext\n", encoding="utf-8")
    if limit == "input":
        monkeypatch.setattr(file_snapshot, "MAX_EDIT_SNAPSHOT_INPUT_BYTES", 4)
    elif limit == "lines":
        monkeypatch.setattr(file_snapshot, "MAX_EDIT_SNAPSHOT_LINES", 1)
    elif limit == "output":
        monkeypatch.setattr(file_snapshot, "MAX_EDIT_SNAPSHOT_BYTES", 4)
    else:
        def fail(*args, **kwargs):
            raise MemoryError("display only")
        monkeypatch.setattr(file_snapshot.difflib, "unified_diff", fail)
    monkeypatch.setattr(filesystem, "full_host_access_active", lambda: True)
    result = await _handler(tmp_path)(ToolCall(
        tool_use_id="edit", tool_name="edit_file",
        arguments={"path": str(path), "old_text": "before", "new_text": "after"},
    ))
    assert not result.is_error
    assert path.read_text(encoding="utf-8") == "after\ncontext\n"
    assert result.file_snapshot == {
        "kind": "edit", "path": str(path), "content": "", "content_unavailable": True,
    }


async def test_delivery_projection_preserves_snapshot_without_adding_model_text(monkeypatch):
    agent = Agent(provider=object(), config=AgentConfig())
    original = ToolResult(
        tool_use_id="write", tool_name="write_file", content="Written 6000 bytes",
        file_snapshot={"kind": "write", "path": "source.txt", "content": "secret body" * 6000},
    )
    reduced = ToolResult(tool_use_id="write", tool_name="write_file", content="Written")
    monkeypatch.setattr(agent, "_project_tool_result_for_llm", AsyncMock(return_value=reduced))
    result = await agent._project_tool_result_for_delivery(original)
    assert result.file_snapshot == original.file_snapshot
    assert result.file_snapshot is not original.file_snapshot
    assert result.content == "Written"


@pytest.mark.parametrize("kind", ["write", "edit"])
async def test_large_snapshot_is_durable_with_bodyless_event_reference(tmp_path: Path, kind: str):
    content = "正文 🦐\n" * 12_000
    snapshot = {"kind": kind, "path": "source.txt", "content": content}
    context = ToolContext(
        artifact_media_root=str(tmp_path), artifact_session_id="durable-session",
        # Transcript and artifact stores have different configured budgets.
        artifact_disk_budget_bytes=1,
        sandbox_gateway_config=SimpleNamespace(attachments=SimpleNamespace(
            transcript_disk_budget_bytes=10 * 1024 * 1024,
        )),
    )
    retained = await retain_file_snapshot(snapshot, context=context)
    assert retained["content"] == ""
    assert retained["bytes"] == len(content.encode("utf-8"))
    assert "content_unavailable" not in retained
    assert transcript_material_path(
        tmp_path, "durable-session", retained["sha256_ref"],
    ).read_bytes() == content.encode("utf-8")
    assert len(json.dumps(retained)) < 300
    assert snapshot["content"] == content


@pytest.mark.parametrize("size", [4096, 4097])
async def test_inline_threshold_uses_utf8_bytes(tmp_path: Path, size: int) -> None:
    content = "é" * (size // 2) + "x" * (size % 2)
    snapshot = {"kind": "write", "path": "source.txt", "content": content}
    retained = await retain_file_snapshot(snapshot, context=ToolContext(
        artifact_media_root=str(tmp_path), artifact_session_id="threshold",
    ))
    assert ("sha256_ref" in retained) is (size > 4096)
    if size <= 4096:
        assert retained == snapshot
    else:
        assert retained["bytes"] == size


@pytest.mark.parametrize("failure", ["missing-context", "storage-error", "budget"])
async def test_spill_failure_keeps_success_and_marks_content_unavailable(
    tmp_path: Path, monkeypatch, caplog, failure: str,
) -> None:
    from opensquilla import attachment_refs

    content = "PRIVATE_CONTENT_DO_NOT_LOG" * 5000
    context_fields = {}
    if failure != "missing-context":
        context_fields = {
            "artifact_media_root": str(tmp_path / "media"),
            "artifact_session_id": "failure-session",
            "sandbox_gateway_config": SimpleNamespace(attachments=SimpleNamespace(
                transcript_disk_budget_bytes=1 if failure == "budget" else 10 * 1024 * 1024,
            )),
        }
    if failure == "storage-error":
        def fail_write(**kwargs):
            raise OSError("PRIVATE_CONTENT_DO_NOT_LOG")

        monkeypatch.setattr(attachment_refs, "write_transcript_material", fail_write)

    path = tmp_path / "source.txt"
    result = await _handler(tmp_path, **context_fields)(ToolCall(
        tool_use_id="write", tool_name="write_file",
        arguments={"path": str(path), "content": content},
    ))
    assert not result.is_error, result.content
    assert path.read_text(encoding="utf-8") == content
    assert result.file_snapshot == {
        "kind": "write", "path": str(path), "content": "",
        "bytes": len(content.encode("utf-8")), "content_unavailable": True,
    }
    assert "PRIVATE_CONTENT_DO_NOT_LOG" not in caplog.text


async def test_large_write_dispatch_saves_complete_body_before_delivery(tmp_path: Path) -> None:
    path = tmp_path / "source.txt"
    media_root = tmp_path / "media"
    content = "saved full body\n" * 10_000
    result = await _handler(
        tmp_path, artifact_media_root=str(media_root), artifact_session_id="write-session",
    )(ToolCall(tool_use_id="large-write", tool_name="write_file", arguments={
        "path": str(path), "content": content,
    }))
    assert not result.is_error, result.content
    assert transcript_material_path(
        media_root, "write-session", result.file_snapshot["sha256_ref"],
    ).read_bytes() == content.encode("utf-8")
    assert result.file_snapshot["content"] == ""
    assert "saved full body" not in result.content


async def test_small_edit_of_large_line_stores_full_diff_before_delivery(tmp_path: Path) -> None:
    """A tiny argument still durably retains a complete diff within capture limits."""
    path = tmp_path / "large.json"
    media_root = tmp_path / "media"
    original = '{"value":"' + "x" * (2 * 1024 * 1024) + '","version":1}\n'
    path.write_text(original, encoding="utf-8")
    result = await _handler(
        tmp_path, artifact_media_root=str(media_root), artifact_session_id="edit-session",
    )(ToolCall(tool_use_id="large-edit", tool_name="edit_file", arguments={
        "path": str(path), "old_text": '"version":1', "new_text": '"version":2',
    }))
    assert not result.is_error, result.content
    updated = original.replace('"version":1', '"version":2')
    assert path.read_text(encoding="utf-8") == updated
    retained = result.file_snapshot
    assert 4 * 1024 * 1024 < retained["bytes"] < 8 * 1024 * 1024
    assert len(json.dumps(retained)) < 10_000
    saved = transcript_material_path(
        media_root, "edit-session", retained["sha256_ref"],
    ).read_bytes().decode("utf-8")
    assert saved == build_file_edit_snapshot(str(path), original, updated)["content"]
    assert len(result.content) < 1000
    assert "version" not in result.content


async def test_many_moderate_snapshots_do_not_overflow_history_envelope(tmp_path: Path) -> None:
    from opensquilla.gateway.snapshot_transfer import SnapshotTransferError, freeze_json

    context = ToolContext(
        artifact_media_root=str(tmp_path), artifact_session_id="long-turn",
    )
    full_snapshots = [{
        "kind": "write", "path": f"file-{index}.txt",
        "content": f"{index:04d}\n" + "x" * (64 * 1024 - 5),
    } for index in range(401)]
    with pytest.raises(SnapshotTransferError, match="SNAPSHOT_TOO_LARGE"):
        freeze_json({"tool_calls": [{"file_snapshot": item} for item in full_snapshots]})

    retained = [await retain_file_snapshot(item, context=context) for item in full_snapshots]
    history = {"tool_calls": [{"file_snapshot": item} for item in retained]}
    assert freeze_json(history) is not None
    assert len(json.dumps(history).encode("utf-8")) < 100_000
    assert all(item["content"] == "" and "sha256_ref" in item for item in retained)
    for index in (0, len(retained) - 1):
        assert transcript_material_path(
            tmp_path, "long-turn", retained[index]["sha256_ref"],
        ).read_bytes().decode("utf-8") == full_snapshots[index]["content"]
