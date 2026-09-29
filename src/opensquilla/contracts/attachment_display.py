"""Bounded display metadata for attachment markers and session manifests."""

from __future__ import annotations

NAME_MAX_BYTES = 160
MIME_MAX_BYTES = 120


def bounded_attachment_text(value: object, *, fallback: str, max_bytes: int) -> str:
    if not isinstance(value, str):
        return fallback
    normalized = " ".join(value.strip().split())
    if not normalized:
        return fallback
    encoded = normalized.encode("utf-8")
    if len(encoded) <= max_bytes:
        return normalized
    return encoded[:max_bytes].decode("utf-8", errors="ignore") or fallback


def normalize_attachment_display_name(
    value: object, *, fallback: str = "attachment",
) -> str:
    """Keep the basename and bound its UTF-8 length for model-visible text."""

    if isinstance(value, str):
        value = value.strip().replace("\\", "/").rsplit("/", 1)[-1]
    return bounded_attachment_text(value, fallback=fallback, max_bytes=NAME_MAX_BYTES)


def normalize_attachment_display_mime(value: object) -> str:
    """Bound a MIME label without retaining parameters or control characters."""

    if not isinstance(value, str):
        return "application/octet-stream"
    normalized = value.split(";", 1)[0].strip().lower()
    if "/" not in normalized or any(char in normalized for char in "\r\n"):
        return "application/octet-stream"
    return bounded_attachment_text(
        normalized,
        fallback="application/octet-stream",
        max_bytes=MIME_MAX_BYTES,
    )
