from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from starlette.applications import Starlette

from opensquilla.artifact_session import ArtifactSessionService
from opensquilla.artifact_session.working_files import WorkingFiles
from opensquilla.artifacts import ArtifactStore
from opensquilla.gateway.artifacts import register_artifact_routes
from opensquilla.gateway.config import AttachmentsConfig, AuthConfig, GatewayConfig
from opensquilla.gateway.execution_workspaces import build_execution_workspace_factory
from opensquilla.gateway.generated_artifact_adoption import GeneratedArtifactAdopter
from opensquilla.gateway.middleware import AuthMiddleware
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.builtin.workspace_preview import open_workspace_preview
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


@pytest.fixture
async def files(tmp_path):
    config = GatewayConfig(
        auth=AuthConfig(mode="token", token="fixture-secret"),
        attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
    )
    async with SessionStorage(tmp_path / "state.db") as storage:
        manager = SessionManager(
            storage,
            execution_workspace_factory=(
                build_execution_workspace_factory(config, profile_home=tmp_path)
            ),
        )
        session = await manager.create("agent:main:webchat:file-menu")
        root = Path(session.execution_workspace["root"])
        store = ArtifactStore(tmp_path / "media")
        service = await ArtifactSessionService.from_session_storage(storage)
        adopter = GeneratedArtifactAdopter(
            service=service,
            store=store,
            session_key=session.session_key,
            session_id=session.session_id,
            workspace=str(root),
            preview_service=Mock(),
        )
        context = ToolContext(
            is_owner=True,
            caller_kind=CallerKind.WEB,
            session_key=session.session_key,
            session_id=session.session_id,
            workspace_dir=str(root),
            artifact_session_id=session.session_id,
            artifact_media_root=str(tmp_path / "media"),
            workspace_preview_opener=adopter.open_workspace_preview,
        )
        documents = []
        for site in ("one", "two"):
            (root / site).mkdir()
            for name in ("index.html", "editorial.html", "dashboard.html", "minimal.html"):
                (root / site / name).write_bytes(f"<h1>{site}/{name}</h1>".encode())
            (root / site / "style.css").write_text("h1 { color: red }")
            token = current_tool_context.set(context)
            try:
                result = json.loads(
                    await open_workspace_preview(
                        path=f"{site}/index.html", bundle="directory", bundle_root=site
                    )
                )
                documents.append(result["documentId"])
            finally:
                current_tool_context.reset(token)
        app = Starlette()
        register_artifact_routes(app, config=config, session_manager=manager)
        app.add_middleware(AuthMiddleware, config=config)
        headers = {
            "Authorization": "Bearer fixture-secret",
            "x-opensquilla-session-key": session.session_key,
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=headers
        ) as client:
            yield client, documents, root, service, storage, session, config
        await service.close()


def url(document, **params):
    return f"/api/v1/artifact-documents/{document}/working-file", params


async def get(client, document, **params):
    path, query = url(document, **params)
    return await client.get(path, params=query)


async def counts(service):
    async with service.repository._read_transaction("test.file-menu-counts") as conn:
        result = []
        for table in (
            "artifact_documents",
            "artifact_revisions",
            "document_publications",
            "artifact_audit_events",
        ):
            cursor = await conn.execute(f"SELECT COUNT(*) FROM {table}")
            result.append((await cursor.fetchone())[0])
        return result


async def test_each_site_and_page_resolves_current_bytes_without_writes(files):
    client, documents, root, service, *_ = files
    before = await counts(service)
    for site, document in zip(("one", "two"), documents, strict=True):
        for name in ("index.html", "editorial.html", "dashboard.html", "minimal.html"):
            response = await get(client, document, pagePath=name)
            assert response.status_code == 200, response.text
            assert response.content == (root / site / name).read_bytes()
            assert response.headers["cache-control"] == "no-store"
            metadata = await get(client, document, pagePath=name, format="metadata")
            assert metadata.json()["sourcePath"] == str(root / site / name)
            assert metadata.json()["pagePath"] == name
            assert metadata.json()["size"] == len(response.content)
    assert await counts(service) == before


async def test_external_edit_is_live_but_version_download_stays_immutable(files):
    client, documents, root, service, *_ = files
    document = documents[0]
    revision = (await service.get_document_head(document)).revision
    download = f"/api/v1/artifact-documents/{document}?revisionId={revision.revision_id}"
    original = await client.get(download)
    assert original.status_code == 200
    before = await counts(service)
    (root / "one/index.html").write_bytes(b"<h1>external change\r\n</h1>")
    current = await get(client, document)
    assert current.content == b"<h1>external change\r\n</h1>"
    assert (await client.get(download)).content == original.content
    assert await counts(service) == before


@pytest.mark.parametrize(
    "page",
    [
        "../two/index.html",
        "/index.html",
        "style.css",
        "missing.html",
        "a\\index.html",
        "index.html?x",
        "%2e%2e/index.html",
    ],
)
async def test_non_members_and_unsafe_paths_fail_closed(files, page):
    client, documents, *_ = files
    for format_ in ("metadata", "content"):
        assert (await get(client, documents[0], pagePath=page, format=format_)).status_code == 404


async def test_auth_origin_and_session_are_required(files):
    client, documents, _, _, storage, session, _ = files
    path, _ = url(documents[0])
    assert (await client.get(path, headers={"Authorization": "Bearer invalid"})).status_code == 401
    assert (await client.get(path, headers={"Origin": "null"})).status_code == 403
    assert (await client.get(path, headers={"x-opensquilla-session-key": ""})).status_code == 404
    other = session.model_copy(
        update={"session_key": "agent:main:webchat:other", "session_id": "other"}
    )
    await storage.upsert_session(other)
    assert (
        await client.get(path, headers={"x-opensquilla-session-key": other.session_key})
    ).status_code == 404
    assert (await get(client, documents[0], format="other")).status_code == 400


async def test_deleted_source_and_symlink_never_fall_back_to_snapshot(files):
    client, documents, root, *_ = files
    page = root / "one/editorial.html"
    page.unlink()
    assert (await get(client, documents[0], pagePath="editorial.html")).status_code == 404
    page.symlink_to(root / "two/index.html")
    assert (await get(client, documents[0], pagePath="editorial.html")).status_code == 404


async def test_revoked_reads_and_missing_binding_are_not_materialized(files):
    client, documents, root, service, storage, _, config = files
    config.sandbox.denied_read_roots = [str(root / "one")]
    assert (await get(client, documents[0])).status_code == 404
    assert (await get(client, documents[1])).status_code == 200
    async with storage._write_transaction("test.remove-binding") as conn:
        await conn.execute(
            "DELETE FROM artifact_working_files WHERE document_id=?", (documents[1],)
        )
    before = await counts(service)
    assert (await get(client, documents[1])).status_code == 404
    assert await counts(service) == before


async def test_workspace_revocation_after_read_discards_bytes(files, monkeypatch):
    client, documents, _, _, storage, session, _ = files
    original = WorkingFiles.bundle
    import asyncio

    loop = asyncio.get_running_loop()

    async def revoke():
        current = await storage.get_session(session.session_key)
        await storage.upsert_session(current.model_copy(update={"epoch": current.epoch + 1}))

    def paused_read(binding, **kwargs):
        bundle = original(binding, **kwargs)
        asyncio.run_coroutine_threadsafe(revoke(), loop).result(timeout=5)
        return bundle

    monkeypatch.setattr(WorkingFiles, "bundle", paused_read)
    assert (await get(client, documents[0])).status_code == 404


async def test_legacy_working_copy_is_not_presented_as_original_source(files):
    client, documents, _, service, storage, *_ = files
    async with storage._write_transaction("test.remove-source-binding") as conn:
        await conn.execute(
            "DELETE FROM artifact_working_sources WHERE document_id=?", (documents[0],)
        )
    before = await counts(service)
    assert (await get(client, documents[0], format="metadata")).status_code == 404
    assert (await get(client, documents[0])).status_code == 404
    assert await counts(service) == before
