"""Cloudflare quick-tunnel parsing, retries, and origin lifecycle tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from opensquilla.gateway.origin_guard import dynamic_origins_snapshot, revoke_dynamic_origin
from opensquilla.gateway.tunnel import TunnelManager, TunnelUnavailableError


@pytest.fixture(autouse=True)
def _clean_origins() -> Iterator[None]:
    for origin in dynamic_origins_snapshot():
        revoke_dynamic_origin(origin)
    yield
    for origin in dynamic_origins_snapshot():
        revoke_dynamic_origin(origin)


class _FakePopen:
    """Minimal Popen double that yields a quick-tunnel URL on stdout."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self._index = 0
        self.stdout = self
        self.terminated = False
        self.killed = False

    def readline(self) -> str:
        if self._index < len(self._lines):
            line = self._lines[self._index]
            self._index += 1
            return line
        return ""

    def __iter__(self):
        return iter(())

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float = 3) -> int:
        return 0


def test_quick_tunnel_parses_domain_and_registers_origin(monkeypatch) -> None:
    from opensquilla.gateway import tunnel as tunnel_mod

    fake = _FakePopen([
        "INF 2024-01-01T00:00:00Z hello",
        "INF +--------------------------------------------------------------------+",
        "INF |  https://random-words-123.trycloudflare.com  |",
        "INF Registered tunnel connection connIndex=0",
    ])
    monkeypatch.setattr(
        tunnel_mod.TunnelManager, "_find_cloudflared", lambda self: "C:/fake/cloudflared.exe"
    )
    manager = TunnelManager(
        port=18791,
        cloudflared_path="C:/fake/cloudflared.exe",
        poll_timeout_seconds=5,
        popen_factory=lambda *a, **k: fake,
    )

    info = manager.ensure_tunnel()

    assert info.mode == "cloudflare"
    assert info.base_url == "https://random-words-123.trycloudflare.com/control"
    assert info.origin == "https://random-words-123.trycloudflare.com"
    assert info.origin in dynamic_origins_snapshot()
    assert fake.terminated is False

    manager.close()
    assert info.origin not in dynamic_origins_snapshot()
    assert fake.terminated is True


def test_missing_cloudflared_is_downloaded_with_pinned_digest(monkeypatch) -> None:
    """No cloudflared on PATH: the pinned binary is fetched and used."""

    from opensquilla.gateway import tunnel as tunnel_mod

    fake = _FakePopen([
        "INF |  https://downloaded-abc.trycloudflare.com  |",
        "INF Registered tunnel connection connIndex=0",
    ])
    downloads: list[dict] = []

    def _fake_downloader(dest_dir, *, version, expected_sha256, platform_suffix):
        downloads.append(
            {
                "version": version,
                "sha256": expected_sha256,
                "suffix": platform_suffix,
            }
        )
        return Path(dest_dir) / f"cloudflared-{version}-{platform_suffix}"

    monkeypatch.setattr(
        tunnel_mod.TunnelManager, "_find_cloudflared", lambda self: None
    )
    monkeypatch.setattr(
        tunnel_mod, "_platform_asset_suffix", lambda: "windows-amd64.exe"
    )
    manager = TunnelManager(
        port=18791,
        poll_timeout_seconds=5,
        download_dir="D:/fake/bin",
        popen_factory=lambda *a, **k: fake,
        downloader=_fake_downloader,
    )

    info = manager.ensure_tunnel()

    assert info.mode == "cloudflare"
    assert info.base_url == "https://downloaded-abc.trycloudflare.com/control"
    assert len(downloads) == 1
    assert downloads[0]["version"] == tunnel_mod.CLOUDFLARED_VERSION
    assert downloads[0]["suffix"] == "windows-amd64.exe"
    # The digest must be the pinned one, not an empty pass-through.
    assert downloads[0]["sha256"] == tunnel_mod.CLOUDFLARED_SHA256["windows-amd64.exe"]
    assert downloads[0]["sha256"] != "PENDING"
    manager.close()


def test_dead_quick_tunnel_is_replaced_on_next_ensure(monkeypatch) -> None:
    from opensquilla.gateway import tunnel as tunnel_mod

    class _DyingPopen(_FakePopen):
        def poll(self) -> int | None:
            # Alive while its scripted lines are being consumed, dead after.
            return 1 if self._index >= len(self._lines) else None

    first = _DyingPopen([
        "INF |  https://dying-abc.trycloudflare.com  |",
        "INF Registered tunnel connection connIndex=0",
    ])
    second = _DyingPopen([
        "INF |  https://fresh-abc.trycloudflare.com  |",
        "INF Registered tunnel connection connIndex=0",
    ])
    spawned = [first, second]
    monkeypatch.setattr(
        tunnel_mod.TunnelManager,
        "_find_cloudflared",
        lambda self: "C:/fake/cloudflared.exe",
    )
    manager = TunnelManager(
        port=18791,
        cloudflared_path="C:/fake/cloudflared.exe",
        poll_timeout_seconds=5,
        popen_factory=lambda *a, **k: spawned.pop(0),
    )

    stale = manager.ensure_tunnel()
    assert stale.base_url == "https://dying-abc.trycloudflare.com/control"
    # The process has exited; the next ensure call must not keep serving the
    # dead public hostname.
    fresh = manager.ensure_tunnel()
    assert fresh.base_url == "https://fresh-abc.trycloudflare.com/control"
    manager.close()
    for origin in (
        "https://dying-abc.trycloudflare.com",
        "https://fresh-abc.trycloudflare.com",
    ):
        assert origin not in dynamic_origins_snapshot()


def test_download_cloudflared_verifies_sha256(tmp_path: Path, monkeypatch) -> None:
    import hashlib
    import io
    import urllib.request

    from opensquilla.gateway import tunnel as tunnel_mod

    payload = b"fake-cloudflared-binary"
    digest = hashlib.sha256(payload).hexdigest()

    class _FakeResponse:
        def __init__(self, data: bytes) -> None:
            self._buffer = io.BytesIO(data)

        def read(self, n: int = -1) -> bytes:
            return self._buffer.read(n)

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *args) -> None:
            return None

    def _fake_urlopen(url: str, timeout: float | None = None) -> _FakeResponse:
        assert "releases/download/9.9.9/" in url
        return _FakeResponse(payload)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    target = tunnel_mod.download_cloudflared(
        tmp_path,
        version="9.9.9",
        expected_sha256=digest,
    )

    assert target.exists()
    assert target.read_bytes() == payload

    with pytest.raises(RuntimeError):
        tunnel_mod.download_cloudflared(
            tmp_path / "other",
            version="9.9.9",
            expected_sha256="0" * 64,
        )


def test_download_cloudflared_falls_back_to_mirror_sources(
    tmp_path: Path, monkeypatch
) -> None:
    """A blocked official source must not doom the download: mirrors follow."""

    import hashlib
    import io
    import urllib.error
    import urllib.request

    from opensquilla.gateway import tunnel as tunnel_mod

    payload = b"fake-cloudflared-binary"
    digest = hashlib.sha256(payload).hexdigest()
    requested_urls: list[str] = []

    class _FakeResponse:
        def __init__(self, data: bytes) -> None:
            self._buffer = io.BytesIO(data)

        def read(self, n: int = -1) -> bytes:
            return self._buffer.read(n)

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *args) -> None:
            return None

    def _fake_urlopen(url: str, timeout: float | None = None) -> _FakeResponse:
        requested_urls.append(url)
        if "github.com/cloudflare" in url and not url.startswith("https://ghfast"):
            # Official source behaves as if blocked: connect timeout.
            raise urllib.error.URLError("timed out")
        return _FakeResponse(payload)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    target = tunnel_mod.download_cloudflared(
        tmp_path,
        version="9.9.9",
        expected_sha256=digest,
        sources=(
            "https://github.com/cloudflare/cloudflared/releases/download",
            "https://ghfast.top/https://github.com/cloudflare/cloudflared/releases/download",
        ),
    )

    assert target.read_bytes() == payload
    assert len(requested_urls) == 2
    assert requested_urls[1].startswith("https://ghfast.top/")


def test_download_sources_env_override(monkeypatch) -> None:
    from opensquilla.gateway import tunnel as tunnel_mod

    monkeypatch.setenv(
        "OPENSQUILLA_CLOUDFLARED_MIRRORS",
        "https://mirror-a.example/dl/, https://mirror-b.example/dl",
    )

    assert tunnel_mod._configured_download_sources() == (
        "https://mirror-a.example/dl",
        "https://mirror-b.example/dl",
    )


def test_ensure_tunnel_reuses_active_tunnel(monkeypatch) -> None:
    from opensquilla.gateway import tunnel as tunnel_mod

    fake = _FakePopen([
        "INF |  https://stable-abc.trycloudflare.com  |",
        "INF Registered tunnel connection connIndex=0",
    ])
    monkeypatch.setattr(
        tunnel_mod.TunnelManager, "_find_cloudflared", lambda self: "C:/fake/cloudflared.exe"
    )
    manager = TunnelManager(
        port=18791,
        cloudflared_path="C:/fake/cloudflared.exe",
        poll_timeout_seconds=5,
        popen_factory=lambda *a, **k: fake,
    )

    first = manager.ensure_tunnel()
    second = manager.ensure_tunnel()

    assert first.base_url == second.base_url
    assert manager.active is True
    manager.close()
    assert manager.active is False


def test_url_without_edge_registration_is_not_a_usable_tunnel(monkeypatch) -> None:
    """Regression for Cloudflare error 1033.

    cloudflared publishes the quick-tunnel hostname *before* it dials the
    edge. A hijacked TCP :443 (proxy TUN fake-ip) fails the handshake, and the
    published hostname then answers every phone request with error 1033.
    A hostname without a registered connection must never be handed out.
    """

    from opensquilla.gateway import tunnel as tunnel_mod

    fake = _FakePopen([
        "INF |  https://published-but-dead.trycloudflare.com  |",
        'ERR Unable to establish connection with Cloudflare edge '
        'error="TLS handshake with edge error: EOF" ip=198.51.100.38',
        "INF Initiating shutdown",
    ])
    monkeypatch.setattr(
        tunnel_mod.TunnelManager, "_find_cloudflared", lambda self: "C:/fake/cloudflared.exe"
    )
    manager = TunnelManager(
        port=18791,
        cloudflared_path="C:/fake/cloudflared.exe",
        poll_timeout_seconds=0.5,
        popen_factory=lambda *a, **k: fake,
        auto_download=False,
    )

    with pytest.raises(TunnelUnavailableError):
        manager.ensure_tunnel()
    assert dynamic_origins_snapshot() == ()


def test_quic_is_attempted_before_http2(monkeypatch) -> None:
    """Proxy TUN adapters hijack TCP :443 but pass UDP, so quic leads."""

    from opensquilla.gateway import tunnel as tunnel_mod

    def _dead() -> _FakePopen:
        return _FakePopen(["INF no url", "INF Initiating shutdown"])

    http2 = _FakePopen([
        "INF |  https://via-http2.trycloudflare.com  |",
        "INF Registered tunnel connection connIndex=0",
    ])
    attempts: list[list[str]] = []

    def _factory(*args: object, **kwargs: object) -> _FakePopen:
        argv = [str(a) for a in (args[0] if args else [])]
        attempts.append(argv)
        # Both quic attempts fail; only the http2 attempt registers.
        return http2 if len(attempts) == 3 else _dead()

    monkeypatch.setattr(
        tunnel_mod.TunnelManager, "_find_cloudflared", lambda self: "C:/fake/cloudflared.exe"
    )
    manager = TunnelManager(
        port=18791,
        cloudflared_path="C:/fake/cloudflared.exe",
        poll_timeout_seconds=0.5,
        popen_factory=_factory,
        auto_download=False,
    )

    info = manager.ensure_tunnel()

    assert info.base_url == "https://via-http2.trycloudflare.com/control"
    # quic leads and is retried once: behind a proxy TUN the edge handshake is
    # intermittent, and a single "context deadline exceeded" must not concede
    # to the transport that a hijacked TCP :443 fails outright.
    assert [a[a.index("--protocol") + 1] for a in attempts] == ["quic", "quic", "http2"]
    # No fallback URL is issued, and no --edge-ip-version pinning remains.
    assert "--edge-ip-version" not in attempts[0]
    manager.close()


def test_missing_cloudflared_fails_without_fallback(monkeypatch) -> None:
    """Remote control is tunnel-only; a missing binary must fail loudly."""

    from opensquilla.gateway import tunnel as tunnel_mod

    monkeypatch.setattr(tunnel_mod.TunnelManager, "_find_cloudflared", lambda self: None)
    manager = TunnelManager(port=18791, auto_download=False)

    with pytest.raises(TunnelUnavailableError):
        manager.ensure_tunnel()
    assert dynamic_origins_snapshot() == ()


def test_cloudflared_output_is_decoded_as_utf8(monkeypatch) -> None:
    """Regression: gbk codec can't decode byte 0x91.

    cloudflared logs UTF-8 and prints localized adapter names, e.g.
    "ICMP proxy will use fe80::1 in zone 以太网". The packaged desktop app runs
    under cp936, where text=True without an explicit encoding decoded that
    line with gbk and aborted tunnel startup on byte 0x91 (the tail of the
    UTF-8 sequence for 网). The spawn must pin UTF-8 so a log line can never
    decide whether remote control works.
    """

    from opensquilla.gateway import tunnel as tunnel_mod

    captured: dict[str, object] = {}

    def _factory(*args: object, **kwargs: object) -> _FakePopen:
        captured.update(kwargs)
        return _FakePopen([
            "INF ICMP proxy will use fe80::e5ca:5edd:aa61:7681 in zone 以太网",
            "INF |  https://utf8-abc.trycloudflare.com  |",
            "INF Registered tunnel connection connIndex=0",
        ])

    monkeypatch.setattr(
        tunnel_mod.TunnelManager, "_find_cloudflared", lambda self: "C:/fake/cloudflared.exe"
    )
    manager = TunnelManager(
        port=18791,
        cloudflared_path="C:/fake/cloudflared.exe",
        poll_timeout_seconds=1,
        popen_factory=_factory,
        auto_download=False,
    )

    info = manager.ensure_tunnel()

    assert info.base_url == "https://utf8-abc.trycloudflare.com/control"
    assert captured.get("encoding") == "utf-8"
    # A single undecodable byte must degrade to a replacement char, not raise.
    assert captured.get("errors") == "replace"
    manager.close()


def test_undecodable_log_line_does_not_abort_tunnel(monkeypatch) -> None:
    """A strict stream that raises mid-read must not fail a healthy tunnel."""

    from opensquilla.gateway import tunnel as tunnel_mod

    class _StrictPopen(_FakePopen):
        """Raises once, the way a cp936-decoded stream did, then recovers."""

        def __init__(self) -> None:
            super().__init__([
                "INF |  https://strict-abc.trycloudflare.com  |",
                "INF Registered tunnel connection connIndex=0",
            ])
            self._raised = False

        def readline(self) -> str:
            if not self._raised:
                self._raised = True
                raise UnicodeDecodeError("gbk", b"\x91", 0, 1, "illegal multibyte sequence")
            return super().readline()

    monkeypatch.setattr(
        tunnel_mod.TunnelManager, "_find_cloudflared", lambda self: "C:/fake/cloudflared.exe"
    )
    manager = TunnelManager(
        port=18791,
        cloudflared_path="C:/fake/cloudflared.exe",
        poll_timeout_seconds=1,
        popen_factory=lambda *a, **k: _StrictPopen(),
        auto_download=False,
    )

    info = manager.ensure_tunnel()

    assert info.base_url == "https://strict-abc.trycloudflare.com/control"
    manager.close()
