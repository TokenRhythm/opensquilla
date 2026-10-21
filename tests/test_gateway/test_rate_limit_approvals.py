from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.middleware import RateLimitMiddleware
from opensquilla.onboarding.config_store import persist_config


def test_default_allows_shared_ip_file_and_preview_activity() -> None:
    app = Starlette()

    async def ordinary_ui_request(_request):
        return JSONResponse({"ok": True})

    requests = [
        ("GET", "/api/v1/workspace-files/apps"),
        ("GET", "/api/v1/workspace-files/content"),
        ("GET", "/api/v1/workspace-files/page"),
        ("POST", "/api/v1/artifacts/document/preview-leases"),
        ("DELETE", "/api/v1/artifact-preview-leases/lease"),
    ]
    for method, path in requests:
        app.add_route(path, ordinary_ui_request, methods=[method])
    app.add_middleware(RateLimitMiddleware, config=GatewayConfig())

    # Several clients on localhost (or behind one NAT) share the same IP.
    # Real preview switching fans out across all of these routes.
    with TestClient(app) as client:
        for _ in range(30):
            for method, path in requests:
                assert client.request(method, path).status_code == 200


def test_http_rate_limit_environment_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_RATE_ENABLED", "true")
    assert GatewayConfig().rate_limit.enabled is True


def test_existing_http_rate_limit_opt_in_survives_unrelated_save(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[rate_limit]\nenabled = true\nmax_requests = 77\n", encoding="utf-8")
    config = GatewayConfig.load(path, read_only=True)
    assert config.rate_limit.enabled is True
    assert config.rate_limit.max_requests == 77
    config.log_level = "WARNING"
    persist_config(config, path=path, backup=False)

    stored = tomllib.loads(path.read_text(encoding="utf-8"))
    assert stored["rate_limit"] == {"enabled": True, "max_requests": 77}
    assert GatewayConfig.load(path, read_only=True).rate_limit.enabled is True


def test_config_example_does_not_enable_http_request_buckets() -> None:
    example = Path(__file__).resolve().parents[2] / "opensquilla.toml.example"
    config = tomllib.loads(example.read_text(encoding="utf-8"))
    assert config["rate_limit"]["enabled"] is False


@pytest.mark.parametrize("path", ["/api/sessions", "/api/content/read"])
def test_explicit_limit_retry_after_tracks_remaining_window(
    path: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Starlette()

    async def endpoint(_request):
        return JSONResponse({"ok": True})

    app.add_route(path, endpoint, methods=["GET"])
    config = GatewayConfig(rate_limit={
        "enabled": True,
        "max_requests": 1,
        "window_seconds": 60,
        "content_max_requests": 1,
        "content_window_seconds": 60,
    })
    app.add_middleware(RateLimitMiddleware, config=config)
    now = [1000.0]
    monkeypatch.setattr("opensquilla.gateway.middleware.time.time", lambda: now[0])

    with TestClient(app) as client:
        assert client.get(path).status_code == 200
        now[0] += 12.25
        rejected = client.get(path)
        assert rejected.status_code == 429
        assert rejected.headers["retry-after"] == "48"
        now[0] = 1060.0
        assert client.get(path).status_code == 200


def test_explicit_zero_request_limit_returns_429_without_a_prior_request() -> None:
    app = Starlette()
    config = GatewayConfig(rate_limit={"enabled": True, "max_requests": 0})
    app.add_middleware(RateLimitMiddleware, config=config)
    with TestClient(app) as client:
        response = client.get("/api/sessions")
        assert response.status_code == 429
        assert response.headers["retry-after"] == "60"


def test_approval_polling_does_not_consume_generic_api_rate_limit() -> None:
    app = Starlette()

    async def approvals(_request):
        return JSONResponse({"approvals": []})

    async def sessions(_request):
        return JSONResponse({"ok": True})

    app.add_route("/api/approvals", approvals, methods=["GET"])
    app.add_route("/api/sessions", sessions, methods=["GET"])
    config = GatewayConfig()
    config.rate_limit.enabled = True
    config.rate_limit.max_requests = 1
    config.rate_limit.window_seconds = 60
    app.add_middleware(RateLimitMiddleware, config=config)

    with TestClient(app) as client:
        assert client.get("/api/approvals").status_code == 200
        assert client.get("/api/approvals").status_code == 200
        assert client.get("/api/sessions").status_code == 200
        assert client.get("/api/sessions").status_code == 429


def test_content_range_reads_use_a_separate_bounded_lane() -> None:
    app = Starlette()

    async def content(_request):
        return JSONResponse({"ok": True})

    async def sessions(_request):
        return JSONResponse({"ok": True})

    app.add_route("/api/content/read", content, methods=["GET"])
    app.add_route("/api/sessions", sessions, methods=["GET"])
    config = GatewayConfig()
    config.rate_limit.enabled = True
    config.rate_limit.max_requests = 1
    config.rate_limit.window_seconds = 60
    config.rate_limit.content_max_requests = 2
    config.rate_limit.content_window_seconds = 60
    app.add_middleware(RateLimitMiddleware, config=config)

    with TestClient(app) as client:
        # A content range does not consume the generic control/API bucket.
        assert client.get("/api/content/read").status_code == 200
        assert client.get("/api/sessions").status_code == 200
        assert client.get("/api/sessions").status_code == 429

        # The content lane remains bounded and reports its own category.
        assert client.get("/api/content/read").status_code == 200
        limited = client.get("/api/content/read")
        assert limited.status_code == 429
        assert limited.json()["code"] == "CONTENT_RATE_LIMITED"
        assert limited.headers["retry-after"] == "60"
