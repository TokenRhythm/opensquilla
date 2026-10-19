from __future__ import annotations

import asyncio
import io
import threading
import zipfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from test_artifacts_download import _app, _publish
from test_working_file_actions import counts
from test_working_file_actions import files as files

from opensquilla import artifact_archives as html_archive
from opensquilla.application.artifact_workbench import ContentExportError
from opensquilla.artifact_session.models import Actor, ActorKind, ArtifactBlobRef
from opensquilla.artifact_session.working_files import WorkingFiles
from opensquilla.artifacts import ArtifactBundle, ArtifactBundleSourceFile, ArtifactStore
from opensquilla.gateway.html_archive import HtmlZipExporter, TemporaryArchiveResponse


def unpack(payload: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def bundle(html: bytes = b"<h1>example</h1>") -> ArtifactBundle:
    return ArtifactBundle(
        "index.html", (ArtifactBundleSourceFile("index.html", "text/html", html),)
    )


async def valid() -> None:
    pass


@pytest.fixture
def export_tmp(tmp_path, monkeypatch):
    root = tmp_path / "exports"
    root.mkdir()
    original = html_archive.tempfile.mkdtemp
    monkeypatch.setattr(
        html_archive.tempfile,
        "mkdtemp",
        lambda **kwargs: original(dir=root, **kwargs),
    )
    return root


def test_remote_artifact_zip_auth_scope_and_normal_download(tmp_path, export_tmp):
    payload = "<h1>中文网页</h1>".encode()
    ref = _publish(tmp_path, payload=payload, name="网页.html", mime="text/html")
    url = f"/api/v1/artifacts/{ref.id}"
    params = {"sessionKey": "agent:main:webchat:ok", "format": "zip"}
    with TestClient(_app(tmp_path, host="0.0.0.0"), client=("192.168.1.20", 1234)) as client:
        assert client.get(url, params=params).status_code == 401
        client.headers["Authorization"] = "Bearer secret"
        assert client.get(url, params={**params, "sessionKey": "other"}).status_code == 404
        response = client.get(url, params=params)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/zip"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        assert ".zip" in response.headers["content-disposition"]
        assert unpack(response.content) == {"网页.html": payload}
        assert client.get(url, params={"sessionKey": params["sessionKey"]}).content == payload
        assert client.get(url, params={**params, "variant": "thumb"}).status_code == 400
        assert client.head(url, params=params).status_code == 405
    assert list(export_tmp.iterdir()) == []


@pytest.mark.parametrize("payload", [b'<img src="missing.png">', b'<img src="../secret.png">'])
def test_legacy_known_missing_or_unsafe_dependency_is_not_silently_omitted(
    tmp_path,
    export_tmp,
    payload,
):
    ref = _publish(tmp_path, payload=payload, name="page.html", mime="text/html")
    with TestClient(_app(tmp_path)) as client:
        response = client.get(
            f"/api/v1/artifacts/{ref.id}",
            params={"sessionKey": "agent:main:webchat:ok", "format": "zip"},
            headers={"Authorization": "Bearer secret"},
        )
    assert response.status_code == 409
    assert response.json()["code"] == "ZIP_INCOMPLETE"
    assert list(export_tmp.iterdir()) == []


def test_non_html_zip_is_unsupported_without_changing_original_download(tmp_path):
    ref = _publish(tmp_path)
    with TestClient(_app(tmp_path)) as client:
        client.headers["Authorization"] = "Bearer secret"
        url = f"/api/v1/artifacts/{ref.id}?sessionKey=agent:main:webchat:ok"
        assert client.get(url + "&format=zip").status_code == 415
        assert client.get(url).content == b"hello artifact"


async def test_working_snapshot_differs_from_fixed_version_without_database_writes(
    files, export_tmp
):
    client, documents, root, service, _, session, config = files
    document = await service.get_document(documents[0])
    first = await service.get_revision(document.head_revision_id)
    store = ArtifactStore(Path(config.attachments.media_root))
    ref = store.publish_bytes(
        b"<h1>new saved revision</h1>",
        session_id=session.session_id,
        session_key=session.session_key,
        name="index.html",
        mime="text/html",
        source="test",
    )
    await service.commit_revision(
        document_id=document.document_id,
        expected_head_revision_id=first.revision_id,
        expected_state_revision=document.state_revision,
        artifact=ArtifactBlobRef(ref.id, ref.sha256, ref.name, ref.mime, ref.size),
        actor=Actor(ActorKind.AGENT, "test"),
    )
    (root / "one/index.html").write_bytes(b"<h1>unsaved live content</h1>")
    before = await counts(service)
    path = f"/api/v1/artifact-documents/{document.document_id}"
    old = await client.get(path, params={"format": "zip", "revisionId": first.revision_id})
    head = await client.get(path, params={"format": "zip"})
    live = await client.get(path + "/working-file", params={"format": "zip"})
    assert old.status_code == head.status_code == live.status_code == 200
    assert unpack(old.content)["index.html"] == b"<h1>one/index.html</h1>"
    assert unpack(head.content) == {"index.html": b"<h1>new saved revision</h1>"}
    assert unpack(live.content)["index.html"] == b"<h1>unsaved live content</h1>"
    assert "style.css" in unpack(live.content)
    other = (await service.get_document_head(documents[1])).revision
    assert (
        await client.get(path, params={"format": "zip", "revisionId": other.revision_id})
    ).status_code == 404
    assert await counts(service) == before
    assert list(export_tmp.iterdir()) == []


async def test_working_zip_keeps_origin_owner_scope_and_read_guards(files, export_tmp, monkeypatch):
    client, documents, root, _, _, _, config = files
    url = f"/api/v1/artifact-documents/{documents[0]}/working-file?format=zip"
    assert (await client.get(url, headers={"Authorization": "Bearer wrong"})).status_code == 401
    assert (await client.get(url, headers={"Origin": "null"})).status_code == 403
    assert (
        await client.get(url, headers={"x-opensquilla-session-key": "other"})
    ).status_code == 404
    config.sandbox.denied_read_roots = [str(root / "one")]
    assert (await client.get(url)).status_code == 404
    config.sandbox.denied_read_roots = []
    from opensquilla.gateway import artifacts

    async def not_owner(*_):
        return False

    monkeypatch.setattr(artifacts, "_request_principal_is_owner", not_owner)
    assert (await client.get(url)).status_code == 403
    assert list(export_tmp.iterdir()) == []


async def test_working_directory_missing_reference_is_rejected(files, export_tmp):
    client, documents, root, *_ = files
    (root / "one/index.html").write_bytes(b'<link rel="stylesheet" href="missing.css">')
    response = await client.get(
        f"/api/v1/artifact-documents/{documents[0]}/working-file?format=zip",
    )
    assert response.status_code == 409
    assert response.json()["code"] == "ZIP_INCOMPLETE"
    assert list(export_tmp.iterdir()) == []


async def test_zip_head_never_captures_or_compresses(files, export_tmp, monkeypatch):
    client, documents, _, service, *_ = files
    revision = (await service.get_document_head(documents[0])).revision

    def forbidden(*_):
        raise AssertionError("HEAD must not capture a ZIP")

    monkeypatch.setattr(html_archive, "_build_archive", forbidden)
    for path in (
        f"/api/v1/artifacts/{revision.artifact_id}",
        f"/api/v1/artifact-documents/{documents[0]}",
        f"/api/v1/artifact-documents/{documents[0]}/working-file",
    ):
        response = await client.head(path, params={"format": "zip"})
        assert response.status_code == 405
        assert response.headers["allow"] == "GET"
    assert list(export_tmp.iterdir()) == []


async def test_owner_revoked_during_zip_export_cleans_material(files, export_tmp, monkeypatch):
    from opensquilla.gateway import artifacts

    client, documents, *_ = files
    checks = 0

    async def owner(*_):
        nonlocal checks
        checks += 1
        return checks == 1

    monkeypatch.setattr(artifacts, "_request_principal_is_owner", owner)
    response = await client.get(
        f"/api/v1/artifact-documents/{documents[0]}/working-file?format=zip",
    )
    assert response.status_code == 403
    assert checks == 2
    assert list(export_tmp.iterdir()) == []


async def test_working_file_change_during_collection_is_not_a_mixed_zip(
    files, export_tmp, monkeypatch,
):
    client, documents, root, *_ = files
    original = WorkingFiles.bundle

    def changing(binding, **kwargs):
        result = original(binding, **kwargs)
        (root / "one/index.html").write_bytes(b"<h1>changed while collecting</h1>")
        return result

    monkeypatch.setattr(WorkingFiles, "bundle", changing)
    response = await client.get(
        f"/api/v1/artifact-documents/{documents[0]}/working-file?format=zip",
    )
    assert response.status_code == 409
    assert response.json()["code"] == "ZIP_SOURCE_CHANGED"
    assert list(export_tmp.iterdir()) == []


async def test_workspace_revocation_after_capture_cleans_zip(files, export_tmp, monkeypatch):
    client, documents, _, _, storage, session, _ = files
    original = WorkingFiles.bundle
    loop = asyncio.get_running_loop()

    async def revoke():
        current = await storage.get_session(session.session_key)
        await storage.upsert_session(current.model_copy(update={"epoch": current.epoch + 1}))

    def read_and_revoke(binding, **kwargs):
        result = original(binding, **kwargs)
        asyncio.run_coroutine_threadsafe(revoke(), loop).result(timeout=5)
        return result

    monkeypatch.setattr(WorkingFiles, "bundle", read_and_revoke)
    response = await client.get(
        f"/api/v1/artifact-documents/{documents[0]}/working-file?format=zip",
    )
    assert response.status_code == 404
    assert list(export_tmp.iterdir()) == []


async def test_external_and_dynamic_references_remain_unchanged(export_tmp):
    html = b'<img src="https://example.invalid/image.png"><script>fetch(variable)</script>'
    material = await HtmlZipExporter().export(lambda: bundle(html), validate=valid)
    try:
        assert unpack(material.path.read_bytes()) == {"index.html": html}
    finally:
        await material.cleanup()
    assert list(export_tmp.iterdir()) == []


@pytest.mark.parametrize("limit", ["input", "output", "files"])
async def test_export_budgets_fail_and_leave_no_temporary_file(export_tmp, monkeypatch, limit):
    field = {
        "input": "DEFAULT_ARTIFACT_BUNDLE_MAX_BYTES",
        "output": "MAX_ZIP_BYTES",
        "files": "DEFAULT_ARTIFACT_BUNDLE_MAX_FILES",
    }[limit]
    monkeypatch.setattr(html_archive, field, 0)
    with pytest.raises(ContentExportError) as error:
        await HtmlZipExporter().export(bundle, validate=valid)
    assert error.value.code == "ZIP_TOO_LARGE"
    assert list(export_tmp.iterdir()) == []


async def test_two_export_slots_cover_capture_and_live_response_and_queued_cancel(export_tmp):
    exporter = HtmlZipExporter()
    first, second = await asyncio.gather(
        exporter.export(bundle, validate=valid),
        exporter.export(bundle, validate=valid),
    )
    invoked = threading.Event()

    def capture():
        invoked.set()
        return bundle()

    waiting = asyncio.create_task(exporter.export(capture, validate=valid))
    await asyncio.sleep(0.03)
    assert not invoked.is_set()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert not invoked.is_set()
    next_export = asyncio.create_task(exporter.export(capture, validate=valid))
    await first.cleanup()
    third = await asyncio.wait_for(next_export, timeout=5)
    assert invoked.is_set()
    await second.cleanup()
    await third.cleanup()
    assert list(export_tmp.iterdir()) == []


async def test_capture_cancellation_settles_thread_before_releasing_slot_and_cleanup(export_tmp):
    exporter = HtmlZipExporter()
    entered = threading.Event()
    release = threading.Event()

    def blocked_capture():
        entered.set()
        assert release.wait(5)
        return bundle()

    task = asyncio.create_task(exporter.export(blocked_capture, validate=valid))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()  # Repeated cancellation must not race worker/file cleanup.
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert list(export_tmp.iterdir()) == []
    material = await exporter.export(bundle, validate=valid)
    await material.cleanup()


async def test_response_disconnect_finally_cleans_file_and_releases_slot(export_tmp):
    exporter = HtmlZipExporter()
    material = await exporter.export(bundle, validate=valid)
    response = TemporaryArchiveResponse(material)

    async def receive():
        return {"type": "http.disconnect"}

    async def send(_):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await response({"type": "http", "method": "GET", "headers": []}, receive, send)
    assert list(export_tmp.iterdir()) == []
    replacement = await exporter.export(bundle, validate=valid)
    await replacement.cleanup()
