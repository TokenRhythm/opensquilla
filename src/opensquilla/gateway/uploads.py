"""Bridge upload endpoint + in-memory store for the file_uuid path.

The store maps an opaque ``file_uuid`` to the bytes of an uploaded file.
A ``.meta`` marker file is written to disk at insert time so that, after
a gateway restart, ``get(file_uuid)`` for an entry whose bytes are gone
returns the specific :class:`AttachmentLostInRestartError` instead of the
generic :class:`AttachmentNotFoundError`. That lets clients show
"uploaded file lost in restart, please re-upload" instead of
"unknown uuid".

Per-uuid ``asyncio.Lock`` protects the resolver/sweeper race: both the
resolver and the sweeper acquire the lock; the sweeper skips locked uuids
and retries on the next sweep tick. Refcounting was rejected as more state
under cancellation. The explicit eviction hook is wired into
``rpc_sessions._handle_sessions_send`` and fires only on the success
path after ``start_turn_via_runtime`` returns (locked semantic).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid as _uuid
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from opensquilla.contracts.attachment_sniff import sniff_mime_from_bytes
from opensquilla.contracts.attachments import (
    ALLOWED_MEDIA_TYPES,
    MSG_MIME,
    OPAQUE_ATTACHMENT_BYTES,
    OPAQUE_MIME,
    attachment_category,
    attachment_size_limit_for_mime,
    can_stage_attachment_mime,
    normalize_attachment_mime,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.origin_guard import forbidden_origin_response, request_origin_allowed
from opensquilla.paths import native_io_path

log = logging.getLogger(__name__)


_ALLOWED_MIMES: frozenset[str] = ALLOWED_MEDIA_TYPES

_DEFAULT_MAX_FILE_BYTES = 30 * 1024 * 1024
_DEFAULT_MAX_TOTAL_BYTES = 300 * 1024 * 1024
_DEFAULT_TTL_SECONDS = 10 * 60


class UploadStoreError(Exception):
    """Base class for upload-store-specific errors."""


class UploadOversizeError(UploadStoreError):
    pass


class UploadUnsupportedMimeError(UploadStoreError):
    pass


class UploadStoreFullError(UploadStoreError):
    """Admitting the payload would push staged bytes past the aggregate cap.

    Raised instead of evicting: staged uuids carry a TTL promise to clients,
    so the store rejects new work rather than breaking outstanding uploads.
    """


class AttachmentNotFoundError(UploadStoreError):
    """The uuid is unknown to this store (never inserted, or already swept)."""


class AttachmentLostInRestartError(UploadStoreError):
    """The marker exists on disk but the in-memory bytes are gone.

    Concrete UX hook for "uploaded file lost in restart".
    """


@dataclass
class _Entry:
    name: str
    mime: str
    sha256: str
    size: int
    bytes: bytes
    expires_at: float


class UploadStore:
    """Bridge upload store: in-memory bytes + on-disk ``.meta`` markers.

    The store enforces the configured size and MIME caps so a malicious
    or buggy client cannot smuggle disallowed bytes past it. The route
    handler MAY duplicate those checks but MUST NOT skip them.
    """

    def __init__(
        self,
        marker_dir: Path | None,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
        accept_opaque: bool = True,
        *,
        max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
    ) -> None:
        self.marker_dir: Path | None = Path(marker_dir) if marker_dir is not None else None
        self.ttl_seconds = ttl_seconds
        self.max_file_bytes = max_file_bytes
        self.accept_opaque = accept_opaque
        self.max_total_bytes = max_total_bytes
        self._entries: dict[str, _Entry] = {}
        # WeakValueDictionary so a per-uuid lock is reclaimed as soon as no
        # coroutine holds it — a get()/put() miss can no longer leak locks
        # without bound. While an operation is inside `async with lock` a strong
        # ref keeps the entry alive, so concurrent access to the same uuid still
        # serializes on the same lock.
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._lock_for_locks = asyncio.Lock()
        if self.marker_dir is not None:
            native_io_path(self.marker_dir).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ helpers

    async def _get_uuid_lock(self, file_uuid: str) -> asyncio.Lock:
        async with self._lock_for_locks:
            lock = self._locks.get(file_uuid)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[file_uuid] = lock
            return lock

    def _marker_path(self, file_uuid: str) -> Path | None:
        if self.marker_dir is None:
            return None
        return self.marker_dir / f"{file_uuid}.meta"

    def _read_marker(self, file_uuid: str) -> dict[str, Any] | None:
        path = self._marker_path(file_uuid)
        if path is None:
            return None
        native_path = native_io_path(path)
        try:
            return cast(dict[str, Any], json.loads(native_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return None

    def _marker_expired(self, marker: dict[str, Any]) -> bool:
        expires_at = marker.get("expires_at")
        if not isinstance(expires_at, (int, float, str)):
            return False
        try:
            return float(expires_at) < self._now()
        except ValueError:
            return False

    def _write_marker(self, file_uuid: str, meta: dict[str, Any]) -> None:
        path = self._marker_path(file_uuid)
        if path is None:
            return
        try:
            native_io_path(path).write_text(json.dumps(meta), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - filesystem failure path
            log.warning("uploads.marker_write_failed uuid=%s err=%s", file_uuid, exc)

    def _delete_marker(self, file_uuid: str) -> None:
        path = self._marker_path(file_uuid)
        if path is None:
            return
        try:
            native_io_path(path).unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass

    def _now(self) -> float:
        return time.time()

    # ------------------------------------------------------------------ public

    async def put(self, name: str, mime: str, payload: bytes) -> str:
        """Insert a new attachment; return its opaque file_uuid."""

        file_uuid, _expires_at = await self.put_with_expiry(name, mime, payload)
        return file_uuid

    async def put_with_expiry(self, name: str, mime: str, payload: bytes) -> tuple[str, float]:
        """Insert a new attachment; return ``(file_uuid, expires_at_epoch)``.

        Exposing ``expires_at`` lets the upload route tell the client the staged
        lifetime up front so a slow compose can re-upload before the send fails.
        """

        normalized_mime = normalize_attachment_mime(mime)
        if normalized_mime is None:
            raise UploadUnsupportedMimeError(f"mime {mime!r} is not allowed")
        if not self.accept_opaque and normalized_mime not in _ALLOWED_MIMES:
            raise UploadUnsupportedMimeError(f"mime {mime!r} is not allowed")
        # Email stays non-stageable policy-wise, so its cap resolves to the
        # inline text ceiling even on this staged path. Strict deployments
        # keep the legacy stageable set (pdf/image/office), so text stays at
        # the 2MB inline cap rather than the 30MiB staged-text ceiling.
        if self.accept_opaque:
            staged = can_stage_attachment_mime(normalized_mime)
        else:
            staged = attachment_category(normalized_mime) in {"pdf", "image", "office"}
        mime_limit = attachment_size_limit_for_mime(normalized_mime, staged=staged)
        max_bytes = min(self.max_file_bytes, mime_limit)
        if len(payload) > max_bytes:
            raise UploadOversizeError(
                f"upload exceeds {max_bytes} byte cap for {normalized_mime} "
                f"(got {len(payload)})"
            )
        if len(payload) > self.max_total_bytes:
            # A payload larger than the aggregate cap can never be staged, so
            # this is a permanent per-payload condition (413), never the
            # retryable store-full rejection.
            raise UploadOversizeError(
                f"upload of {len(payload)} bytes exceeds the "
                f"{self.max_total_bytes} byte staged-upload store cap"
            )

        file_uuid = f"u-{_uuid.uuid4().hex}"
        sha = hashlib.sha256(payload).hexdigest()
        expires_at = self._now() + self.ttl_seconds
        entry = _Entry(
            name=name,
            mime=normalized_mime,
            sha256=sha,
            size=len(payload),
            bytes=payload,
            expires_at=expires_at,
        )

        # Sweep before insert so the eviction loop runs at least once per
        # successful put (no background thread needed).
        await self._sweep_expired_locked()

        lock = await self._get_uuid_lock(file_uuid)
        admitted = False
        async with lock:
            # Aggregate cap, computed over ALL held entries (expired entries a
            # concurrent resolver keeps locked past the sweep still occupy
            # RAM). The check and the insert sit in one zero-await stretch, so
            # concurrent puts cannot interleave between them and overshoot.
            staged_total = sum(e.size for e in self._entries.values())
            if staged_total + entry.size > self.max_total_bytes:
                log.warning(
                    "uploads.store_full staged=%d incoming=%d cap=%d",
                    staged_total,
                    entry.size,
                    self.max_total_bytes,
                )
            else:
                admitted = True
                self._entries[file_uuid] = entry
                self._write_marker(
                    file_uuid,
                    {
                        "sha256": sha,
                        "mime": normalized_mime,
                        "name": name,
                        "size": len(payload),
                        "expires_at": expires_at,
                    },
                )
        if not admitted:
            # Drop the per-uuid lock minted for this rejected upload so bursts
            # of rejections cannot grow the lock dict without bound.
            async with self._lock_for_locks:
                self._locks.pop(file_uuid, None)
            raise UploadStoreFullError(
                f"upload store is full ({staged_total} of {self.max_total_bytes} "
                f"bytes staged); staged uploads expire within "
                f"{int(self.ttl_seconds)}s — retry shortly or send pending "
                "attachments first"
            )
        return file_uuid, expires_at

    async def get(self, file_uuid: str) -> tuple[bytes, dict[str, Any]]:
        """Return ``(bytes, metadata)`` for an active uuid; raise otherwise."""

        await self._sweep_expired_locked()
        lock = await self._get_uuid_lock(file_uuid)
        async with lock:
            entry = self._entries.get(file_uuid)
            if entry is None:
                marker = self._read_marker(file_uuid)
                if marker is not None and self._marker_expired(marker):
                    self._delete_marker(file_uuid)
                    raise AttachmentNotFoundError(file_uuid)
                if marker is not None:
                    raise AttachmentLostInRestartError(file_uuid)
                raise AttachmentNotFoundError(file_uuid)
            if entry.expires_at < self._now():
                self._entries.pop(file_uuid, None)
                self._delete_marker(file_uuid)
                raise AttachmentNotFoundError(file_uuid)
            return entry.bytes, {
                "name": entry.name,
                "mime": entry.mime,
                "sha256": entry.sha256,
                "size": entry.size,
            }

    async def evict(self, file_uuid: str) -> bool:
        """Explicit eviction; returns True if the entry existed."""

        lock = await self._get_uuid_lock(file_uuid)
        async with lock:
            existed = file_uuid in self._entries
            self._entries.pop(file_uuid, None)
            self._delete_marker(file_uuid)
        async with self._lock_for_locks:
            self._locks.pop(file_uuid, None)
        return existed

    async def _sweep_expired_locked(self) -> int:
        now = self._now()
        expired = [u for u, e in list(self._entries.items()) if e.expires_at < now]
        if not expired:
            return 0
        count = 0
        removed: list[str] = []
        for u in expired:
            lock = self._locks.get(u)
            # Skip-without-blocking: if the lock is held a resolver/upload
            # is in flight; this pass leaves it for the next sweep tick.
            if lock is not None and lock.locked():
                continue
            self._entries.pop(u, None)
            self._delete_marker(u)
            removed.append(u)
            count += 1
        if removed:
            async with self._lock_for_locks:
                for u in removed:
                    lock = self._locks.get(u)
                    if lock is not None and not lock.locked():
                        self._locks.pop(u, None)
        return count


# ---------------------------------------------------------------------------
# HTTP route registration.
# ---------------------------------------------------------------------------


def _extract_authorization_token(request: Request) -> str | None:
    """Header-only token extraction.

    The multipart upload endpoint deliberately rejects query-string token auth
    (which the existing JSON-RPC routes accept for legacy convenience). A
    cross-origin attacker can craft a multipart POST with a forged ``?token=…``
    query but cannot set arbitrary headers on a plain ``<form>`` submission, so
    requiring the ``Authorization`` header closes that surface.
    """

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return request.headers.get("x-opensquilla-token")


def register_upload_routes(
    app: Starlette,
    *,
    config: GatewayConfig,
    store: UploadStore,
) -> None:
    """Register POST /api/v1/files/upload on the given Starlette app."""

    async def upload_handler(request: Request) -> JSONResponse:
        if not request_origin_allowed(request, config):
            return forbidden_origin_response()
        if config.auth.mode == "token":
            if config.auth.token and _extract_authorization_token(request) != config.auth.token:
                return JSONResponse(
                    {
                        "error": (
                            "Authorization header (Bearer …) required for "
                            "/api/v1/files/upload"
                        ),
                        "code": "UNAUTHORIZED",
                    },
                    status_code=401,
                )

        try:
            form = await request.form()
        except Exception as exc:
            return JSONResponse(
                {"error": f"multipart/form-data required: {exc}"}, status_code=400
            )

        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return JSONResponse(
                {"error": "missing 'file' multipart field"}, status_code=400
            )

        filename = getattr(upload, "filename", None) or "attachment"
        content_type = getattr(upload, "content_type", None) or form.get("mime") or ""
        normalized_mime = normalize_attachment_mime(content_type)

        attachments_cfg = getattr(config, "attachments", None)
        accept_opaque = bool(getattr(attachments_cfg, "accept_opaque", True))

        # Legacy fail-closed admission rejects a missing/invalid claim before
        # the payload is read, preserving the strict-mode error precedence.
        if not accept_opaque and normalized_mime is None:
            return JSONResponse(
                {"error": "missing or invalid 'mime' / content-type"}, status_code=400
            )

        payload = await upload.read()
        if not isinstance(payload, bytes) or len(payload) == 0:
            return JSONResponse(
                {"error": "empty upload"}, status_code=400
            )

        if not accept_opaque:
            if normalized_mime is None:
                # Unreachable: rejected before the payload read; kept so the
                # legacy branch below is well-typed.
                return JSONResponse(
                    {"error": "missing or invalid 'mime' / content-type"}, status_code=400
                )
            # Legacy fail-closed admission: the claimed mime alone decides.
            resolved_mime = normalized_mime
        elif normalized_mime in _ALLOWED_MIMES:
            resolved_mime = normalized_mime
        else:
            # Unrendered or missing claim: adopt the sniffed rendered type when
            # the bytes identify one (with the OLE carve-out mirroring ingest);
            # otherwise stage as opaque under the claimed label.
            sniffed = sniff_mime_from_bytes(payload)
            if sniffed in _ALLOWED_MIMES and not (
                sniffed == MSG_MIME and normalized_mime is not None
            ):
                resolved_mime = sniffed
            else:
                resolved_mime = normalized_mime or OPAQUE_MIME

        if accept_opaque and attachment_category(resolved_mime) == "opaque":
            opaque_cap = getattr(attachments_cfg, "opaque_max_bytes", None)
            if not isinstance(opaque_cap, int) or opaque_cap <= 0:
                opaque_cap = OPAQUE_ATTACHMENT_BYTES
            if len(payload) > opaque_cap:
                return JSONResponse(
                    {
                        "error": (
                            f"upload exceeds {opaque_cap} byte cap for "
                            f"{resolved_mime} (got {len(payload)})"
                        ),
                        "code": "TOO_LARGE",
                    },
                    status_code=413,
                )

        try:
            file_uuid, expires_at = await store.put_with_expiry(
                filename, resolved_mime, payload
            )
        except UploadOversizeError as exc:
            return JSONResponse({"error": str(exc), "code": "TOO_LARGE"}, status_code=413)
        except UploadUnsupportedMimeError as exc:
            return JSONResponse(
                {"error": str(exc), "code": "UNSUPPORTED_MEDIA_TYPE"}, status_code=415
            )
        except UploadStoreFullError as exc:
            # Retryable capacity condition (staged entries expire within the
            # TTL), distinct from per-file 413 and rate-limit 429.
            return JSONResponse(
                {"error": str(exc), "code": "UPLOAD_STORE_FULL"}, status_code=507
            )

        return JSONResponse(
            {
                "file_uuid": file_uuid,
                "filename": filename,
                "mime": resolved_mime,
                "size": len(payload),
                # Staged lifetime so a client can re-upload before a slow compose
                # sends against an expired uuid (issue #468).
                "expires_at": expires_at,
                "ttl_seconds": store.ttl_seconds,
            }
        )

    app.router.routes.append(
        Route("/api/v1/files/upload", upload_handler, methods=["POST"])
    )


# ---------------------------------------------------------------------------
# Singleton accessor.
# ---------------------------------------------------------------------------


_default_store: UploadStore | None = None


def get_upload_store() -> UploadStore:
    """Return the process-global upload store, lazily constructed.

    Tests that need a clean store should pass a fresh ``UploadStore`` to the
    function under test; production code that just wants the default reaches
    in via this accessor.
    """

    global _default_store
    if _default_store is None:
        _default_store = UploadStore(marker_dir=None)
    return _default_store


def set_upload_store(store: UploadStore | None) -> None:
    """Override the singleton (production wiring + test reset)."""

    global _default_store
    _default_store = store
