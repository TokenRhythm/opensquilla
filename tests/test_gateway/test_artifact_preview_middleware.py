from __future__ import annotations

import structlog
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from opensquilla.gateway.config import AuthConfig, ControlUiConfig, GatewayConfig
from opensquilla.gateway.middleware import (
    AuthMiddleware,
    ErrorHandlingMiddleware,
    RateLimitMiddleware,
)

_CAPABILITY_TOKEN = "0123456789abcdef0123456789abcdef"
_CAPABILITY_PATH = f"/api/v1/artifact-preview/{_CAPABILITY_TOKEN}/index.html"


def _rate_limited_app() -> Starlette:
    async def ok(_request) -> Response:
        return JSONResponse({"ok": True})

    config = GatewayConfig()
    config.rate_limit.enabled = True
    config.rate_limit.max_requests = 1
    config.rate_limit.window_seconds = 60
    app = Starlette(
        routes=[
            Route(
                "/api/v1/artifact-preview/{token}/{resource_path:path}",
                ok,
                methods=["GET", "HEAD", "POST"],
            ),
            Route(
                "/api/v1/artifacts/{artifact_id}/preview-leases",
                ok,
                methods=["POST"],
            ),
            Route(
                "/api/v1/artifact-preview-leases/{lease_id}/renew",
                ok,
                methods=["POST"],
            ),
            Route(
                "/api/v1/artifact-preview-leases/{lease_id}",
                ok,
                methods=["DELETE"],
            ),
        ],
    )
    app.add_middleware(RateLimitMiddleware, config=config)
    return app


def test_preview_capability_get_and_head_do_not_consume_api_rate_limit() -> None:
    app = _rate_limited_app()

    with TestClient(app) as client:
        assert client.get(_CAPABILITY_PATH).status_code == 200
        assert client.head(_CAPABILITY_PATH).status_code == 200
        assert client.get(_CAPABILITY_PATH).status_code == 200

        control_path = "/api/v1/artifacts/art-test/preview-leases"
        assert client.post(control_path).status_code == 200
        assert client.post(control_path).status_code == 429


def test_preview_lease_controls_and_non_read_capability_methods_remain_limited() -> None:
    cases = (
        ("POST", "/api/v1/artifacts/art-test/preview-leases"),
        ("POST", "/api/v1/artifact-preview-leases/apl-test/renew"),
        ("DELETE", "/api/v1/artifact-preview-leases/apl-test"),
        ("POST", _CAPABILITY_PATH),
        ("GET", "/api/v1/artifact-preview/not-a-capability/index.html"),
    )

    for method, path in cases:
        app = _rate_limited_app()
        with TestClient(app) as client:
            assert client.request(method, path).status_code == 200
            assert client.request(method, path).status_code == 429


def test_preview_capability_catchall_redacts_bearer_path_and_exception() -> None:
    local_path = "/private/synthetic/operator/artifacts/meta.json"
    exception_message = f"synthetic preview failure at {local_path}"

    async def boom(_request) -> Response:
        raise OSError(exception_message)

    app = Starlette(
        routes=[
            Route(
                "/api/v1/artifact-preview/{token}/{resource_path:path}",
                boom,
                methods=["GET"],
            )
        ],
        middleware=[Middleware(ErrorHandlingMiddleware)],
    )

    with structlog.testing.capture_logs() as captured:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(_CAPABILITY_PATH)

    assert response.status_code == 500
    assert response.json() == {
        "error": "Internal Server Error",
        "code": "INTERNAL_ERROR",
    }
    serialized_logs = repr(captured)
    assert _CAPABILITY_TOKEN not in serialized_logs
    assert _CAPABILITY_PATH not in serialized_logs
    assert exception_message not in serialized_logs
    assert local_path not in serialized_logs
    assert captured == [
        {
            "category": "unhandled_exception",
            "event": "http.request_failed",
            "log_level": "error",
            "method": "GET",
            "path_class": "artifact_preview_capability",
        }
    ]


def test_preview_control_catchall_redacts_lease_id_and_exception() -> None:
    lease_id = "apl-synthetic-high-entropy-lease"
    control_path = f"/api/v1/artifact-preview-leases/{lease_id}/renew"
    exception_message = "synthetic preview control failure with private material"

    async def boom(_request) -> Response:
        raise OSError(exception_message)

    app = Starlette(
        routes=[
            Route(
                "/api/v1/artifact-preview-leases/{lease_id}/renew",
                boom,
                methods=["POST"],
            )
        ],
        middleware=[Middleware(ErrorHandlingMiddleware)],
    )

    with structlog.testing.capture_logs() as captured:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(control_path)

    assert response.status_code == 500
    assert response.json() == {
        "error": "Internal Server Error",
        "code": "INTERNAL_ERROR",
    }
    serialized_logs = repr(captured)
    assert lease_id not in serialized_logs
    assert control_path not in serialized_logs
    assert exception_message not in serialized_logs
    assert captured == [
        {
            "category": "unhandled_exception",
            "event": "http.request_failed",
            "log_level": "error",
            "method": "POST",
            "path_class": "artifact_preview_control",
        }
    ]


def _root_mounted_token_app() -> Starlette:
    async def ok(_request) -> Response:
        return JSONResponse({"ok": True})

    config = GatewayConfig(
        auth=AuthConfig(mode="token", token="secret"),
        control_ui=ControlUiConfig(base_path="/"),
    )
    app = Starlette(
        routes=[
            Route("/", ok, methods=["GET", "HEAD", "POST"]),
            Route("/workspace", ok, methods=["GET", "HEAD", "POST"]),
            Route("/api", ok, methods=["GET"]),
            Route("/api/config", ok, methods=["GET"]),
            Route(
                "/api/v1/artifacts/{artifact_id}/preview-leases",
                ok,
                methods=["POST"],
            ),
            Route("/health", ok, methods=["GET"]),
            Route("/ready", ok, methods=["GET"]),
        ]
    )
    app.add_middleware(AuthMiddleware, config=config)
    return app


def test_root_mounted_control_ui_does_not_bypass_token_auth_for_api() -> None:
    protected = (
        ("GET", "/api"),
        ("GET", "/api/config"),
        ("POST", "/api/v1/artifacts/art-test/preview-leases"),
    )

    with TestClient(_root_mounted_token_app()) as client:
        for method, path in protected:
            assert client.request(method, path).status_code == 401
            assert (
                client.request(
                    method,
                    path,
                    headers={"Authorization": "Bearer wrong"},
                ).status_code
                == 401
            )
            assert (
                client.request(
                    method,
                    path,
                    headers={"Authorization": "Bearer secret"},
                ).status_code
                == 200
            )


def test_root_mounted_control_ui_only_exempts_read_only_ui_requests() -> None:
    with TestClient(_root_mounted_token_app()) as client:
        assert client.get("/").status_code == 200
        assert client.head("/workspace").status_code == 200
        assert client.post("/workspace").status_code == 401
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200
