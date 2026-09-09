"""Transcript attachment persistence + replay.

The gateway-side transcript writer used to inline the full base64 of
every attachment. Now, attachments that were originally **staged**
with ``file_uuid`` are written to a
content-addressable per-session directory instead — the envelope keeps
just ``{sha256_ref, name, mime, size}``. Inline attachments retain the
existing envelope shape so existing replay paths are unchanged.

The persisted envelope field is ``sha256_ref``, never ``file_uuid``:
``file_uuid`` is an upload-store concept that must not leak into engine
replay paths.

The on-disk byte budget is enforced at write time: when the next staged
write would exceed ``disk_budget_bytes``, the writer raises before the
turn is accepted. Staged material must never fall back to persistent
inline base64.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

from opensquilla.attachment_refs import (
    AttachmentMaterialBudgetError,
    attachment_ref_marker,
    is_attachment_ref,
    write_transcript_material,
)
from opensquilla.prompt_annotations import normalize_prompt_annotation_snapshots

log = logging.getLogger(__name__)

# Marker text for replay when the persisted file is missing on disk.
_MISSING_ATTACHMENT_TEMPLATE = "[attachment unavailable: {name}]"
_WINDOWS_REPLACE_RETRIES = 3
_WINDOWS_REPLACE_RETRY_DELAY_S = 0.02


def _new_attachment_id() -> str:
    """Allocate a logical occurrence id independent of content-addressed bytes."""

    return f"att_{secrets.token_urlsafe(18)}"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write *data* to *path* atomically via tmp + fsync + os.replace."""

    tmp_path = Path(str(path) + f".tmp.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(_WINDOWS_REPLACE_RETRIES if os.name == "nt" else 1):
            try:
                os.replace(tmp_path, path)
                break
            except PermissionError:
                if os.name != "nt" or attempt + 1 >= _WINDOWS_REPLACE_RETRIES:
                    raise
                time.sleep(_WINDOWS_REPLACE_RETRY_DELAY_S)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _display_attachment_name(raw: Any, fallback: str = "attachment") -> str:
    if not isinstance(raw, str):
        return fallback
    collapsed = " ".join(raw.strip().split())
    if not collapsed:
        return fallback
    return collapsed[:160]


def _was_staged(attachment: dict[str, Any]) -> bool:
    return bool(attachment.get("_was_staged"))


def _transcript_dir(media_root: Path, session_id: str) -> Path:
    return Path(media_root) / "transcripts" / session_id


def build_transcript_attachment_envelope(
    *,
    text: str,
    display_text: str | None = None,
    attachments: list[dict[str, Any]],
    session_id: str,
    media_root: Path,
    persist_enabled: bool,
    disk_budget_bytes: int | None = None,
    prompt_annotations: object = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Build the JSON envelope written to ``transcript_entries.content``.

    Returns ``(envelope_json, disk_writes)`` where ``disk_writes`` is a
    list of ``{"path", "sha256", "size"}`` dicts describing which staged
    attachments landed on disk (one per unique sha within this call).

    When ``disk_budget_bytes`` is provided and a staged write would exceed it,
    the function raises instead of falling back to persistent inline base64.
    With persistence disabled, every attachment shape becomes metadata plus
    an unavailable marker; existing material is neither linked nor deleted.
    """

    persisted_attachments: list[dict[str, Any]] = []
    disk_writes: list[dict[str, Any]] = []

    for attachment in attachments:
        media_type = (
            attachment.get("type") or attachment.get("mime") or attachment.get("media_type")
        )
        name = attachment.get("name", "attachment")
        if not persist_enabled:
            size = attachment.get("size")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                size = None
                data = attachment.get("data")
                if isinstance(data, str):
                    try:
                        size = len(base64.b64decode(data, validate=True))
                    except (ValueError, TypeError):
                        pass
            persisted_attachments.append(
                {
                    "name": name,
                    "mime": media_type,
                    "size": size,
                    "missing_reason": "attachment persistence disabled",
                }
            )
            continue
        if is_attachment_ref(attachment):
            sha = attachment["sha256"]
            persisted = {
                "attachment_id": _new_attachment_id(),
                "sha256_ref": sha,
                "name": name,
                "mime": media_type,
                "size": attachment.get("size"),
            }
            # Transcript refs are historically session-scoped and can be
            # reconstructed from ``sha256_ref``. Managed input refs instead
            # need their logical resource identity to resolve a current path
            # after restart, fork, or execution-environment changes. Never
            # persist the optional absolute material path.
            store = attachment.get("store")
            if isinstance(store, str) and store and store != "transcript":
                persisted["store"] = store
                for key in ("owner", "resource_id", "pending_input_id", "source"):
                    value = attachment.get(key)
                    if isinstance(value, str) and value:
                        persisted[key] = value
            persisted_attachments.append(persisted)
            continue
        data = attachment.get("data")
        if not isinstance(data, str) or not isinstance(media_type, str):
            continue

        if _was_staged(attachment):
            try:
                payload = base64.b64decode(data, validate=True)
            except (ValueError, TypeError) as exc:
                log.warning("transcript.persist_decode_failed name=%s err=%s", name, exc)
                persisted_attachments.append(
                    {
                        "name": name,
                        "mime": media_type,
                        "missing_reason": "attachment decode failed",
                    }
                )
                continue

            try:
                sha, path, wrote = write_transcript_material(
                    media_root=media_root,
                    session_id=session_id,
                    payload=payload,
                    disk_budget_bytes=disk_budget_bytes,
                )
            except AttachmentMaterialBudgetError as exc:
                log.warning(
                    "transcript.disk.budget_exceeded session=%s name=%s size=%d",
                    session_id,
                    name,
                    len(payload),
                )
                raise ValueError("attachment material exceeds transcript disk budget") from exc
            if wrote:
                disk_writes.append({"path": str(path), "sha256": sha, "size": len(payload)})

            persisted_attachments.append(
                {
                    "attachment_id": _new_attachment_id(),
                    "sha256_ref": sha,
                    "name": name,
                    "mime": media_type,
                    "size": len(payload),
                }
            )
        else:
            persisted_attachments.append(
                {
                    "attachment_id": _new_attachment_id(),
                    "type": media_type,
                    "name": name,
                    "data": data,
                }
            )

    envelope_payload: dict[str, Any] = {"text": text, "attachments": persisted_attachments}
    if display_text is not None:
        envelope_payload["display_text"] = display_text
    normalized_annotations = normalize_prompt_annotation_snapshots(prompt_annotations)
    if normalized_annotations:
        envelope_payload["prompt_annotations"] = list(normalized_annotations)
    envelope = json.dumps(envelope_payload)
    return envelope, disk_writes


def rebuild_attachments_for_replay(
    envelope_json: str,
    *,
    session_id: str,
    media_root: Path,
) -> tuple[str, list[dict[str, Any]]]:
    """Rebuild ``(text, attachments)`` for engine replay.

    Staged ``sha256_ref`` entries become historical text markers, not
    provider attachments. Missing or non-durable staged material also degrades
    to markers so replay never re-inlines staged bytes as base64.
    """

    try:
        parsed = json.loads(envelope_json)
    except (json.JSONDecodeError, ValueError):
        return envelope_json, []

    if not isinstance(parsed, dict):
        return envelope_json, []

    raw_text = parsed.get("text")
    text = raw_text if isinstance(raw_text, str) else ""
    raw_atts = parsed.get("attachments")
    if not isinstance(raw_atts, list):
        return text, []

    rebuilt: list[dict[str, Any]] = []
    missing_markers: list[str] = []

    for entry in raw_atts:
        if not isinstance(entry, dict):
            continue
        sha = entry.get("sha256_ref")
        if isinstance(sha, str) and sha:
            store = entry.get("store") or "transcript"
            ref: dict[str, Any] = {
                "kind": "attachment_ref",
                "sha256": sha,
                "material_id": sha,
                "name": _display_attachment_name(entry.get("name")),
                "mime": entry.get("mime") or entry.get("type") or "application/octet-stream",
                "size": entry.get("size"),
                "store": store,
                "scope": session_id,
            }
            for key in ("owner", "resource_id", "pending_input_id"):
                value = entry.get(key)
                if isinstance(value, str) and value:
                    ref[key] = value
            try:
                from opensquilla.attachment_refs import attachment_ref_material_path

                path = attachment_ref_material_path(ref, media_root=media_root)
            except ValueError:
                path = None
            if path is None:
                missing_markers.append(
                    _MISSING_ATTACHMENT_TEMPLATE.format(
                        name=_display_attachment_name(entry.get("name"))
                    )
                )
                continue
            if not path.exists():
                missing_markers.append(
                    _MISSING_ATTACHMENT_TEMPLATE.format(
                        name=_display_attachment_name(entry.get("name"))
                    )
                )
                log.warning(
                    "transcript.replay.missing_attachment session=%s sha=%s name=%s",
                    session_id,
                    sha,
                    entry.get("name", "?"),
                )
                continue
            mime = entry.get("mime") or entry.get("type")
            if not isinstance(mime, str):
                continue
            raw_size = entry.get("size")
            size = raw_size if isinstance(raw_size, int) else path.stat().st_size
            ref["type"] = mime
            ref["mime"] = mime
            ref["size"] = size
            ref["source"] = entry.get("source") or "transcript"
            ref["_was_staged"] = True
            missing_markers.append(attachment_ref_marker(ref))
        else:
            missing_reason = entry.get("missing_reason")
            if isinstance(missing_reason, str) and missing_reason:
                missing_markers.append(
                    "[attachment unavailable: "
                    f"{_display_attachment_name(entry.get('name'))}: {missing_reason}]"
                )
                continue
            # Inline-shaped entry — pass through.
            data = entry.get("data")
            mime = entry.get("type") or entry.get("mime")
            if isinstance(data, str) and isinstance(mime, str):
                raw_name = entry.get("name", "attachment")
                rebuilt.append(
                    {
                        "type": mime,
                        "data": data,
                        "name": raw_name if isinstance(raw_name, str) else "attachment",
                    }
                )

    if missing_markers:
        text = "\n".join([text, *missing_markers]).strip()

    return text, rebuilt
