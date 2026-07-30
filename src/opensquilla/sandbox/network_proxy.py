"""Asyncio local HTTP proxy core for sandbox-managed network access."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import os
import socket
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from opensquilla.sandbox.domain_validation import normalize_domain
from opensquilla.sandbox.network_guard import NetworkDecision
from opensquilla.sandbox.network_runtime import (
    NetworkPolicyRequest,
    NetworkProtocol,
    call_network_policy_decider,
)

_HEADER_LIMIT = 64 * 1024
_MAX_BODY_BYTES = 1_048_576
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_DEFAULT_HEADER_READ_TIMEOUT_SECONDS = 5.0
_DEFAULT_BODY_READ_TIMEOUT_SECONDS = 5.0
_DEFAULT_RESPONSE_READ_TIMEOUT_SECONDS = 5.0
_CHUNK_SIZE = 64 * 1024
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_RFC2544_FAKE_IP_NETWORK = ipaddress.IPv4Network("198.18.0.0/15")
_HARD_BLOCKED_NETWORKS = (
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


@dataclass(frozen=True)
class _ParsedRequest:
    method: str
    target: str
    version: str
    scheme: str
    host: str
    port: int
    origin_form: str | None
    content_length: int


@dataclass(frozen=True)
class _UpstreamProxy:
    host: str
    port: int
    authorization: str | None = None


class _ProxyDeniedError(ValueError):
    """Internal fail-closed signal for syntactically valid but denied requests."""


class SandboxProxyServer:
    def __init__(
        self,
        decide: Callable[[str], NetworkDecision] | None = None,
        *,
        policy_decider: object | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        header_read_timeout_seconds: float = _DEFAULT_HEADER_READ_TIMEOUT_SECONDS,
        body_read_timeout_seconds: float = _DEFAULT_BODY_READ_TIMEOUT_SECONDS,
        response_read_timeout_seconds: float = _DEFAULT_RESPONSE_READ_TIMEOUT_SECONDS,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
        resolver: Callable[[str, int], tuple[str, int]] | None = None,
        on_upstream_opened: Callable[[NetworkDecision], Awaitable[None] | None] | None = None,
    ) -> None:
        if policy_decider is None and decide is not None and hasattr(decide, "decide"):
            policy_decider = decide
            decide = None
        if decide is None and policy_decider is None:
            raise TypeError("SandboxProxyServer requires decide or policy_decider")
        self._decide = decide
        self._policy_decider = policy_decider
        self._resolver = resolver or _resolve_validated_upstream
        self._on_upstream_opened = on_upstream_opened
        self.host = host
        self.port = port
        self._header_read_timeout_seconds = header_read_timeout_seconds
        self._body_read_timeout_seconds = body_read_timeout_seconds
        self._response_read_timeout_seconds = response_read_timeout_seconds
        self._max_response_bytes = max(1, max_response_bytes)
        self._server: asyncio.Server | None = None
        self._active_tasks: set[asyncio.Task[None]] = set()
        self._active_writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        if self._server is not None:
            return

        server = await asyncio.start_server(self._accept_client, self.host, self.port)
        socket = next(iter(server.sockets or ()), None)
        if socket is None:
            server.close()
            await server.wait_closed()
            raise RuntimeError("sandbox proxy failed to bind")

        bound_host, bound_port = socket.getsockname()[:2]
        self.host = str(bound_host)
        self.port = int(bound_port)
        self._server = server

    async def stop(self) -> None:
        server = self._server
        if server is not None:
            self._server = None
            server.close()
            await asyncio.sleep(0)

        current_task = asyncio.current_task()
        tasks = [
            task
            for task in self._active_tasks
            if task is not current_task and not task.done()
        ]
        for task in tasks:
            task.cancel()

        writers = tuple(self._active_writers)
        for writer in writers:
            writer.close()
            with suppress(RuntimeError):
                writer.transport.abort()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        for writer in writers:
            with suppress(ConnectionError, RuntimeError):
                await writer.wait_closed()

        if server is not None:
            await server.wait_closed()

    def _accept_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._active_writers.add(writer)
        task = asyncio.create_task(self._handle(reader, writer))
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._active_tasks.add(task)
        self._active_writers.add(writer)
        try:
            header = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                timeout=self._header_read_timeout_seconds,
            )
            if len(header) > _HEADER_LIMIT:
                raise ValueError("request_header_too_large")
            request = _parse_request(header)
            if not request.host:
                raise ValueError("empty_host")
            decision = await self._decide_request(request, writer)
            if decision.status == "allow" and request.host:
                try:
                    if request.method == "CONNECT":
                        await self._tunnel_connect(request, reader, writer, decision)
                    else:
                        await self._forward_http(request, header, reader, writer, decision)
                except _ProxyDeniedError:
                    await _write_response(
                        writer,
                        _response(403, "Forbidden", b"Network access denied.\n"),
                    )
                except Exception:
                    await _write_response(
                        writer,
                        _response(502, "Bad Gateway", b"Upstream connection failed.\n"),
                    )
            else:
                await _write_response(
                    writer,
                    _response(403, "Forbidden", _network_denial_body(decision)),
                )
        except (
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            ValueError,
        ):
            await _write_response(
                writer,
                _response(403, "Forbidden", b"Network access denied.\n"),
            )
        except Exception:
            await _write_response(
                writer,
                _response(403, "Forbidden", b"Network access denied.\n"),
            )
        finally:
            self._active_writers.discard(writer)
            if task is not None:
                self._active_tasks.discard(task)
            writer.close()
            with suppress(ConnectionError, RuntimeError):
                await writer.wait_closed()

    async def _decide_request(
        self,
        request: _ParsedRequest,
        writer: asyncio.StreamWriter,
    ) -> NetworkDecision:
        if self._policy_decider is not None:
            return await call_network_policy_decider(
                self._policy_decider,
                NetworkPolicyRequest(
                    protocol=_protocol_for_request(request),
                    host=request.host,
                    port=request.port,
                    method=request.method,
                    client_addr=_client_addr(writer),
                ),
            )
        if self._decide is None:
            raise ValueError("missing_network_decider")
        return self._decide(request.host)
    async def _open_upstream(
        self,
        request: _ParsedRequest,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if request.method == "CONNECT":
            proxy = _upstream_proxy_for_connect(self.host, self.port)
            if proxy is not None:
                return await _open_upstream_proxy_tunnel(
                    request,
                    proxy,
                    timeout_seconds=self._header_read_timeout_seconds,
                )
        try:
            connect_host, connect_port = self._resolver(request.host, request.port)
            connect_host, connect_port = _validate_resolver_destination(
                connect_host,
                connect_port,
            )
        except Exception as exc:
            raise _ProxyDeniedError("unsafe_upstream_resolution") from exc
        open_kwargs: dict[str, Any] = {}
        if request.scheme == "https" and request.method != "CONNECT":
            open_kwargs = {"ssl": True, "server_hostname": request.host}
        upstream_reader, upstream_writer = await asyncio.open_connection(
            connect_host,
            connect_port,
            **open_kwargs,
        )
        self._active_writers.add(upstream_writer)
        return upstream_reader, upstream_writer

    async def _notify_upstream_opened(self, decision: NetworkDecision) -> None:
        if self._on_upstream_opened is None:
            return
        try:
            result = self._on_upstream_opened(decision)
            if result is not None:
                await result
        except Exception:
            return

    async def _forward_http(
        self,
        request: _ParsedRequest,
        header: bytes,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        decision: NetworkDecision,
    ) -> None:
        if request.origin_form is None:
            raise ValueError("missing_origin_form")
        upstream_reader, upstream_writer = await self._open_upstream(request)
        try:
            await self._notify_upstream_opened(decision)
            body = b""
            if request.content_length:
                try:
                    body = await asyncio.wait_for(
                        reader.readexactly(request.content_length),
                        timeout=self._body_read_timeout_seconds,
                    )
                except TimeoutError as exc:
                    raise _ProxyDeniedError("request_body_timeout") from exc
            upstream_writer.write(_rewrite_http_header(request, header) + body)
            await upstream_writer.drain()

            response_bytes = 0
            while response_bytes < self._max_response_bytes:
                read_size = min(_CHUNK_SIZE, self._max_response_bytes - response_bytes)
                try:
                    chunk = await asyncio.wait_for(
                        upstream_reader.read(read_size),
                        timeout=self._response_read_timeout_seconds,
                    )
                except TimeoutError:
                    if response_bytes == 0:
                        await _write_response(
                            writer,
                            _response(
                                502,
                                "Bad Gateway",
                                b"Upstream response timed out.\n",
                            ),
                        )
                    break
                if not chunk:
                    break
                response_bytes += len(chunk)
                writer.write(chunk)
                await writer.drain()
        finally:
            self._active_writers.discard(upstream_writer)
            upstream_writer.close()
            with suppress(ConnectionError, RuntimeError):
                await upstream_writer.wait_closed()

    async def _tunnel_connect(
        self,
        request: _ParsedRequest,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        decision: NetworkDecision,
    ) -> None:
        upstream_reader, upstream_writer = await self._open_upstream(request)
        try:
            await self._notify_upstream_opened(decision)
            await _write_response(
                writer,
                b"HTTP/1.1 200 Connection Established\r\n\r\n",
            )
            await _relay_tunnel(reader, writer, upstream_reader, upstream_writer)
        finally:
            self._active_writers.discard(upstream_writer)
            upstream_writer.close()
            with suppress(ConnectionError, RuntimeError):
                await upstream_writer.wait_closed()


async def _relay_tunnel(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    client_to_upstream = asyncio.create_task(
        _relay_stream(client_reader, upstream_writer),
    )
    upstream_to_client = asyncio.create_task(
        _relay_stream(upstream_reader, client_writer),
    )
    tasks = {client_to_upstream, upstream_to_client}
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*done, *pending, return_exceptions=True)


async def _relay_stream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while True:
            chunk = await reader.read(_CHUNK_SIZE)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, RuntimeError):
        return


def _extract_request_host(header: bytes) -> str:
    return _parse_request(header).host


def _protocol_for_request(request: _ParsedRequest) -> NetworkProtocol:
    if request.method == "CONNECT":
        return NetworkProtocol.HTTPS_CONNECT
    if request.scheme == "https":
        return NetworkProtocol.HTTPS
    return NetworkProtocol.HTTP


def _client_addr(writer: asyncio.StreamWriter) -> str | None:
    peername = writer.get_extra_info("peername")
    if not peername:
        return None
    if isinstance(peername, tuple) and peername:
        return str(peername[0])
    return str(peername)


def _parse_request(header: bytes) -> _ParsedRequest:
    text = header.decode("iso-8859-1", errors="replace")
    lines = text.split("\r\n")
    if not lines or not lines[0].strip():
        raise ValueError("empty_request")

    request_parts = lines[0].split()
    if len(request_parts) != 3:
        raise ValueError("malformed_request_line")

    method = request_parts[0].upper()
    target = request_parts[1]
    version = request_parts[2].upper()
    if not version.startswith("HTTP/"):
        raise ValueError("malformed_request_line")
    if _transfer_encoding_values(lines[1:]):
        raise ValueError("unsupported_transfer_encoding")
    content_length = _content_length(lines[1:])
    if content_length > _MAX_BODY_BYTES:
        raise ValueError("request_body_too_large")
    if method == "CONNECT":
        host, port = _host_port_from_connect_target(target)
        return _ParsedRequest(
            method=method,
            target=target,
            version=version,
            scheme="https",
            host=host,
            port=port,
            origin_form=None,
            content_length=content_length,
        )
    if "://" in target:
        host_values = _host_values(lines[1:])
        if len(host_values) > 1:
            raise ValueError("invalid_host_header")
        scheme, host, port, origin_form = _parts_from_absolute_url(target)
        if host_values:
            header_host, header_port = _host_port_from_authority(
                host_values[0],
                require_port=False,
                default_port=_default_port_for_scheme(scheme),
            )
            if header_host != host or header_port != port:
                raise ValueError("host_header_mismatch")
        return _ParsedRequest(
            method=method,
            target=target,
            version=version,
            scheme=scheme,
            host=host,
            port=port,
            origin_form=origin_form,
            content_length=content_length,
        )

    host_values = _host_values(lines[1:])
    if len(host_values) != 1:
        raise ValueError("invalid_host_header")
    host, port = _host_port_from_authority(
        host_values[0],
        require_port=False,
        default_port=80,
    )
    return _ParsedRequest(
        method=method,
        target=target,
        version=version,
        scheme="http",
        host=host,
        port=port,
        origin_form=_origin_form_target(target),
        content_length=content_length,
    )


def _host_from_absolute_url(target: str) -> str:
    _scheme, host, _port, _origin_form = _parts_from_absolute_url(target)
    return host


def _parts_from_absolute_url(target: str) -> tuple[str, str, int, str]:
    try:
        parsed = urlsplit(target)
        parsed.port
        hostname = parsed.hostname or ""
    except ValueError as exc:
        raise ValueError("malformed_absolute_url") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("malformed_absolute_url")
    if "@" in parsed.netloc or parsed.netloc.endswith(":") or parsed.fragment:
        raise ValueError("malformed_absolute_url")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return (
        scheme,
        _normalize_nonempty_host(hostname),
        parsed.port or _default_port_for_scheme(scheme),
        path,
    )


def _default_port_for_scheme(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _host_from_connect_target(target: str) -> str:
    host, _port = _host_port_from_connect_target(target)
    return host


def _host_port_from_connect_target(target: str) -> tuple[str, int]:
    if "://" in target or any(char in target for char in "/?#"):
        raise ValueError("malformed_connect_target")
    return _host_port_from_authority(target, require_port=True)


def _host_from_authority(authority: str, *, require_port: bool) -> str:
    host, _port = _host_port_from_authority(
        authority,
        require_port=require_port,
        default_port=None if require_port else 80,
    )
    return host


def _host_port_from_authority(
    authority: str,
    *,
    require_port: bool,
    default_port: int | None = None,
) -> tuple[str, int]:
    value = authority.strip()
    if not value or "://" in value or "@" in value:
        raise ValueError("malformed_authority")
    if any(char in value for char in "/?#"):
        raise ValueError("malformed_authority")

    try:
        parsed = urlsplit(f"//{value}")
        port = parsed.port
        hostname = parsed.hostname or ""
    except ValueError as exc:
        raise ValueError("malformed_authority") from exc

    if parsed.path or parsed.query or parsed.fragment:
        raise ValueError("malformed_authority")
    if value.endswith(":"):
        raise ValueError("malformed_authority")
    if require_port and port is None:
        raise ValueError("missing_port")
    if port is None:
        if default_port is None:
            raise ValueError("missing_port")
        port = default_port
    return _normalize_nonempty_host(hostname), port


def _host_values(lines: list[str]) -> list[str]:
    values: list[str] = []
    for line in lines:
        name, separator, value = line.partition(":")
        if separator and name.strip().lower() == "host":
            values.append(value)
    return values


def _content_length(lines: list[str]) -> int:
    values: list[str] = []
    for line in lines:
        name, separator, value = line.partition(":")
        if separator and name.strip().lower() == "content-length":
            values.append(value.strip())
    if not values:
        return 0
    if len(values) != 1:
        raise ValueError("invalid_content_length")
    try:
        length = int(values[0])
    except ValueError as exc:
        raise ValueError("invalid_content_length") from exc
    if length < 0:
        raise ValueError("invalid_content_length")
    return length


def _transfer_encoding_values(lines: list[str]) -> list[str]:
    values: list[str] = []
    for line in lines:
        name, separator, value = line.partition(":")
        if separator and name.strip().lower() == "transfer-encoding":
            values.append(value.strip())
    return values


def _origin_form_target(target: str) -> str:
    if not target:
        raise ValueError("empty_request_target")
    if target == "*":
        return target
    if not target.startswith("/") or "://" in target:
        raise ValueError("malformed_request_target")
    return target


def _rewrite_http_header(request: _ParsedRequest, header: bytes) -> bytes:
    text = header.decode("iso-8859-1", errors="replace")
    lines = text.split("\r\n")
    header_lines = lines[1:-2]
    hop_by_hop = _hop_by_hop_header_names(header_lines)
    rewritten: list[str] = [
        f"{request.method} {request.origin_form} {request.version}",
        f"Host: {_authority_for_request(request)}",
    ]
    for line in header_lines:
        name, separator, value = line.partition(":")
        if not separator:
            continue
        lowered = name.strip().lower()
        if lowered == "host" or lowered in hop_by_hop:
            continue
        rewritten.append(f"{name}:{value}")
    rewritten.append("Connection: close")
    return ("\r\n".join(rewritten) + "\r\n\r\n").encode("iso-8859-1")


def _hop_by_hop_header_names(header_lines: list[str]) -> set[str]:
    names = set(_HOP_BY_HOP_HEADERS)
    for line in header_lines:
        name, separator, value = line.partition(":")
        if separator and name.strip().lower() == "connection":
            for token in value.split(","):
                token = token.strip().lower()
                if token:
                    names.add(token)
    return names


def _authority_for_request(request: _ParsedRequest) -> str:
    if request.port == _default_port_for_scheme(request.scheme):
        return request.host
    return f"{request.host}:{request.port}"


def _normalize_nonempty_host(host: str) -> str:
    normalized = normalize_domain(host)
    if not normalized:
        raise ValueError("empty_host")
    return normalized


def _identity_resolver(host: str, port: int) -> tuple[str, int]:
    return host, port


def _upstream_proxy_for_connect(
    managed_proxy_host: str,
    managed_proxy_port: int,
) -> _UpstreamProxy | None:
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        raw = os.environ.get(key, "").strip()
        if not raw:
            continue
        proxy = _parse_upstream_proxy(raw)
        if proxy is None:
            continue
        if _same_loopback_endpoint(
            proxy.host,
            proxy.port,
            managed_proxy_host,
            managed_proxy_port,
        ):
            continue
        return proxy
    return None


def _parse_upstream_proxy(raw: str) -> _UpstreamProxy | None:
    value = raw if "://" in raw else f"http://{raw}"
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() != "http" or not host:
        return None
    normalized_host = host.strip("[]")
    normalized_port = int(port or 80)
    authorization = None
    if parsed.username is not None:
        password = parsed.password or ""
        token = f"{parsed.username}:{password}".encode()
        authorization = "Basic " + base64.b64encode(token).decode("ascii")
    return _UpstreamProxy(normalized_host, normalized_port, authorization)


def _same_loopback_endpoint(
    left_host: str,
    left_port: int,
    right_host: str,
    right_port: int,
) -> bool:
    if int(left_port) != int(right_port):
        return False
    return _is_loopback_name(left_host) and _is_loopback_name(right_host)


def _is_loopback_name(host: str) -> bool:
    raw = str(host or "").strip().strip("[]").lower()
    if raw == "localhost":
        return True
    try:
        return ipaddress.ip_address(raw).is_loopback
    except ValueError:
        return False


async def _open_upstream_proxy_tunnel(
    request: _ParsedRequest,
    proxy: _UpstreamProxy,
    *,
    timeout_seconds: float,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    upstream_reader, upstream_writer = await asyncio.open_connection(proxy.host, proxy.port)
    target = _connect_authority_for_request(request)
    header_lines = [
        f"CONNECT {target} HTTP/1.1",
        f"Host: {target}",
    ]
    if proxy.authorization:
        header_lines.append(f"Proxy-Authorization: {proxy.authorization}")
    upstream_writer.write(("\r\n".join(header_lines) + "\r\n\r\n").encode("iso-8859-1"))
    await upstream_writer.drain()
    try:
        response = await asyncio.wait_for(
            upstream_reader.readuntil(b"\r\n\r\n"),
            timeout=timeout_seconds,
        )
    except Exception:
        upstream_writer.close()
        with suppress(ConnectionError, RuntimeError):
            await upstream_writer.wait_closed()
        raise
    if not _proxy_connect_response_allows_tunnel(response):
        upstream_writer.close()
        with suppress(ConnectionError, RuntimeError):
            await upstream_writer.wait_closed()
        raise ConnectionError("upstream proxy refused CONNECT tunnel")
    return upstream_reader, upstream_writer


def _proxy_connect_response_allows_tunnel(response: bytes) -> bool:
    status_line = response.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
    parts = status_line.split()
    if len(parts) < 2:
        return False
    try:
        status = int(parts[1])
    except ValueError:
        return False
    return 200 <= status < 300


def _connect_authority_for_request(request: _ParsedRequest) -> str:
    return f"{request.host}:{request.port}"


def _resolve_validated_upstream(host: str, port: int) -> tuple[str, int]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve hostname: {host}") from exc
    if not infos:
        raise ValueError(f"Cannot resolve hostname: {host}")

    first_destination: tuple[str, int] | None = None
    trusted_fake_networks = _trusted_fake_ip_networks()
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        raw_addr = sockaddr[0]
        try:
            addr = ipaddress.ip_address(raw_addr)
        except ValueError as exc:
            raise ValueError(f"Cannot parse resolved address for {host}: {raw_addr}") from exc
        reason = _unsafe_resolved_address_reason(addr, trusted_fake_networks)
        if reason is not None:
            raise ValueError(f"Blocked: {host} resolves to {addr} ({reason})")
        if first_destination is None:
            first_destination = (str(addr), int(sockaddr[1] if len(sockaddr) > 1 else port))

    if first_destination is None:
        raise ValueError(f"Cannot resolve hostname: {host}")
    return first_destination


def _validate_resolver_destination(host: str, port: int) -> tuple[str, int]:
    try:
        normalized_port = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid resolved port") from exc
    if not 0 <= normalized_port <= 65535:
        raise ValueError("Invalid resolved port")

    raw_host = str(host or "").strip()
    if not raw_host:
        raise ValueError("Empty resolved host")

    try:
        addr = ipaddress.ip_address(raw_host.strip("[]"))
    except ValueError:
        return _resolve_validated_upstream(raw_host, normalized_port)

    reason = _unsafe_resolved_address_reason(addr, _trusted_fake_ip_networks())
    if reason is not None:
        raise ValueError(f"Blocked: resolver returned {addr} ({reason})")
    return str(addr), normalized_port


def _trusted_fake_ip_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [_RFC2544_FAKE_IP_NETWORK]
    try:
        from opensquilla.tools.ssrf import get_trusted_fake_ip_cidrs

        values: tuple[str, ...] = tuple(get_trusted_fake_ip_cidrs())
    except Exception:
        values = ()
    for value in values:
        try:
            network = ipaddress.ip_network(value)
        except ValueError:
            continue
        if network not in networks:
            networks.append(network)
    return tuple(networks)


def _unsafe_resolved_address_reason(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
    trusted_fake_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    for network in _HARD_BLOCKED_NETWORKS:
        if addr.version == network.version and addr in network:
            return f"hard-blocked network {network}"
    if _is_trusted_fake_ip(addr, trusted_fake_networks):
        return None
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
        if isinstance(addr, ipaddress.IPv4Address) and addr in _RFC2544_FAKE_IP_NETWORK:
            return (
                "reserved/private range; configure [tools].trusted_fake_ip_cidrs "
                f"with {_RFC2544_FAKE_IP_NETWORK} only if this is fake-IP DNS"
            )
        return "private/internal range"
    return None


def _is_trusted_fake_ip(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    return any(addr.version == network.version and addr in network for network in trusted_networks)


async def _write_response(writer: asyncio.StreamWriter, response: bytes) -> None:
    if writer.is_closing():
        return
    with suppress(ConnectionError, RuntimeError):
        writer.write(response)
        await writer.drain()


def _response(status_code: int, reason: str, body: bytes) -> bytes:
    return (
        f"HTTP/1.1 {status_code} {reason}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + body


def _network_denial_body(decision: NetworkDecision) -> bytes:
    reason = " ".join(str(decision.reason or "").split())[:500]
    if not reason or reason in {"denied", "not_allowed"}:
        return b"Network access denied.\n"
    return f"Network access denied: {reason}\n".encode("utf-8", errors="replace")


__all__ = ["SandboxProxyServer"]
