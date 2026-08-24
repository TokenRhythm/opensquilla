from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from opensquilla.artifacts import (
    ARTIFACT_BUNDLE_BLOBS_DIR,
    ArtifactBundle,
    ArtifactBundleSourceFile,
    ArtifactStore,
)
from opensquilla.gateway import desktop_artifact_bridge as bridge_module
from opensquilla.gateway.artifact_preview import (
    ArtifactPreviewLeaseService,
    PreviewLeaseExpiredError,
    create_artifact_preview_resource_app,
    register_artifact_preview_routes,
)
from opensquilla.gateway.config import AttachmentsConfig, AuthConfig, GatewayConfig
from opensquilla.gateway.middleware import AuthMiddleware

_SESSION_KEY = "agent:main:webchat:preview"
_SESSION_ID = "session-preview"
_AUTH_HEADERS = {
    "Authorization": "Bearer secret",
    "x-opensquilla-session-key": _SESSION_KEY,
}


@pytest.fixture(autouse=True)
def _isolate_desktop_bridge_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep preview auth tests independent from process-level bridge state."""

    monkeypatch.setattr(bridge_module, "_runtime_client_initialized", False)
    monkeypatch.setattr(bridge_module, "_runtime_client", None)
    monkeypatch.delenv("OPENSQUILLA_DESKTOP_ARTIFACT_BRIDGE_URL", raising=False)
    monkeypatch.delenv("OPENSQUILLA_DESKTOP_ARTIFACT_BRIDGE_TOKEN", raising=False)


class _SessionManager:
    async def get_session(self, session_key: str) -> object | None:
        if session_key == _SESSION_KEY:
            return SimpleNamespace(session_id=_SESSION_ID)
        return None


def _config(tmp_path: Path, *, host: str = "127.0.0.1") -> GatewayConfig:
    return GatewayConfig(
        host=host,
        auth=AuthConfig(mode="token", token="secret"),
        attachments=AttachmentsConfig(media_root=str(tmp_path)),
    )


def _app(
    tmp_path: Path,
    *,
    service: ArtifactPreviewLeaseService | None = None,
    host: str = "127.0.0.1",
    allowed_origins: list[str] | None = None,
) -> tuple[Starlette, ArtifactPreviewLeaseService]:
    config = _config(tmp_path, host=host)
    config.cors.allowed_origins = allowed_origins or []
    app = Starlette(debug=False)
    lease_service = register_artifact_preview_routes(
        app,
        config=config,
        session_manager=_SessionManager(),
        service=service,
    )
    app.add_middleware(AuthMiddleware, config=config)
    return app, lease_service


def _publish_html(tmp_path: Path, payload: bytes | None = None):
    return ArtifactStore(tmp_path).publish_bytes(
        payload or b"<!doctype html><script>window.previewRan=true</script>",
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
        name="index.html",
        mime="text/html",
        source="publish_artifact",
    )


def _create(
    client: TestClient,
    artifact_id: str,
    *,
    mode: str = "offline",
    preview_client: str = "web",
    origin: str | None = "http://127.0.0.1:18791",
):
    headers = dict(_AUTH_HEADERS)
    if origin is not None:
        headers["Origin"] = origin
    return client.post(
        f"/api/v1/artifacts/{artifact_id}/preview-leases",
        json={"version": 1, "mode": mode, "client": preview_client},
        headers=headers,
    )


def test_offline_lease_serves_html_without_gateway_credentials(tmp_path: Path) -> None:
    ref = _publish_html(tmp_path)
    app, _service = _app(tmp_path)

    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        created = _create(client, ref.id)
        assert created.status_code == 201, created.text
        payload = created.json()
        resource = client.get(payload["launch_url"])
        head = client.head(payload["launch_url"])
        ranged = client.get(payload["launch_url"], headers={"Range": "bytes=0-8"})

    assert payload["effective_mode"] == "offline"
    assert payload["source"] == {
        "kind": "single_file",
        "collection_status": "not_applicable",
        "file_count": 1,
        "total_bytes": ref.size,
        "warning_codes": [],
    }
    assert resource.status_code == 200
    assert resource.content.startswith(b"<!doctype")
    assert resource.headers["etag"] == f'"{ref.sha256}"'
    assert resource.headers["referrer-policy"] == "no-referrer"
    assert resource.headers["x-content-type-options"] == "nosniff"
    assert resource.headers["x-dns-prefetch-control"] == "off"
    assert resource.headers["access-control-allow-origin"] == "null"
    assert (
        "connect-src http://127.0.0.1:18791/api/v1/artifact-preview/"
        in resource.headers["content-security-policy"]
    )
    assert "webrtc 'block'" in resource.headers["content-security-policy"]
    assert head.status_code == 200
    assert head.content == b""
    assert ranged.status_code == 206
    assert ranged.content == b"<!doctype"


def test_legacy_single_file_with_local_dependencies_is_explicitly_partial(
    tmp_path: Path,
) -> None:
    ref = _publish_html(
        tmp_path,
        b'<!doctype html><link rel="stylesheet" href="./app.css">'
        b'<script type="module" src="./app.js"></script>',
    )
    app, _service = _app(tmp_path)

    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        payload = _create(client, ref.id).json()
        missing = client.get(
            payload["launch_url"].rsplit("/", 1)[0] + "/app.js",
            headers={"Sec-Fetch-Dest": "script"},
        )

    assert payload["source"] == {
        "kind": "single_file",
        "collection_status": "partial",
        "file_count": 1,
        "total_bytes": ref.size,
        "warning_codes": ["legacy_single_file_dependencies_unavailable"],
    }
    assert missing.status_code == 404


def test_stale_remote_import_bundle_warning_is_revalidated_for_preview(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    stale_remote_import = store.publish_bundle(
        ArtifactBundle(
            entrypoint="index.html",
            files=(
                ArtifactBundleSourceFile(
                    path="index.html",
                    mime="text/html",
                    data=(
                        b"<style>@import url('https://fonts.googleapis.com/css2?family=Inter');"
                        b"</style><h1>Remote font</h1>"
                    ),
                ),
            ),
            collection_status="partial",
            warning_codes=("missing_dependency",),
        ),
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
        name="legacy-remote-font.html",
        mime="text/html",
        source="stale-preview-revalidation-test",
    )
    actual_missing_dependency = store.publish_bundle(
        ArtifactBundle(
            entrypoint="index.html",
            files=(
                ArtifactBundleSourceFile(
                    path="index.html",
                    mime="text/html",
                    data=b"<link rel='stylesheet' href='missing.css'><h1>Incomplete</h1>",
                ),
            ),
            collection_status="partial",
            warning_codes=("missing_dependency",),
        ),
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
        name="actual-missing-dependency.html",
        mime="text/html",
        source="stale-preview-revalidation-test",
    )
    app, _service = _app(tmp_path)

    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        stale_payload = _create(client, stale_remote_import.id).json()
        missing_payload = _create(client, actual_missing_dependency.id).json()

    assert stale_payload["source"] == {
        "kind": "bundle",
        "collection_status": "complete",
        "file_count": 1,
        "total_bytes": stale_remote_import.size,
        "warning_codes": [],
    }
    assert missing_payload["source"] == {
        "kind": "bundle",
        "collection_status": "partial",
        "file_count": 1,
        "total_bytes": actual_missing_dependency.size,
        "warning_codes": ["missing_dependency"],
    }
    unchanged = store.describe_preview_bundle(
        stale_remote_import.id,
        session_id=_SESSION_ID,
    )
    assert unchanged is not None
    assert unchanged.collection_status == "partial"
    assert unchanged.warning_codes == ("missing_dependency",)


def test_full_loopback_lease_uses_isolated_random_localhost_origin(tmp_path: Path) -> None:
    ref = _publish_html(tmp_path)
    app, service = _app(tmp_path)
    service.set_listener_port(43123)

    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        created = _create(client, ref.id, mode="full")

    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["effective_mode"] == "full"
    parsed = urlsplit(payload["launch_url"])
    assert parsed.scheme == "http"
    assert parsed.hostname is not None
    assert parsed.hostname.startswith("p-")
    assert parsed.hostname.endswith(".localhost")
    assert parsed.port == 43123
    assert payload["preview_origin"] == f"http://{parsed.hostname}:43123"

    resource_app = create_artifact_preview_resource_app(service)
    with TestClient(resource_app, base_url=payload["preview_origin"]) as resource_client:
        resource = resource_client.get(parsed.path)
        wrong_host = resource_client.get(
            parsed.path,
            headers={"Host": "p-00000000000000000000000000000000.localhost:43123"},
        )

    assert resource.status_code == 200
    assert "content-security-policy" not in resource.headers
    assert "access-control-allow-origin" not in resource.headers
    assert wrong_host.status_code == 404


def test_desktop_candidate_preview_materialization_is_opaque_and_session_fenced(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ref = _publish_html(tmp_path, b"<!doctype html><h1>candidate</h1>")
    app, service = _app(tmp_path)
    service.set_listener_port(43123)
    handle = "candidate_0123456789abcdef"
    service.register_candidate_preview(
        handle=handle,
        artifact_id=ref.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
    )
    bridge_token = "desktop-bridge-secret"
    monkeypatch.setenv("OPENSQUILLA_DESKTOP_ARTIFACT_BRIDGE_TOKEN", bridge_token)

    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        unauthorized = client.post(
            "/api/v1/desktop-artifact-candidate-preview/resolve",
            json={"version": 1, "candidateHandle": handle},
            headers={"Authorization": "Bearer wrong"},
        )
        resolved = client.post(
            "/api/v1/desktop-artifact-candidate-preview/resolve",
            json={"version": 1, "candidateHandle": handle},
            headers={"Authorization": f"Bearer {bridge_token}"},
        )
        released = client.delete(
            f"/api/v1/desktop-artifact-candidate-preview/{handle}",
            headers={"Authorization": f"Bearer {bridge_token}"},
        )

    assert unauthorized.status_code == 401
    assert resolved.status_code == 200, resolved.text
    payload = resolved.json()
    assert payload["candidate_handle"] == handle
    assert payload["candidate_artifact_id"] == ref.id
    assert payload["scope_id"] == _SESSION_KEY
    # Candidate materialization has no client-controlled mode.  With no
    # canonical lease proving an explicit mode, it must fail closed to offline.
    assert payload["effective_mode"] == "offline"
    assert payload["launch_url"].startswith("http://p-")
    assert ref.id not in handle
    assert released.status_code == 204


def test_candidate_preview_authentication_precedes_request_validation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ref = _publish_html(tmp_path, b"<!doctype html><h1>candidate</h1>")
    app, service = _app(tmp_path)
    service.set_listener_port(43123)
    handle = "candidate_0123456789abcdef"
    service.register_candidate_preview(
        handle=handle,
        artifact_id=ref.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
    )
    bridge_token = "desktop-bridge-secret"
    monkeypatch.setenv("OPENSQUILLA_DESKTOP_ARTIFACT_BRIDGE_TOKEN", bridge_token)

    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        unauthorized = client.post(
            "/api/v1/desktop-artifact-candidate-preview/resolve",
            json={"version": 1, "candidateHandle": handle, "mode": "bogus"},
            headers={"Authorization": "Bearer wrong"},
        )
        malformed = client.post(
            "/api/v1/desktop-artifact-candidate-preview/resolve",
            json={"version": 1, "candidateHandle": handle, "mode": "bogus"},
            headers={"Authorization": f"Bearer {bridge_token}"},
        )

    assert unauthorized.status_code == 401
    assert malformed.status_code == 400


def test_candidate_preview_is_always_offline_even_with_canonical_full(
    tmp_path: Path,
) -> None:
    ref = _publish_html(tmp_path)
    service = ArtifactPreviewLeaseService(config=_config(tmp_path))
    full_lease, _ = service.create(
        artifact_id=ref.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
        mode="full",
        client="desktop",
    )
    handle = "candidate_0123456789abcdef"
    service.register_candidate_preview(
        handle=handle,
        artifact_id=ref.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
    )
    # Candidate HTML is controlled by the model and may contain scripts or
    # interactive elements.  It must not inherit the canonical full-network
    # realm used for ordinary user previews.
    assert service.resolve_candidate_preview(handle).mode == "offline"

    # Canonical leases and a previously materialized candidate lease must not
    # influence the candidate's fail-closed mode.
    offline_lease, _ = service.create(
        artifact_id=ref.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
        mode="offline",
        client="web",
    )
    assert service.resolve_candidate_preview(handle).mode == "offline"
    candidate_lease, _ = service.create(
        artifact_id=ref.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
        mode="full",
        client="desktop",
    )
    service.attach_candidate_lease(handle, candidate_lease.lease_id)
    service.revoke(
        full_lease.lease_id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
    )
    service.revoke(
        offline_lease.lease_id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
    )
    assert service.resolve_candidate_preview(handle).mode == "offline"


def test_retiring_candidate_preview_revokes_materialized_lease(
    tmp_path: Path,
) -> None:
    ref = _publish_html(tmp_path)
    service = ArtifactPreviewLeaseService(config=_config(tmp_path))
    handle = "candidate_0123456789abcdef"
    service.register_candidate_preview(
        handle=handle,
        artifact_id=ref.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
    )
    lease, token = service.create(
        artifact_id=ref.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
        mode="offline",
        client="desktop",
    )
    assert service.attach_candidate_lease(handle, lease.lease_id) is True

    retired = service.retire_candidate_preview(handle)

    assert retired is not None
    assert retired.lease_id == lease.lease_id
    with pytest.raises(PreviewLeaseExpiredError):
        service.resolve_token(token)
    assert service.attach_candidate_lease(handle, lease.lease_id) is False


def test_candidate_lease_attach_is_fenced_to_resolved_binding_identity(
    tmp_path: Path,
) -> None:
    first = _publish_html(tmp_path, b"<!doctype html><h1>first</h1>")
    second = _publish_html(tmp_path, b"<!doctype html><h1>second</h1>")
    service = ArtifactPreviewLeaseService(config=_config(tmp_path))
    handle = "candidate_0123456789abcdef"
    service.register_candidate_preview(
        handle=handle,
        artifact_id=first.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
    )
    resolved = service.resolve_candidate_preview(handle)
    service.register_candidate_preview(
        handle=handle,
        artifact_id=second.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
    )
    lease, _ = service.create(
        artifact_id=first.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
        mode="offline",
        client="desktop",
    )

    assert service.attach_candidate_lease(
        handle,
        lease.lease_id,
        expected_artifact_id=resolved.artifact_id,
        expected_session_id=resolved.session_id,
        expected_session_key=resolved.session_key,
    ) is False
    assert service.resolve_candidate_preview(handle).artifact_id == second.id
    # The caller must revoke a lease when the fenced attach fails; this test
    # also confirms the lease remains independently revocable and is not
    # silently associated with the replacement candidate mapping.
    service.revoke(
        lease.lease_id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
    )


def test_replacing_candidate_binding_revokes_previous_materialized_lease(
    tmp_path: Path,
) -> None:
    first = _publish_html(tmp_path, b"<!doctype html><h1>first</h1>")
    second = _publish_html(tmp_path, b"<!doctype html><h1>second</h1>")
    service = ArtifactPreviewLeaseService(config=_config(tmp_path))
    handle = "candidate_0123456789abcdef"
    service.register_candidate_preview(
        handle=handle,
        artifact_id=first.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
    )
    lease, token = service.create(
        artifact_id=first.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
        mode="offline",
        client="desktop",
    )
    assert service.attach_candidate_lease(handle, lease.lease_id) is True

    service.register_candidate_preview(
        handle=handle,
        artifact_id=second.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
    )

    assert service.resolve_candidate_preview(handle).artifact_id == second.id
    with pytest.raises(PreviewLeaseExpiredError):
        service.resolve_token(token)


def test_candidate_lease_attach_requires_candidate_offline_mode(
    tmp_path: Path,
) -> None:
    ref = _publish_html(tmp_path)
    service = ArtifactPreviewLeaseService(config=_config(tmp_path))
    canonical, _ = service.create(
        artifact_id=ref.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
        mode="full",
        client="desktop",
    )
    handle = "candidate_0123456789abcdef"
    service.register_candidate_preview(
        handle=handle,
        artifact_id=ref.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
    )
    resolved = service.resolve_candidate_preview(handle)
    assert resolved.mode == "offline"
    service.revoke(
        canonical.lease_id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
    )
    candidate, _ = service.create(
        artifact_id=ref.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
        mode="offline",
        client="desktop",
    )

    assert service.attach_candidate_lease(
        handle,
        candidate.lease_id,
        expected_artifact_id=resolved.artifact_id,
        expected_session_id=resolved.session_id,
        expected_session_key=resolved.session_key,
        expected_mode=resolved.mode,
    ) is True
    service.revoke(
        candidate.lease_id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
    )
    assert service.resolve_candidate_preview(handle).mode == "offline"


def test_candidate_lease_attach_rejects_revoked_or_cross_scope_lease(
    tmp_path: Path,
) -> None:
    first = _publish_html(tmp_path, b"<!doctype html><h1>first</h1>")
    second = _publish_html(tmp_path, b"<!doctype html><h1>second</h1>")
    service = ArtifactPreviewLeaseService(config=_config(tmp_path))
    handle = "candidate_0123456789abcdef"
    service.register_candidate_preview(
        handle=handle,
        artifact_id=first.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
    )
    wrong_artifact, _ = service.create(
        artifact_id=second.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
        mode="offline",
        client="desktop",
    )
    assert service.attach_candidate_lease(handle, wrong_artifact.lease_id) is False
    service.revoke(
        wrong_artifact.lease_id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
    )
    matching, _ = service.create(
        artifact_id=first.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
        mode="offline",
        client="desktop",
    )
    service.revoke(
        matching.lease_id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
    )
    assert service.attach_candidate_lease(handle, matching.lease_id) is False


def test_candidate_preview_endpoint_uses_canonical_mode_not_request_input(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ref = _publish_html(tmp_path, b"<!doctype html><h1>candidate</h1>")
    app, service = _app(tmp_path)
    service.set_listener_port(43123)
    service.create(
        artifact_id=ref.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
        mode="full",
        client="desktop",
    )
    handle = "candidate_0123456789abcdef"
    service.register_candidate_preview(
        handle=handle,
        artifact_id=ref.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
    )
    bridge_token = "desktop-bridge-secret"
    monkeypatch.setenv("OPENSQUILLA_DESKTOP_ARTIFACT_BRIDGE_TOKEN", bridge_token)

    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        rejected = client.post(
            "/api/v1/desktop-artifact-candidate-preview/resolve",
            # The bridge protocol is intentionally opaque and does not accept
            # a client-controlled mode field.
            json={"version": 1, "candidateHandle": handle, "mode": "offline"},
            headers={"Authorization": f"Bearer {bridge_token}"},
        )
        resolved = client.post(
            "/api/v1/desktop-artifact-candidate-preview/resolve",
            json={"version": 1, "candidateHandle": handle},
            headers={"Authorization": f"Bearer {bridge_token}"},
        )

    assert rejected.status_code == 400
    assert resolved.status_code == 200, resolved.text
    # The request cannot opt into full mode, and neither can a canonical full
    # lease: model-controlled candidate previews are always offline.
    assert resolved.json()["effective_mode"] == "offline"


def test_candidate_preview_force_offline_overrides_canonical_full(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ref = _publish_html(tmp_path)
    service = ArtifactPreviewLeaseService(config=_config(tmp_path))
    service.create(
        artifact_id=ref.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
        mode="full",
        client="desktop",
    )
    monkeypatch.setenv("OPENSQUILLA_PREVIEW_FORCE_OFFLINE", "1")
    handle = "candidate_0123456789abcdef"
    service.register_candidate_preview(
        handle=handle,
        artifact_id=ref.id,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
    )
    assert service.resolve_candidate_preview(handle).mode == "offline"


def test_https_loopback_webui_can_use_full_preview_mode(tmp_path: Path) -> None:
    ref = _publish_html(tmp_path)
    app, service = _app(tmp_path)
    service.set_listener_port(43123)

    with TestClient(
        app,
        base_url="https://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        created = _create(
            client,
            ref.id,
            mode="full",
            origin="https://127.0.0.1:18791",
        )

    assert created.status_code == 201, created.text
    assert created.json()["effective_mode"] == "full"
    assert created.json()["launch_url"].startswith("http://p-")


def test_full_web_preview_origin_can_clear_site_data_before_revoke(tmp_path: Path) -> None:
    ref = _publish_html(tmp_path)
    app, service = _app(tmp_path)
    service.set_listener_port(43123)

    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        payload = _create(client, ref.id, mode="full").json()

    assert payload["preview_origin"]
    resource_app = create_artifact_preview_resource_app(service)
    with TestClient(resource_app, base_url=payload["preview_origin"]) as resource_client:
        cleared = resource_client.get("/.opensquilla/clear-site-data")

    assert cleared.status_code == 204
    assert cleared.headers["clear-site-data"] == '"cache", "cookies", "storage"'
    assert cleared.headers["cache-control"] == "no-store"


def test_public_web_request_is_rejected_before_preview_creation(tmp_path: Path) -> None:
    ref = _publish_html(tmp_path)
    app, service = _app(
        tmp_path,
        host="0.0.0.0",
        allowed_origins=["https://gateway.example"],
    )
    service.set_listener_port(43123)

    with TestClient(
        app,
        base_url="https://gateway.example",
        client=("203.0.113.8", 51000),
    ) as client:
        created = _create(
            client,
            ref.id,
            mode="full",
            origin="https://gateway.example",
        )

    assert created.status_code == 401


def test_desktop_originless_loopback_request_can_use_full_mode(tmp_path: Path) -> None:
    ref = _publish_html(tmp_path)
    app, service = _app(tmp_path)
    service.set_listener_port(43123)

    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        created = _create(
            client,
            ref.id,
            mode="full",
            preview_client="desktop",
            origin=None,
        )

    assert created.status_code == 201
    assert created.json()["effective_mode"] == "full"


def test_desktop_offline_uses_loopback_transport_and_serves_resources(
    tmp_path: Path,
) -> None:
    ref = _publish_html(tmp_path)
    app, service = _app(tmp_path)
    service.set_listener_port(43123)

    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        created = _create(
            client,
            ref.id,
            mode="offline",
            preview_client="desktop",
            origin=None,
        )

    assert created.status_code == 201
    payload = created.json()
    assert payload["effective_mode"] == "offline"
    assert payload["launch_url"].startswith("http://p-")
    parsed = urlsplit(payload["launch_url"])
    assert payload["preview_origin"] == f"{parsed.scheme}://{parsed.netloc}"

    resource_app = create_artifact_preview_resource_app(service)
    with TestClient(resource_app, base_url=payload["preview_origin"]) as resource_client:
        response = resource_client.get(parsed.path)

    assert response.status_code == 200
    assert "connect-src 'self' data: blob:" in response.headers["content-security-policy"]
    assert "webrtc 'block'" in response.headers["content-security-policy"]
    assert response.headers["x-dns-prefetch-control"] == "off"
    assert "access-control-allow-origin" not in response.headers


def test_force_offline_keeps_desktop_on_loopback_transport(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_PREVIEW_FORCE_OFFLINE", "1")
    ref = _publish_html(tmp_path)
    app, service = _app(tmp_path)
    service.set_listener_port(43123)

    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        created = _create(
            client,
            ref.id,
            mode="full",
            preview_client="desktop",
            origin=None,
        )

    assert created.status_code == 201
    assert created.json()["effective_mode"] == "offline"
    assert created.json()["launch_url"].startswith("http://p-")


def test_create_rejects_cross_origin_before_allocating_lease(tmp_path: Path) -> None:
    ref = _publish_html(tmp_path)
    app, service = _app(tmp_path)

    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        rejected = _create(client, ref.id, origin="https://evil.example")
        accepted = _create(client, ref.id)

    assert rejected.status_code == 403
    assert rejected.json()["code"] == "FORBIDDEN_ORIGIN"
    assert accepted.status_code == 201
    assert len(service._leases_by_id) == 1


def test_lease_limit_is_scoped_per_session(tmp_path: Path) -> None:
    ref = _publish_html(tmp_path)
    app, _service = _app(tmp_path)

    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        responses = [_create(client, ref.id) for _ in range(9)]

    assert [response.status_code for response in responses[:8]] == [201] * 8
    assert responses[8].status_code == 429
    assert responses[8].json()["code"] == "PREVIEW_LEASE_LIMIT"


def test_renew_and_delete_require_the_original_session_scope(tmp_path: Path) -> None:
    ref = _publish_html(tmp_path)
    app, _service = _app(tmp_path)

    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        created = _create(client, ref.id)
        payload = created.json()
        wrong = client.post(
            f"/api/v1/artifact-preview-leases/{payload['lease_id']}/renew",
            headers={
                "Authorization": "Bearer secret",
                "x-opensquilla-session-key": "agent:main:webchat:other",
                "Origin": "http://127.0.0.1:18791",
            },
        )
        renewed = client.post(
            f"/api/v1/artifact-preview-leases/{payload['lease_id']}/renew",
            headers={**_AUTH_HEADERS, "Origin": "http://127.0.0.1:18791"},
        )
        deleted = client.delete(
            f"/api/v1/artifact-preview-leases/{payload['lease_id']}",
            headers={**_AUTH_HEADERS, "Origin": "http://127.0.0.1:18791"},
        )
        expired_resource = client.get(payload["launch_url"])

    assert wrong.status_code == 404
    assert renewed.status_code == 200
    assert renewed.json()["lease_id"] == payload["lease_id"]
    assert set(renewed.json()) == {"version", "lease_id", "expires_at"}
    assert deleted.status_code == 204
    assert expired_resource.status_code == 410
    assert expired_resource.json()["code"] == "PREVIEW_LEASE_EXPIRED"


def test_idle_expiry_returns_gone_while_unknown_tokens_return_not_found(tmp_path: Path) -> None:
    now = [1_700_000_000.0]
    config = _config(tmp_path)
    service = ArtifactPreviewLeaseService(config=config, idle_seconds=10, clock=lambda: now[0])
    app, _service = _app(tmp_path, service=service)
    ref = _publish_html(tmp_path)

    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        payload = _create(client, ref.id).json()
        now[0] += 11
        expired = client.get(payload["launch_url"])
        unknown = client.get(
            "/api/v1/artifact-preview/00000000000000000000000000000000/index.html"
        )

    assert expired.status_code == 410
    assert unknown.status_code == 404


def test_resource_integrity_failure_is_409_and_does_not_leak_paths(tmp_path: Path) -> None:
    ref = _publish_html(tmp_path)
    app, _service = _app(tmp_path)

    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        payload = _create(client, ref.id).json()
        ArtifactStore(tmp_path).path_for(ref).write_bytes(b"tampered")
        response = client.get(payload["launch_url"])

    assert response.status_code == 409
    assert response.json() == {
        "error": "Artifact integrity check failed",
        "code": "INTEGRITY_ERROR",
    }
    assert str(tmp_path) not in response.text


def test_missing_manifest_entry_blob_rejects_lease_as_integrity_failure(
    tmp_path: Path,
) -> None:
    bundle = ArtifactBundle(
        entrypoint="index.html",
        files=(
            ArtifactBundleSourceFile(
                path="index.html",
                mime="text/html",
                data=b"<p>entry</p>",
            ),
        ),
    )
    store = ArtifactStore(tmp_path)
    ref = store.publish_bundle(
        bundle,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
        name="index.html",
        mime="text/html",
        source="publish_artifact",
    )
    manifest = store.describe_preview_bundle(ref.id, session_id=_SESSION_ID)
    assert manifest is not None
    entry = next(item for item in manifest.files if item.path == manifest.entrypoint)
    (
        store.path_for(ref).parent
        / ARTIFACT_BUNDLE_BLOBS_DIR
        / entry.sha256
    ).unlink()
    app, _service = _app(tmp_path)

    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        response = _create(client, ref.id)

    assert response.status_code == 409
    assert response.json()["code"] == "INTEGRITY_ERROR"


def test_corrupt_non_entry_bundle_member_rejects_lease_before_issuance(
    tmp_path: Path,
) -> None:
    bundle = ArtifactBundle(
        entrypoint="index.html",
        files=(
            ArtifactBundleSourceFile(
                path="assets/app.js",
                mime="text/javascript",
                data=b"window.bundleLoaded = true",
            ),
            ArtifactBundleSourceFile(
                path="index.html",
                mime="text/html",
                data=b'<script src="./assets/app.js"></script>',
            ),
        ),
    )
    store = ArtifactStore(tmp_path)
    ref = store.publish_bundle(
        bundle,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
        name="index.html",
        mime="text/html",
        source="publish_artifact",
    )
    manifest = store.describe_preview_bundle(ref.id, session_id=_SESSION_ID)
    assert manifest is not None
    member = next(item for item in manifest.files if item.path == "assets/app.js")
    (
        store.path_for(ref).parent
        / ARTIFACT_BUNDLE_BLOBS_DIR
        / member.sha256
    ).write_bytes(b"tampered")
    app, service = _app(tmp_path)

    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        response = _create(client, ref.id)

    assert response.status_code == 409
    assert response.json()["code"] == "INTEGRITY_ERROR"
    assert service._leases_by_id == {}


def test_spa_fallback_applies_only_to_document_navigation(tmp_path: Path) -> None:
    ref = _publish_html(tmp_path)
    app, _service = _app(tmp_path)

    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        payload = _create(client, ref.id).json()
        token = payload["launch_url"].split("/")[4]
        document = client.get(
            f"/api/v1/artifact-preview/{token}/dashboard/settings",
            headers={"Accept": "text/html"},
        )
        asset = client.get(
            f"/api/v1/artifact-preview/{token}/missing.js",
            headers={"Accept": "*/*"},
        )
        misleading_accept = client.get(
            f"/api/v1/artifact-preview/{token}/missing-module.js",
            headers={"Accept": "text/html", "Sec-Fetch-Dest": "script"},
        )
        navigate = client.get(
            f"/api/v1/artifact-preview/{token}/client-route",
            headers={"Accept": "*/*", "Sec-Fetch-Mode": "navigate"},
        )

    assert document.status_code == 200
    assert document.content.startswith(b"<!doctype")
    assert asset.status_code == 404
    assert misleading_accept.status_code == 404
    assert navigate.status_code == 200


def test_bundle_manifest_resources_are_served_by_logical_path(tmp_path: Path) -> None:
    bundle = ArtifactBundle(
        entrypoint="index.html",
        files=(
            ArtifactBundleSourceFile(
                path="assets/app.js",
                mime="text/javascript",
                data=b"window.bundleLoaded = true",
            ),
            ArtifactBundleSourceFile(
                path="index.html",
                mime="text/html",
                data=b'<script type="module" src="./assets/app.js"></script>',
            ),
        ),
        collection_status="partial",
        warning_codes=("DYNAMIC_REFERENCE",),
    )
    ref = ArtifactStore(tmp_path).publish_bundle(
        bundle,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
        name="index.html",
        mime="text/html",
        source="publish_artifact",
    )
    app, _service = _app(tmp_path)

    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        payload = _create(client, ref.id).json()
        token = payload["launch_url"].split("/")[4]
        script = client.get(
            f"/api/v1/artifact-preview/{token}/assets/app.js",
            headers={"Accept": "*/*"},
        )

    assert payload["source"] == {
        "kind": "bundle",
        "collection_status": "partial",
        "file_count": 2,
        "total_bytes": 79,
        "warning_codes": ["DYNAMIC_REFERENCE"],
    }
    assert script.status_code == 200
    assert script.content == b"window.bundleLoaded = true"
    assert script.headers["content-type"].startswith("text/javascript")
    assert "connect-src " in script.headers["content-security-policy"]
    assert "webrtc 'block'" in script.headers["content-security-policy"]
    assert script.headers["x-dns-prefetch-control"] == "off"
    assert script.headers["permissions-policy"].startswith("camera=()")


def test_unknown_bundle_version_is_not_misreported_as_a_complete_preview(
    tmp_path: Path,
) -> None:
    bundle = ArtifactBundle(
        entrypoint="index.html",
        files=(
            ArtifactBundleSourceFile(
                path="index.html",
                mime="text/html",
                data=b"<p>future</p>",
            ),
        ),
    )
    store = ArtifactStore(tmp_path)
    ref = store.publish_bundle(
        bundle,
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
        name="index.html",
        mime="text/html",
        source="publish_artifact",
    )
    manifest_path = store.path_for(ref).parent / "bundle.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = 999
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    app, _service = _app(tmp_path)

    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        response = _create(client, ref.id)

    assert response.status_code == 409
    assert response.json()["code"] == "BUNDLE_UNSUPPORTED"
