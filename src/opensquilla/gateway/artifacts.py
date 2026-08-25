"""HTTP download route for generated artifacts."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from opensquilla.artifact_session import (
    ArtifactNotFoundError as ArtifactSessionNotFoundError,
)
from opensquilla.artifact_session import ArtifactSessionService
from opensquilla.artifacts import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.origin_guard import (
    forbidden_origin_response,
    request_origin_allowed,
)
from opensquilla.gateway.origin_guard import (
    request_principal_is_owner as _request_principal_is_owner,
)
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.paths import media_root_from_config, native_io_path

_OPENABLE_HTML_MIMES = frozenset({"text/html", "application/xhtml+xml"})
_OPENABLE_HTML_SUFFIXES = frozenset({".html", ".htm", ".xhtml"})
_MIME_EXTENSION_FALLBACKS = {
    "text/html": ".html",
    "application/xhtml+xml": ".xhtml",
}
_OPEN_CACHE_MAX_AGE_SECONDS = 60 * 60
_UNSAFE_OPEN_FILENAME_RE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]+')


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


def _normalized_mime(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _is_html_artifact(ref: Any) -> bool:
    mime = _normalized_mime(str(getattr(ref, "mime", "") or ""))
    if mime in _OPENABLE_HTML_MIMES:
        return True
    return Path(str(getattr(ref, "name", "") or "")).suffix.lower() in _OPENABLE_HTML_SUFFIXES


def _safe_open_filename(name: str) -> str:
    base = Path(str(name or "artifact")).name.strip()
    base = base.replace("\\", "_")
    cleaned = _UNSAFE_OPEN_FILENAME_RE.sub("_", base).strip()
    return cleaned or "artifact"


def _extension_for_open_name(name: str, mime: str) -> str:
    if Path(name).suffix:
        return ""
    return _MIME_EXTENSION_FALLBACKS.get(_normalized_mime(mime), "")


def _artifact_open_cache_dir() -> Path:
    root = Path(tempfile.gettempdir()) / "opensquilla-artifacts"
    try:
        root.mkdir(parents=True, exist_ok=False, mode=0o700)
    except FileExistsError:
        pass
    if root.is_symlink() or not root.is_dir():
        raise OSError("unsafe artifact open temp directory")
    if sys.platform != "win32":
        uid = getattr(os, "getuid", lambda: None)()
        stat_result = root.stat()
        if uid is not None and getattr(stat_result, "st_uid", uid) != uid:
            raise OSError("artifact open temp directory is owned by another user")
        if stat_result.st_mode & 0o077:
            root.chmod(0o700)
            stat_result = root.stat()
            if stat_result.st_mode & 0o077:
                raise OSError("artifact open temp directory permissions are too broad")
    return root


def _prune_artifact_open_cache(root: Path) -> None:
    try:
        now = time.time()
        for entry in root.iterdir():
            try:
                if now - entry.stat().st_mtime > _OPEN_CACHE_MAX_AGE_SECONDS:
                    entry.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _materialize_artifact_for_open(ref: Any, source: Path) -> Path:
    root = _artifact_open_cache_dir()
    _prune_artifact_open_cache(root)
    name = _safe_open_filename(str(getattr(ref, "name", "") or "artifact"))
    suffix = _extension_for_open_name(name, str(getattr(ref, "mime", "") or ""))
    destination = root / f"{uuid4()}-{name}{suffix}"
    shutil.copyfile(native_io_path(source), destination)
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return destination


def _open_path_with_default_app(path: Path) -> str | None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
            return None
        command = ("open", str(path)) if sys.platform == "darwin" else ("xdg-open", str(path))
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if process.poll() not in (None, 0):
            return "system opener failed"
        return None
    except Exception:
        return "system opener failed"


def register_artifact_routes(
    app: Starlette,
    *,
    config: GatewayConfig,
    session_manager: Any = None,
) -> None:
    """Register GET /api/v1/artifacts/{artifact_id} on the given Starlette app."""

    async def document_download_handler(request: Request) -> FileResponse | JSONResponse:
        document_id = request.path_params.get("document_id", "")
        session_key = (
            request.query_params.get("sessionKey")
            or request.query_params.get("session_key")
            or request.headers.get("x-opensquilla-session-key")
            or ""
        )
        session_id = await _session_id_for_download(session_manager, session_key)
        storage = get_session_storage(session_manager)
        if not session_id or storage is None:
            return JSONResponse(
                {"error": "Artifact document not found", "code": "NOT_FOUND"},
                status_code=404,
            )
        try:
            service = await ArtifactSessionService.from_session_storage(storage)
            document = await service.get_document(str(document_id))
            if document.session_key != session_key or document.session_id != session_id:
                raise ArtifactSessionNotFoundError("artifact document not found")
            revision_id = request.query_params.get("revisionId") or document.head_revision_id
            revision = await service.get_revision(str(revision_id))
            if revision.document_id != document.document_id:
                raise ArtifactSessionNotFoundError("artifact revision not found")
            ref, path = ArtifactStore(_media_root_from_config(config)).resolve_for_download(
                revision.artifact_id,
                session_id=session_id,
            )
        except ArtifactIntegrityError as exc:
            return JSONResponse(
                {"error": str(exc), "code": "INTEGRITY_ERROR"},
                status_code=409,
            )
        except (
            ArtifactSessionNotFoundError,
            ArtifactNotFoundError,
            ValueError,
        ):
            return JSONResponse(
                {"error": "Artifact document not found", "code": "NOT_FOUND"},
                status_code=404,
            )

        filename = document.name if revision_id == document.head_revision_id else revision.filename
        return FileResponse(native_io_path(path), media_type=ref.mime, filename=filename)

    async def download_handler(request: Request) -> FileResponse | JSONResponse:
        artifact_id = request.path_params.get("artifact_id", "")
        session_key = (
            request.query_params.get("sessionKey")
            or request.query_params.get("session_key")
            or request.headers.get("x-opensquilla-session-key")
            or ""
        )
        session_id = await _session_id_for_download(session_manager, session_key)
        if not session_id:
            return JSONResponse(
                {"error": "Artifact not found", "code": "NOT_FOUND"},
                status_code=404,
            )

        want_thumbnail = request.query_params.get("variant") == "thumb"

        store = ArtifactStore(_media_root_from_config(config))
        try:
            ref, path = store.resolve_for_download(str(artifact_id), session_id=session_id)
            if want_thumbnail:
                thumbnail = store.resolve_thumbnail_for_download(
                    str(artifact_id), session_id=session_id
                )
                if thumbnail is not None:
                    _, thumb_path = thumbnail
                    return FileResponse(native_io_path(thumb_path), media_type="image/webp")
        except ArtifactIntegrityError as exc:
            return JSONResponse({"error": str(exc), "code": "INTEGRITY_ERROR"}, status_code=409)
        except (ArtifactNotFoundError, ValueError):
            return JSONResponse(
                {"error": "Artifact not found", "code": "NOT_FOUND"},
                status_code=404,
            )

        return FileResponse(native_io_path(path), media_type=ref.mime, filename=ref.name)

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
        session_id = await _session_id_for_download(session_manager, session_key)
        if not session_id:
            return JSONResponse(
                {"error": "Artifact not found", "code": "NOT_FOUND"},
                status_code=404,
            )

        store = ArtifactStore(_media_root_from_config(config))
        try:
            ref, path = store.resolve_for_download(str(artifact_id), session_id=session_id)
        except ArtifactIntegrityError as exc:
            return JSONResponse({"error": str(exc), "code": "INTEGRITY_ERROR"}, status_code=409)
        except (ArtifactNotFoundError, ValueError):
            return JSONResponse(
                {"error": "Artifact not found", "code": "NOT_FOUND"},
                status_code=404,
            )

        if not _is_html_artifact(ref):
            return JSONResponse(
                {
                    "error": "Artifact type is not supported for native open",
                    "code": "UNSUPPORTED_ARTIFACT_OPEN",
                },
                status_code=415,
            )

        try:
            open_path = _materialize_artifact_for_open(ref, path)
        except OSError:
            return JSONResponse(
                {"error": "Artifact open failed", "code": "OPEN_FAILED"},
                status_code=503,
            )

        if _open_path_with_default_app(open_path):
            return JSONResponse(
                {"error": "Artifact open failed", "code": "OPEN_FAILED"},
                status_code=503,
            )

        return JSONResponse({"ok": True, "status": "accepted"}, status_code=202)

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
