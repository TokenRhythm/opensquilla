"""Attachment material references shared across gateway, transcript, and runtime."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
from pathlib import Path
from typing import Any

from opensquilla.paths import native_io_path

ATTACHMENT_REF_KIND = "attachment_ref"
TRANSCRIPT_MATERIAL_STORE = "transcript"
PENDING_CHAT_INPUT_MATERIAL_STORE = "pending_chat_input"
_PENDING_CHAT_INPUT_DIR = ".pending-chat-inputs"
_PENDING_CHAT_INPUT_MANIFEST = ".manifest.json"
_PENDING_CHAT_INPUT_PROMOTIONS = ".promotions.json"


class AttachmentMaterialBudgetError(ValueError):
    """Raised when material bytes cannot be written within the configured budget."""


class PendingChatInputManifestConflictError(ValueError):
    """A staged queue identity was reused for different attachment content."""


class PendingChatInputManifestCorruptError(ValueError):
    """A staged queue manifest exists but cannot be safely recovered."""


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


def _pending_input_owner_segment(pending_input_id: str) -> str:
    if not isinstance(pending_input_id, str) or not pending_input_id.strip():
        raise ValueError("pending input id is required for attachment material")
    return hashlib.sha256(
        f"opensquilla-pending-input:{pending_input_id.strip()}".encode()
    ).hexdigest()


def pending_chat_input_material_dir(
    media_root: Path,
    session_id: str,
    pending_input_id: str,
) -> Path:
    """Return the private owner directory for one staged queue item.

    The externally supplied pending id is hashed before it reaches a path
    segment.  Each queue item owns a distinct directory, so cancellation can
    remove its durable upload copy without racing another item that happens to
    contain identical bytes.
    """

    return (
        transcript_material_dir(media_root, session_id)
        / _PENDING_CHAT_INPUT_DIR
        / _pending_input_owner_segment(pending_input_id)
    )


def pending_chat_input_material_path(
    media_root: Path,
    session_id: str,
    pending_input_id: str,
    sha256: str,
) -> Path:
    return pending_chat_input_material_dir(
        media_root,
        session_id,
        pending_input_id,
    ) / _validate_sha256(sha256)


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


def write_pending_chat_input_material(
    *,
    media_root: Path,
    session_id: str,
    pending_input_id: str,
    payload: bytes,
    disk_budget_bytes: int | None = None,
) -> tuple[str, Path, bool]:
    """Durably stage bytes under one pending input's private owner directory."""

    sha = hashlib.sha256(payload).hexdigest()
    path = pending_chat_input_material_path(
        media_root,
        session_id,
        pending_input_id,
        sha,
    )
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


def make_pending_chat_input_attachment_ref(
    *,
    sha256: str,
    name: str,
    mime: str,
    size: int,
    session_id: str,
    pending_input_id: str,
    source: str,
) -> dict[str, Any]:
    sha = _validate_sha256(sha256)
    pending_input_id = pending_input_id.strip()
    _pending_input_owner_segment(pending_input_id)
    return {
        "kind": ATTACHMENT_REF_KIND,
        "type": mime,
        "mime": mime,
        "name": name,
        "size": size,
        "sha256": sha,
        "material_id": sha,
        "store": PENDING_CHAT_INPUT_MATERIAL_STORE,
        "scope": session_id,
        "pending_input_id": pending_input_id,
        "source": source,
        "_was_staged": True,
    }


def write_pending_chat_input_manifest(
    *,
    media_root: Path,
    session_id: str,
    pending_input_id: str,
    enqueue_fingerprint: str,
    attachments: list[dict[str, Any]],
) -> None:
    """Persist non-secret recovery metadata after all staged bytes are durable.

    The manifest deliberately contains neither upload UUIDs nor inline bytes.
    It closes the useful crash window between materialization and the SQLite
    insert: an idempotent retry can recover the already-durable references even
    when the in-memory upload store disappeared with the old Gateway process.
    """

    if not enqueue_fingerprint.startswith("sha256:"):
        raise ValueError("pending input enqueue fingerprint is invalid")
    for attachment in attachments:
        _validate_pending_chat_input_ref(
            attachment,
            session_id=session_id,
            pending_input_id=pending_input_id,
        )
    manifest = {
        "schema_version": 1,
        "session_id": session_id,
        "pending_input_id": pending_input_id,
        "enqueue_fingerprint": enqueue_fingerprint,
        "attachments": attachments,
    }
    path = pending_chat_input_material_dir(
        media_root,
        session_id,
        pending_input_id,
    ) / _PENDING_CHAT_INPUT_MANIFEST
    _atomic_write_bytes(
        path,
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )


def pending_chat_input_manifest_exists(
    *,
    media_root: Path,
    session_id: str,
    pending_input_id: str,
) -> bool:
    """Return whether an owner has a manifest, including an invalid one."""

    path = pending_chat_input_material_dir(
        media_root,
        session_id,
        pending_input_id,
    ) / _PENDING_CHAT_INPUT_MANIFEST
    return native_io_path(path).is_file()


def read_pending_chat_input_manifest(
    *,
    media_root: Path,
    session_id: str,
    pending_input_id: str,
) -> dict[str, Any] | None:
    path = pending_chat_input_material_dir(
        media_root,
        session_id,
        pending_input_id,
    ) / _PENDING_CHAT_INPUT_MANIFEST
    try:
        raw = json.loads(native_io_path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != 1
        or raw.get("session_id") != session_id
        or raw.get("pending_input_id") != pending_input_id
        or not isinstance(raw.get("enqueue_fingerprint"), str)
        or not isinstance(raw.get("attachments"), list)
    ):
        return None
    attachments: list[dict[str, Any]] = []
    try:
        for attachment in raw["attachments"]:
            _validate_pending_chat_input_ref(
                attachment,
                session_id=session_id,
                pending_input_id=pending_input_id,
            )
            # Hash and size verification ensures a torn/corrupted owner store
            # can never be promoted into a transcript.
            read_attachment_ref_bytes(attachment, media_root=media_root)
            attachments.append(dict(attachment))
    except (OSError, ValueError):
        return None
    return {**raw, "attachments": attachments}


def record_pending_chat_input_promotion(
    *,
    media_root: Path,
    source_session_id: str,
    pending_input_id: str,
    target_session_id: str,
    material_ids: set[str],
) -> None:
    """Record canonical paths that may need unreferenced cleanup after failure."""

    normalized_ids = sorted({_validate_sha256(value) for value in material_ids})
    if not normalized_ids:
        return
    path = pending_chat_input_material_dir(
        media_root,
        source_session_id,
        pending_input_id,
    ) / _PENDING_CHAT_INPUT_PROMOTIONS
    existing = read_pending_chat_input_promotions(
        media_root=media_root,
        source_session_id=source_session_id,
        pending_input_id=pending_input_id,
    )
    targets = {target: sorted(values) for target, values in existing.items()}
    targets[target_session_id] = sorted(
        {*targets.get(target_session_id, []), *normalized_ids}
    )
    _atomic_write_bytes(
        path,
        json.dumps(
            {"schema_version": 1, "targets": targets},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )


def read_pending_chat_input_promotions(
    *,
    media_root: Path,
    source_session_id: str,
    pending_input_id: str,
) -> dict[str, set[str]]:
    """Return promotion cleanup hints, failing closed on invalid metadata."""

    path = pending_chat_input_material_dir(
        media_root,
        source_session_id,
        pending_input_id,
    ) / _PENDING_CHAT_INPUT_PROMOTIONS
    try:
        raw = json.loads(native_io_path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    targets = raw.get("targets")
    if raw.get("schema_version") != 1 or not isinstance(targets, dict):
        return {}
    normalized: dict[str, set[str]] = {}
    try:
        for target_session_id, material_ids in targets.items():
            if not isinstance(target_session_id, str) or not target_session_id:
                return {}
            if not isinstance(material_ids, list):
                return {}
            normalized[target_session_id] = {
                _validate_sha256(material_id) for material_id in material_ids
            }
    except ValueError:
        return {}
    return normalized


def cleanup_pending_chat_input_material(
    *,
    media_root: Path,
    session_id: str,
    pending_input_id: str,
) -> bool:
    """Remove only the private material owner for one pending input."""

    owner = native_io_path(
        pending_chat_input_material_dir(
            media_root,
            session_id,
            pending_input_id,
        )
    )
    if not owner.exists() or owner.is_symlink() or not owner.is_dir():
        return False
    shutil.rmtree(owner)
    parent = owner.parent
    try:
        parent.rmdir()
    except OSError:
        # Best-effort cleanup: the parent may be non-empty or concurrently managed.
        pass
    return True


def _validate_pending_chat_input_ref(
    ref: Any,
    *,
    session_id: str,
    pending_input_id: str,
) -> None:
    if not is_attachment_ref(ref):
        raise ValueError("pending attachment is not a material ref")
    if ref.get("store") != PENDING_CHAT_INPUT_MATERIAL_STORE:
        raise ValueError("pending attachment uses the wrong material store")
    if ref.get("scope") != session_id:
        raise ValueError("pending attachment belongs to a different session")
    if ref.get("pending_input_id") != pending_input_id:
        raise ValueError("pending attachment belongs to a different queue item")
    _validate_sha256(ref.get("sha256") or ref.get("material_id"))


def promote_pending_chat_input_attachments(
    attachments: list[dict[str, Any]],
    *,
    media_root: Path,
    pending_input_id: str,
    target_session_id: str,
    disk_budget_bytes: int | None = None,
) -> list[dict[str, Any]]:
    """Copy private pending refs into canonical transcript material refs."""

    promotion_groups: dict[str, set[str]] = {}
    for attachment in attachments:
        source_session_id = attachment.get("scope")
        if not isinstance(source_session_id, str) or not source_session_id:
            raise ValueError("pending attachment session scope is required")
        _validate_pending_chat_input_ref(
            attachment,
            session_id=source_session_id,
            pending_input_id=pending_input_id,
        )
        promotion_groups.setdefault(source_session_id, set()).add(
            _validate_sha256(attachment.get("sha256") or attachment.get("material_id"))
        )
    # Record before linking. A crash can then leave a harmless cleanup hint,
    # never an undiscoverable canonical orphan.
    for source_session_id, material_ids in promotion_groups.items():
        record_pending_chat_input_promotion(
            media_root=media_root,
            source_session_id=source_session_id,
            pending_input_id=pending_input_id,
            target_session_id=target_session_id,
            material_ids=material_ids,
        )

    promoted: list[dict[str, Any]] = []
    for attachment in attachments:
        source_session_id = attachment.get("scope")
        if not isinstance(source_session_id, str) or not source_session_id:
            raise ValueError("pending attachment session scope is required")
        _validate_pending_chat_input_ref(
            attachment,
            session_id=source_session_id,
            pending_input_id=pending_input_id,
        )
        payload = read_attachment_ref_bytes(attachment, media_root=media_root)
        sha = hashlib.sha256(payload).hexdigest()
        source_path = pending_chat_input_material_path(
            media_root,
            source_session_id,
            pending_input_id,
            sha,
        )
        target_path = transcript_material_path(media_root, target_session_id, sha)
        if not native_io_path(target_path).exists():
            native_io_path(target_path.parent).mkdir(parents=True, exist_ok=True)
            try:
                # Pending and transcript material normally share a filesystem.
                # A hardlink makes promotion durable without temporarily
                # charging the same bytes twice against the session budget.
                os.link(native_io_path(source_path), native_io_path(target_path))
            except OSError:
                write_transcript_material(
                    media_root=media_root,
                    session_id=target_session_id,
                    payload=payload,
                    disk_budget_bytes=disk_budget_bytes,
                )
        mime = attachment.get("mime") or attachment.get("type")
        if not isinstance(mime, str) or not mime:
            raise ValueError("pending attachment mime is required")
        name = attachment.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("pending attachment name is required")
        promoted.append(
            make_attachment_ref(
                sha256=sha,
                name=name,
                mime=mime,
                size=len(payload),
                session_id=target_session_id,
                source="pending_chat_input",
            )
        )
    return promoted


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
    scope = ref.get("scope")
    if not isinstance(scope, str) or not scope:
        raise ValueError("attachment ref scope is required")
    sha = _validate_sha256(ref.get("sha256") or ref.get("material_id"))
    if store == TRANSCRIPT_MATERIAL_STORE:
        path = transcript_material_path(media_root, scope, sha)
    elif store == PENDING_CHAT_INPUT_MATERIAL_STORE:
        pending_input_id = ref.get("pending_input_id")
        if not isinstance(pending_input_id, str) or not pending_input_id:
            raise ValueError("pending attachment ref id is required")
        path = pending_chat_input_material_path(
            media_root,
            scope,
            pending_input_id,
            sha,
        )
    else:
        raise ValueError(f"unsupported attachment material store {store!r}")
    payload = native_io_path(path).read_bytes()
    actual_sha = hashlib.sha256(payload).hexdigest()
    if actual_sha != sha:
        raise ValueError("attachment material hash mismatch")
    size = ref.get("size")
    if isinstance(size, int) and size >= 0 and len(payload) != size:
        raise ValueError("attachment material size mismatch")
    return payload
