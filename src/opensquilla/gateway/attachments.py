"""HTTP download route for transcript attachment material."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from opensquilla.application.artifact_workbench import (
    ArtifactContentApplication,
    AttachmentContentQuery,
    ContentIntegrityError,
    ContentNotFoundError,
)
from opensquilla.gateway.adapters.artifact_content import GatewayArtifactContentPort
from opensquilla.gateway.config import GatewayConfig
from opensquilla.paths import media_root_from_config


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

    content = ArtifactContentApplication(
        GatewayArtifactContentPort(config, session_manager=session_manager)
    )

    async def download_handler(request: Request) -> FileResponse | JSONResponse:
        sha = str(request.path_params.get("sha256", "")).lower()
        session_key = (
            request.query_params.get("sessionKey")
            or request.query_params.get("session_key")
            or request.headers.get("x-opensquilla-session-key")
            or ""
        )
        try:
            material = await content.attachment(AttachmentContentQuery(session_key, sha))
        except (ContentNotFoundError, ValueError):
            return JSONResponse(
                {"error": "Attachment not found", "code": "NOT_FOUND"},
                status_code=404,
            )
        except ContentIntegrityError:
            return JSONResponse(
                {"error": "Attachment integrity check failed", "code": "INTEGRITY_ERROR"},
                status_code=409,
            )

        return FileResponse(
            material.path,
            media_type=_safe_media_type(request.query_params.get("mime")),
            filename=_safe_download_name(request.query_params.get("name")),
        )

    app.router.routes.append(
        Route("/api/v1/attachments/{sha256}", download_handler, methods=["GET", "HEAD"])
    )
