"""HTTP download route for transcript attachment material."""

from __future__ import annotations

import asyncio
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
from opensquilla.gateway.attachment_text_preview import read_attachment_text_preview
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.legacy_file_snapshot import (
    LegacyFileSnapshotBusyError,
    LegacyFileSnapshotRecovery,
)


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
    recovery = LegacyFileSnapshotRecovery(config, session_manager)

    async def download_handler(request: Request) -> Response:
        sha = str(request.path_params.get("sha256", "")).lower()
        session_key = (
            request.query_params.get("sessionKey")
            or request.query_params.get("session_key")
            or request.headers.get("x-opensquilla-session-key")
            or ""
        )
        from opensquilla.gateway.auth import resolve_auth_async
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
            principal = await resolve_auth_async(
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
            message_id = request.query_params.get("messageId")
            tool_use_id = request.query_params.get("toolUseId")
            if tool_use_id is not None:
                if not message_id or not tool_use_id or raw_index is not None:
                    raise ContentNotFoundError("historical file content unavailable")
                await recovery.recover(
                    session_key=session_key, message_id=message_id,
                    tool_use_id=tool_use_id, sha256=sha,
                )
                query = AttachmentContentQuery(session_key, sha)
            else:
                query = AttachmentContentQuery(
                    session_key, sha, message_id=message_id,
                    attachment_index=int(raw_index) if raw_index is not None else None,
                    revision=request.query_params.get("revision"),
                    source=request.query_params.get("source"),
                )
            material = await content.attachment(query)
            if request.query_params.get("preview") == "1":
                if material.path is None:
                    raise ContentNotFoundError("historical file content unavailable")
                preview = await asyncio.to_thread(read_attachment_text_preview, material.path)
                return JSONResponse(preview)
        except UnicodeDecodeError:
            return JSONResponse(
                {"error": "Attachment is not valid UTF-8 text", "code": "UNSUPPORTED_MEDIA_TYPE"},
                status_code=415,
            )
        except (ContentNotFoundError, ValueError, OSError):
            return JSONResponse(
                {"error": "Attachment not found", "code": "NOT_FOUND"},
                status_code=404,
            )
        except ContentIntegrityError:
            return JSONResponse(
                {"error": "Attachment integrity check failed", "code": "INTEGRITY_ERROR"},
                status_code=409,
            )
        except LegacyFileSnapshotBusyError:
            return JSONResponse(
                {"error": "Attachment recovery is busy", "code": "TEMPORARILY_UNAVAILABLE"},
                status_code=503,
                headers={"Retry-After": "1"},
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
