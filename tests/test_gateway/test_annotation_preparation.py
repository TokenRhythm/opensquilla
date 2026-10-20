from __future__ import annotations

import asyncio
import copy
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    ArtifactBlobRef,
    ArtifactSessionService,
)
from opensquilla.artifact_session.working_files import get_working_files
from opensquilla.artifacts import ArtifactStore, collect_artifact_bundle
from opensquilla.engine.types import TextDeltaEvent
from opensquilla.gateway.config import AttachmentsConfig, GatewayConfig
from opensquilla.gateway.execution_workspaces import build_execution_workspace_factory
from opensquilla.gateway.page_context import resolve_page_context
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.working_versions import with_working_versions
from opensquilla.gateway.workspace_files import WorkspaceFiles, WorkspaceFileUnavailableError
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.source_edit_contract import workspace_file_reference, workspace_reference_id
from tests.test_office_format import office_package


@pytest.fixture
async def annotation_env(tmp_path):
    config = GatewayConfig(attachments=AttachmentsConfig(media_root=str(tmp_path / "media")))
    async with SessionStorage(tmp_path / "state.db") as storage:
        manager = SessionManager(
            storage,
            execution_workspace_factory=build_execution_workspace_factory(
                config,
                profile_home=tmp_path,
            ),
        )
        session = await manager.create("agent:main:webchat:annotation")
        service = await ArtifactSessionService.from_session_storage(storage)
        try:
            yield SimpleNamespace(
                config=config,
                storage=storage,
                manager=manager,
                session=session,
                service=service,
                store=ArtifactStore(tmp_path / "media"),
                root=Path(session.execution_workspace["root"]),
                files=WorkspaceFiles(config, manager),
                ctx=RpcContext(conn_id="annotation-test", config=config, session_manager=manager),
            )
        finally:
            await service.close()


async def _open(env, *, path=None, resource=None, intent="annotate", **kwargs):
    params = {"sessionKey": env.session.session_key, "intent": intent, **kwargs}
    if path is not None:
        resolved = await env.files.resolve(env.session.session_key, [path])
        params["workspaceFile"] = {
            "path": path,
            "workspaceBinding": resolved["workspaceBinding"],
            "expectedRevision": resolved["files"][0]["revision"],
        }
    else:
        params["resourceRef"] = resource
    return await get_dispatcher().dispatch(
        "test:annotation-open",
        "workbench.resources.open",
        params,
        env.ctx,
    )


@pytest.mark.parametrize("format", ["html", "docx", "pptx", "xlsx"])
async def test_reference_current_document_resolution_preserves_workspace_scope(
    annotation_env, format,
):
    env = annotation_env
    path = env.root / f"plan.{format}"
    path.write_bytes(b"<h1>Current</h1>" if format == "html" else office_package(format, "Current"))
    reference = workspace_file_reference(
        path.name, revision="file_" + "0" * 16, start_line=1, end_line=1,
        session_key=env.session.session_key, workspace_id=workspace_reference_id(env.root),
    )
    assert reference is not None
    async with env.storage.conn.execute("SELECT total_changes()") as cursor:
        before_discovery = (await cursor.fetchone())[0]
    # Unregistered binary Office references must reach the same file preview
    # as path/tree entries, without importing or creating a Document.
    unresolved = await env.files.resolve(
        env.session.session_key, [path.name], reference=reference,
    )
    assert unresolved["referenceScopeValidated"] is True
    assert unresolved["files"][0]["path"] == path.name
    assert unresolved["files"][0]["annotationCapability"] == {"supported": True}
    assert "documentTarget" not in unresolved["files"][0]
    assert not await env.service.list_documents(session_key=env.session.session_key)
    async with env.storage.conn.execute("SELECT total_changes()") as cursor:
        assert (await cursor.fetchone())[0] == before_discovery
    for scope in (
        {"workspaceId": "workspace_other"},
        {"sessionKey": "agent:main:webchat:another"},
        {"gatewayInstanceId": "other-instance"},
    ):
        invalid = copy.deepcopy(reference)
        invalid["scope"].update(scope)
        with pytest.raises((WorkspaceFileUnavailableError, RpcHandlerError)):
            await env.files.resolve(env.session.session_key, [path.name], reference=invalid)
    env.config.sandbox.denied_read_globs = [f"**/{path.name}"]
    denied = await env.files.resolve(env.session.session_key, [path.name], reference=reference)
    assert denied["files"] == []
    env.config.sandbox.denied_read_globs = []
    opened = await _open(env, path=path.name)
    assert opened.error is None, opened.error
    document_id = opened.payload["document"]["documentId"]
    async with env.storage.conn.execute("SELECT total_changes()") as cursor:
        before = (await cursor.fetchone())[0]

    # Opening current content ignores the old content pin, including for real
    # binary Office packages, while keeping every identity/authority constraint.
    del reference["locator"]["startLine"]
    del reference["locator"]["endLine"]
    resolved = await env.files.resolve(
        env.session.session_key, [path.name], reference=reference,
    )
    assert resolved["referenceScopeValidated"] is True
    assert resolved["files"][0]["documentTarget"]["documentId"] == document_id
    for scope in (
        {"workspaceId": "workspace_other"},
        {"sessionKey": "agent:main:webchat:another"},
        {"gatewayInstanceId": "other-instance"},
    ):
        invalid = copy.deepcopy(reference)
        invalid["scope"].update(scope)
        with pytest.raises((WorkspaceFileUnavailableError, RpcHandlerError)):
            await env.files.resolve(env.session.session_key, [path.name], reference=invalid)
    with pytest.raises(WorkspaceFileUnavailableError):
        await env.files.resolve(env.session.session_key, ["another.docx"], reference=reference)
    async with env.storage.conn.execute("SELECT total_changes()") as cursor:
        assert (await cursor.fetchone())[0] == before


@pytest.mark.parametrize("format", ["html", "docx", "pptx", "xlsx"])
async def test_register_original_once_and_refresh_only_when_annotating(annotation_env, format):
    env = annotation_env
    path = env.root / f"plan.{format}"
    original = b"<h1>Original</h1>" if format == "html" else office_package(format, "Original")
    changed = b"<h1>Changed</h1>" if format == "html" else office_package(format, "Changed")
    if format in {"pptx", "xlsx"}:
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(changed)) as source, zipfile.ZipFile(output, "w") as target:
            for item in source.infolist():
                payload = source.read(item)
                if item.filename in {"ppt/presentation.xml", "xl/workbook.xml"}:
                    payload = b"<!-- external edit -->" + payload
                target.writestr(item, payload)
        changed = output.getvalue()
    path.write_bytes(original)
    inventory = await env.files.resolve(env.session.session_key, [path.name])
    assert inventory["files"][0]["annotationCapability"] == {"supported": True}
    assert "documentTarget" not in inventory["files"][0]
    assert not await env.service.list_documents(session_key=env.session.session_key)
    opened = await _open(env, path=path.name)
    assert opened.error is None, opened.error
    result = opened.payload
    document_id = result["document"]["documentId"]
    assert Path(result["workingFile"]) == path
    assert path.read_bytes() == original
    binding = await get_working_files(env.service, document_id)
    assert binding.source_path == path.name
    again = await _open(env, path=path.name)
    assert again.error is None, again.error
    assert again.payload["pageContext"] == result["pageContext"]
    assert len(await env.service.list_revisions(document_id)) == 1
    path.write_bytes(changed)
    inventory = await env.files.resolve(env.session.session_key, [path.name])
    assert inventory["files"][0]["documentTarget"] == {"documentId": document_id}
    assert len(await env.service.list_revisions(document_id)) == 1
    ordinary = await _open(
        env,
        resource={"type": "document", "id": document_id},
        intent="edit-current",
    )
    assert ordinary.error is None, ordinary.error
    assert ordinary.payload["pageContext"] == result["pageContext"]
    assert path.read_bytes() == changed
    refreshed = await _open(env, path=path.name)
    assert refreshed.error is None, refreshed.error
    assert refreshed.payload["baselineUpdated"] is True
    assert refreshed.payload["previousRevisionId"] == result["pageContext"]["revisionId"]
    assert path.read_bytes() == changed
    head = await env.service.get_document_head(document_id)
    assert head.revision.source.value == "manual"
    assert head.revision.revision_id == refreshed.payload["pageContext"]["revisionId"]
    assert len(await env.service.list_revisions(document_id)) == 2


async def test_html_child_path_reuses_registered_bundle_and_preserves_page(annotation_env):
    env = annotation_env
    site = env.root / "site"
    site.mkdir()
    (site / "index.html").write_text("<h1>Home</h1>")
    (site / "pricing.html").write_text("<h1>Pricing</h1>")
    bundle = collect_artifact_bundle(
        site / "index.html",
        workspace_root=env.root,
        mode="directory",
        bundle_root=site,
    )
    ref = env.store.publish_bundle(
        bundle,
        session_key=env.session.session_key,
        session_id=env.session.session_id,
        name="index.html",
        mime="text/html",
        source="test",
        visibility="internal",
    )
    commit, _ = await env.service.register_working_source(
        session_key=env.session.session_key,
        session_id=env.session.session_id,
        name=ref.name,
        initial_artifact=ArtifactBlobRef(
            artifact_id=ref.id,
            sha256=ref.sha256,
            filename=ref.name,
            media_type=ref.mime,
            byte_size=ref.size,
        ),
        actor=Actor(kind=ActorKind.USER, actor_id="test"),
        working_source={
            "workspace": str(env.root),
            "source_path": "site/index.html",
            "relative_root": "site",
            "entrypoint": "index.html",
            "bundle_mode": "directory",
            "bundle_root": "site",
        },
    )
    inventory = await env.files.resolve(env.session.session_key, ["site/pricing.html"])
    assert inventory["files"][0]["documentTarget"] == {
        "documentId": commit.document.document_id,
        "pagePath": "pricing.html",
    }
    (site / "faq.html").write_text("<h1>New live page</h1>")
    live = await env.files.resolve(env.session.session_key, ["site/faq.html"])
    assert live["files"][0]["documentTarget"] == {
        "documentId": commit.document.document_id, "pagePath": "faq.html",
    }
    assert len(await env.service.list_revisions(commit.document.document_id)) == 1
    result = await _open(env, path="site/pricing.html")
    assert result.error is None, result.error
    assert result.payload["pageContext"]["pagePath"] == "pricing.html"
    assert result.payload["document"]["documentId"] == commit.document.document_id
    assert len(await env.service.list_documents(session_key=env.session.session_key)) == 1


async def test_bundle_member_change_rejects_send_without_refreshing_baseline(annotation_env):
    env = annotation_env
    (env.root / "index.html").write_text('<link rel="stylesheet" href="style.css"><h1>Home</h1>')
    css = env.root / "style.css"
    css.write_text("h1 {color: red}")
    opened = await _open(env, path="index.html")
    assert opened.error is None, opened.error
    context = {**opened.payload["pageContext"], "annotations": [{"text": "Make this blue"}]}
    css.write_text("h1 {color: green}")
    with pytest.raises(RpcHandlerError) as conflict:
        await resolve_page_context(
            context,
            env.ctx,
            session_key=env.session.session_key,
            session_id=env.session.session_id,
            workspace=str(env.root),
            candidate_id="turn-one",
        )
    assert conflict.value.code == "DOCUMENT_CHANGED"
    assert len(await env.service.list_revisions(opened.payload["document"]["documentId"])) == 1
    prepared = await _open(env, path="index.html")
    assert prepared.error is None, prepared.error
    assert prepared.payload["baselineUpdated"] is True
    assert prepared.payload["pageContext"]["sha256"] == context["sha256"]
    assert prepared.payload["pageContext"]["revisionId"] != context["revisionId"]


async def test_first_prepare_concurrency_deduplicates_document(annotation_env):
    env = annotation_env
    (env.root / "index.html").write_text("<h1>Shared</h1>")
    first, second = await asyncio.gather(
        _open(env, path="index.html"),
        _open(env, path="index.html"),
    )
    assert first.error is None, first.error
    assert second.error is None, second.error
    assert first.payload["pageContext"] == second.payload["pageContext"]
    assert len(await env.service.list_documents(session_key=env.session.session_key)) == 1


async def test_prepare_returns_concurrent_baseline(annotation_env, monkeypatch):
    from opensquilla.gateway import annotation_preparation

    env = annotation_env
    path = env.root / "index.html"
    path.write_text("<h1>Original</h1>")
    original = await _open(env, path=path.name)
    assert original.error is None, original.error
    original_revision = original.payload["revision"]["revisionId"]
    document_id = original.payload["document"]["documentId"]
    refresh = annotation_preparation.refresh_annotation_baseline
    first_waiting = asyncio.Event()
    continue_first = asyncio.Event()

    async def pause_first_refresh(*args, **kwargs):
        if asyncio.current_task() is first_task:
            first_waiting.set()
            await continue_first.wait()
        return await refresh(*args, **kwargs)

    monkeypatch.setattr(annotation_preparation, "refresh_annotation_baseline", pause_first_refresh)
    first_task = asyncio.create_task(_open(env, path=path.name))
    try:
        # The first client has opened v1. A second client registers the external
        # edit before the first client checks the source against the new head.
        await asyncio.wait_for(first_waiting.wait(), timeout=5)
        path.write_text("<h1>External edit</h1>")
        second = await _open(env, path=path.name)
        assert second.error is None, second.error
    finally:
        continue_first.set()
        first = await first_task

    assert first.error is None, first.error
    head = await env.service.get_document_head(document_id)
    for response in (first.payload, second.payload):
        assert response["revision"]["revisionId"] == head.revision.revision_id
        assert response["pageContext"]["revisionId"] == head.revision.revision_id
        assert response["pageContext"]["sha256"] == head.revision.artifact_sha256
        assert response["baselineUpdated"] is True
        assert response["previousRevisionId"] == original_revision
    assert len(await env.service.list_revisions(document_id)) == 2
    assert path.read_text() == "<h1>External edit</h1>"


async def test_workspace_input_requires_fresh_identity_and_is_mutually_exclusive(annotation_env):
    env = annotation_env
    path = env.root / "index.html"
    path.write_text("<h1>One</h1>")
    resolved = await env.files.resolve(env.session.session_key, [path.name])
    params = {
        "sessionKey": env.session.session_key,
        "intent": "annotate",
        "workspaceFile": {
            "path": path.name,
            "workspaceBinding": resolved["workspaceBinding"],
            "expectedRevision": resolved["files"][0]["revision"],
        },
    }
    path.write_text("<h1>Changed since listing</h1>")
    dispatch = get_dispatcher().dispatch
    changed = await dispatch("test:stale", "workbench.resources.open", params, env.ctx)
    assert changed.error is not None
    mixed = await dispatch(
        "test:mixed",
        "workbench.resources.open",
        {**params, "resourceRef": {"type": "document", "id": "unexpected"}},
        env.ctx,
    )
    assert mixed.error is not None
    assert not await env.service.list_documents(session_key=env.session.session_key)


async def test_html_execution_rechecks_queued_selection_before_starting_agent(annotation_env):
    env = annotation_env
    path = env.root / "index.html"
    path.write_text("<h1>Before</h1>")
    opened = await _open(env, path=path.name)
    assert opened.error is None, opened.error
    context = {**opened.payload["pageContext"], "annotations": [{"text": "Change title"}]}
    resolved = await resolve_page_context(
        context,
        env.ctx,
        session_key=env.session.session_key,
        session_id=env.session.session_id,
        workspace=str(env.root),
        candidate_id="queued",
    )
    path.write_text("<h1>Externally changed while queued</h1>")
    started = False

    async def agent():
        nonlocal started
        started = True
        yield TextDeltaEvent(text="Must not execute")

    events = [
        event
        async for event in with_working_versions(
            agent(),
            config=env.config,
            session_manager=env.manager,
            session_key=env.session.session_key,
            session_id=env.session.session_id,
            workspace=str(env.root),
            actor_id="queued",
            annotation_selection=resolved["_annotationSelection"],
        )
    ]
    assert not started
    assert len(events) == 1
    assert events[0].code == "ANNOTATION_SELECTION_STALE"
    assert len(await env.service.list_revisions(opened.payload["document"]["documentId"])) == 1


@pytest.mark.parametrize("format", ["html", "docx", "pptx", "xlsx"])
async def test_queued_source_permission_rechecked(annotation_env, format):
    env = annotation_env
    path = env.root / f"queued.{format}"
    original = b"<h1>Original</h1>" if format == "html" else office_package(format, "Original")
    path.write_bytes(original)
    opened = await _open(env, path=path.name)
    assert opened.error is None, opened.error
    context = {
        **opened.payload["pageContext"],
        "annotations": [{"text": "Change selected content"}],
    }
    resolved = await resolve_page_context(
        context,
        env.ctx,
        session_key=env.session.session_key,
        session_id=env.session.session_id,
        workspace=str(env.root),
        candidate_id="queued",
    )
    env.config.sandbox.denied_read_globs = [f"**/{path.name}"]
    assert (await env.files.resolve(env.session.session_key, [path.name]))["files"] == []
    started = False

    async def agent():
        nonlocal started
        started = True
        yield TextDeltaEvent(text="Must not execute")

    events = [
        event
        async for event in with_working_versions(
            agent(),
            config=env.config,
            session_manager=env.manager,
            session_key=env.session.session_key,
            session_id=env.session.session_id,
            workspace=str(env.root),
            actor_id="queued",
            office_candidate=resolved.get("_officeCandidate"),
            annotation_selection=resolved.get("_annotationSelection"),
        )
    ]
    assert not started
    assert len(events) == 1
    assert events[0].code == "ANNOTATION_SELECTION_STALE"
    assert path.read_bytes() == original
    assert len(await env.service.list_revisions(opened.payload["document"]["documentId"])) == 1
    if format != "html":
        assert not Path(resolved["workingFile"]).exists()


async def test_prepare_rejects_source_changing_during_capture(annotation_env, monkeypatch):
    env = annotation_env
    path = env.root / "index.html"
    path.write_text("<h1>Captured</h1>")
    original_publish = ArtifactStore.publish_bundle

    def publish_then_change(store, *args, **kwargs):
        result = original_publish(store, *args, **kwargs)
        path.write_text("<h1>Changed after capture</h1>")
        return result

    monkeypatch.setattr(ArtifactStore, "publish_bundle", publish_then_change)
    result = await _open(env, path=path.name)
    assert result.error is not None
    assert path.read_text() == "<h1>Changed after capture</h1>"
    assert not await env.service.list_documents(session_key=env.session.session_key)


async def test_bundle_read_guard_rejects_denied_dependency(annotation_env):
    env = annotation_env
    (env.root / "index.html").write_text('<link rel="stylesheet" href="secret.css"><h1>Home</h1>')
    (env.root / "secret.css").write_text("h1 {color: red}")
    env.config.sandbox.denied_read_globs = ["**/secret.css"]
    result = await _open(env, path="index.html")
    assert result.error is not None
    assert not await env.service.list_documents(session_key=env.session.session_key)


async def test_invalid_office_source_is_not_registered(annotation_env):
    env = annotation_env
    (env.root / "broken.docx").write_bytes(b"not an OOXML package")
    result = await _open(env, path="broken.docx")
    assert result.error is not None
    assert not await env.service.list_documents(session_key=env.session.session_key)
