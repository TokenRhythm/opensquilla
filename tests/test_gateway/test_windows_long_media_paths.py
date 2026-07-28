from __future__ import annotations

import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.artifacts import (
    ArtifactBundle,
    ArtifactBundleSourceFile,
    ArtifactStore,
)
from opensquilla.attachment_refs import (
    copy_transcript_material,
    make_attachment_ref,
    read_attachment_ref_bytes,
    write_transcript_material,
)
from opensquilla.paths import native_io_path

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows long-path regression")


class _SessionManager:
    async def get_session(self, session_key: str) -> object | None:
        if session_key == "agent:main:webchat:long":
            return SimpleNamespace(session_id="session-parent")
        return None


def _long_media_root(tmp_path: Path) -> Path:
    segment = "media-segment-" + ("x" * 36)
    root = tmp_path.joinpath(segment, segment, segment, segment)
    assert len(os.fspath(root)) > 260
    return root


def _app(media_root: Path):
    pytest.importorskip("starlette.testclient")
    from starlette.applications import Starlette

    from opensquilla.gateway.artifacts import register_artifact_routes
    from opensquilla.gateway.attachments import register_attachment_routes
    from opensquilla.gateway.config import AttachmentsConfig, AuthConfig, GatewayConfig
    from opensquilla.gateway.middleware import AuthMiddleware

    config = GatewayConfig(
        auth=AuthConfig(mode="token", token="secret"),
        attachments=AttachmentsConfig(media_root=str(media_root)),
    )
    app = Starlette(debug=False)
    register_attachment_routes(
        app,
        config=config,
        session_manager=_SessionManager(),
    )
    register_artifact_routes(
        app,
        config=config,
        session_manager=_SessionManager(),
    )
    app.add_middleware(AuthMiddleware, config=config)
    return app


def test_long_media_roundtrips_material_artifacts_and_downloads(tmp_path: Path) -> None:
    pytest.importorskip("starlette.testclient")
    from starlette.testclient import TestClient

    media_root = _long_media_root(tmp_path)
    try:
        attachment_payload = b"long path attachment"
        sha, attachment_path, wrote = write_transcript_material(
            media_root=media_root,
            session_id="session-parent",
            payload=attachment_payload,
            disk_budget_bytes=1024,
        )
        assert wrote is True
        assert not os.fspath(attachment_path).startswith("\\\\?\\")
        attachment_ref = make_attachment_ref(
            sha256=sha,
            name="attachment.txt",
            mime="text/plain",
            size=len(attachment_payload),
            session_id="session-parent",
            source="upload",
        )
        assert (
            read_attachment_ref_bytes(attachment_ref, media_root=media_root)
            == attachment_payload
        )
        assert (
            copy_transcript_material(
                media_root=media_root,
                source_session_id="session-parent",
                target_session_id="session-child",
            )
            == 1
        )
        child_attachment_ref = {**attachment_ref, "scope": "session-child"}
        assert (
            read_attachment_ref_bytes(child_attachment_ref, media_root=media_root)
            == attachment_payload
        )

        store = ArtifactStore(media_root)
        artifact_payload = b"long path artifact"
        artifact = store.publish_bytes(
            artifact_payload,
            session_id="session-parent",
            session_key="agent:main:webchat:long",
            name="report.txt",
            mime="text/plain",
            source="publish_artifact",
        )
        resolved_ref, artifact_path = store.resolve_for_download(
            artifact.id,
            session_id="session-parent",
        )
        assert resolved_ref == artifact
        assert not os.fspath(artifact_path).startswith("\\\\?\\")
        assert (
            store.find_existing_ref(
                session_id="session-parent",
                session_key="agent:main:webchat:long",
                sha256=artifact.sha256,
                name="report.txt",
                mime="text/plain",
            )
            == artifact
        )
        assert (
            store.copy_session_artifacts(
                source_session_id="session-parent",
                target_session_id="session-child",
                target_session_key="agent:main:webchat:child",
            )
            == 1
        )
        child_artifact, _child_path = store.resolve_for_download(
            artifact.id,
            session_id="session-child",
        )
        assert child_artifact.session_key == "agent:main:webchat:child"

        bundle = ArtifactBundle(
            entrypoint="index.html",
            files=(
                ArtifactBundleSourceFile(
                    path="assets/app.js",
                    mime="text/javascript",
                    data=b"window.longPathBundle = true",
                ),
                ArtifactBundleSourceFile(
                    path="index.html",
                    mime="text/html",
                    data=b'<script src="./assets/app.js"></script>',
                ),
            ),
        )
        bundle_ref = store.publish_bundle(
            bundle,
            session_id="session-parent",
            session_key="agent:main:webchat:long",
            name="index.html",
            mime="text/html",
            source="publish_artifact",
        )
        bundle_resource = store.resolve_preview_resource(
            bundle_ref.id,
            session_id="session-parent",
            logical_path="assets/app.js",
        )
        assert bundle_resource.path.read_bytes() == b"window.longPathBundle = true"
        assert (
            store.copy_session_artifacts(
                source_session_id="session-parent",
                target_session_id="session-bundle-child",
                target_session_key="agent:main:webchat:bundle-child",
            )
            == 2
        )
        child_bundle_resource = store.resolve_preview_resource(
            bundle_ref.id,
            session_id="session-bundle-child",
            logical_path="assets/app.js",
        )
        assert child_bundle_resource.path.read_bytes() == b"window.longPathBundle = true"

        with TestClient(_app(media_root)) as client:
            attachment_response = client.get(
                f"/api/v1/attachments/{sha}"
                "?sessionKey=agent:main:webchat:long&name=attachment.txt&mime=text/plain",
                headers={"Authorization": "Bearer secret"},
            )
            artifact_response = client.get(
                f"/api/v1/artifacts/{artifact.id}"
                "?sessionKey=agent:main:webchat:long",
                headers={"Authorization": "Bearer secret"},
            )

        assert attachment_response.status_code == 200
        assert attachment_response.content == attachment_payload
        assert artifact_response.status_code == 200
        assert artifact_response.content == artifact_payload
    finally:
        shutil.rmtree(native_io_path(media_root), ignore_errors=True)
