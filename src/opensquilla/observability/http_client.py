"""Environment proxy routing for passive observability HTTP clients.

Keep proxy discovery separate from TLS configuration: an explicit transport
stops HTTPX from parsing NO_PROXY again, while trust_env still controls CA
environment variables. Mounts preserve bypass decisions across redirects.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import TYPE_CHECKING, Any
from urllib.request import getproxies

from opensquilla.env import trust_env

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    import httpx


def _bypass_pattern(value: str) -> str:
    """Convert one bypass entry to a validated HTTPX mount pattern."""
    import httpx

    if "://" in value:
        url = httpx.URL(value)
        if not url.scheme or not url.host:
            raise ValueError("bypass URL has no scheme or host")
        if url.port is not None and not 0 <= url.port <= 65535:
            raise ValueError("invalid bypass port")
        return str(url)

    # HTTPX's wildcard hostname patterns do not support Unicode domains. Do
    # not invent an apex-only bypass or alter the existing ASCII/IDNA policy.
    if not value.isascii():
        raise ValueError("unsupported bypass hostname")

    # Test the whole unbracketed token first: ::1:8080 is an IPv6 address,
    # whereas [::1]:8080 denotes the address ::1 and an explicit port.
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        address = None
    if isinstance(address, ipaddress.IPv6Address):
        return str(httpx.URL(f"all://[{address}]"))
    if isinstance(address, ipaddress.IPv4Address):
        return f"all://{address}"

    if "/" in value:
        try:
            address = ipaddress.IPv4Address(value.split("/", 1)[0])
        except ValueError:
            pass
        else:
            # HTTPX accepts IPv4 CIDR-shaped entries but its URLPattern ignores
            # the path: they bypass this exact IP, not the whole network. Keep
            # that existing route without introducing broader CIDR semantics.
            return f"all://{address}"

    if value.startswith("["):
        closing = value.find("]")
        if closing < 0:
            raise ValueError("unclosed IPv6 bypass host")
        address = ipaddress.IPv6Address(value[1:closing])
        suffix = value[closing + 1 :]
        if suffix and (
            not suffix.startswith(":")
            or not suffix[1:].isascii()
            or not suffix[1:].isdigit()
            or not 0 <= int(suffix[1:]) <= 65535
        ):
            raise ValueError("invalid IPv6 bypass port")
        return str(httpx.URL(f"all://[{address}]{suffix}"))

    if any(char.isspace() or char in "[]/@?#\\" for char in value):
        raise ValueError("invalid bypass host")
    subdomains_only = value.startswith(".")
    host_value = value[1:] if subdomains_only else value
    url = httpx.URL(f"all://{host_value}")
    if not url.host or (url.port is not None and not 0 <= url.port <= 65535):
        raise ValueError("invalid bypass host or port")
    host = url.raw_host.decode("ascii")
    prefix = "*." if subdomains_only else "*"
    # localhost is an exact match, as in HTTPX's environment proxy handling.
    if host == "localhost" and not subdomains_only and url.port is None:
        prefix = ""
    port = f":{url.port}" if url.port is not None else ""
    return str(httpx.URL(f"all://{prefix}{host}{port}"))


def observability_httpx_kwargs(*, asynchronous: bool = False) -> dict[str, Any]:
    """Honor the network opt-in without unsafe NO_PROXY parsing or env writes.

    Invalid bypass entries are ignored individually, preserving valid proxy
    routes. Invalid proxies or TLS configuration still fail; never silently
    retry a proxied request as direct traffic.
    """
    import httpx

    if not trust_env():
        return {"trust_env": False}

    # urllib preserves lowercase precedence and Windows/macOS system proxy
    # discovery. All parsing below operates on this snapshot, not os.environ.
    proxies = getproxies()
    bypass_entries = [entry.strip() for entry in proxies.get("no", "").split(",")]
    patterns: dict[str, httpx.Proxy | None] = {}
    if "*" not in bypass_entries:
        for scheme in ("http", "https", "all"):
            value = proxies.get(scheme)
            if value:
                url = value if "://" in value else f"http://{value}"
                # Validate every proxy before constructing transports. Do not
                # turn a malformed proxy into an implicit direct route.
                patterns[f"{scheme}://"] = httpx.Proxy(url)
        invalid_count = 0
        for entry in bypass_entries:
            if not entry:
                continue
            try:
                patterns[_bypass_pattern(entry)] = None
            except (ValueError, httpx.InvalidURL):
                invalid_count += 1
        if invalid_count:
            # Bypass values may include internal hostnames or credentials.
            log.warning("observability.no_proxy_invalid ignored_entries=%d", invalid_count)

    transport_type = httpx.AsyncHTTPTransport if asynchronous else httpx.HTTPTransport
    return {
        "trust_env": True,
        "transport": transport_type(trust_env=True),
        "mounts": {
            pattern: None if proxy is None else transport_type(proxy=proxy, trust_env=True)
            for pattern, proxy in patterns.items()
        },
    }


def _build_async_client(timeout: float) -> httpx.AsyncClient:
    import httpx

    return httpx.AsyncClient(timeout=timeout, **observability_httpx_kwargs(asynchronous=True))


async def create_observability_async_client(*, timeout: float) -> httpx.AsyncClient:
    """Construct proxy/TLS state off the loop and close late cancelled results."""
    pending = asyncio.create_task(asyncio.to_thread(_build_async_client, timeout))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        # Cancelling to_thread cannot stop the worker. Keep ownership until
        # construction and close settle, without blocking the event loop or
        # leaving an unowned cleanup task behind. Repeated cancel is deferred.
        try:
            client = await _settle_cancelled_work(pending)
            await _settle_cancelled_work(asyncio.create_task(client.aclose()))
        except Exception as exc:
            log.debug(
                "observability.cancelled_client_cleanup_failed error_type=%s",
                type(exc).__name__,
            )
        raise


async def _settle_cancelled_work[T](task: asyncio.Task[T]) -> T:
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()
