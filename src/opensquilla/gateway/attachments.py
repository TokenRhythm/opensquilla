"""HTTP download route for transcript attachment material."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from opensquilla.attachment_refs import transcript_material_path
from opensquilla.gateway.config import GatewayConfig
from opensquilla.paths import media_root_from_config, native_io_path


async def _session_id_for_download(session_manager: Any, session_key: str) -> str | None:
    if not session_key:
        return None
    if session_manager is None:
        return session_key
    get_session = getattr(session_manager, "get_session", None)
    if not callable(get_session):
        return session_key
    try:
        session = await get_session(session_key)
    except Exception:
        return None
    session_id = getattr(session, "session_id", None)
    return session_id if isinstance(session_id, str) and session_id else None


def _media_root_from_config(config: GatewayConfig) -> Path:
    return media_root_from_config(config)


def _safe_download_name(value: object) -> str:
    raw = str(value or "").strip()
    cleaned = " ".join(raw.replace("/", " ").replace("\\", " ").split())
    return cleaned[:160] or "attachment"


def _safe_media_type(value: object) -> str:
    raw = str(value or "").strip()
    if not raw or "/" not in raw or any(ch in raw for ch in "\r\n;"):
        return "application/octet-stream"
    return raw[:120]


def register_attachment_routes(
    app: Starlette,
    *,
    config: GatewayConfig,
    session_manager: Any = None,
) -> None:
    """Register GET /api/v1/attachments/{sha256} on the given Starlette app."""

    async def download_handler(request: Request) -> FileResponse | JSONResponse:
        sha = str(request.path_params.get("sha256", "")).lower()
        session_key = (
            request.query_params.get("sessionKey")
            or request.query_params.get("session_key")
            or request.headers.get("x-opensquilla-session-key")
            or ""
        )
        session_id = await _session_id_for_download(session_manager, session_key)
        if not session_id:
            return JSONResponse(
                {"error": "Attachment not found", "code": "NOT_FOUND"},
                status_code=404,
            )

        try:
            path = transcript_material_path(_media_root_from_config(config), session_id, sha)
        except ValueError:
            return JSONResponse(
                {"error": "Attachment not found", "code": "NOT_FOUND"},
                status_code=404,
            )
        native_path = native_io_path(path)
        if not native_path.exists() or not native_path.is_file():
            return JSONResponse(
                {"error": "Attachment not found", "code": "NOT_FOUND"},
                status_code=404,
            )

        try:
            actual_sha = hashlib.sha256(native_path.read_bytes()).hexdigest()
        except OSError:
            return JSONResponse(
                {"error": "Attachment not found", "code": "NOT_FOUND"},
                status_code=404,
            )
        if actual_sha != sha:
            return JSONResponse(
                {"error": "Attachment integrity check failed", "code": "INTEGRITY_ERROR"},
                status_code=409,
            )

        return FileResponse(
            native_path,
            media_type=_safe_media_type(request.query_params.get("mime")),
            filename=_safe_download_name(request.query_params.get("name")),
        )

    app.router.routes.append(
        Route("/api/v1/attachments/{sha256}", download_handler, methods=["GET", "HEAD"])
    )
