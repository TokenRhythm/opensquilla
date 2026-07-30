"""Pin the Control UI Content-Security-Policy.

The chat surface previews artifacts (notably generated images) by fetching the
authenticated bytes and rendering an object URL (``blob:``) in an ``<img>``.
If ``img-src`` omits ``blob:`` the browser blocks every generated-image
preview while the file still downloads fine — a "the UI lied" failure. The
Workbench also renders trusted, locally fetched PDF/HTML bytes in blob-backed
frames. Browser-grade local previews use a fresh ``p-<token>.localhost`` origin,
and their close-time site-data cleanup is a fetch to that same origin. These
tests pin those narrow exceptions and keep the header scoped to the Control UI
path.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from opensquilla.gateway.middleware import SecurityHeadersMiddleware


def _client(path_prefix: str = "/control") -> TestClient:
    async def ok(_request):  # type: ignore[no-untyped-def]
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/control/ping", ok),
            Route("/other", ok),
            Route("/", ok, methods=["GET", "POST"]),
            Route("/workspace", ok, methods=["GET", "HEAD", "POST"]),
            Route("/api", ok),
            Route("/api/config", ok),
            Route("/ws", ok),
            Route("/health", ok),
            Route("/ready", ok),
        ]
    )
    app.add_middleware(SecurityHeadersMiddleware, path_prefix=path_prefix)
    return TestClient(app)


def test_csp_allows_blob_images_for_artifact_previews() -> None:
    response = _client().get("/control/ping")

    assert response.status_code == 200
    csp = response.headers.get("content-security-policy", "")
    assert "img-src 'self' data: blob:;" in csp, csp
    assert "font-src 'self' data:;" in csp, csp


def test_csp_allows_only_workbench_and_isolated_local_preview_frames() -> None:
    csp = _client().get("/control/ping").headers.get("content-security-policy", "")
    directives = [directive.strip() for directive in csp.split(";") if directive.strip()]

    assert [
        directive for directive in directives if directive.startswith("frame-src ")
    ] == ["frame-src 'self' blob: http://*.localhost:*"], csp
    assert [
        directive for directive in directives if directive.startswith("object-src ")
    ] == ["object-src 'none'"], csp
    assert [
        directive for directive in directives if directive.startswith("frame-ancestors ")
    ] == ["frame-ancestors 'self'"], csp


def test_csp_still_constrains_default_and_connect_sources() -> None:
    csp = _client().get("/control/ping").headers.get("content-security-policy", "")

    # The only cross-origin connect exception is the randomized loopback
    # preview origin used for Clear-Site-Data cleanup. Remote hosts, HTTPS
    # origins, and bare arbitrary schemes remain excluded.
    assert "default-src 'self';" in csp, csp
    assert "connect-src 'self' ws: wss: http://*.localhost:*;" in csp, csp
    assert "blob:" not in csp.split("img-src", 1)[0], csp


def test_csp_allows_only_same_origin_blob_and_https_media() -> None:
    csp = _client().get("/control/ping").headers.get("content-security-policy", "")
    directives = [directive.strip() for directive in csp.split(";") if directive.strip()]

    assert [
        directive for directive in directives if directive.startswith("media-src ")
    ] == ["media-src 'self' blob: https:"], csp


def test_security_headers_scoped_to_control_prefix() -> None:
    response = _client().get("/other")

    assert response.status_code == 200
    assert "content-security-policy" not in response.headers
    assert "x-frame-options" not in response.headers


def test_security_headers_use_path_boundaries_for_non_root_mount() -> None:
    response = _client().get("/control-plane")

    assert response.status_code == 404
    assert "content-security-policy" not in response.headers
    assert "x-frame-options" not in response.headers


def test_root_mount_headers_apply_only_to_read_only_ui_requests() -> None:
    with _client("/") as client:
        for path in ("/", "/workspace", "/other"):
            response = client.get(path)
            assert response.status_code == 200
            assert "content-security-policy" in response.headers
            assert response.headers["x-frame-options"] == "DENY"

        for path in ("/api", "/api/config", "/ws", "/health", "/ready"):
            response = client.get(path)
            assert response.status_code == 200
            assert "content-security-policy" not in response.headers
            assert "x-frame-options" not in response.headers

        mutation = client.post("/workspace")
        assert mutation.status_code == 200
        assert "content-security-policy" not in mutation.headers
        assert "x-frame-options" not in mutation.headers
