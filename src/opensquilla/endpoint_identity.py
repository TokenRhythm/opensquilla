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


__all__ = ["base_url_allows_credential_reuse", "credential_env_for_endpoint"]
