"""HTTP download route for generated artifacts."""

from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from opensquilla.application.artifact_workbench import (
    ArtifactContentApplication,
    ArtifactContentQuery,
    ContentIntegrityError,
    ContentNotFoundError,
    DocumentContentQuery,
    NativeArtifactOpen,
    NativeArtifactOpenApplication,
    NativeArtifactOpenError,
    NativeArtifactUnsupportedError,
    WorkingFileQuery,
)
from opensquilla.gateway.adapters import artifact_native as _artifact_native
from opensquilla.gateway.adapters.artifact_content import GatewayArtifactContentPort
from opensquilla.gateway.adapters.artifact_native import (
    GatewayNativeArtifactOpenPort,
)
from opensquilla.gateway.adapters.artifact_native import (
    _artifact_open_cache_dir as _artifact_open_cache_dir,
)
from opensquilla.gateway.adapters.artifact_native import (
    _materialize_artifact_for_open as _materialize_artifact_for_open,
)
from opensquilla.gateway.adapters.artifact_native import (
    _open_path_with_default_app as _open_path_with_default_app,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.origin_guard import (
    forbidden_origin_response,
    request_origin_allowed,
)
from opensquilla.gateway.origin_guard import (
    request_principal_is_owner as _request_principal_is_owner,
)

# Keep the test-facing module handles stable while the implementation lives in
# the native Adapter.  The HTTP module no longer owns platform behavior.
sys = _artifact_native.sys
tempfile = _artifact_native.tempfile


def register_artifact_routes(
    app: Starlette,
    *,
    config: GatewayConfig,
    session_manager: Any = None,
) -> None:
    """Register GET /api/v1/artifacts/{artifact_id} on the given Starlette app."""

    content_port = GatewayArtifactContentPort(config, session_manager=session_manager)
    content = ArtifactContentApplication(content_port)
    native_open = NativeArtifactOpenApplication(
        GatewayNativeArtifactOpenPort(
            content_port,
            materialize=_materialize_artifact_for_open,
            open_path=_open_path_with_default_app,
        )
    )

    async def working_file_handler(request: Request) -> Response:
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if not request_origin_allowed(request, config):
            return forbidden_origin_response()
        # Source paths are host material, unlike an immutable shared delivery.
        if not _request_principal_is_owner(config, request):
            return JSONResponse({"code": "OWNER_REQUIRED"}, status_code=403, headers=headers)
        format_ = request.query_params.get("format", "content")
        if format_ not in {"content", "metadata"}:
            return JSONResponse({"code": "INVALID_REQUEST"}, status_code=400, headers=headers)
        try:
            material = await content.working_file(
                WorkingFileQuery(
                    request.headers.get("x-opensquilla-session-key", ""),
                    request.path_params["document_id"],
                    request.query_params.get("pagePath"),
                )
            )
        except (ContentNotFoundError, ValueError):
            return JSONResponse(
                {"code": "WORKING_FILE_UNAVAILABLE"}, status_code=404, headers=headers
            )
        if format_ == "metadata":
            return JSONResponse(
                {
                    "documentId": material.document_id,
                    "pagePath": material.page_path,
                    "sourcePath": str(material.path),
                    "workspace": material.workspace,
                    "name": material.path.name,
                    "mime": material.media_type,
                    "size": len(material.data),
                },
                headers=headers,
            )
        from urllib.parse import quote

        headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(
            material.path.name, safe=""
        )
        return Response(material.data, media_type=material.media_type, headers=headers)

    async def document_download_handler(request: Request) -> FileResponse | JSONResponse:
        document_id = request.path_params.get("document_id", "")
        session_key = (
            request.query_params.get("sessionKey")
            or request.query_params.get("session_key")
            or request.headers.get("x-opensquilla-session-key")
            or ""
        )
        try:
            material = await content.document(
                DocumentContentQuery(
                    session_key,
                    str(document_id),
                    request.query_params.get("revisionId"),
                )
            )
        except ContentIntegrityError as exc:
            return JSONResponse(
                {"error": str(exc), "code": "INTEGRITY_ERROR"},
                status_code=409,
            )
        except (ContentNotFoundError, ValueError):
            return JSONResponse(
                {"error": "Artifact document not found", "code": "NOT_FOUND"},
                status_code=404,
            )
        return FileResponse(
            material.path,
            media_type=material.media_type,
            filename=material.filename,
        )

    async def download_handler(request: Request) -> FileResponse | JSONResponse:
        artifact_id = request.path_params.get("artifact_id", "")
        session_key = (
            request.query_params.get("sessionKey")
            or request.query_params.get("session_key")
            or request.headers.get("x-opensquilla-session-key")
            or ""
        )
        want_thumbnail = request.query_params.get("variant") == "thumb"
        try:
            material = await content.artifact(
                ArtifactContentQuery(
                    session_key,
                    str(artifact_id),
                    thumbnail=want_thumbnail,
                )
            )
        except ContentIntegrityError as exc:
            return JSONResponse({"error": str(exc), "code": "INTEGRITY_ERROR"}, status_code=409)
        except (ContentNotFoundError, ValueError):
            return JSONResponse(
                {"error": "Artifact not found", "code": "NOT_FOUND"},
                status_code=404,
            )
        return FileResponse(
            material.path,
            media_type=material.media_type,
            filename=material.filename,
        )

    async def open_handler(request: Request) -> JSONResponse:
        if not request_origin_allowed(request, config):
            return forbidden_origin_response()
        if not _request_principal_is_owner(config, request):
            return JSONResponse(
                {"error": "Owner privileges required", "code": "OWNER_REQUIRED"},
                status_code=403,
            )

        artifact_id = request.path_params.get("artifact_id", "")
        session_key = (
            request.query_params.get("sessionKey")
            or request.query_params.get("session_key")
            or request.headers.get("x-opensquilla-session-key")
            or ""
        )
        try:
            await native_open.open(NativeArtifactOpen(session_key, str(artifact_id)))
        except ContentIntegrityError as exc:
            return JSONResponse({"error": str(exc), "code": "INTEGRITY_ERROR"}, status_code=409)
        except NativeArtifactUnsupportedError:
            return JSONResponse(
                {
                    "error": "Artifact type is not supported for native open",
                    "code": "UNSUPPORTED_ARTIFACT_OPEN",
                },
                status_code=415,
            )
        except (ContentNotFoundError, ValueError):
            return JSONResponse(
                {"error": "Artifact not found", "code": "NOT_FOUND"},
                status_code=404,
            )
        except NativeArtifactOpenError:
            return JSONResponse(
                {"error": "Artifact open failed", "code": "OPEN_FAILED"},
                status_code=503,
            )

        return JSONResponse({"ok": True, "status": "accepted"}, status_code=202)

    app.router.routes.append(
        Route(
            "/api/v1/artifact-documents/{document_id}/working-file",
            working_file_handler,
            methods=["GET"],
        )
    )
    app.router.routes.append(
        Route("/api/v1/artifacts/{artifact_id}/open", open_handler, methods=["POST"])
    )
    app.router.routes.append(
        Route(
            "/api/v1/artifact-documents/{document_id}",
            document_download_handler,
            methods=["GET", "HEAD"],
        )
    )
    app.router.routes.append(
        Route("/api/v1/artifacts/{artifact_id}", download_handler, methods=["GET", "HEAD"])
    )
