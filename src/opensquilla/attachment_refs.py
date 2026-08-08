"""Attachment material references shared across gateway, transcript, and runtime."""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path
from typing import Any

from opensquilla.paths import native_io_path

ATTACHMENT_REF_KIND = "attachment_ref"
TRANSCRIPT_MATERIAL_STORE = "transcript"


class AttachmentMaterialBudgetError(ValueError):
    """Raised when material bytes cannot be written within the configured budget."""


def is_attachment_ref(attachment: Any) -> bool:
    return isinstance(attachment, dict) and attachment.get("kind") == ATTACHMENT_REF_KIND


def transcript_material_dir(media_root: Path, session_id: str) -> Path:
    return Path(media_root) / "transcripts" / session_id


def _validate_sha256(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("attachment ref sha256 is invalid")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("attachment ref sha256 is invalid") from exc
    return value.lower()


def transcript_material_path(media_root: Path, session_id: str, sha256: str) -> Path:
    sha = _validate_sha256(sha256)
    return transcript_material_dir(media_root, session_id) / sha


def _media_disk_usage_bytes(media_root: Path) -> int:
    root = Path(media_root) / "transcripts"
    native_root = native_io_path(root)
    if not native_root.exists():
        return 0
    total = 0
    for path in native_root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    native_io_path(path.parent).mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{secrets.token_hex(4)}")
    try:
        with open(native_io_path(tmp_path), "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(native_io_path(tmp_path), native_io_path(path))
    except BaseException:
        try:
            native_io_path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _link_or_copy(src: Path, dst: Path) -> None:
    """Materialize ``dst`` from ``src`` cheaply: hardlink when the filesystem allows
    it, otherwise fall back to an atomic byte copy.

    Material files are content-addressed and never mutated in place (writers always
    replace via a fresh temp file), so a hardlink is safe — the destination keeps its
    own directory entry to the same bytes and survives deletion of the source. The
    copy fallback covers cross-device links, filesystems/platforms without hardlink
    support, and any other ``OSError`` from ``os.link``.
    """
    native_io_path(dst.parent).mkdir(parents=True, exist_ok=True)
    try:
        os.link(native_io_path(src), native_io_path(dst))
        return
    except OSError:
        pass
    _atomic_write_bytes(dst, native_io_path(src).read_bytes())


def write_transcript_material(
    *,
    media_root: Path,
    session_id: str,
    payload: bytes,
    disk_budget_bytes: int | None = None,
) -> tuple[str, Path, bool]:
    """Write payload into the transcript material store and return ``(sha, path, wrote)``."""

    sha = hashlib.sha256(payload).hexdigest()
    path = transcript_material_path(media_root, session_id, sha)
    if native_io_path(path).exists():
        return sha, path, False

    if disk_budget_bytes is not None:
        current = _media_disk_usage_bytes(media_root)
        if current + len(payload) > disk_budget_bytes:
            raise AttachmentMaterialBudgetError(
                "attachment material exceeds transcript disk budget "
                f"({current} + {len(payload)} > {disk_budget_bytes})"
            )

    _atomic_write_bytes(path, payload)
    return sha, path, True


def copy_transcript_material(
    *,
    media_root: Path,
    source_session_id: str,
    target_session_id: str,
    material_ids: set[str] | frozenset[str] | None = None,
) -> int:
    """Duplicate a session's transcript attachment material into a forked child.

    When a session is forked the child transcript references the same attachments by
    content hash, but the material store is keyed by session id, so the child would
    otherwise have nothing on disk to serve over the download route or to replay. This
    copies every material file from the source session's store into the target's unless
    ``material_ids`` restricts the copy to a reachable subset. It is idempotent
    (existing target files are left untouched) and best-effort (individual copy failures
    are skipped). Returns the count of files materialized.
    """
    selected_ids = (
        None
        if material_ids is None
        else {_validate_sha256(material_id) for material_id in material_ids}
    )
    source_dir = transcript_material_dir(media_root, source_session_id)
    native_source_dir = native_io_path(source_dir)
    if not native_source_dir.is_dir():
        return 0
    target_dir = transcript_material_dir(media_root, target_session_id)
    copied = 0
    for source_path in sorted(native_source_dir.iterdir()):
        if not source_path.is_file():
            continue
        name = source_path.name
        # Material files are named by their 64-char sha256 digest; skip atomic-write
        # temp files and anything else that is not a content hash.
        if len(name) != 64 or any(ch not in "0123456789abcdef" for ch in name):
            continue
        if selected_ids is not None and name not in selected_ids:
            continue
        target_path = target_dir / name
        if native_io_path(target_path).exists():
            continue
        try:
            _link_or_copy(source_path, target_path)
        except OSError:
            continue
        copied += 1
    return copied


def make_attachment_ref(
    *,
    sha256: str,
    name: str,
    mime: str,
    size: int,
    session_id: str,
    source: str,
) -> dict[str, Any]:
    sha = _validate_sha256(sha256)
    return {
        "kind": ATTACHMENT_REF_KIND,
        "type": mime,
        "mime": mime,
        "name": name,
        "size": size,
        "sha256": sha,
        "material_id": sha,
        "store": TRANSCRIPT_MATERIAL_STORE,
        "scope": session_id,
        "source": source,
        "_was_staged": True,
    }


def attachment_ref_marker(
    ref: dict[str, Any],
    *,
    prefix: str = "historical attachment omitted",
) -> str:
    mime = ref.get("mime") or ref.get("type") or "attachment"
    name = ref.get("name") if isinstance(ref.get("name"), str) else "attachment"
    return f"[{prefix}: {name} ({mime})]"


def read_attachment_ref_bytes(ref: dict[str, Any], *, media_root: Path) -> bytes:
    if not is_attachment_ref(ref):
        raise ValueError("attachment is not a material ref")
    store = ref.get("store")
    if store != TRANSCRIPT_MATERIAL_STORE:
        raise ValueError(f"unsupported attachment material store {store!r}")
    scope = ref.get("scope")
    if not isinstance(scope, str) or not scope:
        raise ValueError("attachment ref scope is required")
    sha = _validate_sha256(ref.get("sha256") or ref.get("material_id"))
    path = transcript_material_path(media_root, scope, sha)
    payload = native_io_path(path).read_bytes()
    actual_sha = hashlib.sha256(payload).hexdigest()
    if actual_sha != sha:
        raise ValueError("attachment material hash mismatch")
    size = ref.get("size")
    if isinstance(size, int) and size >= 0 and len(payload) != size:
        raise ValueError("attachment material size mismatch")
    return payload
