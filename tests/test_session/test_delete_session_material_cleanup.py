"""Deleting a session cascades to attachment and artifact material stores."""

from __future__ import annotations

import asyncio
import contextvars
import os
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

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
from opensquilla.paths import native_io_path
from opensquilla.project_workspaces import project_path_key
from opensquilla.session import material_cleanup
from opensquilla.session.material_cleanup import (
    reset_session_artifact_cleanup,
    reset_session_material_cleanup,
    rmtree_scoped,
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
    native_io_path(att_dir).mkdir(parents=True, exist_ok=True)
    native_io_path(att_dir / "doc.pdf").write_bytes(b"%PDF-1.4\n")
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


@pytest.mark.parametrize("operation", ["delete", "prune"])
async def test_material_cleanup_does_not_block_gateway_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    media_root = tmp_path / "media"
    set_session_material_cleanup(build_session_material_cleanup(_config(media_root, workspace)))
    node = SessionNode(session_key="agent:main:webchat:slow-cleanup", session_id="target")
    transcript_root = transcript_material_dir(media_root, node.session_id)
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    released_by_event_loop: list[bool] = []
    original_rmtree = shutil.rmtree

    def slow_remove(path, *args, **kwargs):
        if native_io_path(path) == native_io_path(transcript_root):
            loop.call_soon_threadsafe(started.set)
            released_by_event_loop.append(release.wait(timeout=1))
        return original_rmtree(path, *args, **kwargs)

    async with SessionStorage(tmp_path / "sessions.db") as storage:
        await storage.upsert_session(node)
        await _seed_material(media_root, workspace, node.session_id)
        monkeypatch.setattr(material_cleanup.shutil, "rmtree", slow_remove)
        deletion = asyncio.create_task(
            storage.delete_session(node.session_key)
            if operation == "delete" else storage.prune_stale_sessions(10**16)
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=3)
            # Cleanup has started after commit; unrelated event-loop work and
            # database reads must proceed while the filesystem operation waits.
            assert await storage.get_session(node.session_key) is None
            release.set()
            await deletion
            assert released_by_event_loop == [True]
            assert not transcript_root.exists()
            assert not _workspace_attachment_dir(workspace, node.session_id).exists()
        finally:
            release.set()
            await deletion


@pytest.mark.parametrize("cancel", [False, True])
async def test_slow_material_preparation_leaves_loop_responsive_and_cancel_does_not_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    from opensquilla.artifact_session import working_files

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    media_root = tmp_path / "media"
    node = SessionNode(session_key="agent:main:webchat:prepare", session_id="prepare-target")
    listed, internal = await _seed_material(media_root, workspace, node.session_id)
    sibling, _ = await _seed_material(media_root, workspace, "prepare-sibling")
    started, release, completed = asyncio.Event(), threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    original_check = working_files.checked_path

    def slow_check(root, relative):
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(5), "loop did not release read-only workspace check"
            return original_check(root, relative)
        finally:
            completed.set()

    monkeypatch.setattr(working_files, "checked_path", slow_check)
    set_session_material_cleanup(build_session_material_cleanup(_config(media_root, workspace)))
    store = ArtifactStore(media_root)
    async with SessionStorage(tmp_path / "sessions.db") as storage:
        await storage.upsert_session(node)
        task = asyncio.create_task(storage.delete_session(node.session_key))
        try:
            await asyncio.wait_for(started.wait(), 3)
            await asyncio.wait_for(asyncio.sleep(0.01), 0.5)
            assert not task.done()
            # Preparation intentionally remains within the existing transaction.
            assert storage._operation_lock.locked()
            assert transcript_material_dir(media_root, node.session_id).exists()
            if cancel:
                task.cancel("cancel preparation")
                with pytest.raises(asyncio.CancelledError, match="cancel preparation"):
                    await asyncio.wait_for(task, 1)
                assert not completed.is_set()
                assert await storage.get_session(node.session_key) is not None
            release.set()
            if not cancel:
                await task
            async with asyncio.timeout(2):
                while not completed.is_set():
                    await asyncio.sleep(0.005)
            current = await storage.get_session(node.session_key)
            assert (current is not None) is cancel
            assert transcript_material_dir(media_root, node.session_id).exists() is cancel
            assert _workspace_attachment_dir(workspace, node.session_id).exists() is cancel
            if cancel:
                assert store.get_ref(session_id=node.session_id, artifact_id=listed).id == listed
                assert store.get_ref(
                    session_id=node.session_id, artifact_id=internal,
                ).id == internal
            else:
                with pytest.raises(ArtifactNotFoundError):
                    store.get_ref(session_id=node.session_id, artifact_id=listed)
            assert store.get_ref(session_id="prepare-sibling", artifact_id=sibling).id == sibling
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            async with asyncio.timeout(2):
                while not completed.is_set():
                    await asyncio.sleep(0.005)


@pytest.mark.parametrize("worker_fails", [False, True])
async def test_material_cleanup_settles_worker_before_propagating_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_fails: bool,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    media_root = tmp_path / "media"
    node = SessionNode(session_key="agent:main:webchat:cancel-cleanup", session_id="target")
    await _seed_material(media_root, workspace, node.session_id)
    cleanup = await build_session_material_cleanup(_config(media_root, workspace))(node, None)
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    completed = threading.Event()
    original_delete = ArtifactStore.delete_session_artifacts

    def slow_delete(store, session_id):
        loop.call_soon_threadsafe(started.set)
        try:
            if not release.wait(timeout=3):
                raise TimeoutError("test did not release artifact deletion")
            if worker_fails:
                raise RuntimeError("injected late cleanup failure")
            return original_delete(store, session_id)
        finally:
            completed.set()

    monkeypatch.setattr(ArtifactStore, "delete_session_artifacts", slow_delete)
    deletion = asyncio.create_task(cleanup())
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        for _ in range(2):
            deletion.cancel()
            await asyncio.sleep(0)
            assert not deletion.done()
            assert not completed.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await deletion
        assert completed.is_set()
        assert not transcript_material_dir(media_root, node.session_id).exists()
        assert not _workspace_attachment_dir(workspace, node.session_id).exists()
        assert bool(list((media_root / "artifacts").rglob("meta.json"))) is worker_fails
    finally:
        release.set()
        await asyncio.gather(deletion, return_exceptions=True)


async def test_material_cleanup_settles_worker_when_loop_tasks_are_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    media_root = tmp_path / "media"
    node = SessionNode(session_key="agent:main:webchat:loop-shutdown", session_id="target")
    await _seed_material(media_root, workspace, node.session_id)
    cleanup = await build_session_material_cleanup(_config(media_root, workspace))(node, None)
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    context = contextvars.ContextVar("material_cleanup_test_context", default="missing")
    observed_context: list[str] = []
    original_delete = ArtifactStore.delete_session_artifacts

    def slow_delete(store, session_id):
        observed_context.append(context.get())
        started.set()
        try:
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release artifact deletion")
            return original_delete(store, session_id)
        finally:
            completed.set()

    async def isolated_loop() -> None:
        token = context.set("preserved")
        deletion = asyncio.create_task(cleanup())
        tasks = set()
        try:
            assert await asyncio.to_thread(started.wait, 3)
            # Match Runner's cancellation snapshot in a dedicated loop, keeping
            # only this test controller alive to release the filesystem worker.
            tasks = asyncio.all_tasks() - {asyncio.current_task()}
            for task in tasks:
                task.cancel("event loop shutdown")
            await asyncio.wait(tasks, timeout=0.05)
            assert not deletion.done()
            assert not completed.is_set()
        finally:
            release.set()
            await asyncio.gather(*tasks, deletion, return_exceptions=True)
            context.reset(token)
        assert deletion.cancelled()
        assert completed.is_set()

    monkeypatch.setattr(ArtifactStore, "delete_session_artifacts", slow_delete)
    await asyncio.to_thread(lambda: asyncio.run(isolated_loop()))
    assert observed_context == ["preserved"]
    assert not list((media_root / "artifacts").rglob("meta.json"))


async def test_prune_cancellation_finishes_every_committed_session_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    media_root = tmp_path / "media"
    set_session_material_cleanup(build_session_material_cleanup(_config(media_root, workspace)))
    nodes = [
        SessionNode(session_key=f"agent:main:webchat:pruned-{i}", session_id=f"pruned-{i}",
                    created_at=1, updated_at=1)
        for i in range(2)
    ]
    survivor = SessionNode(session_key="agent:main:webchat:kept", session_id="kept",
                           created_at=100, updated_at=100)
    loop = asyncio.get_running_loop()
    started = [asyncio.Event(), asyncio.Event()]
    release = [threading.Event(), threading.Event()]
    completed: list[str] = []
    attempted: list[str] = []
    original_delete = ArtifactStore.delete_session_artifacts

    def slow_delete(store, session_id):
        index = len(attempted)
        attempted.append(session_id)
        loop.call_soon_threadsafe(started[index].set)
        if not release[index].wait(timeout=5):
            raise TimeoutError("test did not release artifact deletion")
        result = original_delete(store, session_id)
        completed.append(session_id)
        return result

    async with SessionStorage(tmp_path / "sessions.db") as storage:
        for node in [*nodes, survivor]:
            await storage.upsert_session(node)
            await _seed_material(media_root, workspace, node.session_id)
        monkeypatch.setattr(ArtifactStore, "delete_session_artifacts", slow_delete)
        pruning = asyncio.create_task(storage.prune_stale_session_records(10))
        try:
            await asyncio.wait_for(started[0].wait(), timeout=3)
            for node in nodes:
                assert await storage.get_session(node.session_key) is None
            pruning.cancel("first prune cancellation")
            await asyncio.sleep(0)
            pruning.cancel("repeated during first cleanup")
            await asyncio.sleep(0)
            assert not pruning.done()
            release[0].set()
            await asyncio.wait_for(started[1].wait(), timeout=3)
            pruning.cancel("repeated during second cleanup")
            await asyncio.sleep(0)
            assert not pruning.done()
            release[1].set()
            with pytest.raises(asyncio.CancelledError, match="first prune cancellation"):
                await pruning
            assert set(completed) == {node.session_id for node in nodes}
            for node in nodes:
                assert not transcript_material_dir(media_root, node.session_id).exists()
                assert not _workspace_attachment_dir(workspace, node.session_id).exists()
            assert await storage.get_session(survivor.session_key) is not None
            assert transcript_material_dir(media_root, survivor.session_id).exists()
            assert _workspace_attachment_dir(workspace, survivor.session_id).exists()
        finally:
            for gate in release:
                gate.set()
            await asyncio.gather(pruning, return_exceptions=True)


async def test_material_cleanup_worker_failure_keeps_committed_delete_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    media_root = tmp_path / "media"
    set_session_material_cleanup(build_session_material_cleanup(_config(media_root, workspace)))

    def fail_delete(store, session_id):
        raise RuntimeError("injected artifact cleanup failure")

    monkeypatch.setattr(ArtifactStore, "delete_session_artifacts", fail_delete)
    async with SessionStorage(tmp_path / "sessions.db") as storage:
        node = SessionNode(session_key="agent:main:webchat:failed-worker", session_id="target")
        await storage.upsert_session(node)
        with capture_logs() as logs:
            await storage.delete_session(node.session_key)
        assert await storage.get_session(node.session_key) is None
        assert any(
            entry["event"] == "session_material_cleanup.failed"
            and entry["error"] == "injected artifact cleanup failure"
            for entry in logs
        )


async def test_material_cleanup_uses_captured_generation_and_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    replacement_workspace = tmp_path / "replacement"
    workspace.mkdir()
    replacement_workspace.mkdir()
    media_root = tmp_path / "media"
    replacement_media = tmp_path / "replacement-media"
    config = _config(media_root, workspace)
    node = SessionNode(session_key="agent:main:webchat:reused-key", session_id="old")
    await _seed_material(media_root, workspace, "old")
    await _seed_material(replacement_media, replacement_workspace, "new")
    cleanup = await build_session_material_cleanup(config)(node, None)
    node.session_id = "new"
    config.workspace_dir = str(replacement_workspace)
    config.attachments.media_root = str(replacement_media)

    await cleanup()

    assert not transcript_material_dir(media_root, "old").exists()
    assert not _workspace_attachment_dir(workspace, "old").exists()
    assert transcript_material_dir(replacement_media, "new").is_dir()
    assert _workspace_attachment_dir(replacement_workspace, "new").is_dir()


async def test_material_cleanup_rechecks_link_components_in_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    media_root = tmp_path / "media"
    node = SessionNode(session_key="agent:main:webchat:changed-root", session_id="target")
    await _seed_material(media_root, workspace, node.session_id)
    cleanup = await build_session_material_cleanup(_config(media_root, workspace))(node, None)
    original_is_symlink = type(workspace).is_symlink

    def replaced_component(path):
        return path == workspace / ".opensquilla" or original_is_symlink(path)

    monkeypatch.setattr(type(workspace), "is_symlink", replaced_component)
    await cleanup()

    assert not transcript_material_dir(media_root, node.session_id).exists()
    assert _workspace_attachment_dir(workspace, node.session_id).is_dir()
    assert not list((media_root / "artifacts").rglob("meta.json"))


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


@pytest.mark.parametrize("operation", ["delete", "prune"])
@pytest.mark.parametrize("long_path", [False, True], ids=["short", "long"])
async def test_material_cleanup_handles_short_and_long_paths(
    tmp_path: Path, operation: str, long_path: bool,
) -> None:
    if long_path and os.name != "nt":
        pytest.skip("Windows extended-length path regression")
    material_root = tmp_path / "material"
    if long_path:
        while len(str(material_root)) <= 280:
            material_root /= "segment-" + "x" * 40
    workspace = tmp_path / "workspace"
    media_root = material_root / "media"
    native_io_path(workspace).mkdir(parents=True)
    set_session_material_cleanup(build_session_material_cleanup(_config(media_root, workspace)))
    try:
        async with SessionStorage(tmp_path / "sessions.db") as storage:
            node = SessionNode(session_key="agent:main:webchat:cleanup-path", session_id="target")
            await storage.upsert_session(node)
            listed, internal = await _seed_material(media_root, workspace, node.session_id)
            await _seed_material(media_root, workspace, "sibling")
            if long_path:
                nested = _workspace_attachment_dir(workspace, node.session_id)
                while len(str(nested)) <= 280:
                    nested /= "nested-" + "x" * 40
                native_io_path(nested).mkdir(parents=True)
                native_io_path(nested / "payload.txt").write_text("nested attachment")
            source = workspace / "authored.txt"
            native_io_path(source).write_text("keep source")

            if operation == "delete":
                await storage.delete_session(node.session_key)
            else:
                await storage.prune_stale_sessions(10**16)

            assert await storage.get_session(node.session_key) is None
            assert not native_io_path(transcript_material_dir(media_root, "target")).exists()
            assert not native_io_path(_workspace_attachment_dir(workspace, "target")).exists()
            assert native_io_path(transcript_material_dir(media_root, "sibling")).is_dir()
            assert native_io_path(_workspace_attachment_dir(workspace, "sibling")).is_dir()
            assert native_io_path(source).read_text() == "keep source"
            for artifact_id in (listed, internal):
                with pytest.raises(ArtifactNotFoundError):
                    ArtifactStore(media_root).get_ref(
                        session_id=node.session_id, artifact_id=artifact_id,
                    )
    finally:
        shutil.rmtree(native_io_path(tmp_path / "material"), ignore_errors=True)


async def test_material_cleanup_logs_failure_and_continues_other_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    media_root = tmp_path / "media"
    set_session_material_cleanup(build_session_material_cleanup(_config(media_root, workspace)))
    async with SessionStorage(tmp_path / "sessions.db") as storage:
        node = SessionNode(session_key="agent:main:webchat:cleanup-failure", session_id="target")
        await storage.upsert_session(node)
        listed, internal = await _seed_material(media_root, workspace, node.session_id)
        transcript_root = transcript_material_dir(media_root, node.session_id)
        original_rmtree = shutil.rmtree

        def deny_transcript(path, *args, **kwargs):
            if native_io_path(path) == native_io_path(transcript_root):
                if kwargs.get("ignore_errors"):
                    return None
                raise PermissionError("injected transcript deletion failure")
            return original_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(material_cleanup.shutil, "rmtree", deny_transcript)
        with capture_logs() as logs:
            await storage.delete_session(node.session_key)

        assert await storage.get_session(node.session_key) is None
        assert transcript_root.is_dir()
        assert not _workspace_attachment_dir(workspace, node.session_id).exists()
        for artifact_id in (listed, internal):
            with pytest.raises(ArtifactNotFoundError):
                ArtifactStore(media_root).get_ref(
                    session_id=node.session_id, artifact_id=artifact_id,
                )
        assert any(
            entry["event"] == "session_material_cleanup.remove_failed"
            and entry["target"] == str(transcript_root)
            and entry["error_type"] == "PermissionError"
            for entry in logs
        )


def test_material_cleanup_missing_directory_race_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target"
    target.mkdir()

    def disappeared(path, *args, **kwargs):
        Path(path).rmdir()
        if kwargs.get("ignore_errors"):
            return None
        raise FileNotFoundError(str(path))

    monkeypatch.setattr(material_cleanup.shutil, "rmtree", disappeared)
    with capture_logs() as logs:
        rmtree_scoped(target, expected_name="target")

    assert not target.exists()
    assert logs == []


@pytest.mark.parametrize("child_kind", ["file", "directory"])
def test_material_cleanup_continues_when_child_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child_kind: str,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    disappearing = target / "disappearing"
    if child_kind == "file":
        disappearing.write_text("attachment removed concurrently")
    else:
        disappearing.mkdir()
    (target / "remaining.txt").write_text("attachment still to clean")
    operation = "unlink" if child_kind == "file" else "rmdir"
    original_remove = getattr(os, operation)
    race_injected = False

    def remove_with_disappearing_child(path, *args, **kwargs):
        nonlocal race_injected
        original_remove(path, *args, **kwargs)
        if Path(path).name == disappearing.name:
            # The child was enumerated, but another actor removed it before
            # rmtree could do so. Exercise rmtree's real per-item error path.
            race_injected = True
            raise FileNotFoundError(str(path))

    monkeypatch.setattr(os, operation, remove_with_disappearing_child)
    with capture_logs() as logs:
        rmtree_scoped(target, expected_name="target")

    assert race_injected
    assert not target.exists()
    assert logs == []


def test_material_cleanup_child_permission_failure_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    blocked = target / "blocked.txt"
    blocked.write_text("locked attachment")
    original_unlink = os.unlink

    def deny_child(path, *args, **kwargs):
        if Path(path).name == blocked.name:
            raise PermissionError("injected child deletion failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", deny_child)
    with capture_logs() as logs:
        rmtree_scoped(target, expected_name="target")

    assert blocked.read_text() == "locked attachment"
    assert any(
        entry["event"] == "session_material_cleanup.remove_failed"
        and entry["target"] == str(target)
        and entry["error_type"] == "PermissionError"
        for entry in logs
    )


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
        try:
            task.symlink_to(default, target_is_directory=True)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                pytest.skip("Windows symlink privilege is unavailable")
            raise
        await storage.delete_session(node.session_key)
        assert _workspace_attachment_dir(default, node.session_id).exists()
        assert not transcript_material_dir(media_root, node.session_id).exists()
        with pytest.raises(ArtifactNotFoundError):
            ArtifactStore(media_root).get_ref(session_id=node.session_id, artifact_id=listed)
