"""Deleting a session cascades to attachment and artifact material stores."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.artifacts import ArtifactNotFoundError, ArtifactStore
from opensquilla.attachment_refs import (
    transcript_material_dir,
    write_pending_chat_input_material,
    write_transcript_material,
)
from opensquilla.attachment_workspace import _safe_path_segment
from opensquilla.execution_workspaces import configured_execution_workspace
from opensquilla.gateway.boot import (
    build_session_material_cleanup,
)
from opensquilla.project_workspaces import project_path_key
from opensquilla.session.material_cleanup import (
    reset_session_artifact_cleanup,
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


async def _seed_material(media_root: Path, workspace: Path, session_id: str) -> tuple[str, str]:
    write_transcript_material(media_root=media_root, session_id=session_id, payload=b"bytes")
    write_pending_chat_input_material(
        media_root=media_root,
        session_id=session_id,
        pending_input_id=f"pending-{session_id}",
        payload=b"queued bytes",
    )
    att_dir = _workspace_attachment_dir(workspace, session_id)
    att_dir.mkdir(parents=True, exist_ok=True)
    (att_dir / "doc.pdf").write_bytes(b"%PDF-1.4\n")
    listed = ArtifactStore(media_root).publish_bytes(
        b"<!doctype html><title>preview</title>",
        session_id=session_id,
        session_key=f"agent:main:webchat:{session_id}",
        name="index.html",
        mime="text/html",
        source="test",
    )
    internal = ArtifactStore(media_root).publish_bytes(
        b"<!doctype html><title>working revision</title>",
        session_id=session_id,
        session_key=f"agent:main:webchat:{session_id}",
        name="index.html",
        mime="text/html",
        source="artifact-session",
        visibility="internal",
    )
    return listed.id, internal.id


@pytest.fixture(autouse=True)
def _reset_hook():
    reset_session_material_cleanup()
    reset_session_artifact_cleanup()
    yield
    reset_session_material_cleanup()
    reset_session_artifact_cleanup()


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
    listed_id, internal_id = await _seed_material(media_root, workspace, "sid-a")

    # A file the agent authored at the workspace root — shared, must survive.
    (workspace / "authored.txt").write_text("keep me")

    assert transcript_material_dir(media_root, "sid-a").is_dir()
    assert _workspace_attachment_dir(workspace, "sid-a").is_dir()
    assert list((media_root / "artifacts").rglob("meta.json"))

    await storage.delete_session("agent:main:webchat:a")

    assert not transcript_material_dir(media_root, "sid-a").exists()
    assert not _workspace_attachment_dir(workspace, "sid-a").exists()
    store = ArtifactStore(media_root)
    with pytest.raises(ArtifactNotFoundError):
        store.get_ref(session_id="sid-a", artifact_id=listed_id)
    with pytest.raises(ArtifactNotFoundError):
        store.get_ref(session_id="sid-a", artifact_id=internal_id)
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
    listed_a, _internal_a = await _seed_material(media_root, workspace, "sid-a")
    listed_b, internal_b = await _seed_material(media_root, workspace, "sid-b")

    await storage.delete_session("agent:main:webchat:a")

    # Sibling session B's material must survive.
    assert transcript_material_dir(media_root, "sid-b").is_dir()
    assert _workspace_attachment_dir(workspace, "sid-b").is_dir()
    store = ArtifactStore(media_root)
    with pytest.raises(ArtifactNotFoundError):
        store.get_ref(session_id="sid-a", artifact_id=listed_a)
    assert store.get_ref(session_id="sid-b", artifact_id=listed_b).id == listed_b
    assert store.get_ref(session_id="sid-b", artifact_id=internal_b).id == internal_b
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


@pytest.mark.parametrize("root_kind", ["managed", "configured", "legacy", "project"])
@pytest.mark.parametrize("operation", ["delete", "prune"])
async def test_delete_captures_effective_material_root(
    tmp_path: Path, root_kind: str, operation: str,
) -> None:
    default = tmp_path / "default"
    actual = tmp_path / "task"
    default.mkdir()
    actual.mkdir()
    media_root = tmp_path / "media"
    set_session_material_cleanup(build_session_material_cleanup(_config(media_root, default)))
    async with SessionStorage(tmp_path / "sessions.db") as storage:
        node = SessionNode(session_key="agent:main:webchat:task", session_id="task")
        if root_kind == "project":
            project = await storage.create_or_restore_project_workspace(
                path=str(actual), path_key=project_path_key(actual),
                display_name="Task", trusted_at=1,
            )
            node.workspace_id = project.workspace_id
        elif root_kind == "legacy":
            node.origin = {
                "sandbox_run_context": {"run_mode": "standard", "workspace": str(actual)},
            }
        else:
            node.execution_workspace = configured_execution_workspace(actual)
            node.execution_workspace["kind"] = root_kind
        await storage.upsert_session(node)
        listed, internal = await _seed_material(media_root, actual, node.session_id)
        await _seed_material(media_root, actual, "sibling")
        wrong_directory = _workspace_attachment_dir(default, node.session_id)
        wrong_directory.mkdir(parents=True)
        (wrong_directory / "keep.txt").write_text("untouched")
        (actual / "index.html").write_text("source must survive")

        if operation == "delete":
            await storage.delete_session(node.session_key)
        else:
            await storage.prune_stale_sessions(10**16)

        assert await storage.get_session(node.session_key) is None
        assert not _workspace_attachment_dir(actual, node.session_id).exists()
        assert _workspace_attachment_dir(actual, "sibling").is_dir()
        assert (wrong_directory / "keep.txt").read_text() == "untouched"
        assert (actual / "index.html").read_text() == "source must survive"
        assert not transcript_material_dir(media_root, node.session_id).exists()
        for artifact_id in (listed, internal):
            with pytest.raises(ArtifactNotFoundError):
                ArtifactStore(media_root).get_ref(
                    session_id=node.session_id, artifact_id=artifact_id,
                )


async def test_project_history_delete_cleans_only_its_sessions_material(tmp_path: Path) -> None:
    default = tmp_path / "default"
    project_root = tmp_path / "project"
    default.mkdir()
    project_root.mkdir()
    media_root = tmp_path / "media"
    set_session_material_cleanup(build_session_material_cleanup(_config(media_root, default)))
    async with SessionStorage(tmp_path / "sessions.db") as storage:
        project = await storage.create_or_restore_project_workspace(
            path=str(project_root), path_key=project_path_key(project_root),
            display_name="Project", trusted_at=1,
        )
        node = SessionNode(
            session_key="agent:main:webchat:project", session_id="project",
            workspace_id=project.workspace_id,
        )
        await storage.upsert_session(node)
        await _seed_material(media_root, project_root, node.session_id)
        await _seed_material(media_root, project_root, "other")
        (project_root / "index.html").write_text("keep source")
        await storage.delete_project_workspace_sessions(project.workspace_id)
        assert not _workspace_attachment_dir(project_root, node.session_id).exists()
        assert _workspace_attachment_dir(project_root, "other").exists()
        assert (project_root / "index.html").read_text() == "keep source"
        assert await storage.get_project_workspace(project.workspace_id) is not None


async def test_invalid_bound_root_never_falls_back_or_deletes_link_target(tmp_path: Path) -> None:
    default = tmp_path / "default"
    task = tmp_path / "task"
    default.mkdir()
    task.mkdir()
    media_root = tmp_path / "media"
    set_session_material_cleanup(build_session_material_cleanup(_config(media_root, default)))
    async with SessionStorage(tmp_path / "sessions.db") as storage:
        node = SessionNode(
            session_key="agent:main:webchat:task", session_id="task",
            execution_workspace=configured_execution_workspace(task),
        )
        await storage.upsert_session(node)
        listed, _ = await _seed_material(media_root, default, node.session_id)
        task.rmdir()
        task.symlink_to(default, target_is_directory=True)
        await storage.delete_session(node.session_key)
        assert _workspace_attachment_dir(default, node.session_id).exists()
        assert not transcript_material_dir(media_root, node.session_id).exists()
        with pytest.raises(ArtifactNotFoundError):
            ArtifactStore(media_root).get_ref(session_id=node.session_id, artifact_id=listed)
