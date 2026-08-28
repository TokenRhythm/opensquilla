"""Utilities for CLI gateway URL normalization."""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse


def normalize_gateway_url(url: str) -> str:
    """Normalize user-supplied gateway URLs to websocket endpoints.

    Supported input shapes:
    - ``http://host:port`` → ``ws://host:port/ws``
    - ``https://host:port`` → ``wss://host:port/ws``
    - ``ws://host:port`` → ``ws://host:port/ws``
    - ``ws://host:port/ws`` unchanged
    - ``wss://...`` handled symmetrically
    - bare ``host:port`` → ``ws://host:port/ws``
    """
    stripped = url.strip()
    if "://" in stripped:
        parsed = urlparse(stripped)
        scheme = parsed.scheme
        netloc = parsed.netloc
        path = parsed.path
    else:
        parsed = urlparse(f"//{stripped}")
        scheme = "ws"
        netloc = parsed.netloc
        path = parsed.path
    params = parsed.params
    query = parsed.query
    fragment = parsed.fragment

    if scheme not in {"http", "https", "ws", "wss"}:
        raise ValueError(f"Unsupported gateway URL scheme: {scheme!r}")

    websocket_scheme = "wss" if scheme in {"https", "wss"} else "ws"
    websocket_path = "/ws" if path in ("", "/") else path

    return urlunparse((websocket_scheme, netloc, websocket_path, params, query, fragment))
