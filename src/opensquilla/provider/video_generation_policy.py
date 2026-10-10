"""Endpoint and credential defaults for configured video providers."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from opensquilla.endpoint_identity import base_url_allows_credential_reuse
from opensquilla.video_generation_defaults import (
    VIDEO_GENERATION_DEFAULT_ENV_KEYS,
    VIDEO_GENERATION_OFFICIAL_BASE_URLS,
)


def is_valid_video_generation_base_url(value: str) -> bool:
    """Accept an HTTP(S) API root without embedded auth or path escapes."""

    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return False
    if "?" in value or "#" in value:
        return False
    try:
        parsed = urlsplit(value)
        _port = parsed.port
        hostname = parsed.hostname or ""
    except (UnicodeError, ValueError):
        return False
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or "\\" in parsed.netloc
        or any(segment in {".", ".."} or "%" in segment for segment in parsed.path.split("/"))
    ):
        return False
    if parsed.scheme.lower() == "http":
        try:
            if hostname.lower() != "localhost" and not ipaddress.ip_address(hostname).is_loopback:
                return False
        except ValueError:
            return False
    return True


def conflicting_video_generation_endpoint_provider(
    provider_id: str, base_url: str
) -> str | None:
    """Reject a provider route pointed at another provider's official origin."""

    for known_id, official_url in VIDEO_GENERATION_OFFICIAL_BASE_URLS.items():
        if known_id != provider_id and base_url_allows_credential_reuse(official_url, base_url):
            return known_id
    return None


__all__ = [
    "VIDEO_GENERATION_DEFAULT_ENV_KEYS",
    "VIDEO_GENERATION_OFFICIAL_BASE_URLS",
    "conflicting_video_generation_endpoint_provider",
    "is_valid_video_generation_base_url",
]
