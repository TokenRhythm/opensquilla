"""Deleting a session cascades to attachment and artifact material stores."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.artifacts import ArtifactStore
from opensquilla.attachment_refs import transcript_material_dir, write_transcript_material
from opensquilla.attachment_workspace import _safe_path_segment
from opensquilla.gateway.boot import build_session_material_cleanup
from opensquilla.session.material_cleanup import (
    reset_session_material_cleanup,
    set_session_material_cleanup,
)
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage


def _config(media_root: Path, workspace: Path) -> SimpleNamespace:
    return SimpleNamespace(
        attachments=SimpleNamespace(media_root=str(media_root)),
        workspace_dir=str(workspace),
        agents=[],
        state_dir=None,
        config_path=None,
    )


def _workspace_attachment_dir(workspace: Path, session_id: str) -> Path:
    segment = _safe_path_segment(session_id, fallback="session")
    return workspace / ".opensquilla" / "attachments" / segment


async def _seed_material(media_root: Path, workspace: Path, session_id: str) -> None:
    write_transcript_material(media_root=media_root, session_id=session_id, payload=b"bytes")
    att_dir = _workspace_attachment_dir(workspace, session_id)
    att_dir.mkdir(parents=True, exist_ok=True)
    (att_dir / "doc.pdf").write_bytes(b"%PDF-1.4\n")
    ArtifactStore(media_root).publish_bytes(
        b"<!doctype html><title>preview</title>",
        session_id=session_id,
        session_key=f"agent:main:webchat:{session_id}",
        name="index.html",
        mime="text/html",
        source="test",
    )


@pytest.fixture(autouse=True)
def _reset_hook():
    reset_session_material_cleanup()
    yield
    reset_session_material_cleanup()


@pytest.mark.asyncio
async def test_delete_session_removes_all_material_stores(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _config(media_root, workspace)
    set_session_material_cleanup(build_session_material_cleanup(config))

    storage = SessionStorage(db_path=":memory:")
    await storage.connect()
    node = SessionNode(session_key="agent:main:webchat:a", session_id="sid-a")
    await storage.upsert_session(node)
    await _seed_material(media_root, workspace, "sid-a")

    # A file the agent authored at the workspace root — shared, must survive.
    (workspace / "authored.txt").write_text("keep me")

    assert transcript_material_dir(media_root, "sid-a").is_dir()
    assert _workspace_attachment_dir(workspace, "sid-a").is_dir()
    assert list((media_root / "artifacts").rglob("meta.json"))

    await storage.delete_session("agent:main:webchat:a")

    assert not transcript_material_dir(media_root, "sid-a").exists()
    assert not _workspace_attachment_dir(workspace, "sid-a").exists()
    assert not list((media_root / "artifacts").rglob("meta.json"))
    # Shared authored file at the workspace root is untouched.
    assert (workspace / "authored.txt").read_text() == "keep me"
    await storage.close()


@pytest.mark.asyncio
async def test_delete_session_leaves_other_sessions_material(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _config(media_root, workspace)
    set_session_material_cleanup(build_session_material_cleanup(config))

    storage = SessionStorage(db_path=":memory:")
    await storage.connect()
    await storage.upsert_session(
        SessionNode(session_key="agent:main:webchat:a", session_id="sid-a")
    )
    await storage.upsert_session(
        SessionNode(session_key="agent:main:webchat:b", session_id="sid-b")
    )
    await _seed_material(media_root, workspace, "sid-a")
    await _seed_material(media_root, workspace, "sid-b")

    await storage.delete_session("agent:main:webchat:a")

    # Sibling session B's material must survive.
    assert transcript_material_dir(media_root, "sid-b").is_dir()
    assert _workspace_attachment_dir(workspace, "sid-b").is_dir()
    remaining_artifact_meta = list((media_root / "artifacts").rglob("meta.json"))
    assert len(remaining_artifact_meta) == 1
    assert '"session_id": "sid-b"' in remaining_artifact_meta[0].read_text()
    await storage.close()


@pytest.mark.asyncio
async def test_delete_session_without_hook_is_still_db_safe(tmp_path: Path) -> None:
    # No hook registered → delete still succeeds (best-effort cleanup is a no-op).
    storage = SessionStorage(db_path=":memory:")
    await storage.connect()
    await storage.upsert_session(
        SessionNode(session_key="agent:main:webchat:a", session_id="sid-a")
    )
    await storage.delete_session("agent:main:webchat:a")
    assert await storage.get_session("agent:main:webchat:a") is None
    await storage.close()
