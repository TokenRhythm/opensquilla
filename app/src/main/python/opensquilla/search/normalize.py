"""Normalization helpers for search queries and results."""

from __future__ import annotations

import posixpath
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

from opensquilla.search.types import SearchHit

_TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "ref"}


def canonicalize_query_key(query: str) -> str:
    """Normalize a query string for cache and dedupe keys."""

    return " ".join(query.strip().lower().split())


def extract_domain(url: str) -> str:
    """Return the lowercased hostname from a URL."""

    return (urlsplit(url).hostname or "").lower()


def canonicalize_url(url: str) -> str:
    """Canonicalize a URL for result deduplication."""

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    netloc = _canonicalize_netloc(parts, scheme)

    path = posixpath.normpath(parts.path or "")
    if path == ".":
        path = ""
    if parts.path.startswith("/") and not path.startswith("/"):
        path = f"/{path}"

    query_items = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(name)
    ]
    query = urlencode(sorted(query_items))

    return urlunsplit((scheme, netloc, path, query, ""))


def dedupe_hits_by_canonical_url(hits: list[SearchHit]) -> tuple[list[SearchHit], int]:
    """Keep the first hit for each canonical URL."""

    seen: set[str] = set()
    deduped: list[SearchHit] = []
    duplicate_count = 0

    for hit in hits:
        if hit.canonical_url in seen:
            duplicate_count += 1
            continue
        seen.add(hit.canonical_url)
        deduped.append(hit)

    return deduped, duplicate_count


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith("utm_") or lowered in _TRACKING_PARAMS


def _canonicalize_netloc(parts: SplitResult, scheme: str) -> str:
    hostname = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        return parts.netloc.lower()

    if port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        return hostname
    return f"{hostname}:{port}"
