"""Transactional reset/delete coverage for session-bound artifact documents."""

from __future__ import annotations

import asyncio
import contextvars
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    ArtifactBlobRef,
    ArtifactKind,
    ArtifactNotFoundError,
    ArtifactSessionService,
)
from opensquilla.artifacts import ArtifactNotFoundError as ArtifactFileNotFoundError
from opensquilla.artifacts import ArtifactRef, ArtifactStore
from opensquilla.gateway.boot import (
    build_session_artifact_cleanup,
    build_session_material_cleanup,
)
from opensquilla.session.material_cleanup import (
    reset_session_artifact_cleanup,
    reset_session_material_cleanup,
    set_session_artifact_cleanup,
    set_session_material_cleanup,
)
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage

USER = Actor(ActorKind.USER, "user-1")


def _config(media_root: Path, workspace: Path) -> SimpleNamespace:
    return SimpleNamespace(
        attachments=SimpleNamespace(media_root=str(media_root)),
        workspace_dir=str(workspace),
        agents=[],
        state_dir=None,
        config_path=None,
    )


def _blob(ref: ArtifactRef) -> ArtifactBlobRef:
    return ArtifactBlobRef(
        artifact_id=ref.id,
        sha256=ref.sha256,
        filename=ref.name,
        media_type=ref.mime,
        byte_size=ref.size,
    )


def _publish(
    store: ArtifactStore,
    *,
    node: SessionNode,
    payload: bytes,
    visibility: str,
) -> ArtifactRef:
    return store.publish_bytes(
        payload,
        session_id=node.session_id,
        session_key=node.session_key,
        name="draft.html",
        mime="text/html",
        source="test",
        visibility=visibility,
    )


@pytest.fixture(autouse=True)
def _reset_cleanup_hooks():
    reset_session_material_cleanup()
    reset_session_artifact_cleanup()
    yield
    reset_session_material_cleanup()
    reset_session_artifact_cleanup()


async def _seed_document(
    storage: SessionStorage,
    store: ArtifactStore,
    node: SessionNode,
) -> tuple[ArtifactSessionService, object, ArtifactRef, ArtifactRef]:
    listed = _publish(store, node=node, payload=b"<h1>original</h1>", visibility="listed")
    internal = _publish(store, node=node, payload=b"<h1>draft</h1>", visibility="internal")
    service = await ArtifactSessionService.from_session_storage(storage)
    created = await service.create_document(
        session_key=node.session_key,
        session_id=node.session_id,
        name="Draft",
        kind=ArtifactKind.HTML,
        initial_artifact=_blob(listed),
        actor=USER,
    )
    committed = await service.commit_revision(
        document_id=created.document.document_id,
        expected_head_revision_id=created.revision.revision_id,
        expected_state_revision=created.document.state_revision,
        artifact=_blob(internal),
        actor=USER,
    )
    # Existing profiles retain old lifecycle rows until the owning session is deleted.
    async with storage._write_transaction("test.seed_historical_editor") as conn:
        await conn.execute(
            """INSERT INTO artifact_edit_sessions (
                edit_session_id, document_id, base_revision_id, last_saved_revision_id,
                mode, status, user_id, state_revision, expires_at,
                last_access_at, created_at, updated_at
            ) VALUES ('edit-reset-seed', ?, ?, ?, 'edit', 'closed', 'user', 1, 0, 1, 1, 1)""",
            (created.document.document_id, created.revision.revision_id,
             committed.revision.revision_id),
        )
    edit = await service.get_edit_session("edit-reset-seed")
    return service, (committed, edit), listed, internal


@pytest.mark.asyncio
async def test_reset_atomically_purges_old_state_and_only_internal_bytes(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _config(media_root, workspace)
    set_session_artifact_cleanup(build_session_artifact_cleanup(config))

    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    node = SessionNode(session_key="agent:main:webchat:reset", session_id="sid-old")
    await storage.upsert_session(node)
    store = ArtifactStore(media_root)
    service, state, listed, internal = await _seed_document(storage, store, node)
    committed, edit = state

    reset = node.model_copy(deep=True)
    reset.session_id = "sid-new"
    reset.epoch = 1
    await storage.reset_session(
        reset,
        expected_session_id=node.session_id,
        expected_epoch=0,
        archive_writer=lambda _snapshot: _noop(),
    )

    current = await storage.get_session(node.session_key)
    assert current is not None and current.session_id == "sid-new"
    assert await service.list_documents(session_key=node.session_key, session_id="sid-old") == ()
    with pytest.raises(ArtifactNotFoundError):
        await service.get_edit_session(edit.edit_session_id)
    with pytest.raises(ArtifactNotFoundError):
        await service.get_document(committed.document.document_id)
    assert store.get_ref(session_id="sid-old", artifact_id=listed.id).id == listed.id
    with pytest.raises(ArtifactFileNotFoundError):
        store.get_ref(session_id="sid-old", artifact_id=internal.id)
    await service.close()
    await storage.close()


async def _noop() -> None:
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_reset_cleanup_keeps_loop_responsive_and_finishes_exact_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    media_root = tmp_path / "media"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    set_session_artifact_cleanup(build_session_artifact_cleanup(_config(media_root, workspace)))
    store = ArtifactStore(media_root)
    old = SessionNode(session_key="agent:main:webchat:cleanup", session_id="sid-old")
    sibling = SessionNode(session_key="agent:main:webchat:sibling", session_id="sid-sibling")
    listed = _publish(store, node=old, payload=b"listed", visibility="listed")
    internal = _publish(store, node=old, payload=b"internal", visibility="internal")
    sibling_internal = _publish(store, node=sibling, payload=b"sibling", visibility="internal")
    new = old.model_copy(update={"session_id": "sid-new", "epoch": 1})
    started, release, completed = asyncio.Event(), threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    context = contextvars.ContextVar("artifact_cleanup_test_context", default="missing")
    original_delete = ArtifactStore.delete_session_internal_artifacts
    observed = []

    def slow_delete(instance, session_id):
        assert threading.get_ident() != loop_thread
        observed.append((session_id, context.get()))
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(5), "event loop did not release artifact cleanup"
            return original_delete(instance, session_id)
        finally:
            completed.set()

    monkeypatch.setattr(ArtifactStore, "delete_session_internal_artifacts", slow_delete)
    async with SessionStorage(tmp_path / "sessions.db") as storage:
        await storage.upsert_session(old)
        token = context.set("captured")
        try:
            task = asyncio.create_task(storage.reset_session(
                new, expected_session_id=old.session_id, expected_epoch=0,
                archive_writer=lambda _snapshot: _noop(),
            ))
        finally:
            context.reset(token)
        try:
            await asyncio.wait_for(started.wait(), 3)
            # The durable reset is committed, and the shared connection remains
            # available while only the old generation's disk cleanup waits.
            current = await asyncio.wait_for(storage.get_session(old.session_key), 1)
            assert current is not None and current.session_id == new.session_id
            new_internal = _publish(store, node=new, payload=b"new", visibility="internal")
            if cancel:
                for _ in range(2):
                    task.cancel("cancel cleanup")
                    await asyncio.sleep(0)
                    assert not task.done()
                    assert not completed.is_set()
            release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError, match="cancel cleanup"):
                    await task
            else:
                await task
            assert completed.is_set()
            assert observed == [(old.session_id, "captured")]
            with pytest.raises(ArtifactFileNotFoundError):
                store.get_ref(session_id=old.session_id, artifact_id=internal.id)
            assert store.get_ref(session_id=old.session_id, artifact_id=listed.id).id == listed.id
            assert store.get_ref(
                session_id=sibling.session_id, artifact_id=sibling_internal.id,
            ).id == sibling_internal.id
            assert store.get_ref(session_id=new.session_id, artifact_id=new_internal.id).id == (
                new_internal.id
            )
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_delete_purges_listed_and_shared_internal_refs(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _config(media_root, workspace)
    set_session_material_cleanup(build_session_material_cleanup(config))

    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    node = SessionNode(session_key="agent:main:webchat:delete", session_id="sid-delete")
    await storage.upsert_session(node)
    store = ArtifactStore(media_root)
    service, _state, listed, internal = await _seed_document(storage, store, node)
    # A second document points at the same internal artifact id. Cleanup must
    # tolerate duplicate DB references and remove the bucket exactly once.
    await service.create_document(
        session_key=node.session_key,
        session_id=node.session_id,
        name="Shared draft",
        kind=ArtifactKind.HTML,
        initial_artifact=_blob(internal),
        actor=USER,
    )

    await storage.delete_session(node.session_key)

    assert await storage.get_session(node.session_key) is None
    assert (
        await service.list_documents(
            session_key=node.session_key,
            session_id=node.session_id,
        )
        == ()
    )
    with pytest.raises(ArtifactFileNotFoundError):
        store.get_ref(session_id=node.session_id, artifact_id=listed.id)
    with pytest.raises(ArtifactFileNotFoundError):
        store.get_ref(session_id=node.session_id, artifact_id=internal.id)
    await service.close()
    await storage.close()


@pytest.mark.asyncio
async def test_reset_db_failure_keeps_old_session_and_all_material(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    node = SessionNode(session_key="agent:main:webchat:rollback", session_id="sid-old")
    await storage.upsert_session(node)
    store = ArtifactStore(media_root)
    service, state, listed, internal = await _seed_document(storage, store, node)
    committed, _edit = state
    async with storage._write_transaction("test.artifact_delete_failure") as conn:
        await conn.execute(
            """
            CREATE TRIGGER fail_artifact_document_delete
            BEFORE DELETE ON artifact_documents
            BEGIN
                SELECT RAISE(ABORT, 'synthetic artifact purge failure');
            END
            """
        )

    reset = node.model_copy(deep=True)
    reset.session_id = "sid-new"
    reset.epoch = 1
    with pytest.raises(sqlite3.IntegrityError, match="synthetic artifact purge failure"):
        await storage.reset_session(
            reset,
            expected_session_id=node.session_id,
            expected_epoch=0,
            archive_writer=lambda _snapshot: _noop(),
        )

    current = await storage.get_session(node.session_key)
    assert current is not None and current.session_id == node.session_id
    durable_document = await service.get_document(committed.document.document_id)
    assert durable_document.session_id == node.session_id
    assert store.get_ref(session_id=node.session_id, artifact_id=listed.id).id == listed.id
    assert store.get_ref(session_id=node.session_id, artifact_id=internal.id).id == internal.id
    await service.close()
    await storage.close()


@pytest.mark.asyncio
async def test_post_commit_cleanup_failure_leaks_bytes_without_restoring_old_state(
    tmp_path: Path,
) -> None:
    async def failing_cleanup(_session_id: str, _session_key: str) -> None:
        raise OSError("disk unavailable")

    set_session_artifact_cleanup(failing_cleanup)
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    node = SessionNode(session_key="agent:main:webchat:gc-fail", session_id="sid-old")
    await storage.upsert_session(node)
    store = ArtifactStore(tmp_path / "media")
    service, _state, _listed, internal = await _seed_document(storage, store, node)

    reset = node.model_copy(deep=True)
    reset.session_id = "sid-new"
    reset.epoch = 1
    await storage.reset_session(
        reset,
        expected_session_id=node.session_id,
        expected_epoch=0,
        archive_writer=lambda _snapshot: _noop(),
    )

    current = await storage.get_session(node.session_key)
    assert current is not None and current.session_id == "sid-new"
    assert await service.list_documents(session_key=node.session_key) == ()
    # Best-effort GC failure may leak bytes, but never resurrects state or
    # points the new session generation at them.
    assert store.get_ref(session_id=node.session_id, artifact_id=internal.id).id == internal.id
    await service.close()
    await storage.close()


@pytest.mark.asyncio
async def test_deleting_fork_parent_preserves_child_head_and_lineage(tmp_path: Path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    parent = SessionNode(session_key="agent:main:webchat:parent", session_id="sid-parent")
    child = SessionNode(session_key="agent:main:webchat:child", session_id="sid-child")
    await storage.upsert_session(parent)
    await storage.upsert_session(child)
    store = ArtifactStore(tmp_path / "media")
    listed = _publish(
        store,
        node=parent,
        payload=b"<h1>fork source</h1>",
        visibility="listed",
    )
    service = await ArtifactSessionService.from_session_storage(storage)
    source = await service.create_document(
        session_key=parent.session_key,
        session_id=parent.session_id,
        name="Source",
        kind=ArtifactKind.HTML,
        initial_artifact=_blob(listed),
        actor=USER,
    )
    snapshots = await service.snapshot_session_heads(session_id=parent.session_id)
    forked = await service.fork_session_heads(
        source_session_id=parent.session_id,
        target_session_key=child.session_key,
        target_session_id=child.session_id,
        snapshots=snapshots,
        actor=USER,
    )

    await storage.delete_session(parent.session_key)

    with pytest.raises(ArtifactNotFoundError):
        await service.get_document(source.document.document_id)
    durable_child = await service.get_document(forked[0].document.document_id)
    durable_revision = await service.get_revision(durable_child.head_revision_id)
    assert durable_revision.copied_from_revision_id == source.revision.revision_id
    assert durable_revision.artifact_id == source.revision.artifact_id
    await service.close()
    await storage.close()
