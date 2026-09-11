from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from yoyo import get_backend, read_migrations

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    ArtifactSessionService,
)
from opensquilla.artifact_session.working_files import (
    ensure_working_files,
    get_working_files,
    save_working_version,
)
from opensquilla.artifacts import ArtifactNotFoundError, ArtifactStore
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import ArtifactEvent
from opensquilla.gateway.artifact_preview import ArtifactPreviewLeaseService
from opensquilla.gateway.config import AttachmentsConfig, GatewayConfig
from opensquilla.gateway.generated_artifact_adoption import GeneratedArtifactAdopter
from opensquilla.tools.builtin.artifacts import publish_artifact
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context

SESSION_KEY = "agent:main:webchat:source-continuity"
SESSION_ID = "source-continuity"


def _turn(service, store, workspace, media_root, preview):
    context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(workspace),
        artifact_media_root=str(media_root),
        artifact_session_id=SESSION_ID,
        session_key=SESSION_KEY,
    )
    adopter = GeneratedArtifactAdopter(
        service=service,
        store=store,
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        workspace=str(workspace),
        source_paths=context.artifact_source_paths,
        preview_service=preview,
    )
    context.generated_artifact_adopter = adopter
    return context, adopter


async def _publish(context, adopter, **arguments):
    token = current_tool_context.set(context)
    try:
        result = json.loads(await publish_artifact(**({"path": "index.html"} | arguments)))
    finally:
        current_tool_context.reset(token)
    payload = next(
        value for value in reversed(context.published_artifacts)
        if value["id"] == result["artifact"]["id"]
    )
    await adopter(ArtifactEvent(**payload))
    return result


async def test_runtime_turn_context_keeps_adopter_bound_to_its_publication_sources(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "index.html"
    source.write_text("<!doctype html><h1>First</h1>")
    media_root = tmp_path / "media"
    store = ArtifactStore(media_root)
    service = await ArtifactSessionService.open(tmp_path / "state.db")
    try:
        base, base_adopter = _turn(service, store, workspace, media_root, None)
        runner = TurnRunner(
            provider_selector=None,
            config=GatewayConfig(attachments=AttachmentsConfig(media_root=str(media_root))),
        )
        first = await runner._with_artifact_context(base, SESSION_KEY)
        first_adopter = first.generated_artifact_adopter
        assert isinstance(first_adopter, GeneratedArtifactAdopter)
        assert first_adopter is not base_adopter
        assert first_adopter.source_paths is first.artifact_source_paths
        first_result = await _publish(first, first_adopter, name="Friendly report.html")

        source.write_text("<!doctype html><h1>Updated</h1>")
        second = await runner._with_artifact_context(base, SESSION_KEY)
        second_adopter = second.generated_artifact_adopter
        assert isinstance(second_adopter, GeneratedArtifactAdopter)
        assert second_adopter.source_paths is second.artifact_source_paths
        assert second_adopter.source_paths is not first_adopter.source_paths
        second_result = await _publish(second, second_adopter, name="Updated report.html")

        documents = await service.list_documents(session_key=SESSION_KEY, session_id=SESSION_ID)
        assert len(documents) == 1
        binding = await get_working_files(service, documents[0].document_id)
        assert binding is not None and binding.entry == source
        head = await service.get_document_head(documents[0].document_id)
        assert head.revision.artifact_id == second_result["artifact"]["id"]
        assert head.revision.artifact_id != first_result["artifact"]["id"]
        assert len(await service.list_revisions(documents[0].document_id)) == 2
        assert len(first.published_artifacts) == len(second.published_artifacts) == 1
        assert base.published_artifacts == []
        assert base.artifact_source_paths == {}
        assert base_adopter.source_paths is base.artifact_source_paths
    finally:
        await service.close()


@pytest.mark.parametrize("stage", ["initial-source", "live-preview", "same-turn", "next-turn"])
async def test_generated_source_remains_the_working_truth(tmp_path: Path, stage: str):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "index.html"
    source.write_text('<!doctype html><link rel="stylesheet" href="style.css"><h1>First</h1>')
    stylesheet = workspace / "style.css"
    stylesheet.write_text("h1{color:navy}")
    media_root = tmp_path / "media"
    store = ArtifactStore(media_root)
    database = tmp_path / "state.db"
    service = await ArtifactSessionService.open(database)
    preview = ArtifactPreviewLeaseService(
        config=GatewayConfig(attachments=AttachmentsConfig(media_root=str(media_root)))
    )
    try:
        context, adopter = _turn(service, store, workspace, media_root, preview)
        first = await _publish(context, adopter)
        documents = await service.list_documents(session_key=SESSION_KEY, session_id=SESSION_ID)
        assert len(documents) == 1
        document_id = documents[0].document_id
        binding = await get_working_files(service, document_id)
        assert binding is not None
        assert first["artifact"]["local_path"] == str(source)
        lease, _token = preview.create(
            artifact_id=first["artifact"]["id"],
            session_id=SESSION_ID,
            session_key=SESSION_KEY,
            mode="offline",
            client="desktop",
        )

        if stage == "initial-source":
            assert binding.entry == source
            return

        stylesheet.write_text("h1{color:crimson}")
        if stage == "live-preview":
            assert preview.resolve_resource(lease, "style.css").data == b"h1{color:crimson}"
            return

        if stage == "next-turn":
            await service.close()
            service = await ArtifactSessionService.open(database)
            context, adopter = _turn(service, store, workspace, media_root, preview)
        second = await _publish(context, adopter)
        assert second["artifact"]["id"] != first["artifact"]["id"]
        documents = await service.list_documents(session_key=SESSION_KEY, session_id=SESSION_ID)
        assert [document.document_id for document in documents] == [document_id]
        head = await service.get_document_head(document_id)
        assert head.revision.artifact_id == second["artifact"]["id"]
        assert len(await service.list_revisions(document_id)) == 2
        assert preview.resolve_resource(lease, "style.css").data == b"h1{color:crimson}"
        await _publish(context, adopter)
        assert len(await service.list_revisions(document_id)) == 2
        original = store.resolve_preview_resource(
            first["artifact"]["id"], session_id=SESSION_ID, logical_path="style.css"
        )
        assert original.path.read_bytes() == b"h1{color:navy}"
    finally:
        await service.close()


async def test_import_without_trusted_source_keeps_an_independent_working_copy(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    unrelated = workspace / "index.html"
    unrelated.write_text("<!doctype html><h1>Unrelated user file</h1>")
    store = ArtifactStore(tmp_path / "media")
    imported = store.publish_bytes(
        b"<!doctype html><h1>Imported document</h1>",
        session_id=SESSION_ID,
        session_key=SESSION_KEY,
        name="index.html",
        mime="text/html",
        source="import",
    )
    service = await ArtifactSessionService.open(tmp_path / "state.db")
    try:
        adopter = GeneratedArtifactAdopter(
            service=service, store=store, session_key=SESSION_KEY,
            session_id=SESSION_ID, workspace=str(workspace),
        )
        await adopter(ArtifactEvent(**imported.to_dict()))
        await adopter(ArtifactEvent(**imported.to_dict()))
        documents = await service.list_documents(session_key=SESSION_KEY, session_id=SESSION_ID)
        assert len(documents) == 1
        binding = await get_working_files(service, documents[0].document_id)
        assert binding is not None and binding.entry != unrelated
        assert binding.entry.read_bytes() == b"<!doctype html><h1>Imported document</h1>"
        assert unrelated.read_text() == "<!doctype html><h1>Unrelated user file</h1>"
    finally:
        await service.close()


@pytest.fixture
async def source_environment(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "index.html"
    source.write_text('<!doctype html><link rel="stylesheet" href="style.css"><h1>First</h1>')
    (workspace / "style.css").write_text("h1{color:navy}")
    media = tmp_path / "media"
    store = ArtifactStore(media)
    database = tmp_path / "state.db"
    service = await ArtifactSessionService.open(database)
    preview = ArtifactPreviewLeaseService(
        config=GatewayConfig(attachments=AttachmentsConfig(media_root=str(media)))
    )
    context, adopter = _turn(service, store, workspace, media, preview)
    env = SimpleNamespace(
        workspace=workspace, source=source, style=workspace / "style.css",
        media=media, store=store, database=database,
        service=service, preview=preview, context=context, adopter=adopter,
    )
    try:
        yield env
    finally:
        await env.service.close()


async def _binding(env):
    documents = await env.service.list_documents(session_key=SESSION_KEY, session_id=SESSION_ID)
    assert len(documents) == 1
    binding = await get_working_files(env.service, documents[0].document_id)
    assert binding is not None
    return binding


async def _save(env, binding):
    return await save_working_version(
        env.service, env.store, document_id=binding.document_id,
        session_key=SESSION_KEY, session_id=SESSION_ID, actor_id="completed-turn",
    )


async def _restore(env, binding, revision):
    from opensquilla.artifact_session.working_files import restore_working_revision

    head = await env.service.get_document_head(binding.document_id)
    await restore_working_revision(
        env.service, env.store,
        document_id=binding.document_id, target_revision_id=revision,
        session_key=SESSION_KEY, session_id=SESSION_ID,
        expected_head_revision_id=head.revision.revision_id,
        expected_state_revision=head.document.state_revision,
        actor=Actor(kind=ActorKind.USER, actor_id="test-user"),
        turn_id=f"source-restore:{revision}:{head.document.state_revision}",
    )


async def _reconcile(env, binding):
    return await ensure_working_files(
        env.service, env.store, document_id=binding.document_id,
        session_key=SESSION_KEY, session_id=SESSION_ID, workspace=str(env.workspace),
    )


async def test_renamed_single_file_maps_virtual_entry_without_spurious_versions(source_environment):
    env = source_environment
    env.source.write_text("<!doctype html><h1>First</h1>")
    first = await _publish(env.context, env.adopter, name="published.html", bundle="none")
    binding = await _binding(env)
    original = await env.service.get_document_head(binding.document_id)
    assert binding.entry == env.source
    assert binding.entrypoint == "published.html"
    assert [file.path for file in binding.bundle().files] == ["published.html"]
    assert await _save(env, binding) is None
    await env.service.close()
    env.service = await ArtifactSessionService.open(env.database)
    env.context, env.adopter = _turn(
        env.service, env.store, env.workspace, env.media, env.preview,
    )
    binding = await _binding(env)
    assert await _save(env, binding) is None
    env.source.write_text("<!doctype html><h1>Second</h1>")
    await _publish(env.context, env.adopter, name="another-name.html", bundle="none")
    binding = await _binding(env)
    assert binding.entrypoint == "another-name.html"
    assert await _save(env, binding) is None
    await _restore(env, binding, original.revision.revision_id)
    restored = await _reconcile(env, binding)
    assert env.source.read_text() == "<!doctype html><h1>First</h1>"
    assert restored.entrypoint == "published.html"
    assert await _save(env, restored) is None
    assert (env.workspace / "style.css").read_text() == "h1{color:navy}"
    assert len(await env.service.list_revisions(binding.document_id)) == 2
    assert first["artifact"]["local_path"] == str(env.source)


async def test_auto_source_preview_and_snapshot_do_not_collect_neighbor_files(source_environment):
    env = source_environment
    (env.workspace / "neighbor.txt").write_text("not part of the page")
    (env.workspace / ".env").write_text("SYNTHETIC_NEIGHBOR=kept")
    first = await _publish(env.context, env.adopter)
    binding = await _binding(env)
    assert {file.path for file in binding.bundle().files} == {"index.html", "style.css"}
    lease, _ = env.preview.create(
        artifact_id=first["artifact"]["id"], session_id=SESSION_ID,
        session_key=SESSION_KEY, mode="offline", client="desktop",
    )
    with pytest.raises(ArtifactNotFoundError):
        env.preview.resolve_resource(lease, "neighbor.txt")
    env.source.write_text(env.source.read_text() + '<script src="added.js"></script>')
    (env.workspace / "added.js").write_text("window.added=true")
    saved = await _save(env, binding)
    assert saved is not None
    manifest = env.store.validate_preview_bundle(saved.revision.artifact_id, session_id=SESSION_ID)
    assert manifest is not None
    assert {file.path for file in manifest.files} == {"index.html", "style.css", "added.js"}


@pytest.mark.parametrize("mode", ["auto", "directory"])
async def test_source_restore_preserves_neighbors_and_untracked_files(source_environment, mode):
    env = source_environment
    if mode == "directory":
        site = env.workspace / "site"
        site.mkdir()
        env.source.rename(site / "index.html")
        (env.workspace / "style.css").rename(site / "style.css")
        env.source = site / "index.html"
        options = {"path": "site/index.html", "bundle": "directory", "bundle_root": "site"}
    else:
        site, options = env.workspace, {}
    (env.workspace / "neighbor.txt").write_text("preserve neighbor")
    (env.workspace / ".git").mkdir()
    (env.workspace / ".git" / "config").write_text("synthetic metadata")
    await _publish(env.context, env.adopter, **options)
    binding = await _binding(env)
    original = await env.service.get_document_head(binding.document_id)
    env.source.write_text(env.source.read_text() + '<script src="obsolete.js"></script>')
    (site / "obsolete.js").write_text("window.old=true")
    (site / "style.css").write_text("h1{color:crimson}")
    await _publish(env.context, env.adopter, **options)
    binding = await _binding(env)
    (site / "style.css").write_text("h1{color:orange}")
    (site / "untracked.txt").write_text("unsaved neighboring work")
    await _restore(env, binding, original.revision.revision_id)
    restored = await _reconcile(env, binding)
    assert restored.entry == env.source
    assert (site / "style.css").read_text() == "h1{color:navy}"
    assert not (site / "obsolete.js").exists()
    assert (site / "untracked.txt").read_text() == "unsaved neighboring work"
    assert (env.workspace / "neighbor.txt").read_text() == "preserve neighbor"
    assert (env.workspace / ".git" / "config").read_text() == "synthetic metadata"
    backups = list((env.workspace / "artifacts").glob("recovered-*/files/**/style.css"))
    assert any(path.read_text() == "h1{color:orange}" for path in backups)
    assert await _reconcile(env, restored) == restored


async def test_partial_source_restore_retains_bytes_and_requires_reconciliation(
    source_environment, monkeypatch,
):
    from opensquilla.artifact_session import working_files as module

    env = source_environment
    await _publish(env.context, env.adopter)
    binding = await _binding(env)
    initial = await env.service.get_document_head(binding.document_id)
    env.source.write_text("<!doctype html><h1>Unpublished user text</h1>")
    (env.workspace / "style.css").write_text("h1{color:orange}")
    original_replace = module.os.replace
    fail = True

    def replace(source, destination):
        nonlocal fail
        if Path(destination).name == "style.css" and fail:
            fail = False
            raise OSError("synthetic source restore interruption")
        return original_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", replace)
    with pytest.raises(OSError, match="synthetic source restore interruption"):
        await _restore(env, binding, initial.revision.revision_id)
    assert (await _binding(env)).base_revision_id == binding.base_revision_id
    assert env.source.read_text() == "<!doctype html><h1>Unpublished user text</h1>"
    assert (env.workspace / "style.css").read_text() == "h1{color:orange}"
    assert (await env.service.get_document_head(binding.document_id)).document == initial.document
    backups = list((env.workspace / "artifacts").glob("recovered-*/files/index.html"))
    assert any("Unpublished user text" in path.read_text() for path in backups)
    await _restore(env, binding, initial.revision.revision_id)
    restored = await _reconcile(env, binding)
    assert restored.base_revision_id == binding.base_revision_id
    assert (env.workspace / "style.css").read_text() == "h1{color:navy}"
    assert await _save(env, restored) is None


async def test_cancelled_source_restore_waits_for_worker_and_preserves_recovery(
    source_environment, monkeypatch,
):
    from opensquilla.artifact_session import working_files as module

    env = source_environment
    await _publish(env.context, env.adopter)
    binding = await _binding(env)
    initial = await env.service.get_document_head(binding.document_id)
    env.source.write_text("<!doctype html><h1>Recover this edit</h1>")
    entered, release = threading.Event(), threading.Event()
    original_replace = module.os.replace
    blocked = False

    def replace(source, destination):
        nonlocal blocked
        result = original_replace(source, destination)
        if Path(destination) == env.source and not blocked:
            blocked = True
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(module.os, "replace", replace)
    task = asyncio.create_task(_restore(env, binding, initial.revision.revision_id))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        second = asyncio.create_task(_restore(env, binding, initial.revision.revision_id))
        await asyncio.sleep(0)
        assert not task.done() and not second.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await second
    restored = await _reconcile(env, binding)
    assert await _save(env, restored) is None
    backups = list((env.workspace / "artifacts").glob("recovered-*/files/index.html"))
    assert any("Recover this edit" in path.read_text() for path in backups)


@pytest.mark.parametrize("changed", [False, True])
async def test_concurrent_first_source_publications_create_one_document(
    source_environment, changed,
):
    env = source_environment
    contexts, adopters, events = [], [], []
    for index in range(2):
        context, adopter = _turn(env.service, env.store, env.workspace, env.media, env.preview)
        if index and changed:
            (env.workspace / "style.css").write_text("h1{color:crimson}")
        token = current_tool_context.set(context)
        try:
            await publish_artifact(path="index.html")
        finally:
            current_tool_context.reset(token)
        contexts.append(context)
        adopters.append(adopter)
        events.append(ArtifactEvent(**context.published_artifacts[0]))
    await asyncio.gather(*(adopter(event) for adopter, event in zip(adopters, events)))
    binding = await _binding(env)
    assert binding.entry == env.source
    assert len(await env.service.list_revisions(binding.document_id)) == (2 if changed else 1)
    async with env.service.repository._read_transaction("test.source_count") as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM artifact_working_sources")
        assert (await cursor.fetchone())[0] == 1


async def test_source_identity_is_scoped_to_session_and_workspace(source_environment):
    env = source_environment
    await _publish(env.context, env.adopter)
    first = await _binding(env)
    original = await env.service.get_document_head(first.document_id)
    (env.workspace / "style.css").write_text("h1{color:crimson}")
    context, adopter = _turn(env.service, env.store, env.workspace, env.media, env.preview)
    context.session_key = adopter.session_key = "agent:main:webchat:other"
    context.artifact_session_id = adopter.session_id = "other-session"
    await _publish(context, adopter)
    other = await env.service.list_documents(session_key=adopter.session_key)
    assert len(other) == 1 and other[0].document_id != first.document_id
    assert (await env.service.get_document_head(first.document_id)).revision == original.revision


async def test_source_links_fail_closed_after_publication(source_environment):
    env = source_environment
    await _publish(env.context, env.adopter)
    binding = await _binding(env)
    env.source.unlink()
    private = env.workspace.parent / "outside.html"
    private.write_text("private source")
    try:
        env.source.symlink_to(private)
    except OSError:
        pytest.skip("This host cannot create symbolic links")
    with pytest.raises(ValueError):
        binding.bundle()
    with pytest.raises(ValueError):
        await _save(env, binding)


@pytest.mark.parametrize("entrypoint", ["runtime", "migration"])
async def test_existing_five_column_working_table_upgrades_without_fabricated_sources(
    tmp_path, entrypoint,
):
    database = tmp_path / "state.db"
    service = await ArtifactSessionService.open(database)
    await service.close()
    with sqlite3.connect(database) as conn:
        conn.execute("DROP TABLE artifact_working_sources")
        assert len(conn.execute("PRAGMA table_info(artifact_working_files)").fetchall()) == 5
    if entrypoint == "migration":
        backend = get_backend(f"sqlite:///{database}")
        try:
            migration = next(item for item in read_migrations(
                str(Path(__file__).resolve().parents[2] / "migrations")
            ) if item.id == "V041__retire_html_editor")
            migration.load()
            with sqlite3.connect(database) as conn:
                migration.module.apply_step(conn)
                migration.module.apply_step(conn)
        finally:
            backend.connection.close()
    service = await ArtifactSessionService.open(database)
    await service.close()
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM artifact_working_sources").fetchone() == (0,)


@pytest.mark.parametrize("transition", ["renamed-none", "root-to-nested", "nested-to-root"])
async def test_retained_preview_lease_tracks_source_layout_without_other_resource_fallback(
    source_environment, transition,
):
    env = source_environment
    if transition == "renamed-none":
        env.source.write_text("<!doctype html><h1>First</h1>")
        before = {"name": "first.html", "bundle": "none"}
        after = {"name": "second.html", "bundle": "none"}
        initial_entry, initial_style = "first.html", None
    else:
        site = env.workspace / "site"
        nested = site / "nested"
        nested.mkdir(parents=True)
        env.source.rename(nested / "index.html")
        (env.workspace / "style.css").rename(nested / "style.css")
        env.source = nested / "index.html"
        auto = {"path": "site/nested/index.html"}
        directory = {"path": "site/nested/index.html", "bundle": "directory", "bundle_root": "site"}
        before, after = (auto, directory) if transition == "root-to-nested" else (directory, auto)
        initial_entry = "index.html" if before == auto else "nested/index.html"
        initial_style = "style.css" if before == auto else "nested/style.css"
    first = await _publish(env.context, env.adopter, **before)
    lease, token = env.preview.create(
        artifact_id=first["artifact"]["id"], session_id=SESSION_ID,
        session_key=SESSION_KEY, mode="offline", client="desktop",
    )
    source_text = env.source.read_text().replace("First", "Second")
    env.source.write_text(source_text)
    await _publish(env.context, env.adopter, **after)
    assert env.preview.resolve_token(token) is lease
    assert lease.entrypoint == initial_entry
    resource = env.preview.resolve_resource(lease, initial_entry)
    assert resource.data == source_text.encode()
    assert resource.path == env.source
    if initial_style:
        assert env.preview.resolve_resource(lease, initial_style).data == b"h1{color:navy}"
    with pytest.raises(ArtifactNotFoundError):
        env.preview.resolve_resource(lease, "missing.txt")
    with pytest.raises(ArtifactNotFoundError):
        env.preview.resolve_resource(lease, "../neighbor.txt")


async def test_directory_source_recovery_stays_outside_collection(source_environment):
    env = source_environment
    site = env.workspace / "artifacts"
    site.mkdir()
    env.source.rename(site / "index.html")
    env.style.rename(site / "style.css")
    env.source = site / "index.html"
    env.style = site / "style.css"
    await _publish(env.context, env.adopter, path="artifacts/index.html",
                   bundle="directory", bundle_root="artifacts")
    binding = await _binding(env)
    original = binding.base_revision_id
    env.source.write_text(env.source.read_text().replace("First", "Second"))
    await _save(env, binding)
    unpublished = env.source.read_text().replace("Second", "Unpublished")
    env.source.write_text(unpublished)
    await _restore(env, binding, original)
    restored = await _reconcile(env, binding)
    assert "First" in env.source.read_text()
    assert {item.path for item in restored.bundle().files} == {"index.html", "style.css"}
    assert await _save(env, restored) is None
    backups = list(env.workspace.glob("artifact-recovery-*/recovered-*/files/artifacts/index.html"))
    assert len(backups) == 1 and backups[0].read_text() == unpublished
    assert not list(site.glob("recovered-*"))


@pytest.mark.parametrize("before,after", [
    ("auto", "directory"), ("directory", "auto"),
    ("none", "directory"), ("directory", "none"),
    ("none", "auto"), ("auto", "none"),
])
async def test_restore_recovers_exact_version_collection_settings(
    source_environment, before, after,
):
    env = source_environment
    site = env.workspace / "site"
    nested = site / "nested"
    nested.mkdir(parents=True)
    env.source.rename(nested / "index.html")
    env.style.rename(nested / "style.css")
    env.source = nested / "index.html"
    env.style = nested / "style.css"
    (site / "owned.txt").write_text("directory resource")

    def arguments(mode):
        result = {"path": "site/nested/index.html", "bundle": mode}
        if mode == "directory":
            result["bundle_root"] = "site"
        if mode == "none":
            result["name"] = "published.html"
        return result

    await _publish(env.context, env.adopter, **arguments(before))
    initial = await _binding(env)
    initial_digest = initial.digest()
    env.source.write_text(env.source.read_text().replace("First", "Second"))
    await _publish(env.context, env.adopter, **arguments(after))
    assert (await _binding(env)).bundle_mode == after
    await _restore(env, initial, initial.base_revision_id)
    restored = await _reconcile(env, initial)
    assert restored.bundle_mode == before
    assert restored.relative_root == initial.relative_root
    assert restored.entrypoint == initial.entrypoint
    assert restored.bundle_root == initial.bundle_root
    assert restored.digest() == initial_digest
    assert await _save(env, restored) is None
    # Restoring the restore revision must keep the same host collection boundary.
    await _restore(env, restored, restored.base_revision_id)
    twice = await _reconcile(env, restored)
    assert twice.bundle_mode == before and twice.digest() == initial_digest
    assert await _save(env, twice) is None


async def test_mode_only_publication_preserves_first_revision_provenance(source_environment):
    env = source_environment
    site = env.workspace / "site"
    site.mkdir()
    env.source.rename(site / "index.html")
    env.style.rename(site / "style.css")
    env.source = site / "index.html"
    env.style = site / "style.css"
    first = await _publish(env.context, env.adopter, path="site/index.html")
    initial = await _binding(env)
    replay = await _publish(env.context, env.adopter, path="site/index.html",
                            bundle="directory", bundle_root="site")
    current = await _binding(env)
    assert replay["artifact"]["id"] == first["artifact"]["id"]
    assert current.base_revision_id == initial.base_revision_id
    assert current.bundle_mode == "directory"
    assert await _save(env, current) is None
    with sqlite3.connect(env.database) as conn:
        row = conn.execute(
            "SELECT bundle_mode FROM artifact_working_source_versions WHERE revision_id=?",
            (initial.base_revision_id,),
        ).fetchone()
        assert row == ("auto",)
    (site / "additional.txt").write_text("new directory member")
    changed = await _save(env, current)
    assert changed is not None
    with sqlite3.connect(env.database) as conn:
        row = conn.execute(
            "SELECT bundle_mode FROM artifact_working_source_versions WHERE revision_id=?",
            (changed.revision.revision_id,),
        ).fetchone()
        assert row == ("directory",)
    await _restore(env, current, initial.base_revision_id)
    restored = await _reconcile(env, current)
    assert restored.bundle_mode == "auto"
    assert not (site / "additional.txt").exists()
    assert await _save(env, restored) is None


@pytest.mark.parametrize("entrypoint", ["runtime", "migration"])
async def test_prototype_source_upgrade_records_only_known_current_revision(
    source_environment, entrypoint,
):
    env = source_environment
    await _publish(env.context, env.adopter)
    first = await _binding(env)
    env.source.write_text(env.source.read_text().replace("First", "Second"))
    changed = await _save(env, first)
    assert changed is not None
    await env.service.close()
    with sqlite3.connect(env.database) as conn:
        conn.execute("DROP TABLE artifact_working_source_versions")
    if entrypoint == "migration":
        backend = get_backend(f"sqlite:///{env.database}")
        try:
            migration = next(item for item in read_migrations(
                str(Path(__file__).resolve().parents[2] / "migrations")
            ) if item.id == "V041__retire_html_editor")
            migration.load()
            with sqlite3.connect(env.database) as conn:
                migration.module.apply_step(conn)
                migration.module.apply_step(conn)
        finally:
            backend.connection.close()
    env.service = await ArtifactSessionService.open(env.database)
    await env.service.repository.initialize()
    with sqlite3.connect(env.database) as conn:
        rows = conn.execute(
            "SELECT revision_id, bundle_mode FROM artifact_working_source_versions",
        ).fetchall()
        assert rows == [(changed.revision.revision_id, "auto")]


async def _dispatch_publish(env, path="index.html", **arguments):
    from opensquilla.engine.agent import _artifact_event_kwargs
    from opensquilla.engine.types import ToolCall
    from opensquilla.tools.dispatch import build_tool_handler
    from opensquilla.tools.registry import ToolRegistry
    from opensquilla.tools.types import ToolSpec

    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="publish_artifact", description="Publish a generated file.",
        parameters={"path": {"type": "string"}, "bundle": {"type": "string"},
                    "bundle_root": {"type": "string"}}, required=["path"],
    ), publish_artifact)
    result = await build_tool_handler(registry, env.context)(ToolCall(
        tool_use_id=f"publish-{len(env.context.published_artifacts)}",
        tool_name="publish_artifact", arguments={"path": path, **arguments},
    ))
    assert not result.is_error, result.content
    events = [ArtifactEvent(**_artifact_event_kwargs(item)) for item in result.artifacts]
    for event in events:
        await env.adopter(event)
    return result, events


async def test_real_dispatch_republishes_prior_content_before_cancel(source_environment):
    env = source_environment
    original = env.source.read_bytes()
    first, initial_events = await _dispatch_publish(env)
    assert len(initial_events) == 1
    initial = await _binding(env)
    env.source.write_text(original.decode().replace("First", "Second"))
    await _dispatch_publish(env)
    second = await _binding(env)
    assert second.base_revision_id != initial.base_revision_id
    env.source.write_bytes(original)
    last, last_events = await _dispatch_publish(env)
    assert len(last_events) == 1
    assert json.loads(last.content)["artifact"]["id"] == json.loads(first.content)["artifact"]["id"]
    current = await _binding(env)
    assert current.base_revision_id not in {initial.base_revision_id, second.base_revision_id}
    head = await env.service.get_document_head(current.document_id)
    assert env.store.resolve_preview_resource(
        head.revision.artifact_id, session_id=SESSION_ID,
    ).path.read_bytes() == original
    # Cancellation skips automatic turn snapshots: the explicit publication is already durable.
    await env.adopter(last_events[0])
    await env.adopter(initial_events[0])
    assert (await _binding(env)).base_revision_id == current.base_revision_id
    assert await _save(env, current) is None


async def test_real_dispatch_identical_html_sources_remain_distinct_across_turns(
    source_environment,
):
    env = source_environment
    duplicate = env.workspace / "copy.html"
    duplicate.write_bytes(env.source.read_bytes())
    first, first_events = await _dispatch_publish(env)
    second, second_events = await _dispatch_publish(env, path="copy.html")
    first_id = json.loads(first.content)["artifact"]["id"]
    second_id = json.loads(second.content)["artifact"]["id"]
    assert first_id != second_id
    assert len(first_events) == len(second_events) == 1
    documents = await env.service.list_documents(session_key=SESSION_KEY, session_id=SESSION_ID)
    assert len(documents) == 2
    bindings = {str(item.entry): item for document in documents
                if (item := await get_working_files(env.service, document.document_id)) is not None}
    assert set(bindings) == {str(env.source), str(duplicate)}
    assert bindings[str(env.source)].document_id != bindings[str(duplicate)].document_id
    first_revision = bindings[str(env.source)].base_revision_id
    second_revision = bindings[str(duplicate)].base_revision_id
    await env.service.close()
    env.service = await ArtifactSessionService.open(env.database)
    env.context, env.adopter = _turn(env.service, env.store, env.workspace, env.media, env.preview)
    same_first, events = await _dispatch_publish(env)
    assert len(events) == 1
    assert json.loads(same_first.content)["artifact"]["id"] == first_id
    same_second, events = await _dispatch_publish(env, path="copy.html")
    assert len(events) == 1
    assert json.loads(same_second.content)["artifact"]["id"] == second_id
    head = await env.service.get_document_head(bindings[str(env.source)].document_id)
    assert head.revision.revision_id == first_revision
    head = await env.service.get_document_head(bindings[str(duplicate)].document_id)
    assert head.revision.revision_id == second_revision
    assert "publication_id" not in first.content
    from opensquilla.artifacts import artifact_payload
    assert "publication_id" not in artifact_payload(first_events[0])


async def test_mode_only_event_replay_does_not_change_current_source_settings(source_environment):
    env = source_environment
    site = env.workspace / "site"
    site.mkdir()
    env.source.rename(site / "index.html")
    env.style.rename(site / "style.css")
    env.source = site / "index.html"
    _, first_events = await _dispatch_publish(env, path="site/index.html")
    _, directory_events = await _dispatch_publish(
        env, path="site/index.html", bundle="directory", bundle_root="site",
    )
    assert len(directory_events) == 1
    current = await _binding(env)
    assert current.bundle_mode == "directory"
    await env.adopter(first_events[0])
    assert (await _binding(env)).bundle_mode == "directory"
    assert (await _binding(env)).base_revision_id == current.base_revision_id


async def test_publication_commit_receipt_resumes_after_restart_before_journal_reservation(
    source_environment, monkeypatch,
):
    from opensquilla.engine.agent import _artifact_event_kwargs

    env = source_environment
    await _dispatch_publish(env)
    initial = await _binding(env)
    env.source.write_text(env.source.read_text().replace("First", "Second"))

    async def interrupted(**_kwargs):
        raise OSError("synthetic interruption before publication reservation")

    monkeypatch.setattr(env.service, "reserve_document_publish_attempt", interrupted)
    with pytest.raises(OSError, match="synthetic interruption"):
        await _dispatch_publish(env)
    saved = await _binding(env)
    assert saved.base_revision_id != initial.base_revision_id
    event = ArtifactEvent(**_artifact_event_kwargs(env.context.published_artifacts[-1]))
    await env.service.close()
    env.service = await ArtifactSessionService.open(env.database)
    env.context, env.adopter = _turn(env.service, env.store, env.workspace, env.media, env.preview)
    assert not env.adopter.source_paths
    await env.adopter(event)
    await env.adopter(event)
    assert (await _binding(env)).base_revision_id == saved.base_revision_id
    publications = await env.service.list_document_publications(session_id=SESSION_ID)
    assert len(publications) == 2
    assert {item.document_id for item in publications} == {saved.document_id}
    assert await _save(env, saved) is None


@pytest.mark.parametrize("name,mime,mode", [
    ("index.xhtml", "application/xhtml+xml", "none"),
    ("source.data", "text/html", "auto"),
    ("source.txt", "text/plain", "auto"),
])
async def test_source_publication_retains_original_mime_and_collection_scope(
    source_environment, name, mime, mode,
):
    env = source_environment
    env.source.rename(env.workspace / name)
    env.source = env.workspace / name
    await _publish(env.context, env.adopter, path=name, mime=mime, bundle=mode)
    binding = await _binding(env)
    assert binding.entry == env.source
    assert binding.entry_mime == mime
    assert await _save(env, binding) is None
    expected = {name, "style.css"} if mime == "text/html" and mode == "auto" else {name}
    assert {item.path for item in binding.bundle().files} == expected
    env.source.write_text(env.source.read_text().replace("First", "Second"))
    changed = await _save(env, binding)
    assert changed is not None
    await _restore(env, binding, binding.base_revision_id)
    restored = await _reconcile(env, binding)
    assert restored.entry_mime == mime
    assert await _save(env, restored) is None
