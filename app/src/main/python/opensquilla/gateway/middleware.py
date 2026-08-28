"""Middleware pipeline: Auth, RateLimit, ErrorHandling, SecurityHeaders."""

from __future__ import annotations

import re
import time
from collections import defaultdict
from collections.abc import Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.origin_guard import (
    forbidden_origin_response,
    request_origin_allowed,
)
from opensquilla.gateway.scopes import is_loopback_address

log = structlog.get_logger(__name__)

_ARTIFACT_PREVIEW_CAPABILITY_PATH_RE = re.compile(
    r"^/api/v1/artifact-preview/[0-9a-f]{32}(?:/|$)"
)
_ARTIFACT_PREVIEW_CONTROL_PATH_RE = re.compile(
    r"^/api/v1/(?:"
    r"artifact-preview-leases/[^/]+(?:/renew)?"
    r"|artifacts/[^/]+/preview-leases"
    r")/?$"
)
_DESKTOP_CANDIDATE_PREVIEW_PATH_RE = re.compile(
    r"^/api/v1/desktop-artifact-candidate-preview(?:/|$)"
)


def _is_artifact_preview_capability_path(path: str) -> bool:
    """Identify bearer resource URLs without matching lease control routes."""
    return _ARTIFACT_PREVIEW_CAPABILITY_PATH_RE.match(path) is not None


def _is_artifact_preview_control_path(path: str) -> bool:
    """Identify preview lease controls whose high-entropy ids must stay out of logs."""
    return _ARTIFACT_PREVIEW_CONTROL_PATH_RE.fullmatch(path) is not None


def _is_desktop_candidate_preview_path(path: str) -> bool:
    return _DESKTOP_CANDIDATE_PREVIEW_PATH_RE.match(path) is not None


def _desktop_candidate_bridge_authorized(request: Request) -> bool:
    if not _is_desktop_candidate_preview_path(request.url.path):
        return False
    peer_ip = request.client.host if request.client is not None else None
    if not is_loopback_address(peer_ip) or request.headers.get("origin") is not None:
        return False
    supplied = request.headers.get("authorization", "")
    if not supplied.startswith("Bearer "):
        return False
    from opensquilla.gateway.desktop_artifact_bridge import (
        desktop_artifact_bridge_token_valid,
    )

    return desktop_artifact_bridge_token_valid(supplied[7:])


_CONTROL_PLANE_PATHS = frozenset({"/health", "/healthz", "/ready", "/readyz"})


def _path_is_at_or_below(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")


def _is_control_plane_path(path: str) -> bool:
    """Return whether *path* belongs to the Gateway control plane."""
    return (
        path in _CONTROL_PLANE_PATHS
        or _path_is_at_or_below(path, "/api")
        or _path_is_at_or_below(path, "/ws")
    )


def _is_control_ui_request(
    request: Request,
    *,
    base_path: str,
    enabled: bool = True,
) -> bool:
    """Match only read-only Control UI documents and assets.

    A root-mounted UI is a catch-all for ordinary browser navigation, but the
    Gateway control plane remains a separate namespace.  In particular,
    ``base_path="/"`` must never turn API authentication or API response
    headers into UI behavior.
    """
    if not enabled or request.method.upper() not in {"GET", "HEAD"}:
        return False
    path = request.url.path
    if _is_control_plane_path(path):
        return False
    normalized_base = base_path.rstrip("/") or "/"
    if normalized_base == "/":
        return path.startswith("/")
    return _path_is_at_or_below(path, normalized_base)


class UnsafeOriginGuardMiddleware(BaseHTTPMiddleware):
    """Reject browser cross-origin mutations before they reach a route.

    Individual high-risk handlers retain their local checks as defense in
    depth, while this shared boundary also protects newly registered POST,
    PUT, PATCH, and DELETE routes. Origin-less native clients and webhooks
    remain compatible.
    """

    _UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    def __init__(self, app: ASGIApp, config: GatewayConfig) -> None:
        super().__init__(app)
        self._config = config

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if (
            request.method.upper() in self._UNSAFE_METHODS
            and not request_origin_allowed(request, self._config)
        ):
            log.warning(
                "gateway.origin_rejected",
                category="unsafe_http_cross_origin",
            )
            return forbidden_origin_response()
        return await call_next(request)  # type: ignore[no-any-return]


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
    PUBLIC_PATH_PREFIXES = ("/api/v1/artifact-preview/",)

    def __init__(self, app: ASGIApp, config: GatewayConfig) -> None:
        super().__init__(app)
        self._config = config

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Skip auth for public endpoints and WebSocket upgrades (WS handles own auth)
        if (
            request.url.path in self.PUBLIC_PATHS
            or request.url.path.startswith(self.PUBLIC_PATH_PREFIXES)
            or _desktop_candidate_bridge_authorized(request)
            or _is_control_ui_request(
                request,
                base_path=self._config.control_ui.base_path,
                enabled=self._config.control_ui.enabled,
            )
        ):
            return await call_next(request)  # type: ignore[no-any-return]

        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)  # type: ignore[no-any-return]

        auth_mode = self._config.auth.mode
        if auth_mode == "none":
            return await call_next(request)  # type: ignore[no-any-return]

        if auth_mode == "token":
            token = self._extract_token(request)
            from opensquilla.gateway.auth import resolve_auth

            peer_ip = request.client.host if request.client is not None else None
            principal = resolve_auth(
                self._config,
                auth_params={"token": token} if token else {},
                role_claim="operator",
                peer_ip=peer_ip,
            )
            if principal is None:
                return JSONResponse(
                    {"error": "Unauthorized", "code": "UNAUTHORIZED"}, status_code=401
                )
            if principal.auth_state == "invalid":
                from opensquilla.gateway.token_store import default_auth_failure_limiter

                await default_auth_failure_limiter().wait_after_failure(
                    peer_ip,
                    principal.token_public_id,
                )
                log.warning(
                    "http.auth_invalid_guest_only",
                    peer_ip=peer_ip,
                    token_public_id=principal.token_public_id,
                )
            if not principal.authenticated:
                return JSONResponse(
                    {"error": "Unauthorized", "code": "UNAUTHORIZED"}, status_code=401
                )
            request.state.principal = principal

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

        path = request.url.path
        if (
            request.method.upper() in {"GET", "HEAD"}
            and _is_artifact_preview_capability_path(path)
        ):
            return await call_next(request)  # type: ignore[no-any-return]

        # Exempt the Control UI shell + static assets from per-IP rate limiting.
        # The SPA pulls ~30 small files on every page load (CSS, JS, fonts);
        # without this exemption a couple of refreshes from a single LAN device
        # blows past the API bucket and the operator sees a hard 429 on the
        # bare HTML. Mutating endpoints under /api/* are still limited.
        if _is_control_ui_request(
            request,
            base_path=self._config.control_ui.base_path,
            enabled=self._config.control_ui.enabled,
        ):
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
            if _is_artifact_preview_capability_path(request.url.path):
                log.error(
                    "http.request_failed",
                    path_class="artifact_preview_capability",
                    method=request.method,
                    category="unhandled_exception",
                )
                return JSONResponse(
                    {"error": "Internal Server Error", "code": "INTERNAL_ERROR"},
                    status_code=500,
                )
            if (
                _is_artifact_preview_control_path(request.url.path)
                or _is_desktop_candidate_preview_path(request.url.path)
            ):
                log.error(
                    "http.request_failed",
                    path_class="artifact_preview_control",
                    method=request.method,
                    category="unhandled_exception",
                )
                return JSONResponse(
                    {"error": "Internal Server Error", "code": "INTERNAL_ERROR"},
                    status_code=500,
                )
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

    def __init__(
        self,
        app: ASGIApp,
        path_prefix: str = "/control",
        *,
        enabled: bool = True,
    ) -> None:
        super().__init__(app)
        self._path_prefix = path_prefix
        self._enabled = enabled

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response: Response = await call_next(request)  # type: ignore[assignment]
        if _is_control_ui_request(
            request,
            base_path=self._path_prefix,
            enabled=self._enabled,
        ):
            response.headers["content-security-policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; "
                "connect-src 'self' ws: wss: http://*.localhost:*; "
                "media-src 'self' blob: https:; "
                "font-src 'self' data:; "
                "frame-src 'self' blob: http://*.localhost:*; "
                "object-src 'none'; "
                "frame-ancestors 'self';"
            )
            response.headers["x-frame-options"] = "DENY"
            response.headers["x-content-type-options"] = "nosniff"
            response.headers["referrer-policy"] = "strict-origin-when-cross-origin"
        return response
