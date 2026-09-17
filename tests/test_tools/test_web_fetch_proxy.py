"""Offline HTTPX/httpcore wire tests; only the socket backend is replaced."""

from __future__ import annotations

import importlib
import socket
import ssl
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import getproxies_environment, proxy_bypass_environment

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from httpcore import ConnectError as CoreConnectError
from httpcore._backends.auto import AutoBackend
from httpcore._backends.mock import AsyncMockStream

from opensquilla.tools import ssrf
from opensquilla.tools.types import SSRFBlockedError

fetch = importlib.import_module("opensquilla.tools.builtin.web_fetch")
PUBLIC_IP = "93.184.216.34"
REMOTE_DNS = "OPENSQUILLA_WEB_FETCH_TRUST_PROXY_DNS"


def _handshake(client_context, server_context, hostname):
    """Exercise real certificate and hostname verification without sockets."""
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_context.wrap_bio(client_in, client_out, server_hostname=hostname)
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    completed = set()
    for _ in range(10):
        for name, peer, outgoing, incoming in (
            ("client", client, client_out, server_in),
            ("server", server, server_out, client_in),
        ):
            if name not in completed:
                try:
                    peer.do_handshake()
                    completed.add(name)
                except ssl.SSLWantReadError:
                    pass
            incoming.write(outgoing.read())
        if len(completed) == 2:
            return
    raise AssertionError("in-memory TLS handshake did not complete")


class WireNetwork:
    def __init__(self):
        self.connections = []
        self.requests = []
        self.tls_hosts = []
        self.dns_queries = []
        self.dns = {}
        self.redirects = {}
        self.server_context = None

    def resolve(self, host, port, *args, **kwargs):
        self.dns_queries.append(host)
        ip = self.dns.get(host, PUBLIC_IP)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 80))]

    async def connect(self, backend, host, port, **kwargs):
        self.connections.append((host, port))
        network = self

        class Stream(AsyncMockStream):
            def __init__(self):
                super().__init__([])
                self.pending = b""

            async def write(self, buffer, timeout=None):
                self.pending += buffer
                if b"\r\n\r\n" not in self.pending:
                    return
                header, self.pending = self.pending.split(b"\r\n\r\n", 1)
                first_line = header.split(b"\r\n", 1)[0].decode("ascii")
                network.requests.append(first_line)
                method, target, _ = first_line.split()
                if method == "CONNECT":
                    self._buffer.append(b"HTTP/1.1 200 Connection established\r\n\r\n")
                elif target in network.redirects:
                    location = network.redirects[target].encode("ascii")
                    self._buffer.append(
                        b"HTTP/1.1 302 Found\r\nLocation: " + location
                        + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                else:
                    self._buffer.append(
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                        b"Content-Length: 2\r\nConnection: close\r\n\r\nok"
                    )

            async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
                network.tls_hosts.append(server_hostname)
                if network.server_context is not None:
                    try:
                        _handshake(ssl_context, network.server_context, server_hostname)
                    except ssl.SSLError as exc:
                        # Match the real socket backend's error translation.
                        raise CoreConnectError(str(exc)) from exc
                return self

        return Stream()


@pytest.fixture
def wire(monkeypatch):
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy",
        "https_proxy", "all_proxy", "no_proxy", "REQUEST_METHOD",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "OPENSQUILLA_TRUST_ENV", REMOTE_DNS,
    ):
        monkeypatch.delenv(name, raising=False)
    # Isolate OS-level proxy settings on macOS/Windows, keeping real env parsing.
    monkeypatch.setattr(ssrf, "getproxies", getproxies_environment)
    monkeypatch.setattr(ssrf, "proxy_bypass", proxy_bypass_environment)
    network = WireNetwork()

    async def connect(backend, host, port, **kwargs):
        return await network.connect(backend, host, port, **kwargs)

    monkeypatch.setattr(AutoBackend, "connect_tcp", connect)
    monkeypatch.setattr(socket, "getaddrinfo", network.resolve)
    monkeypatch.setattr(fetch, "managed_network_httpx_kwargs", lambda: {
        "trust_env": fetch.os.environ.get("OPENSQUILLA_TRUST_ENV") == "1",
    })
    monkeypatch.setattr(fetch, "_RETRY_DELAY_SECONDS", 0)
    fetch._cache.clear()
    yield network
    fetch._cache.clear()


@pytest.mark.parametrize("scheme,proxy_var", [
    ("http", "HTTP_PROXY"), ("http", "http_proxy"),
    ("https", "HTTPS_PROXY"), ("https", "https_proxy"),
    ("https", "ALL_PROXY"),
])
@pytest.mark.parametrize("remote_dns", [False, True])
async def test_proxy_wire_destination_requires_separate_dns_opt_in(
    monkeypatch, wire, scheme, proxy_var, remote_dns,
):
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1")
    monkeypatch.setenv(proxy_var, "http://127.0.0.1:7890")
    if remote_dns:
        monkeypatch.setenv(REMOTE_DNS, "1")
    payload = await fetch.run_web_fetch_payload(f"{scheme}://public.example.test/page")
    assert payload["status"] == 200
    destination = "public.example.test" if remote_dns else PUBLIC_IP
    assert wire.connections == [("127.0.0.1", 7890)]
    if scheme == "https":
        assert wire.requests[0] == f"CONNECT {destination}:443 HTTP/1.1"
        if remote_dns:
            assert wire.tls_hosts == ["public.example.test"]
    else:
        assert wire.requests == [f"GET http://{destination}/page HTTP/1.1"]


@pytest.mark.parametrize("bypass", ["public.example.test", ".example.test", "*"])
async def test_no_proxy_keeps_direct_pin_even_with_remote_dns(monkeypatch, wire, bypass):
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1")
    monkeypatch.setenv(REMOTE_DNS, "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("NO_PROXY", bypass)
    payload = await fetch.run_web_fetch_payload("https://public.example.test/page")
    assert payload["status"] == 200
    assert wire.connections == [(PUBLIC_IP, 443)]
    assert wire.requests == ["GET /page HTTP/1.1"]
    assert wire.tls_hosts == ["public.example.test"]


async def test_no_proxy_literal_cannot_rediscover_environment_proxy(monkeypatch, wire):
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1")
    monkeypatch.setenv(REMOTE_DNS, "1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("NO_PROXY", PUBLIC_IP)
    payload = await fetch.run_web_fetch_payload(f"http://{PUBLIC_IP}/page")
    assert payload["status"] == 200
    assert wire.connections == [(PUBLIC_IP, 80)]
    assert wire.requests == ["GET /page HTTP/1.1"]


@pytest.mark.parametrize("proxy_present,trust_env", [(True, False), (False, True)])
async def test_dns_opt_in_alone_cannot_unpin_direct_requests(
    monkeypatch, wire, proxy_present, trust_env,
):
    monkeypatch.setenv(REMOTE_DNS, "1")
    if proxy_present:
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    if trust_env:
        monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1")
    payload = await fetch.run_web_fetch_payload("http://public.example.test/page")
    assert payload["status"] == 200
    assert wire.connections == [(PUBLIC_IP, 80)]


async def test_redirect_rechecks_proxy_bypass_and_pins_direct_hop(monkeypatch, wire):
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1")
    monkeypatch.setenv(REMOTE_DNS, "1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("NO_PROXY", "direct.example.test")
    wire.redirects["http://public.example.test/start"] = "http://direct.example.test/final"
    payload = await fetch.run_web_fetch_payload("http://public.example.test/start")
    assert payload["final_url"] == "http://direct.example.test/final"
    assert wire.connections == [("127.0.0.1", 7890), (PUBLIC_IP, 80)]
    assert wire.requests == [
        "GET http://public.example.test/start HTTP/1.1", "GET /final HTTP/1.1",
    ]
    assert "direct.example.test" in wire.dns_queries


@pytest.mark.parametrize("redirect", [False, True])
async def test_remote_dns_still_blocks_locally_private_targets(monkeypatch, wire, redirect):
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1")
    monkeypatch.setenv(REMOTE_DNS, "1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    wire.dns["private.example.test"] = "10.0.0.8"
    private = "http://private.example.test/page"
    if redirect:
        wire.redirects["http://public.example.test/start"] = private
    with pytest.raises(SSRFBlockedError):
        await fetch.run_web_fetch_payload(
            "http://public.example.test/start" if redirect else private
        )
    assert len(wire.connections) == int(redirect)


async def test_managed_proxy_overrides_environment_proxy_and_dns_opt_in(monkeypatch, wire):
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1")
    monkeypatch.setenv(REMOTE_DNS, "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setattr(fetch, "managed_network_httpx_kwargs", lambda: {
        "proxy": "http://127.0.0.1:7891", "trust_env": False,
    })
    payload = await fetch.run_web_fetch_payload("https://public.example.test/page")
    assert payload["status"] == 200
    assert wire.connections == [("127.0.0.1", 7891)]
    assert wire.requests[0] == "CONNECT public.example.test:443 HTTP/1.1"


@pytest.fixture
def tls_certificate(tmp_path: Path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "public.example.test")])
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(1)
        .not_valid_before(datetime(2020, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2100, 1, 1, tzinfo=UTC))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("public.example.test"),
        ]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "synthetic-ca.pem"
    key_path = tmp_path / "synthetic-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return cert_path, context


@pytest.mark.parametrize("custom_ca", [False, True])
async def test_remote_proxy_tls_verifies_hostname_and_environment_ca(
    monkeypatch, wire, tls_certificate, custom_ca,
):
    cert_path, wire.server_context = tls_certificate
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1")
    monkeypatch.setenv(REMOTE_DNS, "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    if custom_ca:
        monkeypatch.setenv("SSL_CERT_FILE", str(cert_path))
    payload = await fetch.run_web_fetch_payload("https://public.example.test/page")
    assert wire.tls_hosts and set(wire.tls_hosts) == {"public.example.test"}
    if custom_ca:
        assert payload["status"] == 200
    else:
        assert payload["status"] == 0
        assert "CERTIFICATE_VERIFY_FAILED" in payload["error"]


@pytest.mark.parametrize("remote_dns", [False, True])
async def test_proxy_ssl_cert_dir_reaches_tls_context(monkeypatch, wire, tmp_path, remote_dns):
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    if remote_dns:
        monkeypatch.setenv(REMOTE_DNS, "1")
    original = ssl.create_default_context
    directories = []

    def create_context(*args, **kwargs):
        directories.append(kwargs.get("capath"))
        return original(*args, **kwargs)

    monkeypatch.setattr(ssl, "create_default_context", create_context)
    payload = await fetch.run_web_fetch_payload("https://public.example.test/page")
    assert payload["status"] == 200
    assert directories and all(directory == str(tmp_path) for directory in directories)


async def test_direct_tls_with_trust_env_off_ignores_custom_ca(monkeypatch, wire, tls_certificate):
    cert_path, wire.server_context = tls_certificate
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_path))
    kwargs = fetch._web_fetch_httpx_client_kwargs(
        "https://public.example.test/page", [PUBLIC_IP], {}, {"trust_env": False},
    )
    async with httpx.AsyncClient(**kwargs) as client:
        with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
            await client.get("https://public.example.test/page")


async def test_remote_proxy_tls_rejects_wrong_hostname(monkeypatch, wire, tls_certificate):
    cert_path, wire.server_context = tls_certificate
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1")
    monkeypatch.setenv(REMOTE_DNS, "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_path))
    payload = await fetch.run_web_fetch_payload("https://other.example.test/page")
    assert payload["status"] == 0
    assert "CERTIFICATE_VERIFY_FAILED" in payload["error"]
    assert "Hostname mismatch" in payload["error"]


@pytest.mark.parametrize("value", ["0", "false", "invalid"])
async def test_false_or_unknown_dns_trust_value_keeps_proxy_pinned(monkeypatch, wire, value):
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1")
    monkeypatch.setenv(REMOTE_DNS, value)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    payload = await fetch.run_web_fetch_payload("http://public.example.test/page")
    assert payload["status"] == 200
    assert wire.connections == [("127.0.0.1", 7890)]
    assert wire.requests == [f"GET http://{PUBLIC_IP}/page HTTP/1.1"]
