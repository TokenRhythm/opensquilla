"""Shared SSRF protection for URL-fetching tools."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urlparse
from urllib.request import getproxies, proxy_bypass

from opensquilla.tools.types import SSRFBlockedError, UnsupportedURLSchemeError

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

RFC2544_FAKE_IP_NETWORK = ipaddress.IPv4Network("198.18.0.0/15")

_HARD_BLOCKED_NETWORKS: tuple[IPNetwork, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # RFC 6598 CGNAT / shared address space
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)

_trusted_fake_ip_cidrs: tuple[IPNetwork, ...] = ()


def validate_trusted_fake_ip_cidrs(values: Iterable[str]) -> list[str]:
    """Return normalized fake-IP CIDRs or raise for unsafe entries."""
    networks: list[str] = []
    for raw in values:
        try:
            network = ipaddress.ip_network(str(raw).strip(), strict=False)
        except ValueError as exc:
            raise ValueError(f"trusted_fake_ip_cidrs entry {raw!r} is not a valid CIDR") from exc

        if not isinstance(network, ipaddress.IPv4Network) or not network.subnet_of(
            RFC2544_FAKE_IP_NETWORK
        ):
            raise ValueError(
                "trusted_fake_ip_cidrs may only contain subnets of "
                f"{RFC2544_FAKE_IP_NETWORK}; got {network}"
            )
        networks.append(str(network))
    return networks


def configure_trusted_fake_ip_cidrs(values: Iterable[str]) -> None:
    """Configure process-wide fake-IP CIDRs trusted by URL fetch guards."""
    global _trusted_fake_ip_cidrs
    normalized = validate_trusted_fake_ip_cidrs(values)
    _trusted_fake_ip_cidrs = tuple(ipaddress.ip_network(value) for value in normalized)


def get_trusted_fake_ip_cidrs() -> list[str]:
    """Return the process-wide trusted fake-IP CIDRs as normalized strings."""
    return [str(network) for network in _trusted_fake_ip_cidrs]


def validate_http_url_for_fetch(
    url: str,
    *,
    trusted_fake_ip_cidrs: Iterable[str] | None = None,
) -> list[str]:
    """Validate that an HTTP(S) URL does not resolve to a blocked address.

    Returns the list of vetted IP addresses the hostname resolved to (as
    strings), so callers can *pin* the connection to an approved address and
    avoid a second, unguarded DNS resolution (the DNS-rebinding TOCTOU). The
    return value is safe to ignore for callers that only want the raise-on-block
    behavior.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsupportedURLSchemeError("Only HTTP/HTTPS URLs are supported")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Invalid URL: no hostname")

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve hostname: {hostname}") from exc

    trusted_networks = (
        tuple(
            ipaddress.ip_network(value)
            for value in validate_trusted_fake_ip_cidrs(trusted_fake_ip_cidrs)
        )
        if trusted_fake_ip_cidrs is not None
        else _trusted_fake_ip_cidrs
    )

    vetted: list[str] = []
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        block_reason = _hard_block_reason(addr)
        if block_reason is not None:
            raise SSRFBlockedError(_blocked_message(hostname, addr, block_reason))
        if _is_trusted_fake_ip(addr, trusted_networks):
            vetted.append(str(addr))
            continue
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            reason = (
                f"reserved/private range; configure [tools].trusted_fake_ip_cidrs "
                f"with {RFC2544_FAKE_IP_NETWORK} only if this is fake-IP DNS"
                if addr in RFC2544_FAKE_IP_NETWORK
                else "private/internal range"
            )
            raise SSRFBlockedError(_blocked_message(hostname, addr, reason))
        vetted.append(str(addr))
    return vetted


def _hard_block_reason(addr: IPAddress) -> str | None:
    for network in _HARD_BLOCKED_NETWORKS:
        if addr.version == network.version and addr in network:
            return f"hard-blocked network {network}"
    return None


def _is_trusted_fake_ip(addr: IPAddress, trusted_networks: tuple[IPNetwork, ...]) -> bool:
    return any(addr.version == network.version and addr in network for network in trusted_networks)


def _blocked_message(hostname: str, addr: IPAddress, reason: str) -> str:
    hint = _dns_hijack_hint(hostname, addr)
    return f"Blocked: {hostname} resolves to {addr} ({reason}{hint})"


def _dns_hijack_hint(hostname: str, addr: IPAddress) -> str:
    if _hostname_is_ip_literal(hostname):
        return ""
    if not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved):
        return ""

    hint = (
        "; if this is a public domain, your DNS/proxy may be returning an "
        "ISP-level fake/private IP"
    )
    if addr.version == 4 and addr in RFC2544_FAKE_IP_NETWORK:
        hint += (
            f"; configure [tools].trusted_fake_ip_cidrs = [\"{RFC2544_FAKE_IP_NETWORK}\"] "
            "only for trusted fake-IP DNS"
        )
    else:
        hint += "; check DNS/proxy settings because this range cannot be bypassed"
    return hint


def _hostname_is_ip_literal(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        return False
    return True


def environment_proxy_url(url: str) -> str | None:
    """Return the opted-in environment proxy applicable to ``url``.

    Callers remain responsible for gating this helper with
    ``opensquilla.env.trust_env()``. Resolving the proxy explicitly lets the
    pinned transport preserve DNS-rebinding protection instead of relying on
    HTTPX's ambient proxy discovery, which is disabled by a custom transport.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname or proxy_bypass(hostname):
        return None
    proxies = getproxies()
    proxy = proxies.get(parsed.scheme.lower()) or proxies.get("all")
    return str(proxy) if proxy else None


def pinned_transport(url: str, vetted_ips: list[str], **transport_kwargs: object) -> object | None:
    """Return an httpx transport that pins connections to a vetted IP.

    For HTTPS the connection must reach the pre-validated IP while still
    presenting the original hostname for SNI and certificate verification, so a
    URL rewrite is not enough. This returns an ``httpx.AsyncHTTPTransport``
    subclass that swaps the request URL host to the vetted IP at connect time
    and sets the ``sni_hostname`` extension to the original hostname (httpx then
    verifies the certificate against that name). Returns ``None`` when pinning
    is not applicable (no vetted IPs, or the host is already an IP literal), so
    the caller can use a normal client.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname or not vetted_ips or _hostname_is_ip_literal(hostname):
        return None

    import httpx

    ip = vetted_ips[0]

    class _PinnedTransport(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.host == hostname:
                request.extensions = dict(request.extensions)
                request.extensions.setdefault("sni_hostname", hostname)
                if "host" not in {k.lower() for k in request.headers}:
                    request.headers["Host"] = (
                        hostname if parsed.port is None else f"{hostname}:{parsed.port}"
                    )
                request.url = request.url.copy_with(host=ip)
            return await super().handle_async_request(request)

    return _PinnedTransport(**transport_kwargs)  # type: ignore[arg-type]
