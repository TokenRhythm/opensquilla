"""Outbound tunnel manager for the web remote-control feature.

The desktop gateway normally binds 127.0.0.1, so a phone cannot reach it
directly. This module establishes an outbound quick tunnel (cloudflared) so
the phone can connect from anywhere. If no registered edge connection can be
established, pairing fails instead of issuing an unreachable URL.

Cloudflare path: spawn "cloudflared tunnel --url http://127.0.0.1:<port>",
parse the assigned "https://<random>.trycloudflare.com" domain from stderr,
and register that exact origin with the origin guard. The gateway itself
stays loopback-bound: cloudflared dials localhost outbound.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog

from opensquilla.gateway.origin_guard import register_dynamic_origin, revoke_dynamic_origin

# Pin a specific cloudflared release and verify its SHA-256. Downloading a
# binary from GitHub is supply-chain sensitive; never float to "latest".
CLOUDFLARED_VERSION = "2024.6.1"

# Transport attempt order. quic (UDP :7844) first: proxy TUN adapters such as
# Clash hijack TCP :443 into their fake-ip range, which fails the http2
# transport with "TLS handshake with edge error: EOF" while UDP passes
# through untouched. http2 remains a fallback for networks that block UDP.
_PROTOCOL_ATTEMPTS: tuple[str, ...] = ("quic", "quic", "http2")


class TunnelUnavailableError(RuntimeError):
    """No usable Cloudflare tunnel could be established."""

# Per-platform SHA-256 of the pinned release. Only Windows auto-downloads
# today; other platforms rely on a cloudflared already on PATH.
CLOUDFLARED_SHA256 = {
    "windows-amd64.exe": "934a90eb9608e0d49423f4ba052779fbf80d73c665a795acb75d7bab77cf47cd",
}

# Download sources tried in order: the official GitHub release first, then
# public accelerator mirrors for networks where GitHub is blocked or slow.
# Every source is still verified against the pinned SHA-256, so a mirror can
# only serve the exact pinned bytes or fail. Override with the
# OPENSQUILLA_CLOUDFLARED_MIRRORS environment variable (comma-separated URL
# prefixes, each followed by /<version>/<asset>).
CLOUDFLARED_DOWNLOAD_SOURCES: tuple[str, ...] = (
    "https://github.com/cloudflare/cloudflared/releases/download",
    "https://ghfast.top/https://github.com/cloudflare/cloudflared/releases/download",
    "https://gh-proxy.com/https://github.com/cloudflare/cloudflared/releases/download",
    "https://ghproxy.net/https://github.com/cloudflare/cloudflared/releases/download",
)

log = structlog.get_logger(__name__)


def _configured_download_sources() -> tuple[str, ...]:
    override = os.environ.get("OPENSQUILLA_CLOUDFLARED_MIRRORS", "")
    if override.strip():
        return tuple(
            source.strip().rstrip("/")
            for source in override.split(",")
            if source.strip()
        )
    return CLOUDFLARED_DOWNLOAD_SOURCES


def _platform_asset_suffix() -> str | None:
    # Auto-download is limited to platforms with a pinned digest above.
    if sys.platform == "win32":
        return "windows-amd64.exe"
    return None

_DOMAIN_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)
# cloudflared prints the quick-tunnel URL *before* it dials the edge, so a
# parsed URL proves nothing. This line appears only once a control-plane
# connection is actually registered — it is the only honest success signal.
_REGISTERED_RE = re.compile(r"Registered tunnel connection", re.IGNORECASE)
# Fatal patterns worth failing fast on instead of burning the whole budget.
_FATAL_RE = re.compile(
    r"no free edge addresses|failed to dial to edge|Initiating shutdown",
    re.IGNORECASE,
)

@dataclass(frozen=True)
class TunnelInfo:
    mode: Literal["cloudflare"]
    base_url: str
    origin: str


class TunnelManager:
    """Own the cloudflared subprocess and the dynamic origin registration."""

    def __init__(
        self,
        *,
        port: int = 18791,
        bind_host: str = "127.0.0.1",
        control_base_path: str = "/control",
        cloudflared_path: str | None = None,
        download_dir: str | Path | None = None,
        spawn: bool = True,
        auto_download: bool = True,
        poll_timeout_seconds: float = 30.0,
        popen_factory=None,
        downloader=None,
    ) -> None:
        self._port = int(port)
        self._bind_host = bind_host
        self._base_path = (control_base_path or "/control").rstrip("/") or "/control"
        self._cloudflared_path = cloudflared_path
        self._download_dir = Path(download_dir or tempfile.gettempdir())
        self._spawn = spawn
        self._auto_download = auto_download
        self._poll_timeout = float(poll_timeout_seconds)
        self._process: subprocess.Popen[str] | None = None
        self._popen = popen_factory or subprocess.Popen
        self._downloader = downloader or download_cloudflared
        self._domain: str | None = None
        self._drain_thread: threading.Thread | None = None
        self._lock = threading.RLock()

    def _find_cloudflared(self) -> str | None:
        if self._cloudflared_path and Path(self._cloudflared_path).exists():
            return self._cloudflared_path
        found = shutil.which("cloudflared")
        if found:
            return found
        return None

    def ensure_tunnel(self) -> TunnelInfo:
        """Return the active tunnel, starting one if needed."""

        with self._lock:
            if self._domain:
                poll = getattr(self._process, "poll", None)
                exited = self._process is None or (poll() is not None if callable(poll) else False)
                if exited:
                    self.close()
                else:
                    return self._cloudflare_info(self._domain)
            cloudflared = self._find_cloudflared()
            if cloudflared is None and self._spawn and self._auto_download:
                cloudflared = self._download_cloudflared()
            if not cloudflared or not self._spawn:
                raise TunnelUnavailableError(
                    "cloudflared is unavailable: the remote-control tunnel "
                    "could not be started."
                )
            # quic (UDP :7844) is the primary transport. Proxy TUN adapters
            # (Clash & co.) commonly hijack TCP :443 into a fake-ip range,
            # which kills the http2 transport with a TLS EOF while leaving
            # UDP untouched. http2 stays as a second attempt for networks
            # that block UDP outright.
            failures: list[str] = []
            for attempt, protocol in enumerate(_PROTOCOL_ATTEMPTS, start=1):
                domain, error = self._start_quick_tunnel(cloudflared, protocol)
                if domain:
                    self._domain = domain
                    register_dynamic_origin(f"https://{domain}")
                    log.info(
                        "tunnel.ready", protocol=protocol, domain=domain, attempt=attempt
                    )
                    return self._cloudflare_info(domain)
                failures.append(f"{protocol}#{attempt}: {error}")
                log.warning(
                    "tunnel.attempt_failed",
                    protocol=protocol,
                    attempt=attempt,
                    error=error,
                )
            raise TunnelUnavailableError(
                "Could not establish a Cloudflare tunnel (" + "; ".join(failures) + ")"
            )

    def _download_cloudflared(self) -> str | None:
        """Fetch the pinned cloudflared binary, or return None on failure."""

        suffix = _platform_asset_suffix()
        if suffix is None:
            return None
        try:
            path = self._downloader(
                self._download_dir,
                version=CLOUDFLARED_VERSION,
                expected_sha256=CLOUDFLARED_SHA256[suffix],
                platform_suffix=suffix,
            )
        except Exception as exc:
            log.warning("tunnel.cloudflared_download_failed", error=str(exc))
            return None
        log.info("tunnel.cloudflared_downloaded", path=str(path))
        return str(path)

    def _cloudflare_info(self, domain: str) -> TunnelInfo:
        return TunnelInfo(
            mode="cloudflare",
            base_url=f"https://{domain}{self._base_path}",
            origin=f"https://{domain}",
        )

    def _start_quick_tunnel(
        self, cloudflared: str, protocol: str = "quic"
    ) -> tuple[str | None, str]:
        """Start cloudflared and return (domain, error) once it is *usable*.

        A parsed quick-tunnel URL is not success: cloudflared publishes the
        hostname before dialing the edge, and a failed dial leaves a live
        hostname that answers every request with Cloudflare error 1033. Only
        a registered edge connection counts, so a scanned QR always points at
        a tunnel that already works.
        """

        url = f"http://127.0.0.1:{self._port}"
        try:
            self._process = self._popen(
                [
                    cloudflared,
                    "tunnel",
                    "--url", url,
                    "--no-autoupdate",
                    "--protocol", protocol,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                # cloudflared logs UTF-8 and includes localized adapter names
                # (e.g. "ICMP proxy will use ... in zone 以太网"). text=True
                # alone decodes with the host ANSI code page - cp936 in the
                # packaged desktop app - and one such line aborted tunnel
                # startup with UnicodeDecodeError on byte 0x91. A log byte
                # must never decide whether remote control works.
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            self._process = None
            return None, f"spawn failed: {exc}"

        domain: str | None = None
        last_error = ""
        deadline = time.monotonic() + self._poll_timeout
        assert self._process.stdout is not None
        while time.monotonic() < deadline:
            try:
                line = self._process.stdout.readline()
            except (UnicodeDecodeError, ValueError):
                # Defensive: an injected popen factory may still hand back a
                # strictly-decoding stream. Skip the unreadable chunk rather
                # than failing an otherwise healthy tunnel.
                continue
            if not line:
                last_error = last_error or "cloudflared exited before connecting"
                break
            if domain is None:
                match = _URL_RE.search(line)
                if match:
                    domain = match.group(0).removeprefix("https://").rstrip("/")
            if _REGISTERED_RE.search(line):
                if domain:
                    # Keep draining stdout: an unread pipe eventually blocks
                    # cloudflared on write and stalls the live tunnel.
                    self._start_log_drain()
                    return domain, ""
                last_error = "edge registered before a hostname was announced"
            if " ERR " in line or _FATAL_RE.search(line):
                last_error = line.strip()[-160:]
                if _FATAL_RE.search(line):
                    break
        self.close()
        return None, last_error or "timed out waiting for an edge connection"

    def _start_log_drain(self) -> None:
        """Consume cloudflared stdout for the tunnel's lifetime."""

        process = self._process
        if process is None or process.stdout is None:
            return

        def _drain() -> None:
            try:
                for _ in process.stdout:  # type: ignore[union-attr]
                    pass
            except (OSError, ValueError, UnicodeDecodeError):
                pass

        thread = threading.Thread(target=_drain, daemon=True)
        thread.start()
        self._drain_thread = thread

    def close(self) -> None:
        with self._lock:
            if self._domain:
                revoke_dynamic_origin(f"https://{self._domain}")
                self._domain = None
            if self._process is not None:
                try:
                    self._process.terminate()
                except OSError:
                    pass
                try:
                    self._process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                self._process = None

    @property
    def active(self) -> bool:
        return self._domain is not None or self._process is not None



def download_cloudflared(
    destination_dir: str | Path,
    *,
    version: str = CLOUDFLARED_VERSION,
    expected_sha256: str = "",
    platform_suffix: str = "windows-amd64.exe",
    sources: Sequence[str] = (),
    timeout_seconds: float = 30.0,
) -> Path:
    """Download cloudflared from the first source that serves the pinned bytes.

    Sources are tried in order (official GitHub, then mirrors); a source that
    times out, errors, or fails the SHA-256 check is skipped in favour of the
    next one. Every candidate must match ``expected_sha256`` byte for byte, so
    a mirror can only act as a transport, never as a supplier.
    """

    import urllib.error
    import urllib.request

    dest_dir = Path(destination_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"cloudflared-{version}-{platform_suffix}"
    if target.exists():
        return target
    asset = platform_suffix
    if not asset.startswith("cloudflared-"):
        asset = f"cloudflared-{version}-{platform_suffix}"
    if version == CLOUDFLARED_VERSION and asset.endswith("windows-amd64.exe"):
        # The pinned release publishes the bare binary asset name; the
        # versioned prefix is not part of the Windows asset name.
        asset = "cloudflared-windows-amd64.exe"
    candidates = tuple(sources) or _configured_download_sources()
    tmp = target.with_suffix(".tmp")
    failures: list[str] = []
    for base in candidates:
        url = f"{base}/{version}/{asset}"
        try:
            # The timeout bounds every socket read, so a stalled mirror
            # fails fast instead of wedging the RPC call for minutes.
            with urllib.request.urlopen(url, timeout=timeout_seconds) as response, \
                    tmp.open("wb") as out:
                shutil.copyfileobj(response, out)
        except (OSError, urllib.error.URLError) as exc:
            failures.append(f"{url} ({exc})")
            log.warning("tunnel.cloudflared_source_failed", url=url, error=str(exc))
            continue
        digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
        if expected_sha256 and digest != expected_sha256:
            failures.append(f"{url} (sha256 mismatch)")
            log.warning("tunnel.cloudflared_digest_mismatch", url=url)
            continue
        tmp.rename(target)
        return target
    tmp.unlink(missing_ok=True)
    raise RuntimeError(
        "All cloudflared download sources failed: " + "; ".join(failures)
    )


__all__ = [
    "TunnelUnavailableError",
    "CLOUDFLARED_VERSION",
    "TunnelInfo",
    "TunnelManager",
    "download_cloudflared",
]
