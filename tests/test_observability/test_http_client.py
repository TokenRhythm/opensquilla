from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import ssl
import threading
import time
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.request import getproxies_environment

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from opensquilla.observability import http_client, install_telemetry, update_check, usage_telemetry
from opensquilla.session.storage import SessionStorage

_MANIFEST = {
    "schemaVersion": 1,
    "tag": "v0.5.7",
    "version": "0.5.7",
    "baseVersion": "0.5.7",
    "prerelease": False,
    "releaseUrl": "https://github.com/TokenRhythm/opensquilla/releases/tag/v0.5.7",
}
# Certificate-policy checks exercise real native TLS, including cold platform
# initialization; their fixture budget is separate from deadline assertions.
_TLS_CHECK_TIMEOUT_SECONDS = 10


@pytest.fixture(autouse=True)
def isolated_network(monkeypatch):
    import os

    for name in list(os.environ):
        if name.lower().endswith("_proxy") or name in {
            "OPENSQUILLA_TRUST_ENV",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "REQUEST_METHOD",
            "GITHUB_ACTIONS",
            "PYTEST_CURRENT_TEST",
            "OPENSQUILLA_TESTING",
            "CI",
            "DO_NOT_TRACK",
            "OPENSQUILLA_TELEMETRY_DISABLED",
            "OPENSQUILLA_UPDATE_CHECK_DISABLED",
            "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY",
            "OPENSQUILLA_PRIVACY_DISABLE_PRODUCT_ANALYTICS",
            "OPENSQUILLA_PRIVACY_DISABLE_RELIABILITY_DIAGNOSTICS",
        }:
            monkeypatch.delenv(name, raising=False)
    # Keep real environment parsing, excluding OS registry/system proxy state.
    monkeypatch.setattr(http_client, "getproxies", getproxies_environment)
    monkeypatch.setattr(update_check, "_CACHED_INFO", {})
    monkeypatch.setattr(update_check, "_REFRESH_LOCKS", {})
    monkeypatch.setattr(update_check, "_BACKGROUND_THREADS", {})
    original_getaddrinfo = socket.getaddrinfo

    def loopback_only(host, *args, **kwargs):
        if isinstance(host, bytes):
            host = host.decode()
        if host not in {"localhost", "127.0.0.1", "::1", None}:
            raise AssertionError("These tests must never resolve a non-loopback host")
        return original_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", loopback_only)


@pytest.fixture
def servers():
    running: list[tuple[ThreadingHTTPServer, threading.Thread]] = []

    def start(*, context: ssl.SSLContext | None = None, handshake_timeout: float = 2):
        state = SimpleNamespace(hits=[], redirect_to=None, post_status=202)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                state.hits.append(("GET", self.path))
                if self.path.endswith("/redirect"):
                    self.send_response(302)
                    self.send_header("Location", state.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = json.dumps(_MANIFEST).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                state.hits.append(("POST", self.path, payload))
                self.send_response(state.post_status)
                self.send_header("Content-Length", "0")
                self.end_headers()

        class FixtureServer(ThreadingHTTPServer):
            def __init__(self):
                self.ready = threading.Event()
                self.accepted = threading.Event()
                self.handshake_started = threading.Event()
                self.connections: set[socket.socket] = set()
                self.handshakes: set[socket.socket] = set()
                self.workers: list[threading.Thread] = []
                self.connection_lock = threading.Lock()
                super().__init__(("127.0.0.1", 0), Handler)

            def service_actions(self):
                self.ready.set()

            def get_request(self):
                connection, address = super().get_request()
                if context is not None:
                    connection = context.wrap_socket(
                        connection, server_side=True, do_handshake_on_connect=False
                    )
                with self.connection_lock:
                    self.connections.add(connection)
                self.accepted.set()
                return connection, address

            def process_request(self, request, client_address):
                worker = threading.Thread(
                    target=self.process_request_thread,
                    args=(request, client_address),
                    daemon=True,
                )
                with self.connection_lock:
                    self.workers.append(worker)
                worker.start()

            def process_request_thread(self, request, client_address):
                try:
                    if context is not None:
                        with self.connection_lock:
                            self.handshakes.add(request)
                            self.handshake_started.set()
                        try:
                            request.settimeout(handshake_timeout)
                            request.do_handshake()
                        except OSError:
                            self.shutdown_request(request)
                            return
                        finally:
                            with self.connection_lock:
                                self.handshakes.discard(request)
                    super().process_request_thread(request, client_address)
                finally:
                    with self.connection_lock:
                        self.connections.discard(request)

        server = FixtureServer()
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        running.append((server, thread))
        assert server.ready.wait(timeout=handshake_timeout), (
            "Loopback server accept loop did not become ready"
        )
        scheme = "https" if context else "http"
        state.url = f"{scheme}://127.0.0.1:{server.server_port}"
        state.accepted = server.accepted
        state.handshake_started = server.handshake_started

        def active_handshakes():
            with server.connection_lock:
                return frozenset(server.handshakes)

        def tls_progress():
            with server.connection_lock:
                return {
                    "accepted": server.accepted.is_set(),
                    "handshake_started": server.handshake_started.is_set(),
                    "active_handshakes": len(server.handshakes),
                    "connections": len(server.connections),
                    "live_workers": sum(worker.is_alive() for worker in server.workers),
                    "handler_hits": len(state.hits),
                }

        state.active_handshakes = active_handshakes
        state.tls_progress = tls_progress
        return state

    yield start
    for server, thread in running:
        server.shutdown()
        with server.connection_lock:
            connections = tuple(server.connections)
            workers = tuple(server.workers)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()
        deadline = time.monotonic() + 2
        for worker in workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))
            assert not worker.is_alive()


def _config(tmp_path):
    return SimpleNamespace(
        state_dir=str(tmp_path / "state"),
        privacy=SimpleNamespace(disable_network_observability=False),
    )


@pytest.mark.parametrize("opt_in", [None, "0", "1", "true"])
@pytest.mark.parametrize(
    "proxy_key,no_proxy_key", [("HTTP_PROXY", "NO_PROXY"), ("http_proxy", "no_proxy")]
)
async def test_public_update_and_usage_with_bracketed_ipv6(
    tmp_path,
    monkeypatch,
    servers,
    opt_in,
    proxy_key,
    no_proxy_key,
):
    server = servers()
    if opt_in is not None:
        monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", opt_in)
    monkeypatch.setenv(proxy_key, "http://127.0.0.1:1")
    monkeypatch.setenv(no_proxy_key, "localhost,127.0.0.1,::1,[::1]")
    monkeypatch.setenv(update_check.UPDATE_CHECK_ENDPOINT_ENV, f"{server.url}/stable.json")
    monkeypatch.setenv(usage_telemetry.USAGE_TELEMETRY_ENDPOINT_ENV, f"{server.url}/usage")
    monkeypatch.setattr(
        install_telemetry,
        "ensure_install_telemetry_id",
        lambda **_kwargs: "synthetic-loopback-install",
    )
    config = _config(tmp_path)
    # pytest installs the call-phase marker after fixture setup.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    info = update_check.refresh_update_check(config=config, version="0.5.6", force=True)
    assert info.error is None and info.update_available
    assert info.latest_version == "0.5.7"
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.record_daily_usage(
            day="2026-10-10",
            input_tokens=100,
            output_tokens=20,
            cached_tokens=0,
            cache_write_tokens=0,
            updated_at=1,
        )
        server.post_status = 503
        assert (
            await usage_telemetry.upload_pending_daily_usage(
                storage,
                config=config,
                today=date(2026, 10, 12),
            )
            == 0
        )
        assert len(await storage.list_pending_daily_usage(before_day="2026-10-12")) == 1
        server.post_status = 202
        assert (
            await usage_telemetry.upload_pending_daily_usage(
                storage,
                config=config,
                today=date(2026, 10, 12),
            )
            == 1
        )
        assert await storage.list_pending_daily_usage(before_day="2026-10-12") == []
    finally:
        await storage.close()
    assert [hit[0] for hit in server.hits] == ["GET", "POST", "POST"]


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_opt_in_uses_valid_proxy_and_invalid_bypass_does_not_disable_it(
    monkeypatch,
    servers,
    caplog,
    asynchronous,
):
    proxy = servers()
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1")
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    monkeypatch.setenv("NO_PROXY", "[::1],[::1]:8080,[::1,private.example:bad,例子.测试")
    url = "http://proxy-only.invalid/stable.json"
    if asynchronous:
        async with httpx.AsyncClient(
            timeout=2,
            **http_client.observability_httpx_kwargs(asynchronous=True),
        ) as client:
            assert (await client.get(url)).status_code == 200
    else:
        with httpx.Client(timeout=2, **http_client.observability_httpx_kwargs()) as client:
            assert client.get(url).status_code == 200
    assert proxy.hits == [("GET", url)]
    assert "ignored_entries=3" in caplog.text
    assert "private.example" not in caplog.text
    assert "例子" not in caplog.text


def test_default_does_not_discover_proxy_and_invalid_proxy_does_not_go_direct(
    monkeypatch,
    servers,
):
    server = servers()

    def forbidden_discovery():
        raise AssertionError("Disabled environment must not be consulted")

    monkeypatch.setattr(http_client, "getproxies", forbidden_discovery)
    with httpx.Client(timeout=2, **http_client.observability_httpx_kwargs()) as client:
        assert client.get(server.url).status_code == 200
    server.hits.clear()
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1")
    monkeypatch.setattr(http_client, "getproxies", lambda: {"http": "http://proxy:bad"})
    with pytest.raises(httpx.InvalidURL):
        http_client.observability_httpx_kwargs()
    assert server.hits == []


def test_unreachable_proxy_does_not_retry_direct(monkeypatch, servers):
    direct = servers()
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1")
    monkeypatch.setenv("NO_PROXY", "[::1]")
    with socket.socket() as unreachable_proxy:
        unreachable_proxy.bind(("127.0.0.1", 0))
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{unreachable_proxy.getsockname()[1]}")
        result = update_check._fetch_latest_release(direct.url, "0.5.6", timeout=2)
    assert result[2] is not None
    assert direct.hits == []


@pytest.mark.parametrize("direction", ["proxy_to_direct", "direct_to_proxy"])
def test_redirect_rechecks_bypass_for_each_destination(monkeypatch, servers, direction):
    direct = servers()
    proxy = servers()
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1")
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,[::1]")
    if direction == "proxy_to_direct":
        proxy.redirect_to = f"{direct.url}/stable.json"
        endpoint = "http://proxy-only.invalid/redirect"
    else:
        direct.redirect_to = "http://proxy-only.invalid/stable.json"
        endpoint = f"{direct.url}/redirect"
    result = update_check._fetch_latest_release(endpoint, "0.5.6", timeout=2)
    assert result[0] == "0.5.7" and result[2] is None
    assert len(direct.hits) == len(proxy.hits) == 1


@pytest.mark.parametrize(
    "entry,matching,nonmatching",
    [
        ("[::1]", "http://[::1]:8080", "http://[::2]:8080"),
        ("[::1]:8080", "http://[::1]:8080", "http://[::1]:8081"),
        ("127.0.0.1/8", "http://127.0.0.1:8080", "http://127.0.0.2:8080"),
        ("127.0.0.1:8080", "http://sub.127.0.0.1:8080", "http://127.0.0.1:8081"),
        ("localhost:8080", "http://sub.localhost:8080", "http://localhost:8081"),
        ("::1:8080", "http://[::1:8080]", "http://[::1]:8080"),
        ("example.test:8080", "http://sub.example.test:8080", "http://example.test:8081"),
        (".example.test", "http://sub.example.test", "http://example.test"),
        ("http://example.test:8080", "http://example.test:8080", "https://example.test:8080"),
    ],
)
def test_bypass_host_port_and_domain_semantics(monkeypatch, entry, matching, nonmatching):
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1")
    monkeypatch.setattr(
        http_client,
        "getproxies",
        lambda: {
            "all": "http://127.0.0.1:1",
            "no": entry,
        },
    )
    with httpx.Client(**http_client.observability_httpx_kwargs()) as client:
        # Inspect route identity, avoiding any DNS/socket attempt for example
        # domains or machines without IPv6. Real wire routing is tested above.
        assert client._transport_for_url(httpx.URL(matching)) is client._transport
        assert client._transport_for_url(httpx.URL(nonmatching)) is not client._transport


def test_wildcard_bypass_does_not_construct_unused_proxy(monkeypatch, servers):
    server = servers()
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1")
    monkeypatch.setattr(
        http_client,
        "getproxies",
        lambda: {
            "http": "http://unused:bad",
            "no": "[::1],*",
        },
    )
    with httpx.Client(**http_client.observability_httpx_kwargs()) as client:
        assert client.get(server.url).status_code == 200


@pytest.fixture
def tls_context(tmp_path: Path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-loopback")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime(2020, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2100, 1, 1, tzinfo=UTC))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "synthetic-ca.pem"
    key_path = tmp_path / "synthetic-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return cert_path, context


@pytest.mark.ci_serial
def test_custom_ca_and_hostname_validation_are_preserved(monkeypatch, servers, tls_context):
    cert_path, context = tls_context
    server = servers(context=context, handshake_timeout=_TLS_CHECK_TIMEOUT_SECONDS)
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_path))
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,[::1]")
    valid_error = update_check._fetch_latest_release(
        server.url, "0.5.6", timeout=_TLS_CHECK_TIMEOUT_SECONDS
    )[2]
    assert valid_error is None, (valid_error, server.tls_progress())
    wrong_host = server.url.replace("127.0.0.1", "localhost")
    error = update_check._fetch_latest_release(
        wrong_host, "0.5.6", timeout=_TLS_CHECK_TIMEOUT_SECONDS
    )[2]
    assert error is not None and "CERTIFICATE_VERIFY_FAILED" in error
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "0")
    error = update_check._fetch_latest_release(
        server.url, "0.5.6", timeout=_TLS_CHECK_TIMEOUT_SECONDS
    )[2]
    assert error is not None and "CERTIFICATE_VERIFY_FAILED" in error
    assert len(server.hits) == 1


@pytest.mark.ci_serial
def test_idle_tls_connection_does_not_block_certificate_validation(
    monkeypatch, servers, tls_context, capsys
):
    cert_path, context = tls_context
    server = servers(context=context, handshake_timeout=_TLS_CHECK_TIMEOUT_SECONDS)
    port = int(server.url.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=_TLS_CHECK_TIMEOUT_SECONDS):
        assert server.accepted.wait(timeout=_TLS_CHECK_TIMEOUT_SECONDS)
        assert server.handshake_started.wait(timeout=_TLS_CHECK_TIMEOUT_SECONDS)
        idle_handshakes = server.active_handshakes()
        assert len(idle_handshakes) == 1
        monkeypatch.setenv("SSL_CERT_FILE", str(cert_path))
        monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1")
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,[::1]")
        valid_error = update_check._fetch_latest_release(
            server.url, "0.5.6", timeout=_TLS_CHECK_TIMEOUT_SECONDS
        )[2]
        assert valid_error is None, (valid_error, server.tls_progress())
        assert idle_handshakes <= server.active_handshakes(), server.tls_progress()
        wrong_host = server.url.replace("127.0.0.1", "localhost")
        error = update_check._fetch_latest_release(
            wrong_host, "0.5.6", timeout=_TLS_CHECK_TIMEOUT_SECONDS
        )[2]
        assert error is not None and "CERTIFICATE_VERIFY_FAILED" in error
        monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "0")
        error = update_check._fetch_latest_release(
            server.url, "0.5.6", timeout=_TLS_CHECK_TIMEOUT_SECONDS
        )[2]
        assert error is not None and "CERTIFICATE_VERIFY_FAILED" in error
        assert len(server.hits) == 1
    assert "Exception occurred during processing of request" not in capsys.readouterr().err


def test_install_upload_uses_the_same_safe_network_policy(monkeypatch, servers):
    server = servers()
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,[::1]")
    assert install_telemetry._post_payload(server.url, {"event": "synthetic"}, timeout=2) == (
        True,
        None,
    )
    assert server.hits[0][0] == "POST"


async def test_cancelled_usage_constructor_runs_off_loop_and_closes_late_client(monkeypatch):
    loop_thread = threading.get_ident()
    started = threading.Event()
    release = threading.Event()
    closed = asyncio.Event()

    class LateClient:
        async def aclose(self):
            closed.set()

    def blocking_constructor(_timeout):
        assert threading.get_ident() != loop_thread
        started.set()
        assert release.wait(timeout=5)
        return LateClient()

    monkeypatch.setattr(http_client, "_build_async_client", blocking_constructor)
    task = asyncio.create_task(
        usage_telemetry._post_payload(
            "http://127.0.0.1:1",
            {"event_id": "synthetic-cancelled"},
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=3)
            except asyncio.CancelledError:
                pass
    await asyncio.wait_for(closed.wait(), timeout=2)
    assert closed.is_set()
