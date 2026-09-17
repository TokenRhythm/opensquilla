"""Forking a session carries its attachment/artifact material to the child.

A fork copies transcript rows, but the artifact and attachment material stores are
keyed by session id. Without copying the material a forked conversation references
generated images/files and uploaded attachments that resolve to an empty child bucket
and fail to preview or replay. These tests pin the copy behavior at the storage layer
and end-to-end through ``SessionManager.branch``.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import pytest_asyncio

from opensquilla.artifacts import ArtifactNotFoundError, ArtifactStore, artifact_payload
from opensquilla.attachment_refs import (
    copy_transcript_material,
    make_attachment_ref,
    read_attachment_ref_bytes,
    transcript_material_path,
    write_transcript_material,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus, TranscriptEntry
from opensquilla.session.storage import SessionStorage


def _png_bytes() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (8, 8), color="red").save(out, format="PNG")
    return out.getvalue()


def test_copy_transcript_material_to_child(tmp_path: Path) -> None:
    sha, _path, wrote = write_transcript_material(
        media_root=tmp_path, session_id="parent-1", payload=b"attachment payload"
    )
    assert wrote is True

    copied = copy_transcript_material(
        media_root=tmp_path, source_session_id="parent-1", target_session_id="child-1"
    )
    assert copied == 1

    child_blob = transcript_material_path(tmp_path, "child-1", sha)
    assert child_blob.exists()
    assert child_blob.read_bytes() == b"attachment payload"

    # A child-scoped ref now reads its own copy (replay resolves by current session).
    child_ref = make_attachment_ref(
        sha256=sha,
        name="f.bin",
        mime="application/octet-stream",
        size=len(b"attachment payload"),
        session_id="child-1",
        source="transcript",
    )
    assert read_attachment_ref_bytes(child_ref, media_root=tmp_path) == b"attachment payload"

    # Idempotent: re-copying materializes nothing new.
    assert (
        copy_transcript_material(
            media_root=tmp_path, source_session_id="parent-1", target_session_id="child-1"
        )
        == 0
    )


def test_copy_transcript_material_missing_source_is_noop(tmp_path: Path) -> None:
    assert (
        copy_transcript_material(
            media_root=tmp_path, source_session_id="absent", target_session_id="child-1"
        )
        == 0
    )


@pytest_asyncio.fixture
async def storage():
    store = SessionStorage(":memory:")
    await store.connect()
    yield store
    await store.close()


@pytest.mark.asyncio
async def test_branch_fork_copies_artifact_and_attachment_material(
    storage: SessionStorage, tmp_path: Path
) -> None:
    media_root = tmp_path / "media"
    manager = SessionManager(storage, inject_time_prefix=False, media_root=media_root)

    parent = await manager.create("agent:main:main")

    artifact = ArtifactStore(media_root).publish_bytes(
        _png_bytes(),
        session_id=parent.session_id,
        session_key=parent.session_key,
        name="generated-image.png",
        mime="image/png",
        source="publish_artifact",
    )
    att_sha, _p, _w = write_transcript_material(
        media_root=media_root, session_id=parent.session_id, payload=b"upload-bytes"
    )
    # A transcript entry must exist for fork_transcript to copy (and material to follow).
    await storage.append_transcript_entry(
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="assistant",
            content="here is your image",
            token_count=4,
        )
    )

    child = await manager.branch(
        "agent:main:main", "agent:main:direct:u1", fork_transcript=True
    )
    assert child.forked_from_parent is True

    # The forked child resolves the generated artifact under its own session id.
    child_ref, child_path = ArtifactStore(media_root).resolve_for_download(
        artifact.id, session_id=child.session_id
    )
    assert child_path.read_bytes() == _png_bytes()
    assert child_ref.session_id == child.session_id

    # And the uploaded attachment blob exists under the child's transcript store.
    assert transcript_material_path(media_root, child.session_id, att_sha).exists()


@pytest.mark.asyncio
async def test_branch_without_media_root_is_safe(storage: SessionStorage) -> None:
    # No media_root configured: fork still succeeds, material copy is a no-op.
    manager = SessionManager(storage, inject_time_prefix=False)
    parent = await manager.create("agent:main:main")
    await storage.append_transcript_entry(
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="assistant",
            content="hi",
            token_count=1,
        )
    )
    child = await manager.branch(
        "agent:main:main", "agent:main:direct:u2", fork_transcript=True
    )
    assert child.forked_from_parent is True


@pytest.mark.asyncio
async def test_branch_nested_fork_carries_material_each_generation(
    storage: SessionStorage, tmp_path: Path
) -> None:
    media_root = tmp_path / "media"
    manager = SessionManager(storage, inject_time_prefix=False, media_root=media_root)

    parent = await manager.create("agent:main:main")
    artifact = ArtifactStore(media_root).publish_bytes(
        _png_bytes(),
        session_id=parent.session_id,
        session_key=parent.session_key,
        name="image.png",
        mime="image/png",
        source="publish_artifact",
    )
    await storage.append_transcript_entry(
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="assistant",
            content="image",
            token_count=2,
        )
    )

    child = await manager.branch(
        "agent:main:main", "agent:main:direct:child", fork_transcript=True
    )
    grandchild = await manager.branch(
        "agent:main:direct:child", "agent:main:direct:grandchild", fork_transcript=True
    )

    # The artifact re-resolves under each generation's own session id.
    for session_id in (child.session_id, grandchild.session_id):
        _ref, path = ArtifactStore(media_root).resolve_for_download(
            artifact.id, session_id=session_id
        )
        assert path.read_bytes() == _png_bytes()


@pytest.mark.asyncio
async def test_branch_survives_material_copy_failure(
    storage: SessionStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_root = tmp_path / "media"
    manager = SessionManager(storage, inject_time_prefix=False, media_root=media_root)
    parent = await manager.create("agent:main:main")
    await storage.append_transcript_entry(
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="assistant",
            content="hi",
            token_count=1,
        )
    )

    def _boom(*args: object, **kwargs: object) -> int:
        raise RuntimeError("disk exploded mid-copy")

    monkeypatch.setattr(ArtifactStore, "copy_session_artifacts", _boom)

    # The copy raising must NOT abort the fork: the child is still created/committed.
    child = await manager.branch(
        "agent:main:main", "agent:main:direct:resilient", fork_transcript=True
    )
    assert child.forked_from_parent is True
    assert await manager.get_session("agent:main:direct:resilient") is not None


@pytest.mark.asyncio
async def test_branch_through_turn_copies_only_prefix_reachable_material(
    storage: SessionStorage,
    tmp_path: Path,
) -> None:
    media_root = tmp_path / "media"
    manager = SessionManager(storage, inject_time_prefix=False, media_root=media_root)
    parent = await manager.create("agent:main:main")
    artifact_store = ArtifactStore(media_root)
    selected_artifact = artifact_store.publish_bytes(
        _png_bytes(),
        session_id=parent.session_id,
        session_key=parent.session_key,
        name="selected.png",
        mime="image/png",
        source="publish_artifact",
    )
    later_artifact = artifact_store.publish_bytes(
        b"later artifact bytes",
        session_id=parent.session_id,
        session_key=parent.session_key,
        name="later.bin",
        mime="application/octet-stream",
        source="publish_artifact",
    )
    nested_artifact = artifact_store.publish_bytes(
        b"nested artifact bytes",
        session_id=parent.session_id,
        session_key=parent.session_key,
        name="nested.bin",
        mime="application/octet-stream",
        source="publish_artifact",
    )
    selected_sha, _selected_path, _ = write_transcript_material(
        media_root=media_root,
        session_id=parent.session_id,
        payload=b"selected attachment",
    )
    later_sha, _later_path, _ = write_transcript_material(
        media_root=media_root,
        session_id=parent.session_id,
        payload=b"later attachment",
    )
    nested_sha, _nested_path, _ = write_transcript_material(
        media_root=media_root,
        session_id=parent.session_id,
        payload=b"nested attachment",
    )
    nested_tool_result: object = {
        "artifact": artifact_payload(nested_artifact),
        "attachment": {"sha256_ref": nested_sha},
    }
    for level in range(16):
        nested_tool_result = {f"level_{level}": nested_tool_result}
    selected_turn_id = "turn-with-selected-material"
    later_turn_id = "turn-with-later-material"
    for turn_id, text, sha, artifact in (
        (
            selected_turn_id,
            "selected material",
            selected_sha,
            selected_artifact,
        ),
        (later_turn_id, "later material", later_sha, later_artifact),
    ):
        await storage.append_transcript_entry(
            TranscriptEntry(
                session_id=parent.session_id,
                session_key=parent.session_key,
                role="assistant",
                content=json.dumps(
                    {
                        "text": text,
                        "padding": "x" * 1_000_001 if turn_id == selected_turn_id else "",
                        "attachments": [
                            {
                                "sha256_ref": sha,
                                "name": f"{text}.bin",
                                "mime": "application/octet-stream",
                            }
                        ],
                        "artifacts": [artifact_payload(artifact)],
                    }
                ),
                tool_calls=(
                    [{"id": "nested-call", "result": nested_tool_result}]
                    if turn_id == selected_turn_id
                    else None
                ),
                turn_context={"turn_id": turn_id},
            )
        )
    await storage.create_agent_task(
        AgentTaskRecord(
            task_id=selected_turn_id,
            session_key=parent.session_key,
            status=AgentTaskStatus.SUCCEEDED,
        )
    )

    child = await manager.branch(
        parent.session_key,
        "agent:main:direct:material-prefix",
        fork_transcript=True,
        fork_through_turn_id=selected_turn_id,
    )

    _selected_ref, selected_path = artifact_store.resolve_for_download(
        selected_artifact.id,
        session_id=child.session_id,
    )
    assert selected_path.exists()
    _nested_ref, nested_path = artifact_store.resolve_for_download(
        nested_artifact.id,
        session_id=child.session_id,
    )
    assert nested_path.exists()
    with pytest.raises(ArtifactNotFoundError):
        artifact_store.resolve_for_download(later_artifact.id, session_id=child.session_id)
    assert transcript_material_path(media_root, child.session_id, selected_sha).exists()
    assert transcript_material_path(media_root, child.session_id, nested_sha).exists()
    assert not transcript_material_path(media_root, child.session_id, later_sha).exists()

    # Filtering the child does not mutate or hide material owned by the parent.
    _later_ref, parent_later_path = artifact_store.resolve_for_download(
        later_artifact.id,
        session_id=parent.session_id,
    )
    assert parent_later_path.exists()
    assert transcript_material_path(media_root, parent.session_id, later_sha).exists()
