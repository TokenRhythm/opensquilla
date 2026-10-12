"""Workspace materialization for transcript-backed attachments."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opensquilla.attachment_refs import (
    is_attachment_ref,
    make_attachment_ref,
    read_attachment_ref_bytes,
)

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._@+=, -]+")
_WHITESPACE = re.compile(r"\s+")


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
            f"at {result.rel_path}]"
        )
    detail = result.error or "workspace materialization unavailable"
    return f"[{prefix}: {result.name} ({result.mime}): {detail}]"


class AttachmentWorkspaceMaterializer:
    """Materialize attachment bytes into a controlled workspace path."""

    def __init__(
        self,
        *,
        media_root: Path,
        workspace_dir: str | Path,
        materializable_mimes: Collection[str] | None = None,
        disk_budget_bytes: int | None = None,
    ) -> None:
        self._media_root = Path(media_root)
        self._workspace_root = Path(workspace_dir)
        self._materializable_mimes = (
            frozenset(materializable_mimes) if materializable_mimes is not None else None
        )
        self._disk_budget_bytes = disk_budget_bytes
        # Lazily-scanned bytes under <workspace>/.opensquilla/attachments,
        # kept current across this instance's writes so a batch of
        # materializations pays for one directory walk.
        self._usage_bytes: int | None = None

    def _attachments_root(self) -> Path:
        return self._workspace_root.resolve() / ".opensquilla" / "attachments"

    def _current_usage_bytes(self) -> int:
        if self._usage_bytes is None:
            total = 0
            root = self._attachments_root()
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
                scope=ref["scope"],
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
        return AttachmentWorkspaceMaterialization(
            available=True,
            name=name,
            mime=mime,
            size=len(payload),
            rel_path=rel_path,
        )

    def _target_path(self, *, scope: str, sha: str, name: str) -> Path:
        root = self._workspace_root.resolve()
        session_segment = _safe_path_segment(scope, fallback="session")
        filename = f"{sha[:12]}-{_safe_filename(name)}"
        target_dir = root / ".opensquilla" / "attachments" / session_segment
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved_dir = target_dir.resolve()
        _assert_relative_to(resolved_dir, root)
        target = resolved_dir / filename
        _assert_relative_to(target.resolve(strict=False), root)
        return target

    def _write_or_reuse(
        self,
        target: Path,
        *,
        payload: bytes,
        sha: str,
        size: int,
    ) -> None:
        existing_size: int | None = None
        if target.exists():
            if target.is_symlink():
                pass
            elif not target.is_file():
                raise ValueError("workspace material target is not a regular file")
            else:
                existing = target.read_bytes()
                if len(existing) == size and hashlib.sha256(existing).hexdigest() == sha:
                    # Reuse is always free: an already-materialized file must
                    # never flip to unavailable when the budget fills later.
                    return
                existing_size = len(existing)
        if self._disk_budget_bytes is not None:
            usage = self._current_usage_bytes() - (existing_size or 0)
            if usage + size > self._disk_budget_bytes:
                raise AttachmentWorkspaceBudgetError(
                    "workspace attachment budget exceeded "
                    f"({usage} + {size} > {self._disk_budget_bytes} bytes); "
                    "delete finished sessions or raise "
                    "attachments.workspace_attachment_disk_budget_bytes"
                )
        tmp_path = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
        try:
            with open(tmp_path, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, target)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        _assert_relative_to(target.resolve(strict=True), self._workspace_root.resolve())
        written = target.read_bytes()
        if len(written) != size or hashlib.sha256(written).hexdigest() != sha:
            raise ValueError("workspace material hash mismatch")
        if self._usage_bytes is not None:
            self._usage_bytes += size - (existing_size or 0)


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
        stem = cleaned[: max(1, 180 - len(suffix))]
        cleaned = f"{stem}{suffix}"
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
