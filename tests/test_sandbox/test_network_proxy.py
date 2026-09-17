from __future__ import annotations

import asyncio
import ipaddress
import socket
from contextlib import suppress

import pytest

from opensquilla.sandbox.network_guard import NetworkDecision
from opensquilla.sandbox.network_proxy import SandboxProxyServer

_UPSTREAM_PROXY_ENV_KEYS = (
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
)


@pytest.fixture(autouse=True)
def _isolate_ambient_upstream_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep an ambient proxy out of the direct-upstream assertions.

    ``_open_upstream`` chains a CONNECT through ``HTTPS_PROXY`` and friends when
    one is set, which is deliberate — the managed proxy has to work behind a
    corporate proxy. Most tests here assert the direct path instead, so on any
    machine that exports one of those variables (corporate networks, sandboxed
    CI) the proxy tunnels to the ambient endpoint and the assertions fail
    against it rather than against the resolved upstream. Clear them so the
    default path under test is the one being asserted;
    ``test_proxy_tunnels_connect_via_https_proxy_env`` sets the variable it
    needs afterwards and is unaffected.
    """
    for key in _UPSTREAM_PROXY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _allow_decision(host: str) -> NetworkDecision:
    return NetworkDecision(
        status="allow",
        normalized_host=host,
        reason="test_allow",
        source="test",
    )


def _fake_getaddrinfo(address: str):
    def _inner(host: str, port: int, *args: object, **kwargs: object):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    return _inner


def test_proxy_resolver_allows_rfc2544_fake_ip_dns_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox import network_proxy as mod

    monkeypatch.setattr(mod.socket, "getaddrinfo", _fake_getaddrinfo("198.18.0.24"))

    assert mod._resolve_validated_upstream("github.com", 443) == ("198.18.0.24", 443)


@pytest.mark.parametrize("cgnat_ip", ["100.64.0.1", "100.127.255.254"])
def test_proxy_resolver_fails_closed_on_rfc6598_cgnat_range(cgnat_ip: str) -> None:
    from opensquilla.sandbox import network_proxy as mod

    trusted = mod._trusted_fake_ip_networks()
    reason = mod._unsafe_resolved_address_reason(ipaddress.ip_address(cgnat_ip), trusted)
    assert reason is not None


def _route_public_destination_to_upstream(
    monkeypatch: pytest.MonkeyPatch,
    *,
    public_host: str,
    public_port: int,
    upstream_host: str,
    upstream_port: int,
) -> None:
    real_open_connection = asyncio.open_connection

    async def fake_open_connection(
        host: str,
        port: int,
        *args: object,
        **kwargs: object,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if str(host) == public_host and int(port) == public_port:
            return await real_open_connection(
                upstream_host,
                upstream_port,
                *args,
                **kwargs,
            )
        return await real_open_connection(host, port, *args, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)


async def _send_proxy_request(server: SandboxProxyServer, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection(server.host, server.port)
    try:
        writer.write(request)
        await writer.drain()
        return await reader.read(4096)
    finally:
        writer.close()
        await writer.wait_closed()


async def _wait_for_active_client(server: SandboxProxyServer) -> None:
    for _ in range(50):
        if server._active_writers:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("proxy did not register active client")


async def _send_proxy_request_with_timeout(
    server: SandboxProxyServer,
    request: bytes,
    *,
    timeout: float = 1.0,
) -> bytes:
    reader, writer = await asyncio.open_connection(server.host, server.port)
    try:
        writer.write(request)
        await writer.drain()
        return await asyncio.wait_for(reader.read(4096), timeout=timeout)
    finally:
        writer.close()
        await writer.wait_closed()


async def _send_proxy_request_until_eof(
    server: SandboxProxyServer,
    request: bytes,
    *,
    timeout: float = 1.0,
) -> bytes:
    reader, writer = await asyncio.open_connection(server.host, server.port)
    chunks: list[bytes] = []
    try:
        writer.write(request)
        await writer.drain()
        while True:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        writer.close()
        await writer.wait_closed()


async def test_proxy_forwards_allowed_absolute_http_request_to_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_hosts: list[str] = []
    resolver_calls: list[tuple[str, int]] = []
    upstream_requests: list[bytes] = []

    async def handle_upstream(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        header = await reader.readuntil(b"\r\n\r\n")
        content_length = 0
        for line in header.decode("iso-8859-1").split("\r\n"):
            name, separator, value = line.partition(":")
            if separator and name.lower() == "content-length":
                content_length = int(value.strip())
        body = await reader.readexactly(content_length)
        upstream_requests.append(header + body)
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 12\r\n"
            b"Connection: close\r\n"
            b"\r\n"
            b"proxied-body"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(handle_upstream, "127.0.0.1", 0)
    upstream_socket = next(iter(upstream.sockets or ()), None)
    assert upstream_socket is not None
    upstream_host, upstream_port = upstream_socket.getsockname()[:2]
    public_upstream_host = "93.184.216.34"
    _route_public_destination_to_upstream(
        monkeypatch,
        public_host=public_upstream_host,
        public_port=int(upstream_port),
        upstream_host=str(upstream_host),
        upstream_port=int(upstream_port),
    )

    def decide(host: str) -> NetworkDecision:
        seen_hosts.append(host)
        return _allow_decision(host)

    def resolver(host: str, port: int) -> tuple[str, int]:
        resolver_calls.append((host, port))
        return public_upstream_host, int(upstream_port)

    server = SandboxProxyServer(
        decide,
        resolver=resolver,
    )
    await server.start()
    try:
        response = await _send_proxy_request(
            server,
            b"POST http://Allowed.test/upload?name=x HTTP/1.1\r\n"
            b"Host: Allowed.test\r\n"
            b"User-Agent: pytest\r\n"
            b"Content-Length: 5\r\n"
            b"Connection: keep-alive\r\n"
            b"\r\n"
            b"hello",
        )
    finally:
        await server.stop()
        upstream.close()
        await upstream.wait_closed()

    assert response.startswith(b"HTTP/1.1 200 OK")
    assert b"proxied-body" in response
    assert seen_hosts == ["allowed.test"]
    assert resolver_calls == [("allowed.test", 80)]
    assert upstream_requests == [
        b"POST /upload?name=x HTTP/1.1\r\n"
        b"Host: allowed.test\r\n"
        b"User-Agent: pytest\r\n"
        b"Content-Length: 5\r\n"
        b"Connection: close\r\n"
        b"\r\n"
        b"hello",
    ]


async def test_proxy_forwards_absolute_https_request_with_tls_to_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_hosts: list[str] = []
    open_calls: list[dict[str, object]] = []
    upstream_writes: list[bytes] = []

    class FakeReader:
        def __init__(self) -> None:
            self._chunks = [
                b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n",
                b"",
            ]

        async def read(self, _size: int) -> bytes:
            return self._chunks.pop(0)

    class FakeWriter:
        def write(self, data: bytes) -> None:
            upstream_writes.append(data)

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    def decide(host: str) -> NetworkDecision:
        seen_hosts.append(host)
        return _allow_decision(host)

    server = SandboxProxyServer(
        decide,
        resolver=lambda host, port: ("198.18.0.24", port),
    )
    await server.start()
    real_open_connection = asyncio.open_connection

    async def fake_open_connection(host: str, port: int, **kwargs: object):
        if str(host) == server.host and int(port) == server.port:
            return await real_open_connection(host, port, **kwargs)
        open_calls.append({"host": host, "port": port, **kwargs})
        return FakeReader(), FakeWriter()

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    try:
        response = await _send_proxy_request(
            server,
            b"HEAD https://Allowed.test/check?q=1 HTTP/1.1\r\n"
            b"Host: Allowed.test\r\n"
            b"\r\n",
        )
    finally:
        await server.stop()

    assert response.startswith(b"HTTP/1.1 204 No Content")
    assert seen_hosts == ["allowed.test"]
    assert open_calls == [
        {
            "host": "198.18.0.24",
            "port": 443,
            "ssl": True,
            "server_hostname": "allowed.test",
        }
    ]
    assert upstream_writes == [
        b"HEAD /check?q=1 HTTP/1.1\r\n"
        b"Host: allowed.test\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    ]


async def test_proxy_rejects_mismatched_host_for_absolute_http_request() -> None:
    seen_hosts: list[str] = []
    resolver_calls: list[tuple[str, int]] = []

    def decide(host: str) -> NetworkDecision:
        seen_hosts.append(host)
        return _allow_decision(host)

    def resolver(host: str, port: int) -> tuple[str, int]:
        resolver_calls.append((host, port))
        return "127.0.0.1", 9

    server = SandboxProxyServer(decide, resolver=resolver)
    await server.start()
    try:
        response = await _send_proxy_request(
            server,
            b"GET http://Allowed.test/path HTTP/1.1\r\n"
            b"Host: blocked.test\r\n"
            b"\r\n",
        )
    finally:
        await server.stop()

    assert response.startswith(b"HTTP/1.1 403")
    assert seen_hosts == []
    assert resolver_calls == []


async def test_proxy_forwards_absolute_http_without_host_using_approved_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream_requests: list[bytes] = []

    async def handle_upstream(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        upstream_requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 0\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(handle_upstream, "127.0.0.1", 0)
    upstream_socket = next(iter(upstream.sockets or ()), None)
    assert upstream_socket is not None
    upstream_host, upstream_port = upstream_socket.getsockname()[:2]
    public_upstream_host = "93.184.216.34"
    _route_public_destination_to_upstream(
        monkeypatch,
        public_host=public_upstream_host,
        public_port=int(upstream_port),
        upstream_host=str(upstream_host),
        upstream_port=int(upstream_port),
    )

    server = SandboxProxyServer(
        _allow_decision,
        resolver=lambda host, port: (public_upstream_host, int(upstream_port)),
    )
    await server.start()
    try:
        response = await _send_proxy_request(
            server,
            b"GET http://Allowed.test:8080/path HTTP/1.1\r\n"
            b"\r\n",
        )
    finally:
        await server.stop()
        upstream.close()
        await upstream.wait_closed()

    assert response.startswith(b"HTTP/1.1 200 OK")
    assert upstream_requests == [
        b"GET /path HTTP/1.1\r\n"
        b"Host: allowed.test:8080\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    ]


async def test_proxy_tunnels_allowed_connect_after_validated_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_hosts: list[str] = []
    resolver_calls: list[tuple[str, int]] = []
    upstream_payloads: list[bytes] = []
    upstream_opened: list[NetworkDecision] = []

    async def handle_upstream(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        upstream_payloads.append(await reader.readexactly(4))
        writer.write(b"pong")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(handle_upstream, "127.0.0.1", 0)
    upstream_socket = next(iter(upstream.sockets or ()), None)
    assert upstream_socket is not None
    upstream_host, upstream_port = upstream_socket.getsockname()[:2]
    public_upstream_host = "93.184.216.34"
    _route_public_destination_to_upstream(
        monkeypatch,
        public_host=public_upstream_host,
        public_port=int(upstream_port),
        upstream_host=str(upstream_host),
        upstream_port=int(upstream_port),
    )

    def decide(host: str) -> NetworkDecision:
        seen_hosts.append(host)
        return _allow_decision(host)

    def resolver(host: str, port: int) -> tuple[str, int]:
        resolver_calls.append((host, port))
        return public_upstream_host, int(upstream_port)

    async def on_upstream_opened(decision: NetworkDecision) -> None:
        upstream_opened.append(decision)

    server = SandboxProxyServer(
        decide,
        resolver=resolver,
        on_upstream_opened=on_upstream_opened,
    )
    await server.start()
    reader, writer = await asyncio.open_connection(server.host, server.port)
    try:
        writer.write(b"CONNECT Allowed.test:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1.0)
        writer.write(b"ping")
        await writer.drain()
        tunneled = await asyncio.wait_for(reader.readexactly(4), timeout=1.0)
    finally:
        writer.close()
        await writer.wait_closed()
        await server.stop()
        upstream.close()
        await upstream.wait_closed()

    assert response.startswith(b"HTTP/1.1 200 Connection Established")
    assert tunneled == b"pong"
    assert seen_hosts == ["allowed.test"]
    assert resolver_calls == [("allowed.test", 443)]
    assert upstream_payloads == [b"ping"]
    assert upstream_opened == [_allow_decision("allowed.test")]


async def test_proxy_tunnels_connect_via_https_proxy_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream_proxy_requests: list[bytes] = []
    upstream_proxy_payloads: list[bytes] = []

    async def handle_upstream_proxy(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        upstream_proxy_requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        upstream_proxy_payloads.append(await reader.readexactly(4))
        writer.write(b"pong")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream_proxy = await asyncio.start_server(handle_upstream_proxy, "127.0.0.1", 0)
    upstream_socket = next(iter(upstream_proxy.sockets or ()), None)
    assert upstream_socket is not None
    upstream_host, upstream_port = upstream_socket.getsockname()[:2]
    monkeypatch.setenv("HTTPS_PROXY", f"http://{upstream_host}:{upstream_port}")
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)

    def resolver(_host: str, _port: int) -> tuple[str, int]:
        raise AssertionError("CONNECT should be routed through upstream proxy env")

    server = SandboxProxyServer(_allow_decision, resolver=resolver)
    await server.start()
    reader, writer = await asyncio.open_connection(server.host, server.port)
    try:
        writer.write(b"CONNECT Allowed.test:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1.0)
        writer.write(b"ping")
        await writer.drain()
        tunneled = await asyncio.wait_for(reader.readexactly(4), timeout=1.0)
    finally:
        writer.close()
        await writer.wait_closed()
        await server.stop()
        upstream_proxy.close()
        await upstream_proxy.wait_closed()

    assert response.startswith(b"HTTP/1.1 200 Connection Established")
    assert tunneled == b"pong"
    assert upstream_proxy_requests == [
        b"CONNECT allowed.test:443 HTTP/1.1\r\n"
        b"Host: allowed.test:443\r\n"
        b"\r\n"
    ]
    assert upstream_proxy_payloads == [b"ping"]


async def test_proxy_connect_opens_plain_tcp_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_calls: list[dict[str, object]] = []

    class FakeReader:
        async def read(self, _size: int) -> bytes:
            return b""

    class FakeWriter:
        class _Transport:
            def abort(self) -> None:
                return None

        transport = _Transport()

        def write(self, _data: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    server = SandboxProxyServer(
        _allow_decision,
        resolver=lambda host, port: ("198.18.0.24", port),
    )
    await server.start()
    real_open_connection = asyncio.open_connection

    async def fake_open_connection(host: str, port: int, **kwargs: object):
        if str(host) == server.host and int(port) == server.port:
            return await real_open_connection(host, port, **kwargs)
        open_calls.append({"host": host, "port": port, **kwargs})
        return FakeReader(), FakeWriter()

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    _reader, writer = await asyncio.open_connection(server.host, server.port)
    try:
        writer.write(b"CONNECT Allowed.test:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        for _ in range(50):
            if open_calls:
                break
            await asyncio.sleep(0.01)
    finally:
        writer.close()
        await writer.wait_closed()
        await server.stop()

    assert open_calls == [{"host": "198.18.0.24", "port": 443}]


async def test_proxy_rejects_allowed_connect_when_resolver_denies_upstream() -> None:
    seen_hosts: list[str] = []
    resolver_calls: list[tuple[str, int]] = []

    def decide(host: str) -> NetworkDecision:
        seen_hosts.append(host)
        return _allow_decision(host)

    def resolver(host: str, port: int) -> tuple[str, int]:
        resolver_calls.append((host, port))
        raise ValueError("unsafe resolution")

    server = SandboxProxyServer(decide, resolver=resolver)
    await server.start()
    try:
        response = await _send_proxy_request(
            server,
            b"CONNECT Allowed.test:443 HTTP/1.1\r\n\r\n",
        )
    finally:
        await server.stop()

    assert response.startswith(b"HTTP/1.1 403")
    assert seen_hosts == ["allowed.test"]
    assert resolver_calls == [("allowed.test", 443)]


@pytest.mark.parametrize(
    "unsafe_destination",
    [
        "127.0.0.1",
        "localhost",
        "10.0.0.7",
        "169.254.169.254",
        "240.0.0.1",
        "::1",
    ],
)
async def test_proxy_rejects_allowed_http_when_custom_resolver_returns_unsafe_address(
    monkeypatch: pytest.MonkeyPatch,
    unsafe_destination: str,
) -> None:
    resolver_calls: list[tuple[str, int]] = []
    upstream_requests: list[bytes] = []

    async def handle_upstream(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        upstream_requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 0\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(handle_upstream, "127.0.0.1", 0)
    upstream_socket = next(iter(upstream.sockets or ()), None)
    assert upstream_socket is not None
    upstream_host, upstream_port = upstream_socket.getsockname()[:2]
    _route_public_destination_to_upstream(
        monkeypatch,
        public_host=unsafe_destination,
        public_port=int(upstream_port),
        upstream_host=str(upstream_host),
        upstream_port=int(upstream_port),
    )

    def resolver(host: str, port: int) -> tuple[str, int]:
        resolver_calls.append((host, port))
        return unsafe_destination, int(upstream_port)

    server = SandboxProxyServer(_allow_decision, resolver=resolver)
    await server.start()
    try:
        response = await _send_proxy_request(
            server,
            b"GET http://Allowed.test/path HTTP/1.1\r\n\r\n",
        )
    finally:
        await server.stop()
        upstream.close()
        await upstream.wait_closed()

    assert response.startswith(b"HTTP/1.1 403")
    assert resolver_calls == [("allowed.test", 80)]
    assert upstream_requests == []


async def test_proxy_rejects_oversized_content_length_without_reading_body() -> None:
    server = SandboxProxyServer(_allow_decision)
    await server.start()
    try:
        response = await _send_proxy_request_with_timeout(
            server,
            b"POST http://Allowed.test/upload HTTP/1.1\r\n"
            b"Content-Length: 1048577\r\n"
            b"\r\n",
            timeout=0.5,
        )
    finally:
        await server.stop()

    assert response.startswith((b"HTTP/1.1 403", b"HTTP/1.1 413"))


async def test_proxy_rejects_chunked_request_without_opening_upstream() -> None:
    seen_hosts: list[str] = []
    resolver_calls: list[tuple[str, int]] = []

    def decide(host: str) -> NetworkDecision:
        seen_hosts.append(host)
        return _allow_decision(host)

    def resolver(host: str, port: int) -> tuple[str, int]:
        resolver_calls.append((host, port))
        return "127.0.0.1", 9

    server = SandboxProxyServer(decide, resolver=resolver)
    await server.start()
    try:
        response = await _send_proxy_request(
            server,
            b"POST http://Allowed.test/upload HTTP/1.1\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            b"5\r\nhello\r\n0\r\n\r\n",
        )
    finally:
        await server.stop()

    assert response.startswith(b"HTTP/1.1 403")
    assert seen_hosts == []
    assert resolver_calls == []


async def test_proxy_rejects_transfer_encoding_content_length_conflict() -> None:
    seen_hosts: list[str] = []

    def decide(host: str) -> NetworkDecision:
        seen_hosts.append(host)
        return _allow_decision(host)

    server = SandboxProxyServer(decide)
    await server.start()
    try:
        response = await _send_proxy_request(
            server,
            b"POST http://Allowed.test/upload HTTP/1.1\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Content-Length: 5\r\n"
            b"\r\n"
            b"hello",
        )
    finally:
        await server.stop()

    assert response.startswith(b"HTTP/1.1 403")
    assert seen_hosts == []


async def test_proxy_returns_403_for_unknown_absolute_http_host() -> None:
    seen_hosts: list[str] = []

    def decide(host: str) -> NetworkDecision:
        seen_hosts.append(host)
        return NetworkDecision(
            status="ask",
            normalized_host=host,
            reason="unknown_domain",
            source=None,
        )

    server = SandboxProxyServer(decide)
    await server.start()
    try:
        response = await _send_proxy_request(
            server,
            b"GET http://Example.com/path HTTP/1.1\r\n"
            b"Host: Example.com\r\n"
            b"\r\n",
        )
    finally:
        await server.stop()

    assert response.startswith(b"HTTP/1.1 403")
    assert seen_hosts == ["example.com"]


async def test_proxy_returns_403_for_connect_block() -> None:
    seen_hosts: list[str] = []

    def decide(host: str) -> NetworkDecision:
        seen_hosts.append(host)
        return NetworkDecision(
            status="block",
            normalized_host=host,
            reason="ip_literal",
            source="validation",
        )

    server = SandboxProxyServer(decide)
    await server.start()
    try:
        response = await _send_proxy_request(
            server,
            b"CONNECT 169.254.169.254:443 HTTP/1.1\r\n\r\n",
        )
    finally:
        await server.stop()

    assert response.startswith(b"HTTP/1.1 403")
    assert seen_hosts == ["169.254.169.254"]


async def test_proxy_forwards_allowed_origin_form_http_request_to_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_hosts: list[str] = []
    upstream_requests: list[bytes] = []

    async def handle_upstream(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        upstream_requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(
            b"HTTP/1.1 204 No Content\r\n"
            b"Content-Length: 0\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(handle_upstream, "127.0.0.1", 0)
    upstream_socket = next(iter(upstream.sockets or ()), None)
    assert upstream_socket is not None
    upstream_host, upstream_port = upstream_socket.getsockname()[:2]
    public_upstream_host = "93.184.216.34"
    _route_public_destination_to_upstream(
        monkeypatch,
        public_host=public_upstream_host,
        public_port=int(upstream_port),
        upstream_host=str(upstream_host),
        upstream_port=int(upstream_port),
    )

    def decide(host: str) -> NetworkDecision:
        seen_hosts.append(host)
        return NetworkDecision(
            status="allow",
            normalized_host=host,
            reason="domain_grant",
            source="domain:pypi.org",
        )

    server = SandboxProxyServer(
        decide,
        resolver=lambda host, port: (public_upstream_host, int(upstream_port)),
    )
    await server.start()
    try:
        response = await _send_proxy_request(
            server,
            b"GET /simple HTTP/1.1\r\nHost: PyPI.org:443\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()
        upstream.close()
        await upstream.wait_closed()

    assert response.startswith(b"HTTP/1.1 204")
    assert seen_hosts == ["pypi.org"]
    assert upstream_requests == [
        b"GET /simple HTTP/1.1\r\n"
        b"Host: pypi.org:443\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    ]


async def test_proxy_times_out_upstream_response_that_never_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handle_upstream(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await reader.readuntil(b"\r\n\r\n")
        await asyncio.sleep(1.0)
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()

    upstream = await asyncio.start_server(handle_upstream, "127.0.0.1", 0)
    upstream_socket = next(iter(upstream.sockets or ()), None)
    assert upstream_socket is not None
    upstream_host, upstream_port = upstream_socket.getsockname()[:2]
    public_upstream_host = "93.184.216.34"
    _route_public_destination_to_upstream(
        monkeypatch,
        public_host=public_upstream_host,
        public_port=int(upstream_port),
        upstream_host=str(upstream_host),
        upstream_port=int(upstream_port),
    )

    server = SandboxProxyServer(
        _allow_decision,
        resolver=lambda host, port: (public_upstream_host, int(upstream_port)),
        response_read_timeout_seconds=0.05,
    )
    await server.start()
    try:
        response = await _send_proxy_request_until_eof(
            server,
            b"GET http://Allowed.test/slow HTTP/1.1\r\n\r\n",
            timeout=0.5,
        )
    finally:
        await server.stop()
        upstream.close()
        await upstream.wait_closed()

    assert response.startswith(b"HTTP/1.1 502")


async def test_proxy_caps_oversized_upstream_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handle_upstream(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 4096\r\n"
            b"Connection: close\r\n"
            b"\r\n"
            + (b"x" * 4096)
        )
        await writer.drain()
        await asyncio.sleep(1.0)
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()

    upstream = await asyncio.start_server(handle_upstream, "127.0.0.1", 0)
    upstream_socket = next(iter(upstream.sockets or ()), None)
    assert upstream_socket is not None
    upstream_host, upstream_port = upstream_socket.getsockname()[:2]
    public_upstream_host = "93.184.216.34"
    _route_public_destination_to_upstream(
        monkeypatch,
        public_host=public_upstream_host,
        public_port=int(upstream_port),
        upstream_host=str(upstream_host),
        upstream_port=int(upstream_port),
    )

    server = SandboxProxyServer(
        _allow_decision,
        resolver=lambda host, port: (public_upstream_host, int(upstream_port)),
        max_response_bytes=96,
    )
    await server.start()
    try:
        response = await _send_proxy_request_until_eof(
            server,
            b"GET http://Allowed.test/large HTTP/1.1\r\n\r\n",
            timeout=0.5,
        )
    finally:
        await server.stop()
        upstream.close()
        await upstream.wait_closed()

    assert response.startswith(b"HTTP/1.1 200 OK")
    assert len(response) == 96


async def test_proxy_rejects_allowed_domain_resolving_to_loopback_before_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = SandboxProxyServer(_allow_decision)
    await server.start()

    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host: str, *args: object, **kwargs: object) -> list[tuple]:
        if host.lower() == "allowed.test":
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("127.0.0.1", 80),
                )
            ]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    try:
        response = await _send_proxy_request(
            server,
            b"GET http://Allowed.test/path HTTP/1.1\r\n\r\n",
        )
    finally:
        await server.stop()

    assert response.startswith(b"HTTP/1.1 403")


async def test_proxy_connects_to_validated_concrete_address_without_second_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_calls: list[tuple[str, int]] = []
    resolution_calls = 0
    real_open_connection = asyncio.open_connection

    def fake_getaddrinfo(host: str, port: int, *args: object, **kwargs: object) -> list[tuple]:
        nonlocal resolution_calls
        assert host == "allowed.test"
        resolution_calls += 1
        if resolution_calls == 1:
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("93.184.216.34", port),
                )
            ]
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", port),
            )
        ]

    async def fake_open_connection(host: str, port: int) -> tuple[object, object]:
        if host == server.host and port == server.port:
            return await real_open_connection(host, port)
        open_calls.append((host, port))
        raise OSError("stop after observing destination")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    server = SandboxProxyServer(_allow_decision)
    await server.start()
    try:
        response = await _send_proxy_request(
            server,
            b"GET http://Allowed.test/path HTTP/1.1\r\n\r\n",
        )
    finally:
        await server.stop()

    assert response.startswith(b"HTTP/1.1 502")
    assert resolution_calls == 1
    assert open_calls == [("93.184.216.34", 80)]


async def test_proxy_stop_is_idempotent() -> None:
    server = SandboxProxyServer(
        lambda host: NetworkDecision(
            status="ask",
            normalized_host=host,
            reason="unknown_domain",
            source=None,
        )
    )
    await server.start()

    assert server.host == "127.0.0.1"
    assert server.port > 0

    await server.stop()
    await server.stop()


async def test_proxy_default_endpoint_is_loopback_for_broker_only_egress() -> None:
    server = SandboxProxyServer(_allow_decision)
    await server.start()
    try:
        assert server.host == "127.0.0.1"
        assert server.port > 0
    finally:
        await server.stop()


@pytest.mark.parametrize(
    "target",
    [
        b"http://PyPI.org:99999/simple",
        b"http://PyPI.org:abc/simple",
        b"http://PyPI.org:443:evil/simple",
    ],
)
async def test_proxy_rejects_malformed_absolute_url_port_before_decision(
    target: bytes,
) -> None:
    seen_hosts: list[str] = []

    def decide(host: str) -> NetworkDecision:
        seen_hosts.append(host)
        return _allow_decision(host)

    server = SandboxProxyServer(decide)
    await server.start()
    try:
        response = await _send_proxy_request(
            server,
            b"GET " + target + b" HTTP/1.1\r\n\r\n",
        )
    finally:
        await server.stop()

    assert response.startswith(b"HTTP/1.1 403")
    assert seen_hosts == []


async def test_proxy_rejects_malformed_connect_target_before_decision() -> None:
    seen_hosts: list[str] = []

    def decide(host: str) -> NetworkDecision:
        seen_hosts.append(host)
        return _allow_decision(host)

    server = SandboxProxyServer(decide)
    await server.start()
    try:
        response = await _send_proxy_request(
            server,
            b"CONNECT http://PyPI.org:443/simple HTTP/1.1\r\n\r\n",
        )
    finally:
        await server.stop()

    assert response.startswith(b"HTTP/1.1 403")
    assert seen_hosts == []


async def test_proxy_rejects_duplicate_host_headers_before_decision() -> None:
    seen_hosts: list[str] = []

    def decide(host: str) -> NetworkDecision:
        seen_hosts.append(host)
        return _allow_decision(host)

    server = SandboxProxyServer(decide)
    await server.start()
    try:
        response = await _send_proxy_request(
            server,
            b"GET /simple HTTP/1.1\r\n"
            b"Host: PyPI.org\r\n"
            b"Host: example.com\r\n"
            b"\r\n",
        )
    finally:
        await server.stop()

    assert response.startswith(b"HTTP/1.1 403")
    assert seen_hosts == []


async def test_proxy_rejects_missing_or_empty_host_before_decision() -> None:
    seen_hosts: list[str] = []

    def decide(host: str) -> NetworkDecision:
        seen_hosts.append(host)
        return _allow_decision(host)

    server = SandboxProxyServer(decide)
    await server.start()
    try:
        missing_host = await _send_proxy_request(
            server,
            b"GET /simple HTTP/1.1\r\n\r\n",
        )
        empty_host = await _send_proxy_request(
            server,
            b"GET /simple HTTP/1.1\r\nHost: \r\n\r\n",
        )
    finally:
        await server.stop()

    assert missing_host.startswith(b"HTTP/1.1 403")
    assert empty_host.startswith(b"HTTP/1.1 403")
    assert seen_hosts == []


async def test_proxy_returns_403_when_decision_callback_raises() -> None:
    seen_hosts: list[str] = []

    def decide(host: str) -> NetworkDecision:
        seen_hosts.append(host)
        raise RuntimeError("decision failed")

    server = SandboxProxyServer(decide)
    await server.start()
    try:
        response = await _send_proxy_request(
            server,
            b"GET /simple HTTP/1.1\r\nHost: PyPI.org\r\n\r\n",
        )
    finally:
        await server.stop()

    assert response.startswith(b"HTTP/1.1 403")
    assert seen_hosts == ["pypi.org"]


async def test_proxy_stop_closes_idle_active_client() -> None:
    server = SandboxProxyServer(_allow_decision)
    await server.start()
    reader, writer = await asyncio.open_connection(server.host, server.port)
    try:
        writer.write(b"GET /simple HTTP/1.1\r\n")
        await writer.drain()
        await _wait_for_active_client(server)

        await server.stop()

        with suppress(ConnectionResetError):
            assert await asyncio.wait_for(reader.read(4096), timeout=1.0) == b""
    finally:
        writer.close()
        with suppress(ConnectionResetError):
            await writer.wait_closed()
