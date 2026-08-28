"""HTTP endpoint identity checks for safe credential reuse."""

from __future__ import annotations

from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _http_origin(value: str) -> tuple[str, str, int] | None:
    raw = str(value or "").strip()
    if not raw or any(char.isspace() or ord(char) < 0x20 for char in raw):
        return None
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except (UnicodeError, ValueError):
        return None
    if scheme not in _DEFAULT_PORTS or not host or "\\" in parsed.netloc:
        return None
    return scheme, host, port if port is not None else _DEFAULT_PORTS[scheme]


def base_url_allows_credential_reuse(
    stored_base_url: str,
    candidate_base_url: str | None,
) -> bool:
    """Return whether credentials may follow ``candidate_base_url``.

    An omitted candidate means "keep the stored endpoint". Changed values
    must preserve the HTTP origin (scheme, host, and effective port); any
    ambiguous parse fails closed.
    """
    candidate = str(candidate_base_url or "").strip()
    if not candidate:
        return True
    stored = str(stored_base_url or "").strip()
    if candidate == stored:
        return True
    stored_origin = _http_origin(stored)
    candidate_origin = _http_origin(candidate)
    return stored_origin is not None and candidate_origin == stored_origin


def base_url_matches_official_api(
    official_base_url: str,
    candidate_base_url: str | None,
) -> bool:
    """Return whether ``candidate_base_url`` names the same provider API root.

    Credential reuse is intentionally origin-scoped, but capability defaults
    also depend on the provider's canonical API path.  This stricter identity
    allows a trailing slash and rejects query/fragment/user-info variations.
    """

    official = str(official_base_url or "").strip()
    candidate = str(candidate_base_url or "").strip() or official
    if not official or not candidate:
        return False
    # ``urlsplit`` normalizes a present-but-empty query or fragment to an
    # empty string. Preserve strict root identity by rejecting the raw URL
    # delimiters themselves; percent-encoded path characters remain valid.
    if "?" in official or "#" in official or "?" in candidate or "#" in candidate:
        return False
    try:
        official_parts = urlsplit(official)
        candidate_parts = urlsplit(candidate)
    except (UnicodeError, ValueError):
        return False
    if (
        official_parts.username is not None
        or official_parts.password is not None
        or candidate_parts.username is not None
        or candidate_parts.password is not None
        or official_parts.query
        or official_parts.fragment
        or candidate_parts.query
        or candidate_parts.fragment
    ):
        return False
    official_path = official_parts.path.rstrip("/") or "/"
    candidate_path = candidate_parts.path.rstrip("/") or "/"
    return (
        _http_origin(official) is not None
        and _http_origin(official) == _http_origin(candidate)
        and official_path == candidate_path
    )


def credential_env_for_endpoint(
    *,
    configured_env: str,
    configured_explicitly: bool,
    default_env: str,
    default_base_url: str,
    effective_base_url: str,
) -> str:
    """Resolve an env reference without moving an implicit default across origins.

    A non-default env name is necessarily operator-authored. A configured name
    equal to the registry default is operator-authored only when the config
    model says that field was explicitly set. Otherwise the registry env name
    belongs to the registry endpoint and is available only on that origin.
    """

    configured = str(configured_env or "").strip()
    default = str(default_env or "").strip()
    if configured and (configured != default or configured_explicitly):
        return configured
    if default and base_url_allows_credential_reuse(default_base_url, effective_base_url):
        return default
    return ""


__all__ = [
    "base_url_allows_credential_reuse",
    "base_url_matches_official_api",
    "credential_env_for_endpoint",
]
