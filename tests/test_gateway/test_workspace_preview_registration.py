from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from opensquilla.artifact_session import ArtifactSessionService
from opensquilla.artifact_session.working_files import get_working_files
from opensquilla.artifacts import ArtifactStore
from opensquilla.engine.artifact_delivery import auto_publish_omitted_workspace_artifacts
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import ArtifactEvent
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.generated_artifact_adoption import GeneratedArtifactAdopter
from opensquilla.sandbox.permissions import FileSystemPermissionProfile
from opensquilla.tools.builtin.artifacts import publish_artifact
from opensquilla.tools.builtin.workspace_preview import open_workspace_preview
from opensquilla.tools.types import CallerKind, ToolContext, ToolError, current_tool_context


@pytest.fixture
async def preview(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    site = workspace / "site"
    (site / "pages").mkdir(parents=True)
    (site / "pages" / "index.html").write_text(
        '<link rel="stylesheet" href="../style.css"><h1>Live</h1>'
    )
    (site / "style.css").write_text("h1 { color: navy }")
    (workspace / "report.pdf").write_bytes(b"%PDF-1.4\nfixture")
    service = await ArtifactSessionService.open(tmp_path / "state.db")
    store = ArtifactStore(tmp_path / "media")
    adopter = GeneratedArtifactAdopter(
        service=service, store=store, session_key="agent:main:webchat:preview",
        session_id="preview", workspace=str(workspace), preview_service=Mock(),
    )
    context = ToolContext(
        is_owner=True, caller_kind=CallerKind.WEB, session_key=adopter.session_key,
        session_id=adopter.session_id, workspace_dir=str(workspace),
        artifact_session_id=adopter.session_id, artifact_media_root=str(tmp_path / "media"),
        workspace_preview_opener=adopter.open_workspace_preview,
    )
    try:
        yield context, adopter, workspace
    finally:
        await service.close()


async def _open(context, path="site/pages/index.html", **kwargs):
    token = current_tool_context.set(context)
    try:
        return json.loads(await open_workspace_preview(path=path, **kwargs))
    finally:
        current_tool_context.reset(token)


async def test_real_runtime_context_retains_preview_capability(preview):
    context, adopter, _ = preview
    runner = TurnRunner(provider_selector=None, config=GatewayConfig())
    runtime_context = await runner._with_artifact_context(context, context.session_key)
    assert runtime_context is not context
    assert runtime_context.workspace_preview_opener == context.workspace_preview_opener
    opened = await _open(runtime_context, bundle="directory", bundle_root="site")
    assert opened["created"] is True
    assert len(await adopter.service.list_revisions(opened["documentId"])) == 1
    assert runtime_context.published_artifacts == []


async def test_absolute_workspace_path_is_normalized_and_reopens_same_document(preview):
    context, _, workspace = preview
    opened = await _open(
        context,
        path=str(workspace / "site/pages/index.html"),
        bundle="directory",
        bundle_root="site",
    )
    reopened = await _open(context)
    assert reopened["resourceId"] == opened["resourceId"]
    assert reopened["created"] is False


async def test_directory_nested_entry_and_retry_keep_one_source(preview, monkeypatch):
    context, adopter, workspace = preview
    opened = await _open(context, bundle="directory", bundle_root="site")
    binding = await get_working_files(adopter.service, opened["documentId"])
    assert binding.root == workspace / "site"
    assert binding.entrypoint == "pages/index.html"
    assert binding.entry == workspace / "site/pages/index.html"
    assert {item.path for item in binding.bundle().files} == {"pages/index.html", "style.css"}
    monkeypatch.setattr(
        adopter.store, "publish_bundle", Mock(side_effect=AssertionError("retry copied blob")),
    )
    reopened_context = replace(context, workspace_preview_scopes=[])
    reopened = await _open(reopened_context)
    assert reopened["resourceId"] == opened["resourceId"]
    assert reopened["bundleMode"] == "directory"
    assert reopened["bundleRoot"] == "site"
    assert reopened["created"] is False
    assert reopened_context.workspace_preview_scopes == await adopter.working_source_scopes()
    with pytest.raises(ToolError, match="scope is already registered"):
        await _open(context, bundle="auto")
    async with adopter.service.repository._read_transaction("test.counts") as conn:
        for table, count in (
            ("artifact_documents", 1), ("artifact_revisions", 1), ("document_publications", 0),
        ):
            cursor = await conn.execute(f"SELECT COUNT(*) FROM {table}")
            assert (await cursor.fetchone())[0] == count


async def test_none_mode_is_html_and_notification_failure_does_not_undo_commit(preview):
    context, adopter, _ = preview
    adopter.event_emitter = AsyncMock(side_effect=RuntimeError("client disconnected"))
    opened = await _open(context, bundle="none")
    assert opened["created"] is True
    head = await adopter.service.get_document_head(opened["documentId"])
    assert head.revision.media_type == "text/html"
    assert (await _open(context))["created"] is False
    assert adopter.event_emitter.await_count == 1


@pytest.mark.parametrize("suffix,mime", [
    (".html", "text/html"),
    (".htm", "text/html"),
    (".xhtml", "application/xhtml+xml"),
])
@pytest.mark.parametrize("mode", ["none", "auto", "directory"])
async def test_entry_mime_survives_registration_reopen_and_publication(preview, suffix, mime, mode):
    context, adopter, workspace = preview
    path = f"site/pages/entry{suffix}"
    payload = (
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head>'
        b'<link rel="stylesheet" href="../style.css" /></head>'
        b'<body><h1>Live</h1></body></html>'
    )
    (workspace / path).write_bytes(payload)
    collection = {"bundle": mode, **({"bundle_root": "site"} if mode == "directory" else {})}
    opened = await _open(context, path=path, **collection)
    head = await adopter.service.get_document_head(opened["documentId"])
    assert head.revision.media_type == mime
    ref = adopter.store.get_ref(
        session_id=context.session_id, artifact_id=head.revision.artifact_id,
    )
    assert ref.mime == mime
    binding = await get_working_files(adopter.service, opened["documentId"])
    assert binding.entry_mime == mime
    entry = next(file for file in binding.bundle().files if file.path == binding.entrypoint)
    assert entry.mime == mime
    assert entry.data == payload

    reopened = await _open(context, path=path)
    assert reopened["resourceId"] == opened["resourceId"]
    assert reopened["created"] is False
    assert len(await adopter.service.list_revisions(opened["documentId"])) == 1
    assert context.published_artifacts == []
    async with adopter.service.repository._read_transaction("test.preview-only") as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM document_publications")
        assert (await cursor.fetchone())[0] == 0

    context.generated_artifact_adopter = adopter
    adopter.source_paths = context.artifact_source_paths
    token = current_tool_context.set(context)
    try:
        published = json.loads(await publish_artifact(path=path, **collection))
    finally:
        current_tool_context.reset(token)
    event = next(
        item for item in context.published_artifacts if item["id"] == published["artifact"]["id"]
    )
    assert event["mime"] == mime
    await adopter(ArtifactEvent(**event))
    documents = await adopter.service.list_documents(
        session_key=context.session_key, session_id=context.session_id,
    )
    assert [document.document_id for document in documents] == [opened["documentId"]]


@pytest.mark.parametrize("mode", ["none", "auto", "directory"])
async def test_persisted_preview_scope_does_not_suppress_sibling_pdf(preview, mode):
    context, adopter, workspace = preview
    await _open(context, bundle=mode, **({"bundle_root": "site"} if mode == "directory" else {}))
    context.workspace_preview_scopes = await adopter.working_source_scopes()
    context.workspace_file_writes = [
        {"path": str(workspace / "site/pages/index.html"), "created": True,
         "name": "index.html", "relative_path": "site/pages/index.html"},
        {"path": str(workspace / "report.pdf"), "created": True,
         "name": "report.pdf", "relative_path": "report.pdf"},
    ]
    events = auto_publish_omitted_workspace_artifacts(
        context, final_text="Finished site/pages/index.html and report.pdf",
    )
    assert events.failure_summaries == []
    assert [event["name"] for event in events.artifacts] == ["report.pdf"]


@pytest.mark.parametrize("field,value", [
    ("workspace_dir", "/other"), ("session_id", "other"), ("session_key", "other"),
])
async def test_wrong_context_cannot_register_source(preview, field, value):
    context, adopter, _ = preview
    with pytest.raises(ToolError, match="no longer matches"):
        await _open(replace(context, **{field: value}))
    assert not await adopter.service.list_documents(
        session_key=context.session_key, session_id=context.session_id,
    )


async def test_stale_session_fence_is_checked_even_when_reopening(preview):
    context, adopter, _ = preview
    await _open(context)
    adopter.validate_session = AsyncMock(side_effect=ValueError("session generation changed"))
    with pytest.raises(ToolError, match="session generation changed"):
        await _open(context)


async def test_denied_dependency_aborts_before_registration(preview):
    context, adopter, workspace = preview
    context.sandbox_file_system_profile = FileSystemPermissionProfile.workspace(
        workspace=workspace, denied_read_roots=(workspace / "site/style.css",),
    )
    with pytest.raises(ToolError, match="authorized read scope"):
        await _open(context, bundle="directory", bundle_root="site")
    assert not await adopter.service.list_documents(
        session_key=context.session_key, session_id=context.session_id,
    )
    assert context.workspace_preview_scopes == []


async def test_preview_cannot_collect_other_session_attachment(preview):
    context, adopter, workspace = preview
    context.workspace_strict = True
    private = workspace / ".opensquilla/attachments/other-session"
    private.mkdir(parents=True)
    (private / "index.html").write_text("<h1>Other session</h1>")
    token = current_tool_context.set(context)
    try:
        with pytest.raises(ToolError, match="another session's private material"):
            await open_workspace_preview(path=".opensquilla/attachments/other-session/index.html")
    finally:
        current_tool_context.reset(token)
    assert not await adopter.service.list_documents(
        session_key=context.session_key, session_id=context.session_id,
    )


async def test_preview_then_explicit_publish_uses_same_document(preview):
    context, adopter, workspace = preview
    opened = await _open(context, bundle="directory", bundle_root="site")
    context.generated_artifact_adopter = adopter
    adopter.source_paths = context.artifact_source_paths
    token = current_tool_context.set(context)
    try:
        result = json.loads(await publish_artifact(
            path="site/pages/index.html", bundle="directory", bundle_root="site",
        ))
    finally:
        current_tool_context.reset(token)
    payload = next(
        item for item in context.published_artifacts if item["id"] == result["artifact"]["id"]
    )
    await adopter(ArtifactEvent(**payload))
    documents = await adopter.service.list_documents(
        session_key=context.session_key, session_id=context.session_id,
    )
    assert [document.document_id for document in documents] == [opened["documentId"]]
    artifact_id = result["artifact"]["id"]
    original = adopter.store.resolve_preview_resource(
        artifact_id, session_id=context.session_id, logical_path="style.css",
    ).path.read_bytes()
    (workspace / "site/style.css").write_text("h1 { color: crimson }")
    assert adopter.store.resolve_preview_resource(
        artifact_id, session_id=context.session_id, logical_path="style.css",
    ).path.read_bytes() == original


@pytest.mark.parametrize("bundle_root", [".", "../workspace/site", "/absolute/site"])
async def test_directory_scope_must_be_explicit_relative_subdirectory(preview, bundle_root):
    context, _, _ = preview
    with pytest.raises(ToolError):
        await _open(context, bundle="directory", bundle_root=bundle_root)
