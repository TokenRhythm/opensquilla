"""Large file snapshots follow session material ownership across restart and fork."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.application.artifact_workbench import (
    AttachmentContentQuery,
    ContentIntegrityError,
    ContentNotFoundError,
)
from opensquilla.attachment_refs import transcript_material_dir, transcript_material_path
from opensquilla.file_snapshot import retain_file_snapshot
from opensquilla.gateway.adapters.artifact_content import GatewayArtifactContentPort
from opensquilla.gateway.boot import build_session_material_cleanup
from opensquilla.session.manager import SessionManager
from opensquilla.session.material_cleanup import (
    reset_session_artifact_cleanup,
    reset_session_material_cleanup,
    set_session_material_cleanup,
)
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus, TranscriptEntry
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.types import ToolContext


@pytest.fixture(autouse=True)
def _reset_cleanup_hooks():
    reset_session_material_cleanup()
    reset_session_artifact_cleanup()
    yield
    reset_session_material_cleanup()
    reset_session_artifact_cleanup()


def _config(media_root: Path, workspace: Path) -> SimpleNamespace:
    return SimpleNamespace(
        attachments=SimpleNamespace(media_root=str(media_root)),
        workspace_dir=str(workspace),
        agents=[],
        state_dir=None,
        config_path=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix_fork", [False, True], ids=["full-fork", "prefix-fork"])
async def test_snapshot_material_restart_fork_read_and_delete(
    tmp_path: Path, prefix_fork: bool
) -> None:
    media_root = tmp_path / "media"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _config(media_root, workspace)
    set_session_material_cleanup(build_session_material_cleanup(config))
    db_path = tmp_path / "sessions.db"
    storage = SessionStorage(db_path=str(db_path))
    await storage.connect()
    try:
        manager = SessionManager(storage, inject_time_prefix=False, media_root=media_root)
        parent = await manager.create("agent:main:webchat:snapshot-parent")
        unrelated = await manager.create("agent:main:webchat:snapshot-unrelated")
        payloads = ["当次写入\n" * 20_000, "@@ actual applied edit @@\n" * 5_000]
        snapshots = []
        for index, (kind, body) in enumerate(zip(("write", "edit"), payloads, strict=True)):
            snapshot = await retain_file_snapshot(
                {"kind": kind, "path": "page.html", "content": body},
                context=ToolContext(
                    artifact_media_root=str(media_root),
                    artifact_session_id=parent.session_id,
                ),
            )
            assert "sha256_ref" in snapshot
            assert len(snapshot["content"]) < len(body)
            snapshots.append(snapshot)
            turn_id = f"snapshot-turn-{index}"
            await storage.append_transcript_entry(
                TranscriptEntry(
                    session_id=parent.session_id,
                    session_key=parent.session_key,
                    role="assistant",
                    content="Updated page.html",
                    tool_calls=[{
                        "type": "tool_result",
                        "tool_use_id": f"call-{index}",
                        "result": "Updated page.html",
                        "is_error": False,
                        "file_snapshot": snapshot,
                    }],
                    turn_context={"turn_id": turn_id},
                )
            )
            await storage.create_agent_task(
                AgentTaskRecord(
                    task_id=turn_id,
                    session_key=parent.session_key,
                    status=AgentTaskStatus.SUCCEEDED,
                )
            )
        # Neither current workspace files nor a live result cache are needed to read history.
        current_file = workspace / "page.html"
        current_file.write_text("new unrelated version", encoding="utf-8")
        current_file.unlink()
        await storage.close()
        storage = SessionStorage(db_path=str(db_path))
        await storage.connect()
        manager = SessionManager(storage, inject_time_prefix=False, media_root=media_root)
        port = GatewayArtifactContentPort(config, session_manager=manager)
        restored = await storage.get_transcript(parent.session_id)
        assert [entry.tool_calls[0]["file_snapshot"] for entry in restored] == snapshots
        for snapshot, body in zip(snapshots, payloads, strict=True):
            sha = snapshot["sha256_ref"]
            material = await port.attachment_content(
                AttachmentContentQuery(parent.session_key, sha)
            )
            assert material.path.read_text(encoding="utf-8") == body
            with pytest.raises(ContentNotFoundError):
                await port.attachment_content(AttachmentContentQuery(unrelated.session_key, sha))

        child = await manager.branch(
            parent.session_key,
            "agent:main:webchat:snapshot-child",
            fork_transcript=True,
            fork_through_turn_id="snapshot-turn-0" if prefix_fork else None,
        )
        included = 1 if prefix_fork else 2
        assert child.forked_from_parent
        child_entries = await storage.get_transcript(child.session_id)
        child_snapshots = [entry.tool_calls[0]["file_snapshot"] for entry in child_entries]
        assert child_snapshots == snapshots[:included]
        for index, snapshot in enumerate(snapshots):
            sha = snapshot["sha256_ref"]
            child_path = transcript_material_path(media_root, child.session_id, sha)
            assert child_path.exists() is (index < included)
        if prefix_fork:
            with pytest.raises(ContentNotFoundError):
                await port.attachment_content(
                    AttachmentContentQuery(child.session_key, snapshots[1]["sha256_ref"])
                )

        # Gateway's existing deletion hook removes only the deleted session's material.
        await storage.delete_session(parent.session_key)
        assert not transcript_material_dir(media_root, parent.session_id).exists()
        for snapshot, body in zip(snapshots[:included], payloads[:included], strict=True):
            sha = snapshot["sha256_ref"]
            with pytest.raises(ContentNotFoundError):
                await port.attachment_content(AttachmentContentQuery(parent.session_key, sha))
            material = await port.attachment_content(AttachmentContentQuery(child.session_key, sha))
            assert material.path.read_text(encoding="utf-8") == body

        await storage.delete_session(child.session_key)
        assert not transcript_material_dir(media_root, child.session_id).exists()
        with pytest.raises(ContentNotFoundError):
            await port.attachment_content(
                AttachmentContentQuery(child.session_key, snapshots[0]["sha256_ref"])
            )
        assert await storage.get_session(unrelated.session_key) is not None
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_snapshot_attachment_reader_rejects_corrupt_content(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage, inject_time_prefix=False, media_root=media_root)
        session = await manager.create("agent:main:webchat:snapshot-corrupt")
        snapshot = await retain_file_snapshot(
            {"kind": "write", "path": "file.txt", "content": "original\n" * 10_000},
            context=ToolContext(
                artifact_media_root=str(media_root), artifact_session_id=session.session_id
            ),
        )
        sha = snapshot["sha256_ref"]
        transcript_material_path(media_root, session.session_id, sha).write_bytes(b"corrupt")
        port = GatewayArtifactContentPort(
            _config(media_root, tmp_path / "workspace"), session_manager=manager
        )
        with pytest.raises(ContentIntegrityError):
            await port.attachment_content(AttachmentContentQuery(session.session_key, sha))
    finally:
        await storage.close()
