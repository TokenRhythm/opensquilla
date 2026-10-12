"""Normalize large/raw ingress text into semantic intent plus material metadata."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

LARGE_PASTE_CHARS = 20_000
PAGE_DUMP_CHARS = 8_000
PAGE_DUMP_MARKER_MIN_SCORE = 3
INLINE_TEXT_ATTACHMENT_MAX_BYTES = 2 * 1000 * 1000
TOO_LARGE_MESSAGE = (
    "The pasted text is too large to send directly; please attach a shorter file "
    "or summarize it."
)
LARGE_PASTE_PLACEHOLDER = "Please process the attached pasted text."
PAGE_DUMP_PLACEHOLDER = "Please process the attached WebChat page dump."
GENERATED_TEXT_ATTACHMENT_SOURCE = "input_normalization"
PREVIEW_ONLY_INLINE_POLICY = "preview_only"

NormalizedInputKind = Literal["plain", "large_paste", "page_dump", "too_large"]

_PAGE_DUMP_MARKERS: tuple[str, ...] = (
    "Chat session",
    "agent:main:webchat:",
    "Still waiting for agent response",
    "AI MODEL ROUTER",
    "The provider returned an empty response",
    "Pulsing",
    "Running",
    "Send a message",
    "SYSTEM",
    "SQUILLA",
)


@dataclass(frozen=True)
class NormalizedInput:
    kind: NormalizedInputKind
    message_text: str
    semantic_message: str
    generated_attachments: list[dict[str, Any]] = field(default_factory=list)
    material_chars: int = 0
    material_estimated_tokens: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    if text.isascii():
        return max(1, len(text) // 4)

    ascii_chars = 0
    cjk_chars = 0
    other_non_ascii_chars = 0
    for ch in text:
        codepoint = ord(ch)
        if codepoint < 128:
            ascii_chars += 1
        elif _is_cjk_token_like(codepoint):
            cjk_chars += 1
        else:
            other_non_ascii_chars += 1
    estimate = (ascii_chars // 4) + cjk_chars + ((other_non_ascii_chars + 1) // 2)
    return max(1, estimate)


def _is_cjk_token_like(codepoint: int) -> bool:
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2A6DF
        or 0x2A700 <= codepoint <= 0x2B73F
        or 0x2B740 <= codepoint <= 0x2B81F
        or 0x2B820 <= codepoint <= 0x2CEAF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


def page_dump_marker_score(text: str) -> int:
    if not text:
        return 0
    lowered = text.lower()
    return sum(1 for marker in _PAGE_DUMP_MARKERS if marker.lower() in lowered)


def _normalized_source_hint(source_hint: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(source_hint, dict):
        return {}

    accepted_keys = {
        "caller_kind": ("caller_kind", "callerKind"),
        "channel_kind": ("channel_kind", "channelKind"),
        "source_kind": ("source_kind", "sourceKind"),
    }
    normalized: dict[str, str] = {}
    for canonical_key, aliases in accepted_keys.items():
        for alias in aliases:
            value = source_hint.get(alias)
            if isinstance(value, str):
                normalized[canonical_key] = value.strip().lower()
                break
    return normalized


def _is_web_source(source_hint: dict[str, Any] | None) -> bool:
    normalized = _normalized_source_hint(source_hint)
    return (
        normalized.get("caller_kind") == "web"
        or normalized.get("channel_kind") in {"web", "webchat"}
        or normalized.get("source_kind") == "webui"
    )


def _attachment_name(kind: NormalizedInputKind) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    prefix = "webchat-page-dump" if kind == "page_dump" else "webchat-paste"
    return f"{prefix}-{stamp}.txt"


def _generated_text_attachment(text: str, *, kind: NormalizedInputKind) -> dict[str, Any]:
    payload = text.encode("utf-8")
    return {
        "type": "text/plain",
        "mime": "text/plain",
        "name": _attachment_name(kind),
        "data": base64.b64encode(payload).decode("ascii"),
        "size": len(payload),
        "_generated_by": GENERATED_TEXT_ATTACHMENT_SOURCE,
        "_normalization_kind": kind,
    }


def _guarded_message(kind: NormalizedInputKind) -> str:
    if kind == "page_dump":
        return PAGE_DUMP_PLACEHOLDER
    if kind == "too_large":
        return TOO_LARGE_MESSAGE
    return LARGE_PASTE_PLACEHOLDER


def _attachment_mime(attachment: dict[str, Any]) -> str | None:
    for key in ("type", "mime", "mime_type", "media_type"):
        value = attachment.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _attachment_name_value(attachment: dict[str, Any]) -> str:
    value = attachment.get("name")
    return value if isinstance(value, str) else ""


def _kind_from_generated_attachment_name(name: str) -> NormalizedInputKind | None:
    if name.startswith("webchat-page-dump-") and name.endswith(".txt"):
        return "page_dump"
    if name.startswith("webchat-paste-") and name.endswith(".txt"):
        return "large_paste"
    return None


def _decode_attachment_bytes(attachment: dict[str, Any]) -> bytes | None:
    data = attachment.get("data")
    if isinstance(data, bytes):
        return data
    if not isinstance(data, str) or not data:
        return None
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None


def _looks_like_generated_text_attachment(
    attachment: dict[str, Any],
    *,
    expected_kind: NormalizedInputKind | None = None,
) -> bool:
    if _attachment_mime(attachment) != "text/plain":
        return False
    name_kind = _kind_from_generated_attachment_name(_attachment_name_value(attachment))
    if name_kind is None:
        return False
    return expected_kind is None or name_kind == expected_kind


def infer_normalized_input_from_attachments(
    message_text: str,
    attachments: list[dict[str, Any]] | None,
) -> NormalizedInput | None:
    """Infer WebChat client-side normalization when provenance was dropped."""

    if message_text == PAGE_DUMP_PLACEHOLDER:
        kind: NormalizedInputKind = "page_dump"
    elif message_text == LARGE_PASTE_PLACEHOLDER:
        kind = "large_paste"
    else:
        return None
    if not attachments:
        return None

    matching: list[tuple[dict[str, Any], str]] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        if not _looks_like_generated_text_attachment(attachment, expected_kind=kind):
            continue
        raw_bytes = _decode_attachment_bytes(attachment)
        if raw_bytes is None:
            continue
        try:
            decoded = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            continue
        matching.append((attachment, decoded))

    if not matching:
        return None

    material = matching[0][1]
    marker_score = page_dump_marker_score(material)
    material_tokens = estimate_text_tokens(material)
    metadata = {
        "source": GENERATED_TEXT_ATTACHMENT_SOURCE,
        "original_chars": len(material),
        "material_estimated_tokens": material_tokens,
        "marker_score": marker_score,
        "generated_attachment_count": len(matching),
        "guard_action": "generated_text_attachment",
    }
    return NormalizedInput(
        kind=kind,
        message_text=message_text,
        semantic_message=message_text,
        material_chars=len(material),
        material_estimated_tokens=material_tokens,
        metadata=metadata,
    )


def materialize_generated_text_attachments(
    attachments: list[dict[str, Any]],
    *,
    media_root: Path,
    session_id: str,
    normalization_metadata: dict[str, Any] | None,
    disk_budget_bytes: int | None = None,
) -> list[dict[str, Any]]:
    """Store generated text attachments as transcript material refs."""

    if not normalization_metadata or normalization_metadata.get("guard_action") != (
        "generated_text_attachment"
    ):
        return attachments

    from opensquilla.attachment_refs import (
        is_attachment_ref,
        make_attachment_ref,
        write_transcript_material,
    )

    materialized: list[dict[str, Any]] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            materialized.append(attachment)
            continue
        if is_attachment_ref(attachment):
            materialized.append(attachment)
            continue
        name = _attachment_name_value(attachment)
        name_kind = _kind_from_generated_attachment_name(name)
        generated_by = attachment.get("_generated_by")
        if generated_by != GENERATED_TEXT_ATTACHMENT_SOURCE and name_kind is None:
            materialized.append(attachment)
            continue
        if _attachment_mime(attachment) != "text/plain":
            materialized.append(attachment)
            continue
        raw_bytes = _decode_attachment_bytes(attachment)
        if raw_bytes is None:
            materialized.append(attachment)
            continue

        sha, path, _wrote = write_transcript_material(
            media_root=Path(media_root),
            session_id=session_id,
            payload=raw_bytes,
            disk_budget_bytes=disk_budget_bytes,
        )
        try:
            decoded_text = raw_bytes.decode("utf-8")
            estimated_tokens = estimate_text_tokens(decoded_text)
        except UnicodeDecodeError:
            metadata_tokens = normalization_metadata.get("material_estimated_tokens")
            estimated_tokens = metadata_tokens if isinstance(metadata_tokens, int) else 0
        ref = make_attachment_ref(
            sha256=sha,
            name=name or _attachment_name(name_kind or "large_paste"),
            mime="text/plain",
            size=len(raw_bytes),
            session_id=session_id,
            source=GENERATED_TEXT_ATTACHMENT_SOURCE,
        )
        ref["_generated_by"] = GENERATED_TEXT_ATTACHMENT_SOURCE
        ref["_normalization_kind"] = (
            attachment.get("_normalization_kind")
            if isinstance(attachment.get("_normalization_kind"), str)
            else name_kind
        )
        ref["_provider_inline_policy"] = PREVIEW_ONLY_INLINE_POLICY
        ref["_material_estimated_tokens"] = estimated_tokens
        ref["_material_path"] = str(path)
        materialized.append(ref)
    return materialized


def normalize_incoming_text(
    message_text: str,
    *,
    source_hint: dict[str, Any] | None,
    attachments: list[dict[str, Any]] | None,
) -> NormalizedInput:
    text = message_text or ""
    marker_score = page_dump_marker_score(text)
    is_page_dump = len(text) >= PAGE_DUMP_CHARS and marker_score >= PAGE_DUMP_MARKER_MIN_SCORE
    is_large_paste = len(text) >= LARGE_PASTE_CHARS
    material_tokens = estimate_text_tokens(text)
    metadata = {
        "source": "input_normalization",
        "original_chars": len(text),
        "material_estimated_tokens": material_tokens,
        "marker_score": marker_score,
        "generated_attachment_count": 0,
    }

    if not _is_web_source(source_hint) or not (is_page_dump or is_large_paste):
        metadata["guard_action"] = "none"
        return NormalizedInput(
            kind="plain",
            message_text=text,
            semantic_message=text,
            material_chars=len(text),
            material_estimated_tokens=material_tokens,
            metadata=metadata,
        )

    kind: NormalizedInputKind = "page_dump" if is_page_dump else "large_paste"
    raw_bytes = text.encode("utf-8")
    if len(raw_bytes) > INLINE_TEXT_ATTACHMENT_MAX_BYTES:
        message = _guarded_message("too_large")
        metadata["guard_action"] = "blocked_text_too_large"
        return NormalizedInput(
            kind="too_large",
            message_text=message,
            semantic_message=message,
            material_chars=len(text),
            material_estimated_tokens=material_tokens,
            metadata=metadata,
        )

    generated = [_generated_text_attachment(text, kind=kind)]
    message = _guarded_message(kind)
    metadata["guard_action"] = "generated_text_attachment"
    metadata["generated_attachment_count"] = len(generated)
    return NormalizedInput(
        kind=kind,
        message_text=message,
        semantic_message=message,
        generated_attachments=generated,
        material_chars=len(text),
        material_estimated_tokens=material_tokens,
        metadata=metadata,
    )
