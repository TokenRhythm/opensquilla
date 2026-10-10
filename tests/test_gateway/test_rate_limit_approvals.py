from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.middleware import RateLimitMiddleware


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
