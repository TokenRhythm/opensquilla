"""HTTP download route for transcript attachment material."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from opensquilla.application.artifact_workbench import (
    ArtifactContentApplication,
    AttachmentContentQuery,
    ContentIntegrityError,
    ContentNotFoundError,
)
from opensquilla.gateway.adapters.artifact_content import GatewayArtifactContentPort
from opensquilla.gateway.config import GatewayConfig


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

    async def download_handler(request: Request) -> Response:
        sha = str(request.path_params.get("sha256", "")).lower()
        session_key = (
            request.query_params.get("sessionKey")
            or request.query_params.get("session_key")
            or request.headers.get("x-opensquilla-session-key")
            or ""
        )
        from opensquilla.gateway.auth import resolve_auth
        from opensquilla.gateway.guest_rpc_policy import GuestRpcPolicy, guest_owns_session_key
        from opensquilla.gateway.origin_guard import extract_http_token

        principal = getattr(request.state, "principal", None)
        if principal is None:
            auth_params = {}
            token = extract_http_token(request)
            if token:
                auth_params["token"] = token
            guest_key = request.cookies.get("opensquilla_guest_session")
            if guest_key:
                auth_params["guestSessionKey"] = guest_key
            principal = resolve_auth(
                config,
                auth_params,
                "operator",
                peer_ip=request.client.host if request.client else None,
            )
        if principal is None or principal.auth_state == "invalid" or (
            GuestRpcPolicy.is_guest(SimpleNamespace(principal=principal))
            and not guest_owns_session_key(principal.guest_owner_id, session_key)
        ):
            return JSONResponse(
                {"error": "Attachment access is unauthorized", "code": "UNAUTHORIZED"},
                status_code=403,
            )
        try:
            raw_index = request.query_params.get("attachmentIndex")
            material = await content.attachment(AttachmentContentQuery(
                session_key, sha,
                message_id=request.query_params.get("messageId"),
                attachment_index=int(raw_index) if raw_index is not None else None,
                revision=request.query_params.get("revision"),
                source=request.query_params.get("source"),
            ))
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

        if material.data is not None:
            return Response(
                material.data,
                media_type=_safe_media_type(material.media_type),
                headers={"Content-Disposition": "inline"},
            )
        if material.path is None:
            return JSONResponse(
                {"error": "Attachment not found", "code": "NOT_FOUND"}, status_code=404
            )
        return FileResponse(
            material.path,
            media_type=_safe_media_type(request.query_params.get("mime")),
            filename=_safe_download_name(request.query_params.get("name")),
        )

    app.router.routes.append(
        Route("/api/v1/attachments/{sha256}", download_handler, methods=["GET", "HEAD"])
    )
