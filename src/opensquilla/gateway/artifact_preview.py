"""Capability-scoped browser preview routes for generated HTML artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from opensquilla.artifacts import (
    ArtifactBundleUnsupportedError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
    legacy_html_bundle_warning_codes,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.origin_guard import (
    forbidden_origin_response,
    request_origin_allowed,
)
from opensquilla.gateway.scopes import is_loopback_address
from opensquilla.paths import media_root_from_config, native_io_path

log = structlog.get_logger(__name__)

PREVIEW_LEASE_IDLE_SECONDS = 8 * 60 * 60
PREVIEW_LEASE_LIMIT_PER_SESSION = 8
_PREVIEW_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_PREVIEW_AUTHORITY_RE = re.compile(
    r"^p-([0-9a-f]{32})\.localhost:([0-9]{1,5})$"
)
_HTML_MIMES = frozenset({"text/html", "application/xhtml+xml"})
_HTML_SUFFIXES = frozenset({".html", ".htm", ".xhtml"})
_URL_PATH_SAFE = "/!$&'()*+,;=:@-._~"
_CLEAR_SITE_DATA_PATH = ".opensquilla/clear-site-data"


class PreviewLeaseError(ValueError):
    """Base class for preview lease errors."""


class PreviewLeaseNotFoundError(PreviewLeaseError):
    """Raised when a lease id or capability token is unknown."""


class PreviewLeaseExpiredError(PreviewLeaseError):
    """Raised when a known lease has expired or has been revoked."""


class PreviewLeaseLimitError(PreviewLeaseError):
    """Raised when a session already owns the maximum number of leases."""


@dataclass(slots=True)
class ArtifactPreviewLease:
    lease_id: str
    token_hash: str
    artifact_id: str
    session_id: str
    session_key: str
    mode: str
    client: str
    entrypoint: str
    source: dict[str, Any]
    created_at: float
    last_access_at: float


@dataclass(frozen=True, slots=True)
class _ResolvedPreviewResource:
    logical_path: str
    mime: str
    sha256: str
    size: int
    path: Path


class ArtifactPreviewLeaseService:
    """In-memory, read-only capabilities scoped to one artifact and session."""

    def __init__(
        self,
        *,
        config: GatewayConfig,
        idle_seconds: int = PREVIEW_LEASE_IDLE_SECONDS,
        max_per_session: int = PREVIEW_LEASE_LIMIT_PER_SESSION,
        clock: Any = time.time,
    ) -> None:
        self._config = config
        self._idle_seconds = idle_seconds
        self._max_per_session = max_per_session
        self._clock = clock
        self._lock = threading.RLock()
        self._leases_by_id: dict[str, ArtifactPreviewLease] = {}
        self._lease_id_by_token_hash: dict[str, str] = {}
        self._expired_lease_ids: dict[str, float] = {}
        self._expired_token_hashes: dict[str, float] = {}
        self._listener_port: int | None = None

    @property
    def listener_port(self) -> int | None:
        return self._listener_port

    def set_listener_port(self, port: int) -> None:
        if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
            raise ValueError("preview listener port is invalid")
        self._listener_port = port

    def clear_listener_port(self) -> None:
        self._listener_port = None

    def create(
        self,
        *,
        artifact_id: str,
        session_id: str,
        session_key: str,
        mode: str,
        client: str,
    ) -> tuple[ArtifactPreviewLease, str]:
        now = float(self._clock())
        store = self._store()
        ref, entry_path = store.resolve_for_download(artifact_id, session_id=session_id)
        if not _is_html_artifact(ref):
            raise ValueError("only HTML artifacts can be previewed")
        manifest = store.validate_preview_bundle(
            artifact_id,
            session_id=session_id,
        )
        if manifest is None:
            entrypoint = str(getattr(ref, "name", "") or "index.html")
            warning_codes = list(
                legacy_html_bundle_warning_codes(
                    entrypoint,
                    native_io_path(entry_path).read_bytes(),
                )
            )
            source = {
                "kind": "single_file",
                "collection_status": "partial" if warning_codes else "not_applicable",
                "file_count": 1,
                "total_bytes": int(getattr(ref, "size", 0) or 0),
                "warning_codes": list(warning_codes),
            }
        else:
            entrypoint = str(getattr(manifest, "entrypoint", "") or "")
            if not entrypoint:
                raise ArtifactIntegrityError("artifact bundle entrypoint is missing")
            collection_status = str(
                getattr(manifest, "collection_status", "complete") or "complete"
            )
            warning_codes = [
                str(code) for code in (getattr(manifest, "warning_codes", ()) or ())
            ]
            if (
                collection_status == "partial"
                and warning_codes == ["missing_dependency"]
                and store.supports_single_file_editing(
                    artifact_id,
                    session_id=session_id,
                )
            ):
                # Old collectors could treat a remote CSS @import as a missing
                # local dependency. Keep the immutable manifest unchanged, but
                # expose the integrity-checked effective preview state.
                collection_status = "complete"
                warning_codes = []
            source = {
                "kind": "bundle",
                "collection_status": collection_status,
                "file_count": int(getattr(manifest, "file_count", 0) or 0),
                "total_bytes": int(getattr(manifest, "total_size", 0) or 0),
                "warning_codes": warning_codes,
            }

        if manifest is None:
            # The legacy single-file material was already hashed by resolve_for_download,
            # and resolving it here also confirms its preview path contract.
            store.resolve_preview_resource(
                artifact_id,
                session_id=session_id,
                logical_path=entrypoint,
            )

        with self._lock:
            self._purge_expired_locked(now)
            active_for_session = sum(
                lease.session_id == session_id for lease in self._leases_by_id.values()
            )
            if active_for_session >= self._max_per_session:
                raise PreviewLeaseLimitError("preview lease limit reached")
            token = secrets.token_hex(16)
            token_hash = _token_hash(token)
            lease = ArtifactPreviewLease(
                lease_id=f"apl-{secrets.token_urlsafe(18)}",
                token_hash=token_hash,
                artifact_id=artifact_id,
                session_id=session_id,
                session_key=session_key,
                mode=mode,
                client=client,
                entrypoint=entrypoint,
                source=source,
                created_at=now,
                last_access_at=now,
            )
            self._leases_by_id[lease.lease_id] = lease
            self._lease_id_by_token_hash[token_hash] = lease.lease_id
            active_leases = len(self._leases_by_id)
        log.info(
            "gateway.artifact_preview_lease",
            client=client,
            mode=mode,
            result="created",
            source=source["kind"],
            collection_status=source["collection_status"],
            active_leases=active_leases,
        )
        return lease, token

    def renew(
        self,
        lease_id: str,
        *,
        session_id: str,
        session_key: str,
    ) -> ArtifactPreviewLease:
        now = float(self._clock())
        with self._lock:
            self._purge_expired_locked(now)
            lease = self._leases_by_id.get(lease_id)
            if lease is None:
                if lease_id in self._expired_lease_ids:
                    raise PreviewLeaseExpiredError("preview lease expired")
                raise PreviewLeaseNotFoundError("preview lease not found")
            self._assert_session(lease, session_id=session_id, session_key=session_key)
            lease.last_access_at = now
            active_leases = len(self._leases_by_id)
        log.info(
            "gateway.artifact_preview_lease",
            client=lease.client,
            mode=lease.mode,
            result="renewed",
            active_leases=active_leases,
        )
        return lease

    def revoke(
        self,
        lease_id: str,
        *,
        session_id: str,
        session_key: str,
    ) -> None:
        now = float(self._clock())
        with self._lock:
            self._purge_expired_locked(now)
            lease = self._leases_by_id.get(lease_id)
            if lease is None:
                if lease_id in self._expired_lease_ids:
                    raise PreviewLeaseExpiredError("preview lease expired")
                raise PreviewLeaseNotFoundError("preview lease not found")
            self._assert_session(lease, session_id=session_id, session_key=session_key)
            self._expire_locked(lease, now)
            active_leases = len(self._leases_by_id)
        log.info(
            "gateway.artifact_preview_lease",
            client=lease.client,
            mode=lease.mode,
            result="revoked",
            active_leases=active_leases,
        )

    def revoke_all(self) -> None:
        now = float(self._clock())
        with self._lock:
            for lease in tuple(self._leases_by_id.values()):
                self._expire_locked(lease, now)

    def resolve_token(self, token: str) -> ArtifactPreviewLease:
        if _PREVIEW_TOKEN_RE.fullmatch(token) is None:
            raise PreviewLeaseNotFoundError("preview lease not found")
        now = float(self._clock())
        token_hash = _token_hash(token)
        with self._lock:
            self._purge_expired_locked(now)
            lease_id = self._lease_id_by_token_hash.get(token_hash)
            if lease_id is None:
                if token_hash in self._expired_token_hashes:
                    raise PreviewLeaseExpiredError("preview lease expired")
                raise PreviewLeaseNotFoundError("preview lease not found")
            lease = self._leases_by_id.get(lease_id)
            if lease is None:
                raise PreviewLeaseNotFoundError("preview lease not found")
            lease.last_access_at = now
            return lease

    def resolve_resource(
        self,
        lease: ArtifactPreviewLease,
        logical_path: str | None,
    ) -> _ResolvedPreviewResource:
        store = self._store()
        resolver = getattr(store, "resolve_preview_resource", None)
        if callable(resolver):
            resource = resolver(
                lease.artifact_id,
                session_id=lease.session_id,
                logical_path=logical_path,
            )
            return _ResolvedPreviewResource(
                logical_path=str(getattr(resource, "logical_path", "") or lease.entrypoint),
                mime=str(getattr(resource, "mime", "") or "application/octet-stream"),
                sha256=str(getattr(resource, "sha256", "") or ""),
                size=int(getattr(resource, "size", 0) or 0),
                path=Path(getattr(resource, "path")),
            )

        ref, path = store.resolve_for_download(
            lease.artifact_id,
            session_id=lease.session_id,
        )
        accepted_paths = {None, "", "/", lease.entrypoint, str(getattr(ref, "name", "") or "")}
        if logical_path not in accepted_paths:
            raise ArtifactNotFoundError("artifact preview resource not found")
        return _ResolvedPreviewResource(
            logical_path=lease.entrypoint,
            mime=str(getattr(ref, "mime", "") or "application/octet-stream"),
            sha256=str(getattr(ref, "sha256", "") or ""),
            size=int(getattr(ref, "size", 0) or 0),
            path=Path(path),
        )

    def full_launch_url(self, token: str, entrypoint: str) -> str:
        port = self._listener_port
        if port is None:
            raise PreviewLeaseError("preview listener is unavailable")
        return (
            f"http://p-{token}.localhost:{port}/"
            f"{quote(entrypoint.lstrip('/'), safe=_URL_PATH_SAFE)}"
        )

    def expires_at(self, lease: ArtifactPreviewLease) -> str:
        instant = datetime.fromtimestamp(
            lease.last_access_at + self._idle_seconds,
            tz=UTC,
        )
        return instant.isoformat().replace("+00:00", "Z")

    def _store(self) -> ArtifactStore:
        return ArtifactStore(media_root_from_config(self._config))

    @staticmethod
    def _assert_session(
        lease: ArtifactPreviewLease,
        *,
        session_id: str,
        session_key: str,
    ) -> None:
        if lease.session_id != session_id or lease.session_key != session_key:
            raise PreviewLeaseNotFoundError("preview lease not found")

    def _purge_expired_locked(self, now: float) -> None:
        for lease in tuple(self._leases_by_id.values()):
            if now - lease.last_access_at >= self._idle_seconds:
                self._expire_locked(lease, now)
        tombstone_deadline = now - self._idle_seconds
        for lease_id, expired_at in tuple(self._expired_lease_ids.items()):
            if expired_at <= tombstone_deadline:
                self._expired_lease_ids.pop(lease_id, None)
        for token_hash, expired_at in tuple(self._expired_token_hashes.items()):
            if expired_at <= tombstone_deadline:
                self._expired_token_hashes.pop(token_hash, None)

    def _expire_locked(self, lease: ArtifactPreviewLease, now: float) -> None:
        self._leases_by_id.pop(lease.lease_id, None)
        self._lease_id_by_token_hash.pop(lease.token_hash, None)
        self._expired_lease_ids[lease.lease_id] = now
        self._expired_token_hashes[lease.token_hash] = now


def create_artifact_preview_resource_app(
    service: ArtifactPreviewLeaseService,
) -> Starlette:
    """Build the isolated loopback-only resource listener."""

    async def preview_resource(request: Request) -> Response:
        match = _PREVIEW_AUTHORITY_RE.fullmatch(
            request.headers.get("host", "").casefold()
        )
        port = service.listener_port
        if (
            match is None
            or port is None
            or int(match.group(2)) != port
        ):
            return _preview_error("Preview resource not found", "NOT_FOUND", 404)
        if request.path_params.get("resource_path") == _CLEAR_SITE_DATA_PATH:
            return _clear_preview_site_data(service, match.group(1))
        return await _serve_preview_resource(
            request,
            service=service,
            token=match.group(1),
            logical_path=request.path_params.get("resource_path"),
            offline_transport=False,
        )

    return Starlette(
        debug=False,
        routes=[
            Route("/", preview_resource, methods=["GET", "HEAD"]),
            Route("/{resource_path:path}", preview_resource, methods=["GET", "HEAD"]),
        ],
    )


def register_artifact_preview_routes(
    app: Starlette,
    *,
    config: GatewayConfig,
    session_manager: Any = None,
    service: ArtifactPreviewLeaseService | None = None,
) -> ArtifactPreviewLeaseService:
    """Register authenticated lease controls and the remote offline transport."""

    lease_service = service or ArtifactPreviewLeaseService(config=config)

    async def create_lease(request: Request) -> Response:
        if not request_origin_allowed(request, config):
            return forbidden_origin_response()
        session_key, session_id = await _request_session(
            request,
            session_manager=session_manager,
        )
        if not session_key or not session_id:
            return _api_error("Artifact not found", "NOT_FOUND", 404)
        try:
            body = await request.json()
        except Exception:
            return _api_error("Invalid JSON body", "INVALID_REQUEST", 400)
        if not isinstance(body, dict):
            return _api_error("JSON body must be an object", "INVALID_REQUEST", 400)
        version = body.get("version")
        mode = body.get("mode")
        client = body.get("client")
        if version != 1 or isinstance(version, bool):
            return _api_error("version must be 1", "INVALID_REQUEST", 400)
        if mode not in {"full", "offline"}:
            return _api_error("mode must be full or offline", "INVALID_REQUEST", 400)
        if client not in {"desktop", "web"}:
            return _api_error("client must be desktop or web", "INVALID_REQUEST", 400)

        effective_mode = (
            "full"
            if mode == "full"
            and _full_preview_allowed(
                request,
                service=lease_service,
                client=client,
            )
            else "offline"
        )
        if client == "desktop":
            if not _desktop_preview_request_allowed(request):
                return _api_error(
                    "Desktop preview requires a loopback native client",
                    "DESKTOP_PREVIEW_FORBIDDEN",
                    403,
                )
            if lease_service.listener_port is None:
                return _api_error(
                    "Artifact preview listener is unavailable",
                    "PREVIEW_LISTENER_UNAVAILABLE",
                    503,
                )
        artifact_id = str(request.path_params.get("artifact_id") or "")
        try:
            lease, token = await asyncio.to_thread(
                lease_service.create,
                artifact_id=artifact_id,
                session_id=session_id,
                session_key=session_key,
                mode=effective_mode,
                client=client,
            )
        except PreviewLeaseLimitError:
            return _api_error("Preview lease limit reached", "PREVIEW_LEASE_LIMIT", 429)
        except ArtifactBundleUnsupportedError:
            return _api_error(
                "Artifact bundle version is unsupported",
                "BUNDLE_UNSUPPORTED",
                409,
            )
        except ArtifactIntegrityError:
            return _api_error("Artifact integrity check failed", "INTEGRITY_ERROR", 409)
        except (ArtifactNotFoundError, ValueError):
            return _api_error("Artifact not found", "NOT_FOUND", 404)

        use_loopback_transport = client == "desktop" or effective_mode == "full"
        if use_loopback_transport:
            launch_url = lease_service.full_launch_url(token, lease.entrypoint)
            preview_origin = f"http://p-{token}.localhost:{lease_service.listener_port}"
        else:
            encoded_entrypoint = quote(
                lease.entrypoint.lstrip("/"),
                safe=_URL_PATH_SAFE,
            )
            launch_url = f"/api/v1/artifact-preview/{token}/{encoded_entrypoint}"
            preview_origin = None
        payload = _lease_payload(
            lease_service,
            lease,
            launch_url=launch_url,
            preview_origin=preview_origin,
        )
        response = JSONResponse(payload, status_code=201)
        _set_control_no_store(response)
        return response

    async def renew_lease(request: Request) -> Response:
        if not request_origin_allowed(request, config):
            return forbidden_origin_response()
        session_key, session_id = await _request_session(
            request,
            session_manager=session_manager,
        )
        if not session_key or not session_id:
            return _api_error("Preview lease not found", "NOT_FOUND", 404)
        lease_id = str(request.path_params.get("lease_id") or "")
        try:
            lease = lease_service.renew(
                lease_id,
                session_id=session_id,
                session_key=session_key,
            )
        except PreviewLeaseExpiredError:
            return _api_error("Preview lease expired", "PREVIEW_LEASE_EXPIRED", 410)
        except PreviewLeaseNotFoundError:
            return _api_error("Preview lease not found", "NOT_FOUND", 404)
        response = JSONResponse(
            {
                "version": 1,
                "lease_id": lease.lease_id,
                "expires_at": lease_service.expires_at(lease),
            }
        )
        _set_control_no_store(response)
        return response

    async def delete_lease(request: Request) -> Response:
        if not request_origin_allowed(request, config):
            return forbidden_origin_response()
        session_key, session_id = await _request_session(
            request,
            session_manager=session_manager,
        )
        if not session_key or not session_id:
            return _api_error("Preview lease not found", "NOT_FOUND", 404)
        lease_id = str(request.path_params.get("lease_id") or "")
        try:
            lease_service.revoke(
                lease_id,
                session_id=session_id,
                session_key=session_key,
            )
        except PreviewLeaseExpiredError:
            return _api_error("Preview lease expired", "PREVIEW_LEASE_EXPIRED", 410)
        except PreviewLeaseNotFoundError:
            return _api_error("Preview lease not found", "NOT_FOUND", 404)
        response = Response(status_code=204)
        _set_control_no_store(response)
        return response

    async def offline_resource(request: Request) -> Response:
        return await _serve_preview_resource(
            request,
            service=lease_service,
            token=str(request.path_params.get("token") or ""),
            logical_path=request.path_params.get("resource_path"),
            offline_transport=True,
        )

    app.router.routes.extend(
        [
            Route(
                "/api/v1/artifacts/{artifact_id}/preview-leases",
                create_lease,
                methods=["POST"],
            ),
            Route(
                "/api/v1/artifact-preview-leases/{lease_id}/renew",
                renew_lease,
                methods=["POST"],
            ),
            Route(
                "/api/v1/artifact-preview-leases/{lease_id}",
                delete_lease,
                methods=["DELETE"],
            ),
            Route(
                "/api/v1/artifact-preview/{token}",
                offline_resource,
                methods=["GET", "HEAD"],
            ),
            Route(
                "/api/v1/artifact-preview/{token}/{resource_path:path}",
                offline_resource,
                methods=["GET", "HEAD"],
            ),
        ]
    )
    return lease_service


async def _serve_preview_resource(
    request: Request,
    *,
    service: ArtifactPreviewLeaseService,
    token: str,
    logical_path: str | None,
    offline_transport: bool,
) -> Response:
    try:
        lease = service.resolve_token(token)
    except PreviewLeaseExpiredError:
        return _preview_error(
            "Preview lease expired",
            "PREVIEW_LEASE_EXPIRED",
            410,
            offline_transport=offline_transport,
        )
    except PreviewLeaseNotFoundError:
        return _preview_error(
            "Preview resource not found",
            "NOT_FOUND",
            404,
            offline_transport=offline_transport,
        )
    expected_offline_transport = lease.client == "web" and lease.mode == "offline"
    if offline_transport != expected_offline_transport:
        return _preview_error(
            "Preview resource not found",
            "NOT_FOUND",
            404,
            offline_transport=offline_transport,
        )

    try:
        normalized_path = _normalize_resource_path(logical_path)
    except ValueError:
        return _preview_error(
            "Preview resource not found",
            "NOT_FOUND",
            404,
            offline_transport=offline_transport,
        )
    try:
        resource = await asyncio.to_thread(
            service.resolve_resource,
            lease,
            normalized_path,
        )
    except ArtifactNotFoundError:
        if not _is_document_navigation(request):
            return _preview_error(
                "Preview resource not found",
                "NOT_FOUND",
                404,
                offline_transport=offline_transport,
            )
        try:
            resource = await asyncio.to_thread(
                service.resolve_resource,
                lease,
                None,
            )
        except ArtifactBundleUnsupportedError:
            return _preview_error(
                "Artifact bundle version is unsupported",
                "BUNDLE_UNSUPPORTED",
                409,
                offline_transport=offline_transport,
            )
        except ArtifactIntegrityError:
            return _preview_error(
                "Artifact integrity check failed",
                "INTEGRITY_ERROR",
                409,
                offline_transport=offline_transport,
            )
        except (ArtifactNotFoundError, ValueError):
            return _preview_error(
                "Preview resource not found",
                "NOT_FOUND",
                404,
                offline_transport=offline_transport,
            )
    except ArtifactBundleUnsupportedError:
        return _preview_error(
            "Artifact bundle version is unsupported",
            "BUNDLE_UNSUPPORTED",
            409,
            offline_transport=offline_transport,
        )
    except ArtifactIntegrityError:
        return _preview_error(
            "Artifact integrity check failed",
            "INTEGRITY_ERROR",
            409,
            offline_transport=offline_transport,
        )
    except ValueError:
        return _preview_error(
            "Preview resource not found",
            "NOT_FOUND",
            404,
            offline_transport=offline_transport,
        )

    headers = _resource_headers(
        resource,
        offline=lease.mode == "offline",
        opaque_cors=offline_transport,
        offline_source=(
            _offline_capability_source(request, token)
            if offline_transport
            else "'self'"
        ),
    )
    if request.headers.get("if-none-match") == headers["ETag"]:
        return Response(status_code=304, headers=headers)
    return FileResponse(
        native_io_path(resource.path),
        media_type=resource.mime,
        headers=headers,
    )


async def _request_session(
    request: Request,
    *,
    session_manager: Any,
) -> tuple[str | None, str | None]:
    session_key = (
        request.headers.get("x-opensquilla-session-key")
        or request.query_params.get("sessionKey")
        or request.query_params.get("session_key")
    )
    if not session_key:
        return None, None
    if session_manager is None:
        return session_key, session_key
    get_session = getattr(session_manager, "get_session", None)
    if not callable(get_session):
        return session_key, session_key
    try:
        session = await get_session(session_key)
    except Exception:
        return session_key, None
    session_id = getattr(session, "session_id", None) if session is not None else None
    if not isinstance(session_id, str) or not session_id:
        return session_key, None
    return session_key, session_id


def _full_preview_allowed(
    request: Request,
    *,
    service: ArtifactPreviewLeaseService,
    client: str,
) -> bool:
    if _force_offline():
        return False
    if service.listener_port is None:
        return False
    peer_ip = request.client.host if request.client is not None else None
    if not is_loopback_address(peer_ip):
        return False
    if client == "desktop":
        return request.headers.get("origin") is None
    origin = request.headers.get("origin")
    if not origin:
        return False
    try:
        hostname = urlsplit(origin).hostname
    except ValueError:
        return False
    return _is_loopback_hostname(hostname)


def _desktop_preview_request_allowed(request: Request) -> bool:
    peer_ip = request.client.host if request.client is not None else None
    return (
        is_loopback_address(peer_ip)
        and request.headers.get("origin") is None
    )


def _is_loopback_hostname(hostname: str | None) -> bool:
    if hostname is None:
        return False
    normalized = hostname.rstrip(".").casefold()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _force_offline() -> bool:
    return os.getenv("OPENSQUILLA_PREVIEW_FORCE_OFFLINE", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _lease_payload(
    service: ArtifactPreviewLeaseService,
    lease: ArtifactPreviewLease,
    *,
    launch_url: str,
    preview_origin: str | None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "lease_id": lease.lease_id,
        "effective_mode": lease.mode,
        "launch_url": launch_url,
        "preview_origin": preview_origin,
        "entrypoint": lease.entrypoint,
        "expires_at": service.expires_at(lease),
        "idle_timeout_seconds": PREVIEW_LEASE_IDLE_SECONDS,
        "source": lease.source,
    }


def _normalize_resource_path(value: str | None) -> str | None:
    if value in {None, "", "/"}:
        return None
    assert value is not None
    normalized = value.lstrip("/")
    parts = normalized.split("/")
    if (
        "\\" in normalized
        or "\x00" in normalized
        or any(part in {"", ".", ".."} for part in parts)
        or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
    ):
        raise ValueError("preview resource path is invalid")
    return normalized


def _is_document_navigation(request: Request) -> bool:
    destination = request.headers.get("sec-fetch-dest", "").strip().casefold()
    if destination:
        return destination == "document"
    if request.headers.get("sec-fetch-mode", "").strip().casefold() == "navigate":
        return True
    return "text/html" in request.headers.get("accept", "").casefold()


def _resource_headers(
    resource: _ResolvedPreviewResource,
    *,
    offline: bool,
    opaque_cors: bool,
    offline_source: str,
) -> dict[str, str]:
    sha256 = resource.sha256
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        sha256 = hashlib.sha256(native_io_path(resource.path).read_bytes()).hexdigest()
    headers = {
        "Cache-Control": "private, no-store",
        "ETag": f'"{sha256}"',
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
    if offline:
        if opaque_cors:
            headers["Access-Control-Allow-Origin"] = "null"
            headers["Cross-Origin-Resource-Policy"] = "cross-origin"
        # Apply policy to every response, including SVG/XML or a resource later
        # navigated as a subdocument. Browsers ignore inapplicable directives
        # for inert resource types.
        headers["Content-Security-Policy"] = _offline_csp(offline_source)
        # Chromium's HTTP request interception does not cover DNS prefetch or
        # WebRTC ICE/STUN traffic. Disable speculative DNS here and use CSP's
        # dedicated WebRTC directive below so "offline" means no network side
        # channel, not merely no fetch/XHR.
        headers["X-DNS-Prefetch-Control"] = "off"
        headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), display-capture=()"
        )
    return headers


def _clear_preview_site_data(
    service: ArtifactPreviewLeaseService,
    token: str,
) -> Response:
    """Clear ephemeral browser state from the random Web preview origin."""

    try:
        lease = service.resolve_token(token)
    except PreviewLeaseExpiredError:
        return _preview_error("Preview lease expired", "PREVIEW_LEASE_EXPIRED", 410)
    except PreviewLeaseNotFoundError:
        return _preview_error("Preview resource not found", "NOT_FOUND", 404)
    if lease.client != "web" or lease.mode != "full":
        return _preview_error("Preview resource not found", "NOT_FOUND", 404)
    return Response(
        status_code=204,
        headers={
            "Cache-Control": "no-store",
            "Clear-Site-Data": '"cache", "cookies", "storage"',
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _offline_capability_source(request: Request, token: str) -> str:
    origin = str(request.base_url).rstrip("/")
    return f"{origin}/api/v1/artifact-preview/{token}/"


def _offline_csp(source: str) -> str:
    return (
        f"default-src {source} data: blob:; "
        f"script-src {source} 'unsafe-inline' 'unsafe-eval' data: blob:; "
        f"style-src {source} 'unsafe-inline' data: blob:; "
        f"img-src {source} data: blob:; "
        f"font-src {source} data: blob:; "
        f"media-src {source} data: blob:; "
        f"connect-src {source} data: blob:; "
        f"worker-src {source} data: blob:; "
        f"frame-src {source} data: blob:; "
        "object-src 'none'; base-uri 'none'; form-action 'none'; navigate-to 'none'; "
        "webrtc 'block';"
    )


def _preview_error(
    message: str,
    code: str,
    status_code: int,
    *,
    offline_transport: bool = False,
) -> JSONResponse:
    response = JSONResponse({"error": message, "code": code}, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if offline_transport:
        response.headers["Access-Control-Allow-Origin"] = "null"
    return response


def _api_error(message: str, code: str, status_code: int) -> JSONResponse:
    response = JSONResponse({"error": message, "code": code}, status_code=status_code)
    _set_control_no_store(response)
    return response


def _set_control_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _is_html_artifact(ref: Any) -> bool:
    mime = str(getattr(ref, "mime", "") or "").split(";", 1)[0].strip().casefold()
    if mime in _HTML_MIMES:
        return True
    return Path(str(getattr(ref, "name", "") or "")).suffix.casefold() in _HTML_SUFFIXES
