"""Workspace materialization for transcript-backed attachments."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from opensquilla.attachment_refs import (
    is_attachment_ref,
    make_attachment_ref,
    read_attachment_ref_bytes,
)
from opensquilla.contracts.attachment_display import (
    normalize_attachment_display_mime,
    normalize_attachment_display_name,
)
from opensquilla.contracts.attachments import (
    IMAGE_ATTACHMENT_BYTES,
    IMAGE_ATTACHMENT_MIMES,
    attachment_size_limit_for_mime,
    can_stage_attachment_mime,
    normalize_attachment_mime,
)
from opensquilla.contracts.image_validation import validate_image_bytes
from opensquilla.paths import native_io_path

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._@+=, -]+")
_WHITESPACE = re.compile(r"\s+")


class AttachmentWorkspaceConflictError(ValueError):
    """An immutable input was changed externally; never overwrite it."""


class AttachmentWorkspaceBudgetError(ValueError):
    """Materializing the payload would push the workspace attachment
    directory past its disk budget. Existing files are never evicted; the
    attachment degrades to an unavailable marker instead."""


@dataclass(frozen=True)
class AttachmentWorkspaceMaterialization:
    """Result of attempting to make an attachment available inside a workspace."""

    available: bool
    name: str
    mime: str
    size: int
    rel_path: str | None = None
    error: str | None = None
    working_path: str | None = None


def workspace_attachment_budget_from_config(config: Any) -> int | None:
    """Resolve attachments.workspace_attachment_disk_budget_bytes, or None.

    Guarded like every attachments-config read: absent section, non-int, or
    non-positive values mean "unbounded" so config-less runners keep working.
    """

    attachments_cfg = getattr(config, "attachments", None)
    value = getattr(attachments_cfg, "workspace_attachment_disk_budget_bytes", None)
    if isinstance(value, int) and value > 0:
        return value
    return None


def is_materializable_attachment_mime(
    mime: Any,
    materializable_mimes: Collection[str] | None,
) -> bool:
    if not isinstance(mime, str):
        return False
    # None means "materialize any type": opaque attachments are reachable only
    # through their workspace copy, so the materializer must not gate them.
    if materializable_mimes is None:
        return True
    return mime in materializable_mimes


def render_attachment_material_marker(
    result: AttachmentWorkspaceMaterialization,
    *,
    prefix: str,
) -> str:
    if result.available and result.rel_path:
        return (
            f"[{prefix}: {result.name} ({result.mime}, {result.size} bytes) "
            f"at {result.rel_path}; immutable original"
            + (f"; editable working file at {result.working_path}" if result.working_path else
               "; file tools create an independent working file on first edit")
            + "]"
        )
    detail = result.error or "workspace materialization unavailable"
    return f"[{prefix}: {result.name} ({result.mime}): {detail}]"


def render_historical_attachment_material_marker(
    result: AttachmentWorkspaceMaterialization,
) -> str:
    """Bound the materialization details included in historical provider text.

    Filesystem exceptions can include absolute paths, and working-file state
    can outlive the workspace that created it. Neither is a safe input to a
    provider-history budget. Keep a canonical relative path when available;
    otherwise emit a fixed unavailable reason while retaining diagnostics in
    ``result.error`` for the caller.
    """

    safe_mime = normalize_attachment_mime(result.mime)
    if (
        safe_mime is None
        or len(safe_mime) > 120
        or re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", safe_mime) is None
    ):
        safe_mime = "application/octet-stream"
    safe_result = replace(result, name=_safe_filename(result.name), mime=safe_mime)
    unavailable = (
        f"[historical attachment unavailable: {safe_result.name} ({safe_result.mime})]"
    )
    if not result.available or not isinstance(result.rel_path, str):
        return unavailable
    relative = PurePosixPath(result.rel_path)
    parts = relative.parts
    if (
        relative.is_absolute()
        or relative.as_posix() != result.rel_path
        or len(parts) != 4
        or parts[:2] != (".opensquilla", "attachments")
        or len(parts[2]) > 180
        or parts[2] != _safe_path_segment(parts[2], fallback="session")
        or len(parts[3]) > 193
        or re.fullmatch(r"[0-9a-f]{12}-.+", parts[3]) is None
        or parts[3][13:] != _safe_filename(parts[3][13:])
    ):
        return unavailable
    expected_working = (relative.parent / "working" / relative.name).as_posix()
    working_path = result.working_path if result.working_path == expected_working else None
    return render_attachment_material_marker(
        replace(safe_result, working_path=working_path),
        prefix="historical attachment available",
    )


def historical_attachment_capacity_marker(
    attachment: Mapping[str, Any],
    *,
    session_id: str,
    sha256_ref: str | None,
) -> str:
    """Return a side-effect-free upper estimate of an opaque history marker.

    The provider can see an omitted marker, an unavailable marker, or a
    materialized workspace path. Construct all three using the same renderer
    and retain the largest token and character costs without reading or
    writing attachment material.
    """

    raw_mime = (
        attachment.get("type") or attachment.get("mime") or attachment.get("media_type")
    )
    mime = normalize_attachment_display_mime(raw_mime)
    label = normalize_attachment_display_name(attachment.get("name"))
    candidates = [
        f"[historical attachment omitted: {label} ({mime})]",
        f"[historical attachment unavailable: {label} ({mime})]",
        f"[historical attachment unavailable: {label} ({mime}): "
        "attachment data is not valid base64]",
        f"[historical attachment unavailable: {label} ({mime}): "
        "invalid attachment reference]",
    ]
    has_material = bool(
        attachment.get("data") or attachment.get("sha256_ref")
        or attachment.get("sha256") or attachment.get("material_id")
    )
    if has_material:
        scope = _safe_path_segment(session_id or "s" * 180, fallback="session")
        sha = sha256_ref if isinstance(sha256_ref, str) and re.fullmatch(
            r"[0-9a-f]{64}", sha256_ref
        ) else "0" * 64
        safe_name = _safe_filename(_attachment_name(dict(attachment)))
        rel_path = PurePosixPath(
            ".opensquilla", "attachments", scope, f"{sha[:12]}-{safe_name}"
        ).as_posix()
        # Replay selects ``type`` before ``mime``/``media_type`` and passes
        # that value to the materializer.  A legacy row can contain both
        # fields with different values, so use the replay-selected MIME here.
        material_mime = raw_mime.strip() if isinstance(raw_mime, str) else mime
        # A materialized payload is bounded by the file staging limits. The
        # long decimal reserve also covers legacy rows without a stored size.
        result = AttachmentWorkspaceMaterialization(
            available=True,
            name=safe_name,
            mime=material_mime,
            size=10**20 - 1,
            rel_path=rel_path,
        )
        candidates.append(render_historical_attachment_material_marker(result))
        working_path = (
            PurePosixPath(rel_path).parent / "working" / PurePosixPath(rel_path).name
        ).as_posix()
        candidates.append(render_historical_attachment_material_marker(
            replace(result, working_path=working_path),
        ))

    from opensquilla.token_estimation import estimate_tokens

    target_tokens = max(estimate_tokens(candidate) for candidate in candidates)
    # Character admission measures the serialized provider payload. A name
    # containing quotes, backslashes, or control characters expands in JSON.
    target_chars = max(len(json.dumps(candidate, ensure_ascii=False)) for candidate in candidates)
    marker = max(candidates, key=lambda candidate: (estimate_tokens(candidate), len(candidate)))
    serialized_chars = len(json.dumps(marker, ensure_ascii=False))
    if serialized_chars < target_chars:
        marker += "!" * (target_chars - serialized_chars)
    # A SHA derived only at materialization time and an unknown session scope
    # can alter tokenizer segmentation, though their ASCII lengths are fixed.
    unknown_path_bytes = (12 if sha256_ref is None and has_material else 0) + (
        180 if not session_id and has_material else 0
    )
    target_tokens += unknown_path_bytes
    while estimate_tokens(marker) < target_tokens:
        marker += " !"
    return marker


def historical_image_material_capacity_marker(
    attachment: Mapping[str, Any],
    *,
    session_id: str,
) -> str:
    """Reserve the image workspace marker without materializing its bytes.

    The replay path is generated from a sanitized session, content hash, and
    filename. A missing session or a hash that changes during materialization
    can change tokenization, so leave enough room for every unknown path byte.
    The display name follows the runtime's image marker, which can be longer
    than the sanitized path component.
    """

    label = normalize_attachment_display_name(
        attachment.get("name"), fallback="image",
    )
    raw_mime = attachment.get("type") or attachment.get("mime") or attachment.get("media_type")
    mime = normalize_attachment_display_mime(raw_mime)
    scope = _safe_path_segment(session_id or "s" * 180, fallback="session")
    # Use a stable path shape regardless of PNG compression or content. The
    # actual twelve hash characters can vary in tokenizer cost, reserved below.
    sha = "0" * 64
    path = PurePosixPath(
        ".opensquilla", "attachments", scope,
        f"{sha[:12]}-{_safe_filename(_attachment_name(dict(attachment)))}",
    ).as_posix()
    marker = f"[attachment available: {label} ({mime}) at {path}]"

    from opensquilla.token_estimation import estimate_tokens

    # A different twelve-character hash or unknown session scope can contain
    # up to one tokenizer unit per ASCII byte. The suffix also covers their
    # character cost, without depending on filesystem or provider state.
    unknown_path_bytes = 12 + (180 if not session_id else 0)
    target_tokens = estimate_tokens(marker) + unknown_path_bytes
    while estimate_tokens(marker) < target_tokens:
        marker += " !"
    return marker


class AttachmentWorkspaceMaterializer:
    """Materialize attachment bytes into a controlled workspace path."""

    def __init__(
        self,
        *,
        media_root: Path,
        workspace_dir: str | Path,
        materializable_mimes: Collection[str] | None = None,
        disk_budget_bytes: int | None = None,
        authorize_write: Callable[[Path], None] | None = None,
        working_files: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._media_root = Path(media_root)
        self._workspace_root = Path(workspace_dir)
        self._materializable_mimes = (
            frozenset(materializable_mimes) if materializable_mimes is not None else None
        )
        self._disk_budget_bytes = disk_budget_bytes
        self._authorize_write = authorize_write
        self._working_files = working_files if working_files is not None else {}
        # Lazily-scanned bytes under <workspace>/.opensquilla/attachments,
        # kept current across this instance's writes so a batch of
        # materializations pays for one directory walk.
        self._usage_bytes: int | None = None

    def _attachments_root(self) -> Path:
        return self._workspace_root.resolve() / ".opensquilla" / "attachments"

    def _current_usage_bytes(self) -> int:
        if self._usage_bytes is None:
            total = 0
            root = native_io_path(self._attachments_root())
            if root.is_dir():
                for path in root.rglob("*"):
                    try:
                        if path.is_file() and not path.is_symlink():
                            total += path.stat().st_size
                    except OSError:
                        # Session-delete cleanup may race the walk; a vanished
                        # file simply stops counting.
                        continue
            self._usage_bytes = total
        return self._usage_bytes

    def materialize(
        self,
        attachment: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> AttachmentWorkspaceMaterialization:
        name = _safe_filename(_attachment_name(attachment))
        mime = _attachment_mime(attachment)
        size = _attachment_size(attachment)
        if not is_materializable_attachment_mime(mime, self._materializable_mimes):
            return AttachmentWorkspaceMaterialization(
                available=False,
                name=name,
                mime=mime,
                size=size,
                error="attachment type is not materializable",
            )

        try:
            ref = _coerce_attachment_ref(attachment, session_id=session_id)
            payload = read_attachment_ref_bytes(ref, media_root=self._media_root)
            return self._materialize_payload(
                payload,
                name=name,
                mime=mime,
                scope=session_id or ref["scope"],
                sha=ref["sha256"],
            )
        except Exception as exc:  # noqa: BLE001 - materialization is best-effort
            return AttachmentWorkspaceMaterialization(
                available=False,
                name=name,
                mime=mime,
                size=size,
                error=str(exc),
            )

    def materialize_bytes(
        self,
        payload: bytes,
        *,
        name: str,
        mime: str,
        session_id: str | None,
    ) -> AttachmentWorkspaceMaterialization:
        safe_name = _safe_filename(name)
        safe_mime = mime.strip() if isinstance(mime, str) else "application/octet-stream"
        size = len(payload)
        if not is_materializable_attachment_mime(safe_mime, self._materializable_mimes):
            return AttachmentWorkspaceMaterialization(
                available=False,
                name=safe_name,
                mime=safe_mime,
                size=size,
                error="attachment type is not materializable",
            )
        if not isinstance(session_id, str) or not session_id:
            return AttachmentWorkspaceMaterialization(
                available=False,
                name=safe_name,
                mime=safe_mime,
                size=size,
                error="attachment session scope is required",
            )
        try:
            return self._materialize_payload(
                payload,
                name=safe_name,
                mime=safe_mime,
                scope=session_id,
                sha=hashlib.sha256(payload).hexdigest(),
            )
        except Exception as exc:  # noqa: BLE001 - materialization is best-effort
            return AttachmentWorkspaceMaterialization(
                available=False,
                name=safe_name,
                mime=safe_mime,
                size=size,
                error=str(exc),
            )

    def materialize_attachment_path(
        self, attachment: dict[str, Any], session_id: str
    ) -> str | None:
        """Resolve retained original bytes to a controlled path for compaction.

        Display metadata and arbitrary envelope paths never grant file access.
        Native images retain their pixel-validation contract; ordinary files
        are preserved without decoding or parsing their semantic contents.
        """
        mime = normalize_attachment_mime(_attachment_mime(attachment))
        if mime in IMAGE_ATTACHMENT_MIMES:
            return self.materialize_image_path(attachment, session_id)
        if not mime or not session_id or attachment.get("missing_reason"):
            return None
        data = attachment.get("data")
        inline = isinstance(data, str) and bool(data)
        max_bytes = attachment_size_limit_for_mime(
            mime, staged=not inline and can_stage_attachment_mime(mime),
        )
        try:
            if isinstance(data, str) and data:
                if len(data) > ((max_bytes + 2) // 3) * 4:
                    return None
                payload = base64.b64decode(data, validate=True)
            else:
                ref = _coerce_attachment_ref(attachment, session_id=session_id)
                scope = ref.get("scope")
                if (
                    ref.get("store") != "transcript"
                    or not isinstance(scope, str)
                    or not scope
                    or scope in {".", ".."}
                    or any(separator in scope for separator in ("/", "\\", "\x00"))
                    or _attachment_size(ref) > max_bytes
                ):
                    return None
                payload = read_attachment_ref_bytes(ref, media_root=self._media_root)
            if len(payload) > max_bytes:
                return None
            result = self.materialize_bytes(
                payload, name=_attachment_name(attachment), mime=mime, session_id=session_id,
            )
            return result.rel_path if result.available else None
        except (OSError, ValueError):
            return None

    def materialize_image_path(
        self, attachment: dict[str, Any], session_id: str
    ) -> str | None:
        """Return a readable workspace path for retained, validated image material.

        The caller authorizes retention and supplies a canonical transcript
        attachment. Never use an arbitrary path stored in the envelope.
        """
        mime = _attachment_mime(attachment)
        if mime not in IMAGE_ATTACHMENT_MIMES or not session_id or attachment.get("missing_reason"):
            return None
        try:
            data = attachment.get("data")
            if isinstance(data, str) and data:
                if len(data) > ((IMAGE_ATTACHMENT_BYTES + 2) // 3) * 4:
                    return None
                payload = base64.b64decode(data, validate=True)
            else:
                ref = _coerce_attachment_ref(attachment, session_id=session_id)
                if _attachment_size(ref) > IMAGE_ATTACHMENT_BYTES:
                    return None
                payload = read_attachment_ref_bytes(ref, media_root=self._media_root)
            if len(payload) > IMAGE_ATTACHMENT_BYTES:
                return None
            validate_image_bytes(payload, mime)
            result = self.materialize_bytes(
                payload, name=_attachment_name(attachment), mime=mime, session_id=session_id
            )
            return result.rel_path if result.available else None
        except (OSError, ValueError):
            return None

    def _materialize_payload(
        self,
        payload: bytes,
        *,
        name: str,
        mime: str,
        scope: str,
        sha: str,
    ) -> AttachmentWorkspaceMaterialization:
        target = self._target_path(scope=scope, sha=sha, name=name)
        self._write_or_reuse(target, payload=payload, sha=sha, size=len(payload))
        rel_path = target.relative_to(self._workspace_root.resolve()).as_posix()
        self._working_files.setdefault(rel_path, {"sha256": sha, "session_id": scope})
        return AttachmentWorkspaceMaterialization(
            available=True,
            name=name,
            mime=mime,
            size=len(payload),
            rel_path=rel_path,
            working_path=self._working_files.get(rel_path, {}).get("path"),
        )

    def _target_path(self, *, scope: str, sha: str, name: str) -> Path:
        root = self._workspace_root.resolve()
        session_segment = _safe_path_segment(scope, fallback="session")
        filename = f"{sha[:12]}-{_safe_filename(name)}"
        target_dir = root / ".opensquilla" / "attachments" / session_segment
        resolved_dir = target_dir.resolve()
        _assert_relative_to(resolved_dir, root)
        target = resolved_dir / filename
        _assert_relative_to(target.resolve(strict=False), root)
        if self._authorize_write is not None:
            self._authorize_write(target)
        native_io_path(target_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
        return target

    def _write_or_reuse(
        self,
        target: Path,
        *,
        payload: bytes,
        sha: str,
        size: int,
    ) -> None:
        io_target = native_io_path(target)
        if io_target.is_symlink():
            raise AttachmentWorkspaceConflictError("immutable attachment conflicts with a symlink")
        if io_target.exists():
            if not io_target.is_file():
                raise AttachmentWorkspaceConflictError("immutable attachment is not a regular file")
            existing = io_target.read_bytes()
            if len(existing) == size and hashlib.sha256(existing).hexdigest() == sha:
                return
            raise AttachmentWorkspaceConflictError(
                "immutable attachment content conflict; existing file was preserved"
            )
        if self._disk_budget_bytes is not None:
            usage = self._current_usage_bytes()
            if usage + size > self._disk_budget_bytes:
                raise AttachmentWorkspaceBudgetError(
                    "workspace attachment budget exceeded "
                    f"({usage} + {size} > {self._disk_budget_bytes} bytes); "
                    "delete finished sessions or raise "
                    "attachments.workspace_attachment_disk_budget_bytes"
                )
        tmp_path = native_io_path(target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp"))
        try:
            with open(tmp_path, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_path, 0o600)
            # Publish without replacing a file that appeared during materialization.
            # Hard links are atomic on the supported local filesystems (including NTFS).
            try:
                os.link(tmp_path, io_target)
            except FileExistsError:
                if io_target.is_symlink() or not io_target.is_file():
                    raise AttachmentWorkspaceConflictError("immutable attachment target conflict")
                existing = io_target.read_bytes()
                if len(existing) != size or hashlib.sha256(existing).hexdigest() != sha:
                    raise AttachmentWorkspaceConflictError(
                        "immutable attachment content conflict; existing file was preserved"
                    ) from None
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        _assert_relative_to(
            io_target.resolve(strict=True), native_io_path(self._workspace_root).resolve(),
        )
        written = io_target.read_bytes()
        if len(written) != size or hashlib.sha256(written).hexdigest() != sha:
            raise ValueError("workspace material hash mismatch")
        if self._usage_bytes is not None:
            self._usage_bytes += size


def _coerce_attachment_ref(
    attachment: dict[str, Any],
    *,
    session_id: str | None,
) -> dict[str, Any]:
    if is_attachment_ref(attachment):
        return attachment
    sha = attachment.get("sha256_ref") or attachment.get("sha256") or attachment.get("material_id")
    if not isinstance(sha, str) or not sha:
        raise ValueError("attachment sha256_ref is required")
    scope = attachment.get("scope")
    if not isinstance(scope, str) or not scope:
        scope = session_id
    if not isinstance(scope, str) or not scope:
        raise ValueError("attachment session scope is required")
    return make_attachment_ref(
        sha256=sha,
        name=_attachment_name(attachment),
        mime=_attachment_mime(attachment),
        size=_attachment_size(attachment),
        session_id=scope,
        source="transcript",
    )


def _attachment_name(attachment: dict[str, Any]) -> str:
    value = attachment.get("name")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "attachment"


def _attachment_mime(attachment: dict[str, Any]) -> str:
    for key in ("mime", "type", "media_type", "mime_type"):
        value = attachment.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "application/octet-stream"


def _attachment_size(attachment: dict[str, Any]) -> int:
    value = attachment.get("size")
    return value if isinstance(value, int) and value >= 0 else -1


def _safe_filename(value: str) -> str:
    cleaned = value.replace("\\", "/").split("/")[-1]
    cleaned = cleaned.replace("\x00", "")
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", cleaned)
    cleaned = cleaned.strip(" .")
    if not cleaned:
        cleaned = "attachment"
    if len(cleaned) > 180:
        suffix = Path(cleaned).suffix
        if len(suffix) >= 180:
            # A hostile extension can itself exceed the whole filename limit.
            cleaned = cleaned[:180].rstrip(" .")
        else:
            stem = cleaned[: 180 - len(suffix)]
            cleaned = f"{stem}{suffix}"
        cleaned = cleaned or "attachment"
    return cleaned


def _safe_path_segment(value: str, *, fallback: str) -> str:
    cleaned = _safe_filename(value)
    cleaned = cleaned.replace("/", "_").replace("\\", "_")
    return cleaned or fallback


def _assert_relative_to(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("workspace material path escapes workspace") from exc
