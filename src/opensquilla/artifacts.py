"""Generated artifact material references and storage helpers."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import mimetypes
import re
import secrets
from collections.abc import Iterator
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opensquilla.attachment_refs import _atomic_write_bytes, _link_or_copy, _validate_sha256
from opensquilla.paths import native_io_path

_log = logging.getLogger(__name__)

ARTIFACT_REF_KIND = "artifact_ref"
ARTIFACT_STORE = "artifacts"
ARTIFACT_SESSION_BUCKET = "s"
ARTIFACT_MATERIAL_NAME = "data"
ARTIFACT_THUMBNAIL_NAME = "thumb.webp"
ARTIFACT_THUMBNAIL_MAX_EDGE = 512
ARTIFACT_THUMBNAIL_QUALITY = 80
ARTIFACT_STORE_TOKEN_CHARS = 12
LEGACY_ARTIFACT_STORE_TOKEN_CHARS = 16
DEFAULT_ARTIFACT_MAX_BYTES = 30 * 1024 * 1024
DEFAULT_ARTIFACT_DISK_BUDGET_BYTES = 512 * 1024 * 1024
INSTALLER_ARTIFACT_MAX_BYTES = DEFAULT_ARTIFACT_DISK_BUDGET_BYTES
INSTALLER_ARTIFACT_SUFFIXES = frozenset(
    {
        ".appimage",
        ".deb",
        ".dmg",
        ".exe",
        ".msi",
        ".rpm",
        ".snap",
        ".zip",
    }
)
_INSTALLER_MIME_BY_SUFFIX = {
    ".appimage": "application/octet-stream",
    ".deb": "application/vnd.debian.binary-package",
    ".dmg": "application/x-apple-diskimage",
    ".exe": "application/vnd.microsoft.portable-executable",
    ".msi": "application/x-msi",
    ".rpm": "application/x-rpm",
    ".snap": "application/octet-stream",
    ".zip": "application/zip",
}

_UNSAFE_FILENAME_RE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]+')
_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_MIME_RE = re.compile(r"^[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+$")
_ARTIFACT_MARKER_RE = re.compile(
    # Anchor on the marker's guaranteed ` (mime)]` tail rather than stopping at
    # the first ']' — a name may legitimately contain ']'. Do NOT consume
    # surrounding whitespace (that glued adjacent words/lines together);
    # strip_artifact_markers_from_text collapses residual spacing afterwards.
    r"\[generated artifact omitted:\s*[^\n]+? "
    r"\((?:[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+|artifact)\)\]",
    re.IGNORECASE,
)
_PUBLIC_ARTIFACT_FIELDS = (
    "id",
    "kind",
    "sha256",
    "name",
    "mime",
    "size",
    "session_id",
    "source",
    "created_at",
    "store",
)


class ArtifactError(ValueError):
    """Base class for artifact store errors."""


class ArtifactNotFoundError(ArtifactError):
    """Raised when an artifact id is absent for the requested session."""


class ArtifactIntegrityError(ArtifactError):
    """Raised when material bytes no longer match artifact metadata."""


class ArtifactBudgetError(ArtifactError):
    """Raised when artifact publication exceeds file or disk budgets."""


class ArtifactPathError(ArtifactError):
    """Raised when a tool tries to publish a disallowed path."""


@dataclass(frozen=True)
class ArtifactRef:
    id: str
    sha256: str
    name: str
    mime: str
    size: int
    session_id: str
    session_key: str
    source: str
    created_at: str
    download_url: str
    kind: str = ARTIFACT_REF_KIND
    store: str = ARTIFACT_STORE
    has_thumbnail: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ArtifactRef:
        return cls(
            id=_validate_artifact_id(payload.get("id")),
            sha256=_validate_sha256(payload.get("sha256")),
            name=_safe_filename(str(payload.get("name") or "artifact")),
            mime=_safe_mime(payload.get("mime")),
            size=_validate_size(payload.get("size")),
            session_id=_validate_non_empty("session_id", payload.get("session_id")),
            session_key=_validate_non_empty("session_key", payload.get("session_key")),
            source=str(payload.get("source") or "unknown"),
            created_at=str(payload.get("created_at") or ""),
            download_url=str(payload.get("download_url") or ""),
            kind=str(payload.get("kind") or ARTIFACT_REF_KIND),
            store=str(payload.get("store") or ARTIFACT_STORE),
            has_thumbnail=bool(payload.get("has_thumbnail")),
        )


def artifact_marker(ref: dict[str, Any] | ArtifactRef) -> str:
    payload = ref.to_dict() if isinstance(ref, ArtifactRef) else ref
    name = payload.get("name") if isinstance(payload.get("name"), str) else "artifact"
    mime = payload.get("mime") if isinstance(payload.get("mime"), str) else "artifact"
    return f"[generated artifact omitted: {name} ({mime})]"


def strip_artifact_markers_from_text(text: str) -> str:
    if "[generated artifact omitted:" not in text:
        return text
    cleaned = _ARTIFACT_MARKER_RE.sub("", text.replace("\r\n", "\n"))
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def artifact_payload(event_or_ref: Any) -> dict[str, Any]:
    if isinstance(event_or_ref, ArtifactRef):
        raw = event_or_ref.to_dict()
    elif isinstance(event_or_ref, dict):
        raw = dict(event_or_ref)
    else:
        raw = {
            field: getattr(event_or_ref, field)
            for field in (*_PUBLIC_ARTIFACT_FIELDS, "download_url", "has_thumbnail")
            if hasattr(event_or_ref, field)
        }
    payload = {field: raw[field] for field in _PUBLIC_ARTIFACT_FIELDS if field in raw}
    artifact_id = payload.get("id")
    if artifact_id:
        payload["id"] = _validate_artifact_id(artifact_id)
        payload["download_url"] = artifact_download_url(payload["id"])
        # The public payload drops the internal ``has_thumbnail`` boolean and only
        # carries the reconstructed ``thumbnail_url`` string. A persisted transcript
        # artifact is therefore a public payload replayed through this helper: honor
        # an already-present ``thumbnail_url`` so the thumbnail survives history replay,
        # falling back to reconstruction from ``has_thumbnail`` for live events.
        if raw.get("has_thumbnail") or raw.get("thumbnail_url"):
            payload["thumbnail_url"] = artifact_thumbnail_url(payload["id"])
    return payload


def artifact_download_url(artifact_id: str) -> str:
    return f"/api/v1/artifacts/{_validate_artifact_id(artifact_id)}"


def is_installer_artifact_name(name: str | Path) -> bool:
    return Path(str(name)).suffix.casefold() in INSTALLER_ARTIFACT_SUFFIXES


def installer_artifact_mime(name: str | Path) -> str | None:
    return _INSTALLER_MIME_BY_SUFFIX.get(Path(str(name)).suffix.casefold())


def artifact_mime_for_name(name: str | Path) -> str:
    return (
        installer_artifact_mime(name)
        or mimetypes.guess_type(str(name))[0]
        or "application/octet-stream"
    )


def artifact_publish_max_bytes_for_name(
    name: str | Path,
    configured_max_bytes: int | None,
) -> int | None:
    if not is_installer_artifact_name(name):
        return configured_max_bytes
    if configured_max_bytes is None:
        return None
    return max(configured_max_bytes, INSTALLER_ARTIFACT_MAX_BYTES)


def artifact_thumbnail_url(artifact_id: str) -> str:
    return f"{artifact_download_url(artifact_id)}?variant=thumb"


def enrich_artifact_event_dict(event_dict: dict[str, Any]) -> dict[str, Any]:
    """Add a client-facing ``thumbnail_url`` to a serialized artifact event dict.

    The event dataclass carries the ``has_thumbnail`` boolean; this rebuilds the
    public variant URL from the artifact id when a thumbnail exists. The internal
    boolean is dropped so the wire payload matches the public artifact contract.
    """

    has_thumbnail = bool(event_dict.pop("has_thumbnail", False))
    artifact_id = event_dict.get("id")
    if has_thumbnail and isinstance(artifact_id, str) and artifact_id:
        try:
            event_dict["thumbnail_url"] = artifact_thumbnail_url(artifact_id)
        except ValueError:
            pass
    return event_dict


class ArtifactStore:
    """Session-scoped artifact store rooted outside the web static tree."""

    def __init__(self, media_root: str | Path) -> None:
        self.media_root = Path(media_root)

    def publish_bytes(
        self,
        payload: bytes,
        *,
        session_id: str,
        session_key: str,
        name: str,
        mime: str,
        source: str,
        max_bytes: int | None = DEFAULT_ARTIFACT_MAX_BYTES,
        disk_budget_bytes: int | None = DEFAULT_ARTIFACT_DISK_BUDGET_BYTES,
    ) -> ArtifactRef:
        if len(payload) == 0:
            raise ArtifactBudgetError("artifact payload is empty")
        if max_bytes is not None and len(payload) > max_bytes:
            raise ArtifactBudgetError(
                f"artifact exceeds per-file budget ({len(payload)} > {max_bytes})"
            )
        if disk_budget_bytes is not None:
            current = self._disk_usage_bytes()
            if current + len(payload) > disk_budget_bytes:
                raise ArtifactBudgetError(
                    "artifact material exceeds disk budget "
                    f"({current} + {len(payload)} > {disk_budget_bytes})"
                )

        session_id = _validate_non_empty("session_id", session_id)
        session_key = _validate_non_empty("session_key", session_key)
        artifact_id = f"art-{secrets.token_urlsafe(18)}"
        safe_name = _safe_filename(name)
        safe_mime = _safe_mime(mime)
        sha = hashlib.sha256(payload).hexdigest()
        created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        thumbnail_bytes = _build_thumbnail(payload, safe_mime)
        ref = ArtifactRef(
            id=artifact_id,
            sha256=sha,
            name=safe_name,
            mime=safe_mime,
            size=len(payload),
            session_id=session_id,
            session_key=session_key,
            source=source,
            created_at=created_at,
            download_url=artifact_download_url(artifact_id),
            has_thumbnail=thumbnail_bytes is not None,
        )

        artifact_dir = self._artifact_dir(session_id, artifact_id)
        native_artifact_dir = native_io_path(artifact_dir)
        native_artifact_dir.mkdir(parents=True, exist_ok=False)
        try:
            _atomic_write_bytes(artifact_dir / ARTIFACT_MATERIAL_NAME, payload)
            if thumbnail_bytes is not None:
                _atomic_write_bytes(artifact_dir / ARTIFACT_THUMBNAIL_NAME, thumbnail_bytes)
            _atomic_write_bytes(
                artifact_dir / "meta.json",
                json.dumps(ref.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8"),
            )
        except BaseException:
            for path in sorted(native_artifact_dir.glob("*"), reverse=True):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                native_artifact_dir.rmdir()
            except OSError:
                pass
            raise
        return ref

    def publish_file(
        self,
        path: str | Path,
        *,
        session_id: str,
        session_key: str,
        name: str | None = None,
        mime: str = "application/octet-stream",
        source: str,
        max_bytes: int | None = DEFAULT_ARTIFACT_MAX_BYTES,
        disk_budget_bytes: int | None = DEFAULT_ARTIFACT_DISK_BUDGET_BYTES,
    ) -> ArtifactRef:
        payload = native_io_path(path).read_bytes()
        return self.publish_bytes(
            payload,
            session_id=session_id,
            session_key=session_key,
            name=name or Path(path).name,
            mime=mime,
            source=source,
            max_bytes=max_bytes,
            disk_budget_bytes=disk_budget_bytes,
        )

    def resolve_for_download(
        self,
        artifact_id: str,
        *,
        session_id: str,
    ) -> tuple[ArtifactRef, Path]:
        artifact_id = _validate_artifact_id(artifact_id)
        meta_path = self._resolve_meta_path(session_id, artifact_id)
        native_meta_path = native_io_path(meta_path)
        if not native_meta_path.exists():
            raise ArtifactNotFoundError("artifact not found")
        ref = ArtifactRef.from_dict(json.loads(native_meta_path.read_text(encoding="utf-8")))
        if ref.session_id != session_id:
            raise ArtifactNotFoundError("artifact not found")
        path = self.path_for(ref)
        native_path = native_io_path(path)
        if not native_path.exists():
            raise ArtifactNotFoundError("artifact material not found")
        payload = native_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != ref.sha256:
            raise ArtifactIntegrityError("artifact material hash mismatch")
        if len(payload) != ref.size:
            raise ArtifactIntegrityError("artifact material size mismatch")
        return ref, path

    def find_existing_ref(
        self,
        *,
        session_id: str,
        session_key: str,
        sha256: str,
        name: str,
        mime: str | None = None,
    ) -> ArtifactRef | None:
        """Find a previously published logical deliverable in the same session."""

        session_id = _validate_non_empty("session_id", session_id)
        session_key = _validate_non_empty("session_key", session_key)
        sha256 = _validate_sha256(sha256)
        safe_name = _safe_filename(name)
        safe_mime = _safe_mime(mime) if mime else None
        for root in self._artifact_session_roots(session_id):
            native_root = native_io_path(root)
            if not native_root.exists():
                continue
            for meta_path in sorted(native_root.glob("*/meta.json")):
                try:
                    ref = ArtifactRef.from_dict(json.loads(meta_path.read_text(encoding="utf-8")))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if ref.session_id != session_id or ref.session_key != session_key:
                    continue
                if ref.sha256 != sha256 or ref.name != safe_name:
                    continue
                if safe_mime is not None and ref.mime != safe_mime:
                    continue
                try:
                    self.resolve_for_download(ref.id, session_id=session_id)
                except (ArtifactNotFoundError, ArtifactIntegrityError):
                    continue
                return ref
        return None

    def copy_session_artifacts(
        self,
        *,
        source_session_id: str,
        target_session_id: str,
        target_session_key: str,
    ) -> int:
        """Duplicate every artifact owned by ``source_session_id`` into ``target_session_id``.

        Used when a session is forked: the child transcript references each artifact by
        its stable id and a session-less download URL, but the store is session-scoped
        and ``resolve_for_download`` rejects a mismatched session id, so the child needs
        its own copy. Each artifact keeps its id; the copied ``meta.json`` is rebound to
        the child's session id/key and the material (plus any thumbnail) is materialized
        under the child's session bucket. Idempotent and best-effort: already-copied or
        unreadable artifacts are skipped. Returns the number of artifacts copied.
        """
        source_session_id = _validate_non_empty("source_session_id", source_session_id)
        target_session_id = _validate_non_empty("target_session_id", target_session_id)
        target_session_key = _validate_non_empty("target_session_key", target_session_key)
        if target_session_id == source_session_id:
            return 0
        copied = 0
        for meta_path in self._iter_session_meta_paths(source_session_id):
            try:
                ref = ArtifactRef.from_dict(json.loads(meta_path.read_text(encoding="utf-8")))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if ref.session_id != source_session_id:
                continue
            try:
                if self._copy_one_artifact(ref, target_session_id, target_session_key):
                    copied += 1
            except (OSError, ValueError):
                # Best-effort: a single bad artifact (filesystem error, invalid sha)
                # must not stop the rest. ArtifactError is a ValueError subclass.
                continue
        return copied

    def _iter_session_meta_paths(self, session_id: str) -> Iterator[Path]:
        """Yield every artifact ``meta.json`` for ``session_id`` across all store layouts."""
        roots = (
            *self._artifact_session_roots(session_id),
            self.media_root
            / ARTIFACT_STORE
            / _safe_token(_validate_non_empty("session_id", session_id)),
        )
        seen: set[Path] = set()
        for root in roots:
            native_root = native_io_path(root)
            if not native_root.exists():
                continue
            for meta_path in sorted(native_root.glob("*/meta.json")):
                resolved = meta_path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                yield meta_path

    def _copy_one_artifact(
        self,
        ref: ArtifactRef,
        target_session_id: str,
        target_session_key: str,
    ) -> bool:
        """Materialize one artifact under the child session; return True when copied."""
        source_material = self.path_for(ref)
        if not native_io_path(source_material).exists():
            return False
        target_dir = self._artifact_dir(target_session_id, ref.id)
        target_material = target_dir / ARTIFACT_MATERIAL_NAME
        target_meta = target_dir / "meta.json"
        if native_io_path(target_meta).exists() and native_io_path(target_material).exists():
            return False
        native_io_path(target_dir).mkdir(parents=True, exist_ok=True)
        if not native_io_path(target_material).exists():
            _link_or_copy(source_material, target_material)
        has_thumbnail = False
        if ref.has_thumbnail:
            target_thumb = target_dir / ARTIFACT_THUMBNAIL_NAME
            source_thumb = self.thumbnail_path_for(ref)
            if native_io_path(target_thumb).exists():
                has_thumbnail = True
            elif native_io_path(source_thumb).exists():
                _link_or_copy(source_thumb, target_thumb)
                has_thumbnail = True
        # Only advertise a thumbnail the child actually has on disk: a source whose
        # sidecar cannot be located (e.g. a legacy layout without one) is copied
        # without it rather than leaving a dangling has_thumbnail in the child meta.
        child_ref = replace(
            ref,
            session_id=target_session_id,
            session_key=target_session_key,
            has_thumbnail=has_thumbnail,
        )
        _atomic_write_bytes(
            target_meta,
            json.dumps(child_ref.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )
        return True

    def resolve_thumbnail_for_download(
        self,
        artifact_id: str,
        *,
        session_id: str,
    ) -> tuple[ArtifactRef, Path] | None:
        """Return the webp thumbnail sidecar for an artifact, or None if absent.

        Validates and resolves the artifact exactly like ``resolve_for_download`` so
        auth/session scoping is identical, then returns the thumbnail path only when
        the sidecar exists. Older artifacts without a thumbnail yield None so callers
        can fall back to the full file.
        """

        ref, _path = self.resolve_for_download(artifact_id, session_id=session_id)
        if not ref.has_thumbnail:
            return None
        thumb_path = self.thumbnail_path_for(ref)
        if not native_io_path(thumb_path).exists():
            return None
        return ref, thumb_path

    def path_for(self, ref: ArtifactRef) -> Path:
        _validate_sha256(ref.sha256)
        for artifact_dir in (
            self._artifact_dir(ref.session_id, ref.id),
            self._legacy_short_artifact_dir(ref.session_id, ref.id),
        ):
            material_path = artifact_dir / ARTIFACT_MATERIAL_NAME
            if native_io_path(material_path).exists():
                return material_path
        return self._legacy_artifact_dir(ref.session_id, ref.id) / ref.sha256

    def thumbnail_path_for(self, ref: ArtifactRef) -> Path:
        for artifact_dir in (
            self._artifact_dir(ref.session_id, ref.id),
            self._legacy_short_artifact_dir(ref.session_id, ref.id),
        ):
            thumbnail_path = artifact_dir / ARTIFACT_THUMBNAIL_NAME
            if native_io_path(thumbnail_path).exists():
                return thumbnail_path
        return self._artifact_dir(ref.session_id, ref.id) / ARTIFACT_THUMBNAIL_NAME

    def _artifact_dir(self, session_id: str, artifact_id: str) -> Path:
        return self._short_artifact_dir(
            session_id,
            artifact_id,
            token_chars=ARTIFACT_STORE_TOKEN_CHARS,
        )

    def _legacy_short_artifact_dir(self, session_id: str, artifact_id: str) -> Path:
        return self._short_artifact_dir(
            session_id,
            artifact_id,
            token_chars=LEGACY_ARTIFACT_STORE_TOKEN_CHARS,
        )

    def _short_artifact_dir(self, session_id: str, artifact_id: str, *, token_chars: int) -> Path:
        return (
            self.media_root
            / ARTIFACT_STORE
            / ARTIFACT_SESSION_BUCKET
            / _session_store_token(session_id, chars=token_chars)
            / _artifact_store_token(artifact_id, chars=token_chars)
        )

    def _artifact_session_roots(self, session_id: str) -> tuple[Path, ...]:
        return (
            self.media_root
            / ARTIFACT_STORE
            / ARTIFACT_SESSION_BUCKET
            / _session_store_token(session_id, chars=ARTIFACT_STORE_TOKEN_CHARS),
            self.media_root
            / ARTIFACT_STORE
            / ARTIFACT_SESSION_BUCKET
            / _session_store_token(session_id, chars=LEGACY_ARTIFACT_STORE_TOKEN_CHARS),
        )

    def _legacy_artifact_dir(self, session_id: str, artifact_id: str) -> Path:
        return (
            self.media_root
            / ARTIFACT_STORE
            / _safe_token(_validate_non_empty("session_id", session_id))
            / _validate_artifact_id(artifact_id)
        )

    def _resolve_meta_path(self, session_id: str, artifact_id: str) -> Path:
        for artifact_dir in (
            self._artifact_dir(session_id, artifact_id),
            self._legacy_short_artifact_dir(session_id, artifact_id),
            self._legacy_artifact_dir(session_id, artifact_id),
        ):
            meta_path = artifact_dir / "meta.json"
            if native_io_path(meta_path).exists():
                return meta_path
        return self._artifact_dir(session_id, artifact_id) / "meta.json"

    def _disk_usage_bytes(self) -> int:
        root = self.media_root / ARTIFACT_STORE
        native_root = native_io_path(root)
        if not native_root.exists():
            return 0
        total = 0
        for path in native_root.rglob("*"):
            try:
                if path.is_file() and path.name != "meta.json":
                    total += path.stat().st_size
            except OSError:
                continue
        return total


def _build_thumbnail(payload: bytes, mime: str) -> bytes | None:
    """Render a small webp thumbnail for image artifacts.

    Returns the encoded webp bytes, or None when the artifact is not an image,
    Pillow is unavailable, or the bytes cannot be decoded. Any failure here is
    non-fatal: the caller publishes the artifact without a thumbnail.
    """

    if not mime.startswith("image/"):
        return None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(payload)) as image:
            image.load()
            if image.mode in ("RGBA", "LA", "P"):
                source = image.convert("RGBA")
            else:
                source = image.convert("RGB")
            source.thumbnail(
                (ARTIFACT_THUMBNAIL_MAX_EDGE, ARTIFACT_THUMBNAIL_MAX_EDGE),
                Image.Resampling.LANCZOS,
            )
            out = io.BytesIO()
            source.save(out, format="WEBP", quality=ARTIFACT_THUMBNAIL_QUALITY)
            return out.getvalue()
    except Exception:
        _log.debug("artifact thumbnail generation failed for mime=%s", mime, exc_info=True)
        return None


def _safe_filename(name: str) -> str:
    cleaned = Path(name).name.strip() or "artifact"
    cleaned = _UNSAFE_FILENAME_RE.sub("_", cleaned).strip()
    return cleaned[:160] or "artifact"


def _safe_mime(value: Any) -> str:
    if isinstance(value, str):
        normalized = value.split(";", 1)[0].strip()
        if _SAFE_MIME_RE.fullmatch(normalized):
            return normalized
    return "application/octet-stream"


def _safe_token(value: str) -> str:
    cleaned = _SAFE_TOKEN_RE.sub("_", value.strip())
    return cleaned[:180] or "session"


def _session_store_token(session_id: str, *, chars: int = ARTIFACT_STORE_TOKEN_CHARS) -> str:
    raw = _validate_non_empty("session_id", session_id)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:chars]


def _artifact_store_token(artifact_id: str, *, chars: int = ARTIFACT_STORE_TOKEN_CHARS) -> str:
    raw = _validate_artifact_id(artifact_id)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:chars]


def _validate_artifact_id(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("art-"):
        raise ValueError("artifact id is invalid")
    if _safe_token(value) != value:
        raise ValueError("artifact id is invalid")
    return value


def _validate_non_empty(field: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value


def _validate_size(value: Any) -> int:
    if not isinstance(value, int) or value < 0:
        raise ValueError("artifact size is invalid")
    return value
