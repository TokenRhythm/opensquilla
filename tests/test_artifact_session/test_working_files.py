from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path

import pytest

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    ArtifactBlobRef,
    ArtifactKind,
    ArtifactSessionService,
    ArtifactValidationError,
)
from opensquilla.artifact_session.working_files import (
    ensure_working_files,
    save_working_version,
)
from opensquilla.artifacts import (
    ArtifactBundle,
    ArtifactBundleSourceFile,
    ArtifactSource,
    ArtifactStore,
)


@pytest.fixture
async def working_document(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ArtifactStore(tmp_path / "media")
    bundle = ArtifactBundle(
        entrypoint="index.html",
        files=(
            ArtifactBundleSourceFile(
                path="index.html",
                mime="text/html",
                data=(
                    '<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="style.css">'
                    '<h1>发布会</h1><script src="app.js"></script>'
                ).encode(),
            ),
            ArtifactBundleSourceFile(path="style.css", mime="text/css", data=b"h1{color:navy}"),
            ArtifactBundleSourceFile(
                path="app.js", mime="text/javascript", data=b"window.ready=true"
            ),
        ),
    )
    ref = store.publish_bundle(
        bundle,
        session_id="test-session",
        session_key="test-key",
        name="index.html",
        mime="text/html",
        source="test",
    )
    service = await ArtifactSessionService.open(tmp_path / "state.db")
    result = await service.create_document(
        session_key="test-key",
        session_id="test-session",
        name="index.html",
        kind=ArtifactKind.HTML,
        initial_artifact=ArtifactBlobRef(
            artifact_id=ref.id,
            sha256=ref.sha256,
            filename=ref.name,
            media_type=ref.mime,
            byte_size=ref.size,
        ),
        actor=Actor(kind=ActorKind.USER, actor_id="test-user"),
    )
    binding = await ensure_working_files(
        service,
        store,
        document_id=result.document.document_id,
        session_key="test-key",
        session_id="test-session",
        workspace=str(workspace),
    )
    try:
        yield service, store, result, binding
    finally:
        await service.close()


async def test_resource_only_edit_is_saved_and_original_bytes_stay_immutable(working_document):
    service, store, initial, binding = working_document
    original = (binding.root / "style.css").read_bytes()
    (binding.root / "style.css").write_text("h1{color:crimson}")
    saved = await save_working_version(
        service,
        store,
        document_id=binding.document_id,
        session_key="test-key",
        session_id="test-session",
        actor_id="test-turn",
    )
    assert saved is not None
    assert saved.revision.artifact_sha256 == initial.revision.artifact_sha256
    assert saved.revision.artifact_id != initial.revision.artifact_id
    prior = store.resolve_preview_resource(
        initial.revision.artifact_id, session_id="test-session", logical_path="style.css"
    )
    assert prior.path.read_bytes() == original
    current = store.resolve_preview_resource(
        saved.revision.artifact_id, session_id="test-session", logical_path="style.css"
    )
    assert current.path.read_text() == "h1{color:crimson}"
    again = await save_working_version(
        service,
        store,
        document_id=binding.document_id,
        session_key="test-key",
        session_id="test-session",
        actor_id="next-turn",
    )
    assert again is None
    assert len(await service.list_revisions(binding.document_id)) == 2


async def test_restore_recovers_resources_and_preserves_unpublished_bytes(working_document):
    service, store, initial, binding = working_document
    (binding.root / "app.js").write_text("window.ready=false")
    saved = await save_working_version(
        service,
        store,
        document_id=binding.document_id,
        session_key="test-key",
        session_id="test-session",
        actor_id="test-turn",
    )
    assert saved is not None
    (binding.root / "notes.txt").write_text("unfinished edit")
    await service.restore_revision(
        document_id=binding.document_id,
        target_revision_id=initial.revision.revision_id,
        expected_head_revision_id=saved.revision.revision_id,
        expected_state_revision=saved.document.state_revision,
        actor=Actor(kind=ActorKind.USER, actor_id="test-user"),
    )
    restored = await ensure_working_files(
        service,
        store,
        document_id=binding.document_id,
        session_key="test-key",
        session_id="test-session",
        workspace=binding.workspace,
    )
    assert (restored.root / "app.js").read_text() == "window.ready=true"
    backups = list((Path(binding.workspace) / "artifacts").glob("recovered-*/notes.txt"))
    assert len(backups) == 1 and backups[0].read_text() == "unfinished edit"
    again = await ensure_working_files(
        service,
        store,
        document_id=binding.document_id,
        session_key="test-key",
        session_id="test-session",
        workspace=binding.workspace,
    )
    assert again == restored
    assert len(list((Path(binding.workspace) / "artifacts").glob("recovered-*"))) == 1


async def test_working_file_scope_and_link_escape_are_rejected(working_document, tmp_path):
    service, store, _initial, binding = working_document
    with pytest.raises(ArtifactValidationError):
        await ensure_working_files(
            service,
            store,
            document_id=binding.document_id,
            session_key="other-key",
            session_id="test-session",
            workspace=binding.workspace,
        )
    outside = tmp_path / "outside.js"
    outside.write_text("private material")
    (binding.root / "app.js").unlink()
    try:
        (binding.root / "app.js").symlink_to(outside)
    except OSError:
        pytest.skip("This host cannot create symbolic links")
    with pytest.raises(ValueError):
        await asyncio.to_thread(binding.bundle)


async def test_cancelled_restore_waits_for_filesystem_worker_before_reopening(
    working_document,
    monkeypatch,
):
    import threading

    from opensquilla.artifact_session import working_files as module

    service, store, initial, binding = working_document
    (binding.root / "app.js").write_text("window.ready=false")
    saved = await save_working_version(
        service,
        store,
        document_id=binding.document_id,
        session_key="test-key",
        session_id="test-session",
        actor_id="test-turn",
    )
    assert saved is not None
    await service.restore_revision(
        document_id=binding.document_id,
        target_revision_id=initial.revision.revision_id,
        expected_head_revision_id=saved.revision.revision_id,
        expected_state_revision=saved.document.state_revision,
        actor=Actor(kind=ActorKind.USER, actor_id="test-user"),
    )
    started, release = threading.Event(), threading.Event()
    original = module._materialize
    active = 0
    overlapped = False

    def materialize(*args):
        nonlocal active, overlapped
        active += 1
        overlapped |= active > 1
        started.set()
        try:
            assert release.wait(5), "test did not release the filesystem worker"
            return original(*args)
        finally:
            active -= 1

    monkeypatch.setattr(module, "_materialize", materialize)

    async def reopen():
        return await ensure_working_files(
            service,
            store,
            document_id=binding.document_id,
            session_key="test-key",
            session_id="test-session",
            workspace=binding.workspace,
        )

    cancelled = asyncio.create_task(reopen())
    second = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        cancelled.cancel()
        await asyncio.sleep(0)
        cancelled.cancel()
        second = asyncio.create_task(reopen())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second), 0.05)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        if second is not None:
            restored = await second
    assert not overlapped
    assert (restored.root / "app.js").read_text() == "window.ready=true"
    assert (
        restored.base_revision_id
        == (await service.get_document_head(binding.document_id)).revision.revision_id
    )


def _publish_working_bundle(store, binding, *, session_key="test-key", session_id="test-session"):
    return store.publish_bundle(
        binding.bundle(),
        session_id=session_id,
        session_key=session_key,
        name="index.html",
        mime="text/html",
        source="synthetic-publication",
    )


async def test_explicit_publication_versions_immutable_bundle_and_preserves_later_edits(
    working_document,
):
    service, store, initial, binding = working_document
    style = binding.root / "style.css"
    style.write_text("h1{color:crimson}")
    published = _publish_working_bundle(store, binding)
    style.write_text("h1{color:green}")
    arguments = dict(
        document_id=binding.document_id,
        session_key="test-key",
        session_id="test-session",
        actor_id="test-turn",
    )

    saved = await save_working_version(service, store, published_ref=published, **arguments)

    assert saved.revision.artifact_id == published.id
    assert saved.revision.artifact_sha256 == initial.revision.artifact_sha256
    assert style.read_text() == "h1{color:green}"
    resource = store.resolve_preview_resource(
        saved.revision.artifact_id, session_id="test-session", logical_path="style.css"
    )
    assert resource.path.read_text() == "h1{color:crimson}"
    publications = await service.list_document_publications(session_id="test-session")
    assert len(publications) == 1
    assert publications[0].document_id == binding.document_id
    assert publications[0].revision_id == saved.revision.revision_id
    assert publications[0].deliverable_artifact_id == published.id
    assert await service.list_document_publish_attempts_for_recovery() == ()

    later = await save_working_version(service, store, **arguments)
    assert later.revision.artifact_id != published.id
    replay = await save_working_version(service, store, published_ref=published, **arguments)
    assert replay is None
    assert (await service.get_document_head(binding.document_id)).revision == later.revision
    assert len(await service.list_revisions(binding.document_id)) == 3
    assert await service.list_document_publications(session_id="test-session") == publications


@pytest.mark.parametrize(
    "javascript_mime", ("text/javascript", "application/javascript", "application/x-javascript"),
)
async def test_unchanged_bundle_publication_links_current_revision_without_adding_version(
    working_document, monkeypatch, javascript_mime,
):
    from opensquilla.artifact_session.working_files import restore_working_revision
    from opensquilla.engine.types import ArtifactEvent
    from opensquilla.gateway.generated_artifact_adoption import GeneratedArtifactAdopter

    service, store, initial, binding = working_document
    monkeypatch.setitem(mimetypes.types_map, ".js", javascript_mime)
    published = _publish_working_bundle(store, binding)
    assert published.id != initial.revision.artifact_id
    result = await save_working_version(
        service,
        store,
        document_id=binding.document_id,
        session_key="test-key",
        session_id="test-session",
        actor_id="test-turn",
        published_ref=published,
    )
    assert result is None
    assert len(await service.list_revisions(binding.document_id)) == 1
    publications = await service.list_document_publications(session_id="test-session")
    assert publications[0].revision_id == initial.revision.revision_id
    # A later turn has no path provenance, but the exact publication remains canonical.
    adopter = GeneratedArtifactAdopter(
        service=service,
        store=store,
        session_key="test-key",
        session_id="test-session",
        workspace=binding.workspace,
    )
    await adopter(ArtifactEvent(**published.to_dict()))
    assert len(await service.list_documents(session_key="test-key")) == 1

    before_restore = await service.get_document(binding.document_id)
    audits = await service.list_audit_events(binding.document_id)
    _restored, receipt, replayed = await restore_working_revision(
        service, store, document_id=binding.document_id,
        session_key="test-key", session_id="test-session",
        target_revision_id=before_restore.head_revision_id,
        expected_head_revision_id=before_restore.head_revision_id,
        expected_state_revision=before_restore.state_revision,
        actor=Actor(kind=ActorKind.USER, actor_id="test-user"), turn_id="restore-mime-alias",
    )
    assert receipt.validation["no_op"] is True and not replayed
    assert await service.get_document(binding.document_id) == before_restore
    assert await service.list_audit_events(binding.document_id) == audits


async def test_changed_resource_media_type_is_not_deduplicated(working_document, monkeypatch):
    service, store, _initial, binding = working_document
    monkeypatch.setitem(mimetypes.types_map, ".js", "application/json")
    published = _publish_working_bundle(store, binding)
    result = await save_working_version(
        service, store, document_id=binding.document_id,
        session_key="test-key", session_id="test-session",
        actor_id="test-turn", published_ref=published,
    )
    assert result is not None
    assert len(await service.list_revisions(binding.document_id)) == 2
    assert (binding.root / "app.js").read_bytes() == b"window.ready=true"


async def test_publication_replay_recovers_link_after_commit_before_reservation(
    working_document,
    monkeypatch,
):
    service, store, _initial, binding = working_document
    (binding.root / "app.js").write_text("window.ready=false")
    published = _publish_working_bundle(store, binding)
    arguments = dict(
        document_id=binding.document_id,
        session_key="test-key",
        session_id="test-session",
        actor_id="test-turn",
    )
    original = service.reserve_document_publish_attempt

    async def interrupted(**_kwargs):
        raise RuntimeError("synthetic publication interruption")

    monkeypatch.setattr(service, "reserve_document_publish_attempt", interrupted)
    with pytest.raises(RuntimeError, match="synthetic publication interruption"):
        await save_working_version(service, store, published_ref=published, **arguments)
    committed = await service.get_document_head(binding.document_id)
    assert committed.revision.artifact_id == published.id
    assert await service.list_document_publications(session_id="test-session") == ()
    monkeypatch.setattr(service, "reserve_document_publish_attempt", original)
    (binding.root / "app.js").write_text("window.ready='later'")
    later = await save_working_version(service, store, **arguments)

    assert await save_working_version(
        service, store, published_ref=published, **arguments
    ) is None
    assert (await service.get_document_head(binding.document_id)).revision == later.revision
    publication = (await service.list_document_publications(session_id="test-session"))[0]
    assert publication.revision_id == committed.revision.revision_id
    assert len(await service.list_revisions(binding.document_id)) == 3


@pytest.mark.parametrize("invalid", ("session_id", "session_key", "metadata", "bundle"))
async def test_explicit_publication_validates_scope_metadata_and_all_bundle_bytes(
    working_document,
    invalid,
):
    from dataclasses import replace

    service, store, initial, binding = working_document
    published = _publish_working_bundle(store, binding)
    if invalid == "session_id":
        published = replace(published, session_id="other-session")
    elif invalid == "session_key":
        published = replace(published, session_key="other-key")
    elif invalid == "metadata":
        published = replace(published, name="other.html")
    else:
        resource = store.resolve_preview_resource(
            published.id, session_id="test-session", logical_path="app.js"
        )
        resource.path.write_bytes(b"corrupted resource")
    with pytest.raises(ValueError):
        await save_working_version(
            service,
            store,
            document_id=binding.document_id,
            session_key="test-key",
            session_id="test-session",
            actor_id="test-turn",
            published_ref=published,
        )
    assert (await service.get_document_head(binding.document_id)).revision == initial.revision
    assert await service.list_document_publications(session_id="test-session") == ()


async def test_generated_working_publication_uses_shared_exact_source_and_one_document(
    working_document,
):
    from opensquilla.engine.types import ArtifactEvent
    from opensquilla.gateway.generated_artifact_adoption import GeneratedArtifactAdopter

    service, store, _initial, binding = working_document
    source_paths = {}
    emitted = []

    async def emit(value):
        emitted.append(value)

    adopter = GeneratedArtifactAdopter(
        service=service,
        store=store,
        session_key="test-key",
        session_id="test-session",
        workspace=binding.workspace,
        source_paths=source_paths,
        event_emitter=emit,
    )
    (binding.root / "style.css").write_text("h1{color:crimson}")
    published = _publish_working_bundle(store, binding)
    source_paths["publication-current"] = ArtifactSource(
        str(binding.entry), "directory", str(binding.root), published.id,
    )
    event = ArtifactEvent(**published.to_dict(), publication_id="publication-current")

    await adopter(event)
    await adopter(event)

    assert len(await service.list_documents(session_key="test-key")) == 1
    assert len(await service.list_revisions(binding.document_id)) == 2
    head = await service.get_document_head(binding.document_id)
    assert head.revision.artifact_id == published.id
    assert len(emitted) == 1
    assert emitted[0]["documentId"] == binding.document_id
    assert emitted[0]["action"] == "document.published"
    assert await save_working_version(
        service,
        store,
        document_id=binding.document_id,
        session_key="test-key",
        session_id="test-session",
        actor_id="turn-done",
    ) is None


@pytest.mark.parametrize(
    "source_case", ("absent", "relative", "other-file", "other-publication", "workspace")
)
async def test_generated_publication_never_guesses_document_from_name_or_digest(
    working_document,
    tmp_path,
    source_case,
):
    from opensquilla.engine.types import ArtifactEvent
    from opensquilla.gateway.generated_artifact_adoption import GeneratedArtifactAdopter

    service, store, initial, binding = working_document
    published = _publish_working_bundle(store, binding)
    assert published.sha256 == initial.revision.artifact_sha256
    source = {
        "absent": None,
        "relative": str(binding.entry.relative_to(binding.workspace)),
        "other-file": str(tmp_path / "index.html"),
        "other-publication": str(binding.entry),
        "workspace": str(binding.entry),
    }[source_case]
    workspace = binding.workspace
    if source_case == "workspace":
        other_workspace = tmp_path / "other-workspace"
        other_workspace.mkdir()
        workspace = str(other_workspace)
    adopter = GeneratedArtifactAdopter(
        service=service,
        store=store,
        session_key="test-key",
        session_id="test-session",
        workspace=workspace,
        source_paths={"publication-current": ArtifactSource(
            source, "directory", str(binding.root), published.id,
        )} if source is not None else {},
    )

    await adopter(ArtifactEvent(**published.to_dict(), publication_id=(
        "unrelated-publication" if source_case == "other-publication" else "publication-current"
    )))

    assert len(await service.list_documents(session_key="test-key")) == 2
    assert len(await service.list_revisions(binding.document_id)) == 1
    assert (await service.get_document_head(binding.document_id)).revision == initial.revision
    assert await service.list_document_publications(session_id="test-session") == ()


@pytest.mark.parametrize("interrupt_at", ("apply", "promote"))
async def test_working_publication_reuses_normal_restart_recovery(
    working_document,
    monkeypatch,
    interrupt_at,
):
    from opensquilla.gateway.document_resource_recovery import reconcile_pending_document_resources

    service, store, _initial, binding = working_document
    (binding.root / "style.css").write_text("h1{color:purple}")
    published = _publish_working_bundle(store, binding)
    method = (
        "apply_document_publish_attempt" if interrupt_at == "apply"
        else "mark_document_publish_promoted"
    )
    original = getattr(service, method)

    async def interrupted(**_kwargs):
        raise RuntimeError("synthetic recovery interruption")

    monkeypatch.setattr(service, method, interrupted)
    with pytest.raises(RuntimeError, match="synthetic recovery interruption"):
        await save_working_version(
            service,
            store,
            document_id=binding.document_id,
            session_key="test-key",
            session_id="test-session",
            actor_id="test-turn",
            published_ref=published,
        )
    monkeypatch.setattr(service, method, original)
    committed = await service.get_document_head(binding.document_id)

    recovery = await reconcile_pending_document_resources(service, store)

    assert recovery.publishes_applied == 1
    assert recovery.promoted_deliverables == 1
    assert await service.list_document_publish_attempts_for_recovery() == ()
    publication = (await service.list_document_publications(session_id="test-session"))[0]
    assert publication.deliverable_artifact_id == published.id
    assert publication.revision_id == committed.revision.revision_id
    assert len(await service.list_revisions(binding.document_id)) == 2


async def test_explicit_publication_cas_conflict_does_not_overwrite_working_files(
    working_document,
    monkeypatch,
):
    import threading

    from opensquilla.artifact_session import ArtifactConflictError
    from opensquilla.artifact_session import working_files as module

    service, store, initial, binding = working_document
    style = binding.root / "style.css"
    style.write_text("h1{color:orange}")
    published = _publish_working_bundle(store, binding)
    original = module.load_version_bundle
    started, release = threading.Event(), threading.Event()

    def read_bundle(store, artifact_id, session_id):
        value = original(store, artifact_id, session_id)
        if artifact_id == initial.revision.artifact_id:
            started.set()
            assert release.wait(5), "test did not release immutable bundle read"
        return value

    monkeypatch.setattr(module, "load_version_bundle", read_bundle)
    saving = asyncio.create_task(save_working_version(
        service,
        store,
        document_id=binding.document_id,
        session_key="test-key",
        session_id="test-session",
        actor_id="test-turn",
        published_ref=published,
    ))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        restored = await service.restore_revision(
            document_id=binding.document_id,
            target_revision_id=initial.revision.revision_id,
            expected_head_revision_id=initial.revision.revision_id,
            expected_state_revision=initial.document.state_revision,
            actor=Actor(kind=ActorKind.USER, actor_id="test-user"),
            no_op=False,
        )
    finally:
        release.set()
    with pytest.raises(ArtifactConflictError):
        await saving
    assert (await service.get_document_head(binding.document_id)).revision == restored.revision
    assert style.read_text() == "h1{color:orange}"
    assert len(await service.list_revisions(binding.document_id)) == 1
    assert await service.list_document_publications(session_id="test-session") == ()


async def test_generated_source_path_does_not_match_another_sessions_working_document(
    working_document,
):
    from opensquilla.engine.types import ArtifactEvent
    from opensquilla.gateway.generated_artifact_adoption import GeneratedArtifactAdopter

    service, store, initial, binding = working_document
    published = _publish_working_bundle(
        store, binding, session_key="other-key", session_id="other-session"
    )
    adopter = GeneratedArtifactAdopter(
        service=service,
        store=store,
        session_key="other-key",
        session_id="other-session",
        workspace=binding.workspace,
        source_paths={"publication-current": ArtifactSource(
            str(binding.entry), "directory", str(binding.root), published.id,
        )},
    )

    await adopter(ArtifactEvent(**published.to_dict(), publication_id="publication-current"))

    assert (await service.get_document_head(binding.document_id)).revision == initial.revision
    assert len(await service.list_documents(session_key="other-key")) == 1
    assert await service.list_document_publications(session_id="test-session") == ()
    publications = await service.list_document_publications(session_id="other-session")
    assert len(publications) == 1
    assert publications[0].document_id != binding.document_id


async def test_restore_same_head_recovers_original_source_collection_mode(working_document):
    import json

    from opensquilla.artifact_session.working_files import (
        get_working_files,
        restore_working_revision,
    )
    from opensquilla.engine.types import ArtifactEvent
    from opensquilla.gateway.generated_artifact_adoption import GeneratedArtifactAdopter
    from opensquilla.tools.builtin.artifacts import publish_artifact
    from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context

    service, store, initial, managed = working_document
    site = Path(managed.workspace) / "site"
    site.mkdir()
    source = site / "index.html"
    source.write_text('<link rel="stylesheet" href="style.css"><h1>Source project</h1>')
    (site / "style.css").write_text("h1{color:navy}")
    context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=managed.workspace,
        artifact_media_root=str(Path(managed.workspace).parent / "media"),
        artifact_session_id="test-session",
        session_key="test-key",
    )
    adopter = GeneratedArtifactAdopter(
        service=service, store=store, session_key="test-key", session_id="test-session",
        workspace=managed.workspace, source_paths=context.artifact_source_paths,
    )
    context.generated_artifact_adopter = adopter
    token = current_tool_context.set(context)
    try:
        first = json.loads(await publish_artifact(path="site/index.html"))
        assert "artifact" in first, first
        await adopter(ArtifactEvent(**next(
            value for value in reversed(context.published_artifacts)
            if value["id"] == first["artifact"]["id"]
        )))
        documents = await service.list_documents(session_key="test-key", session_id="test-session")
        document = next(
            item for item in documents if item.document_id != initial.document.document_id
        )
        original = await get_working_files(service, document.document_id)
        assert original is not None and original.entry == source
        assert original.bundle_mode == "auto"
        repeated = json.loads(await publish_artifact(
            path="site/index.html", bundle="directory", bundle_root="site",
        ))
        assert "artifact" in repeated, repeated
        await adopter(ArtifactEvent(**next(
            value for value in reversed(context.published_artifacts)
            if value["id"] == repeated["artifact"]["id"]
        )))
    finally:
        current_tool_context.reset(token)
    assert repeated["artifact"]["id"] == first["artifact"]["id"]
    current = await get_working_files(service, document.document_id)
    assert current.bundle_mode == "directory"
    assert current.base_revision_id == original.base_revision_id
    assert current.digest() == original.digest()
    before = await service.get_document(document.document_id)
    result, receipt, replayed = await restore_working_revision(
        service, store, document_id=document.document_id, session_key="test-key",
        session_id="test-session", target_revision_id=original.base_revision_id,
        expected_head_revision_id=before.head_revision_id,
        expected_state_revision=before.state_revision,
        actor=Actor(ActorKind.USER, "test-user"), turn_id="revision-restore:original-source-mode",
    )
    assert not replayed
    assert receipt.validation["no_op"] is False
    assert result.revision.revision_id == original.base_revision_id
    assert result.document.generation == before.generation
    assert result.document.state_revision == before.state_revision + 1
    restored = await get_working_files(service, document.document_id)
    assert restored.bundle_mode == "auto"
    assert restored.bundle_root == original.bundle_root
    assert restored.digest() == original.digest()
    assert len(await service.list_revisions(document.document_id)) == 1
    (site / "new-neighbor.txt").write_text("Unrelated future neighbor")
    assert "new-neighbor.txt" not in {item.path for item in restored.bundle().files}
    assert await save_working_version(
        service, store, document_id=document.document_id, session_key="test-key",
        session_id="test-session", actor_id="explanation-only-after-restore",
    ) is None
    assert (site / "new-neighbor.txt").read_text() == "Unrelated future neighbor"
