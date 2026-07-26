"""Middleware pipeline: Auth, RateLimit, ErrorHandling, SecurityHeaders."""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from opensquilla.gateway.config import GatewayConfig

log = structlog.get_logger(__name__)


class AuthMiddleware(BaseHTTPMiddleware):
    """Token-based auth middleware. Skips public paths."""

    PUBLIC_PATHS = {
        "/health",
        "/healthz",
        "/ready",
        "/readyz",
        # These exist only for a Desktop-spawned gateway and authenticate with
        # the per-instance ownership nonce instead of the operator API token.
        "/api/desktop/identity",
        "/api/desktop/shutdown",
    }

    def __init__(self, app: ASGIApp, config: GatewayConfig) -> None:
        super().__init__(app)
        self._config = config

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Skip auth for public endpoints and WebSocket upgrades (WS handles own auth)
        if request.url.path in self.PUBLIC_PATHS or request.url.path.startswith(
            self._config.control_ui.base_path
        ):
            return await call_next(request)  # type: ignore[no-any-return]

        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)  # type: ignore[no-any-return]

        auth_mode = self._config.auth.mode
        if auth_mode == "none":
            return await call_next(request)  # type: ignore[no-any-return]

        if auth_mode == "token":
            token = self._extract_token(request)
            if token != self._config.auth.token:
                return JSONResponse(
                    {"error": "Unauthorized", "code": "UNAUTHORIZED"}, status_code=401
                )

        elif auth_mode == "trusted-proxy":
            proxy = self._config.auth.trusted_proxy
            forwarded_for = request.headers.get("x-forwarded-for", "")
            if proxy and proxy not in forwarded_for:
                return JSONResponse(
                    {"error": "Unauthorized", "code": "UNAUTHORIZED"}, status_code=401
                )

        return await call_next(request)  # type: ignore[no-any-return]

    def _extract_token(self, request: Request) -> str | None:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:]
        token_header = request.headers.get("x-opensquilla-token")
        if token_header:
            return token_header
        return request.query_params.get("token")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple sliding-window rate limiter per client IP."""

    def __init__(self, app: ASGIApp, config: GatewayConfig) -> None:
        super().__init__(app)
        self._config = config
        # {ip: [(timestamp, count), ...]}
        self._windows: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not self._config.rate_limit.enabled:
            return await call_next(request)  # type: ignore[no-any-return]

        # Exempt the Control UI shell + static assets from per-IP rate limiting.
        # The SPA pulls ~30 small files on every page load (CSS, JS, fonts);
        # without this exemption a couple of refreshes from a single LAN device
        # blows past the API bucket and the operator sees a hard 429 on the
        # bare HTML. Mutating endpoints under /api/* are still limited.
        base = self._config.control_ui.base_path
        path = request.url.path
        if base and (path == base or path.startswith(f"{base}/")):
            return await call_next(request)  # type: ignore[no-any-return]
        if request.method == "GET" and path == "/api/approvals":
            return await call_next(request)  # type: ignore[no-any-return]

        client_ip = self._get_client_ip(request)
        now = time.time()
        window = self._config.rate_limit.window_seconds
        max_req = self._config.rate_limit.max_requests

        # Prune old timestamps
        self._windows[client_ip] = [t for t in self._windows[client_ip] if now - t < window]

        if len(self._windows[client_ip]) >= max_req:
            return JSONResponse(
                {"error": "Too Many Requests", "code": "RATE_LIMITED"}, status_code=429
            )

        self._windows[client_ip].append(now)
        return await call_next(request)  # type: ignore[no-any-return]

    def _get_client_ip(self, request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        if request.client:
            return request.client.host
        return "unknown"


class ErrorHandlingMiddleware(BaseHTTPMiddleware):
    """Catch unhandled exceptions and return structured JSON errors."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            return await call_next(request)  # type: ignore[no-any-return]
        except Exception as exc:
            log.error(
                "http.request_failed",
                path=request.url.path,
                method=request.method,
                error=str(exc),
                exc_info=True,
            )
            return JSONResponse(
                {"error": str(exc), "code": "INTERNAL_ERROR"},
                status_code=500,
            )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Inject security headers (CSP, X-Frame-Options, etc.) on Control UI routes."""

    def __init__(self, app: ASGIApp, path_prefix: str = "/control") -> None:
        super().__init__(app)
        self._path_prefix = path_prefix

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response: Response = await call_next(request)  # type: ignore[assignment]
        if request.url.path.startswith(self._path_prefix):
            response.headers["content-security-policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; "
                "connect-src 'self' ws: wss:; "
                "media-src 'self' blob: https:; "
                "font-src 'self' data:;"
            )
            response.headers["x-frame-options"] = "DENY"
            response.headers["x-content-type-options"] = "nosniff"
            response.headers["referrer-policy"] = "strict-origin-when-cross-origin"
        return response
