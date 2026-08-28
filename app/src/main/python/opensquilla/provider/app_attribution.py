"""Host-gated application attribution for supported provider APIs."""

from __future__ import annotations

from urllib.parse import urlparse

OPENSQUILLA_APP_REFERER = "https://opensquilla.ai"
OPENSQUILLA_APP_TITLE = "OpenSquilla"

_APP_ATTRIBUTION_ROOT_HOSTS = frozenset({"openrouter.ai", "tokenrhythm.studio"})
_APP_ATTRIBUTION_HOST_MARKERS = frozenset({"tokenrhythm"})


def _normalized_hostname(url: str | None) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        if parsed.scheme.lower() not in {"http", "https"}:
            return ""
        host = (parsed.hostname or "").lower()
        if ":" in host or "%" in host:
            return ""
        return host
    except ValueError:
        return ""


def is_host_or_subdomain(url: str | None, root_host: str) -> bool:
    """Return whether ``url`` uses ``root_host`` or one of its subdomains."""
    root = str(root_host or "").strip().lower().lstrip(".")
    if not root or ":" in root or "%" in root:
        return False
    host = _normalized_hostname(url)
    return host == root or host.endswith(f".{root}")


def is_provider_app_host(url: str | None, root_host: str) -> bool:
    """Return whether ``url`` is the allowlisted root host or its subdomain."""
    root = str(root_host or "").strip().lower().lstrip(".")
    if root not in _APP_ATTRIBUTION_ROOT_HOSTS:
        return False
    return is_host_or_subdomain(url, root)


def provider_app_headers(url: str | None) -> dict[str, str]:
    """Return OpenSquilla attribution headers for supported provider hosts."""
    host = _normalized_hostname(url)
    matches_root = any(
        is_provider_app_host(url, root) for root in _APP_ATTRIBUTION_ROOT_HOSTS
    )
    matches_marker = any(marker in host for marker in _APP_ATTRIBUTION_HOST_MARKERS)
    if not matches_root and not matches_marker:
        return {}
    return {
        "HTTP-Referer": OPENSQUILLA_APP_REFERER,
        "X-Title": OPENSQUILLA_APP_TITLE,
    }
