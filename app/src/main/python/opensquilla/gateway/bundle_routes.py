"""Owner-gated HTTP route serving the diagnostics bundle zip.

Deliberately an HTTP route rather than an RPC method: the RPC transport is
WebSocket JSON and cannot stream zip bytes. Auth posture matches an
admin-scoped RPC — AuthMiddleware enforces the gateway token in token mode
(the path is outside the control-UI prefix), and the handler additionally
requires an owner principal so open-mode remote peers are rejected.
"""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import structlog
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.origin_guard import (
    forbidden_origin_response,
)
from opensquilla.gateway.origin_guard import (
    request_origin_allowed as _request_origin_allowed,
)
from opensquilla.gateway.origin_guard import (
    request_principal_is_owner as _request_principal_is_owner,
)

log = structlog.get_logger(__name__)

_DEFAULT_DAYS = 3
_MIN_DAYS = 1
_MAX_DAYS = 30


def _clamped_days(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return _DEFAULT_DAYS
    if isinstance(value, float) and not math.isfinite(value):
        return _DEFAULT_DAYS
    return max(_MIN_DAYS, min(int(value), _MAX_DAYS))


def register_bundle_routes(app: Starlette, *, config: GatewayConfig) -> None:
    """Register POST /api/v1/diagnostics/bundle on the given Starlette app."""

    async def bundle_handler(request: Request) -> FileResponse | JSONResponse:
        if not _request_origin_allowed(request, config):
            return forbidden_origin_response()
        if not _request_principal_is_owner(config, request):
            return JSONResponse(
                {"error": "Owner privileges required", "code": "OWNER_REQUIRED"},
                status_code=403,
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        include_content = body.get("include_content") is True
        days = _clamped_days(body.get("days", _DEFAULT_DAYS))

        # Imported at call time so the gateway app boots without pulling in
        # the collector, and tests can monkeypatch the module attribute.
        from opensquilla.observability import bundle as bundle_module

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        tmp_dir = Path(tempfile.mkdtemp(prefix="opensquilla-bundle-"))
        dest = tmp_dir / f"opensquilla-bundle-{stamp}.zip"

        def _cleanup() -> None:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        try:
            await asyncio.to_thread(
                bundle_module.collect_bundle,
                dest,
                days=days,
                include_content=include_content,
            )
        except Exception as exc:
            # Full traceback goes to the server log only — never the response.
            log.error("bundle_route.generation_failed", error=str(exc), exc_info=True)
            _cleanup()
            return JSONResponse(
                {"error": "Bundle generation failed", "code": "INTERNAL_ERROR"},
                status_code=500,
            )

        return FileResponse(
            os.fspath(dest),
            media_type="application/zip",
            filename=dest.name,
            background=BackgroundTask(_cleanup),
        )

    app.router.routes.append(
        Route("/api/v1/diagnostics/bundle", bundle_handler, methods=["POST"])
    )
