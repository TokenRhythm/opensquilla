"""Bounded, temporary upload storage behind the existing ``file_uuid`` API.

Production uploads keep their bytes on disk with a versioned metadata commit
record, so unexpired uploads survive a gateway restart without retaining every
payload in RAM. Recovery reads only bounded metadata and file stats; payload
integrity is checked when the upload is consumed. Without a storage directory
the store uses the same bounded in-memory lifecycle.

Upload leases remain temporary. Accepted turns and pending queue items copy
bytes into their existing durable material stores before evicting the upload.
Legacy metadata-only records still report ``AttachmentLostInRestartError``.

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
import math
import os
import re
import secrets
import stat
import time
import uuid as _uuid
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from opensquilla.application.artifact_workbench import (
    AttachmentClaimError,
    AttachmentEmptyError,
    AttachmentOpaqueOversizeError,
    AttachmentStage,
    AttachmentStagingApplication,
    AttachmentStagingPolicy,
)
from opensquilla.contracts.attachments import (
    ALLOWED_MEDIA_TYPES,
    IMAGE_ATTACHMENT_MIMES,
    OPAQUE_ATTACHMENT_BYTES,
    attachment_category,
    attachment_size_limit_for_mime,
    can_stage_attachment_mime,
    normalize_attachment_mime,
)
from opensquilla.contracts.image_validation import validate_image_bytes
from opensquilla.gateway.adapters.artifact_content import (
    GatewayAttachmentMimePolicy,
    GatewayAttachmentStagingPort,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.origin_guard import forbidden_origin_response, request_origin_allowed
from opensquilla.paths import native_io_path

log = logging.getLogger(__name__)


_ALLOWED_MIMES: frozenset[str] = ALLOWED_MEDIA_TYPES

_DEFAULT_MAX_FILE_BYTES = 30 * 1024 * 1024
_DEFAULT_MAX_TOTAL_BYTES = 300 * 1024 * 1024
_DEFAULT_TTL_SECONDS = 10 * 60
_METADATA_VERSION = 1
_MAX_METADATA_BYTES = 64 * 1024
_UPLOAD_ID = re.compile(r"u-[A-Za-z0-9_-]{1,128}\Z")
_UPLOAD_TEMP = re.compile(r"\.(u-[A-Za-z0-9_-]{1,128})\.(bin|meta)\.[0-9a-f]{16}\.tmp\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


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
    """The upload metadata exists but its original bytes cannot be verified.

    Concrete UX hook for "uploaded file lost in restart".
    """


@dataclass
class _Entry:
    name: str
    mime: str
    sha256: str
    size: int
    bytes: bytes | None
    expires_at: float


class UploadStore:
    """Temporary file-backed uploads, or bounded memory when no directory is set.

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
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._lock_for_locks = asyncio.Lock()
        self._disk_root: Path | None = None
        if self.marker_dir is not None:
            directory = native_io_path(self.marker_dir)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self._is_link(directory.lstat()):
                raise ValueError("upload directory must not be a symlink or junction")
            self._disk_root = directory.resolve(strict=True)
            self._restore_entries()

    # ------------------------------------------------------------------ helpers

    async def _get_uuid_lock(self, file_uuid: str) -> asyncio.Lock:
        async with self._lock_for_locks:
            lock = self._locks.get(file_uuid)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[file_uuid] = lock
            return lock

    def _marker_path(self, file_uuid: str) -> Path | None:
        return self._disk_path(file_uuid, ".meta")

    def _disk_path(self, file_uuid: str, suffix: str) -> Path | None:
        if self._disk_root is None or _UPLOAD_ID.fullmatch(file_uuid) is None:
            return None
        # Resolve the configured directory once, then refuse replacement with a
        # link. Neither client filenames nor metadata can choose a disk path.
        root = native_io_path(self._disk_root)
        if self._is_link(root.lstat()) or root.resolve() != self._disk_root:
            raise OSError("upload directory was replaced with a link")
        return self._disk_root / f"{file_uuid}{suffix}"

    @staticmethod
    def _is_link(info: os.stat_result) -> bool:
        return stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )

    @classmethod
    def _read_regular_file(cls, path: Path, limit: int) -> bytes:
        native_path = native_io_path(path)
        before = native_path.lstat()
        if cls._is_link(before) or not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise OSError("upload material is not a bounded regular file")
        descriptor = os.open(native_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                cls._is_link(opened)
                or not stat.S_ISREG(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise OSError("upload material changed while opening")
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise OSError("upload material exceeds its recorded size")
        return data

    def _read_marker(self, file_uuid: str) -> dict[str, Any] | None:
        try:
            path = self._marker_path(file_uuid)
            if path is None:
                return None
            value = json.loads(self._read_regular_file(path, _MAX_METADATA_BYTES))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    def _marker_expired(self, marker: dict[str, Any]) -> bool:
        expires_at = marker.get("expires_at")
        return (
            isinstance(expires_at, int | float)
            and self._finite_number(expires_at)
            and expires_at < self._now()
        )

    @staticmethod
    def _finite_number(value: Any) -> bool:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            descriptor = os.open(
                native_io_path(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(native_io_path(temporary), native_io_path(path))
        finally:
            native_io_path(temporary).unlink(missing_ok=True)

    def _persist_entry(self, file_uuid: str, entry: _Entry, payload: bytes) -> None:
        path = self._disk_path(file_uuid, ".bin")
        marker = self._marker_path(file_uuid)
        assert path is not None and marker is not None
        metadata = json.dumps({
            "version": _METADATA_VERSION,
            "sha256": entry.sha256,
            "mime": entry.mime,
            "name": entry.name,
            "size": entry.size,
            "expires_at": entry.expires_at,
        }, allow_nan=False).encode("utf-8")
        if len(metadata) > _MAX_METADATA_BYTES:
            raise ValueError("upload metadata is too large")
        self._atomic_write(path, payload)
        self._atomic_write(marker, metadata)

    def _restore_entries(self) -> None:
        assert self._disk_root is not None
        root = native_io_path(self._disk_root)
        for marker_path in root.glob("u-*.meta"):
            file_uuid = marker_path.stem
            if _UPLOAD_ID.fullmatch(file_uuid) is None:
                continue
            marker = self._read_marker(file_uuid)
            if marker is not None and self._marker_expired(marker):
                self._delete_files(file_uuid)
                continue
            if marker is not None and "version" not in marker:
                # Old releases wrote metadata without payloads. Preserve the
                # specific recoverable lost-in-restart result for their clients.
                continue
            entry = self._entry_from_metadata(marker)
            if entry is not None:
                path = self._disk_path(file_uuid, ".bin")
                assert path is not None
                try:
                    info = native_io_path(path).lstat()
                    if (
                        not self._is_link(info)
                        and stat.S_ISREG(info.st_mode)
                        and info.st_size == entry.size
                    ):
                        self._entries[file_uuid] = entry
                        continue
                except OSError:
                    pass
            self._delete_files(file_uuid)
        # A crash between payload and metadata publication leaves no lease.
        # Only inspect this store's flat directory, never the full media tree.
        for path in root.glob("u-*.bin"):
            if path.stem not in self._entries and _UPLOAD_ID.fullmatch(path.stem):
                self._delete_files(path.stem)
        for path in root.glob(".u-*.tmp"):
            match = _UPLOAD_TEMP.fullmatch(path.name)
            if match is not None:
                try:
                    # Recheck the anchored directory before removing an
                    # interrupted atomic write. Unlink never follows its target.
                    if self._disk_path(match[1], ".meta") is not None:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _entry_from_metadata(self, marker: dict[str, Any] | None) -> _Entry | None:
        if marker is None or set(marker) != {
            "version", "sha256", "mime", "name", "size", "expires_at"
        }:
            return None
        version, sha, mime, name, size, expires = (
            marker[key] for key in ("version", "sha256", "mime", "name", "size", "expires_at")
        )
        if (
            type(version) is not int or version != _METADATA_VERSION
            or not isinstance(sha, str) or _SHA256.fullmatch(sha) is None
            or not isinstance(name, str)
            or not isinstance(mime, str) or normalize_attachment_mime(mime) != mime
            or type(size) is not int or size < 0
            or not self._finite_number(expires)
            or (not self.accept_opaque and mime not in _ALLOWED_MIMES)
        ):
            return None
        staged = can_stage_attachment_mime(mime) and (
            self.accept_opaque or attachment_category(mime) in {"pdf", "image", "office"}
        )
        if size > min(self.max_file_bytes, attachment_size_limit_for_mime(mime, staged=staged)):
            return None
        return _Entry(name, mime, sha, size, None, expires)

    def _delete_files(self, file_uuid: str) -> None:
        for suffix in (".meta", ".bin"):
            try:
                path = self._disk_path(file_uuid, suffix)
                if path is not None:
                    native_io_path(path).unlink(missing_ok=True)
            except OSError:  # pragma: no cover - cleanup cannot follow a replaced directory
                pass

    def _read_payload(self, file_uuid: str, entry: _Entry) -> bytes:
        path = self._disk_path(file_uuid, ".bin")
        assert path is not None
        payload = self._read_regular_file(path, entry.size)
        if len(payload) != entry.size or hashlib.sha256(payload).hexdigest() != entry.sha256:
            raise OSError("upload material no longer matches its metadata")
        return payload

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
                f"upload exceeds {max_bytes} byte cap for {normalized_mime} (got {len(payload)})"
            )
        if len(payload) > self.max_total_bytes:
            # A payload larger than the aggregate cap can never be staged, so
            # this is a permanent per-payload condition (413), never the
            # retryable store-full rejection.
            raise UploadOversizeError(
                f"upload of {len(payload)} bytes exceeds the "
                f"{self.max_total_bytes} byte staged-upload store cap"
            )

        if normalized_mime in IMAGE_ATTACHMENT_MIMES:
            try:
                validate_image_bytes(payload, normalized_mime)
            except ValueError as exc:
                raise UploadUnsupportedMimeError(str(exc)) from exc

        file_uuid = f"u-{_uuid.uuid4().hex}"
        sha = hashlib.sha256(payload).hexdigest()
        expires_at = self._now() + self.ttl_seconds
        entry = _Entry(
            name=name,
            mime=normalized_mime,
            sha256=sha,
            size=len(payload),
            bytes=payload if self._disk_root is None else None,
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
            # storage). The check and reservation sit in one zero-await stretch, so
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
                if self._disk_root is not None:
                    write = asyncio.create_task(
                        asyncio.to_thread(self._persist_entry, file_uuid, entry, payload)
                    )
                    try:
                        await asyncio.shield(write)
                    except BaseException:
                        # A cancelled coroutine cannot stop a filesystem worker.
                        # Shield every cleanup wait: shutdown/deadline callers
                        # may cancel this coroutine repeatedly while it drains.
                        # The worker must finish before removing its files.
                        while not write.done():
                            try:
                                await asyncio.shield(write)
                            except BaseException:
                                continue
                        if not write.cancelled():
                            write.exception()  # Retrieve a failed worker's exception.
                        self._entries.pop(file_uuid, None)
                        self._delete_files(file_uuid)
                        raise
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

        if _UPLOAD_ID.fullmatch(file_uuid) is None:
            raise AttachmentNotFoundError(file_uuid)
        await self._sweep_expired_locked()
        lock = await self._get_uuid_lock(file_uuid)
        async with lock:
            entry = self._entries.get(file_uuid)
            if entry is None:
                marker = self._read_marker(file_uuid)
                if marker is not None and self._marker_expired(marker):
                    self._delete_files(file_uuid)
                    raise AttachmentNotFoundError(file_uuid)
                if marker is not None:
                    raise AttachmentLostInRestartError(file_uuid)
                raise AttachmentNotFoundError(file_uuid)
            if entry.expires_at < self._now():
                self._entries.pop(file_uuid, None)
                self._delete_files(file_uuid)
                raise AttachmentNotFoundError(file_uuid)
            payload = entry.bytes
            if payload is None:
                try:
                    payload = await asyncio.to_thread(self._read_payload, file_uuid, entry)
                except OSError as exc:
                    raise AttachmentLostInRestartError(file_uuid) from exc
            return payload, {
                "name": entry.name,
                "mime": entry.mime,
                "sha256": entry.sha256,
                "size": entry.size,
            }

    async def evict(self, file_uuid: str) -> bool:
        """Explicit eviction; returns True if the entry existed."""

        if _UPLOAD_ID.fullmatch(file_uuid) is None:
            return False
        lock = await self._get_uuid_lock(file_uuid)
        async with lock:
            existed = file_uuid in self._entries
            self._entries.pop(file_uuid, None)
            self._delete_files(file_uuid)
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
            self._delete_files(u)
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


def _authorization_token_matches(config: GatewayConfig, request: Request) -> bool:
    token = _extract_authorization_token(request)
    if token == config.auth.token:
        return True
    from opensquilla.gateway.desktop_ownership import (
        active_desktop_gateway_auth_token_matches,
    )

    return active_desktop_gateway_auth_token_matches(token)


def register_upload_routes(
    app: Starlette,
    *,
    config: GatewayConfig,
    store: UploadStore,
) -> None:
    """Register POST /api/v1/files/upload on the given Starlette app."""

    attachments_cfg = getattr(config, "attachments", None)
    accept_opaque = bool(getattr(attachments_cfg, "accept_opaque", True))
    opaque_cap = getattr(attachments_cfg, "opaque_max_bytes", None)
    if not isinstance(opaque_cap, int) or opaque_cap <= 0:
        opaque_cap = OPAQUE_ATTACHMENT_BYTES
    staging = AttachmentStagingApplication(
        GatewayAttachmentStagingPort(store),
        AttachmentStagingPolicy(
            accept_opaque=accept_opaque,
            opaque_max_bytes=opaque_cap,
        ),
        GatewayAttachmentMimePolicy(),
    )

    async def upload_handler(request: Request) -> JSONResponse:
        if not request_origin_allowed(request, config):
            return forbidden_origin_response()
        if config.auth.mode == "token":
            if config.auth.token and not _authorization_token_matches(config, request):
                return JSONResponse(
                    {
                        "error": (
                            "Authorization header (Bearer …) required for /api/v1/files/upload"
                        ),
                        "code": "UNAUTHORIZED",
                    },
                    status_code=401,
                )

        try:
            form = await request.form()
        except Exception as exc:
            return JSONResponse({"error": f"multipart/form-data required: {exc}"}, status_code=400)

        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return JSONResponse({"error": "missing 'file' multipart field"}, status_code=400)

        filename = getattr(upload, "filename", None) or "attachment"
        content_type = getattr(upload, "content_type", None) or form.get("mime") or ""

        # Legacy fail-closed admission rejects a missing/invalid claim before
        # the payload is read, preserving the strict-mode error precedence.
        try:
            staging.validate_claim(str(content_type))
        except AttachmentClaimError:
            return JSONResponse(
                {"error": "missing or invalid 'mime' / content-type"}, status_code=400
            )

        payload = await upload.read()
        if not isinstance(payload, bytes):
            return JSONResponse({"error": "empty upload"}, status_code=400)

        try:
            staged = await staging.stage(AttachmentStage(filename, str(content_type), payload))
        except AttachmentEmptyError:
            return JSONResponse({"error": "empty upload"}, status_code=400)
        except AttachmentOpaqueOversizeError as exc:
            return JSONResponse({"error": str(exc), "code": "TOO_LARGE"}, status_code=413)
        except UploadOversizeError as exc:
            return JSONResponse({"error": str(exc), "code": "TOO_LARGE"}, status_code=413)
        except UploadUnsupportedMimeError as exc:
            return JSONResponse(
                {"error": str(exc), "code": "UNSUPPORTED_MEDIA_TYPE"}, status_code=415
            )
        except UploadStoreFullError as exc:
            # Retryable capacity condition (staged entries expire within the
            # TTL), distinct from per-file 413 and rate-limit 429.
            return JSONResponse({"error": str(exc), "code": "UPLOAD_STORE_FULL"}, status_code=507)

        return JSONResponse(
            {
                "file_uuid": staged.file_uuid,
                "filename": staged.filename,
                "mime": staged.mime,
                "size": staged.size,
                # Staged lifetime so a client can re-upload before a slow compose
                # sends against an expired uuid (issue #468).
                "expires_at": staged.expires_at,
                "ttl_seconds": store.ttl_seconds,
            }
        )

    app.router.routes.append(Route("/api/v1/files/upload", upload_handler, methods=["POST"]))


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
