"""Download provider-hosted MP4 output with bounded, credential-free requests."""

from __future__ import annotations

import asyncio
import math
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from opensquilla.env import trust_env
from opensquilla.provider.video_generation import (
    VideoGenerationError,
    VideoGenerationPending,
    _publish_video_without_clobber,
)
from opensquilla.tools.fetch_work import run_blocking_fetch_work
from opensquilla.tools.ssrf import (
    environment_proxy_url,
    pinned_transport,
    validate_http_url_for_fetch,
)

_REDIRECT_CODES = {301, 302, 303, 307, 308}
_TRANSIENT_CODES = {403, 404, 408, 429, 500, 502, 503, 504}
_MAX_REDIRECTS = 3
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _origin(url: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(url)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.scheme.lower(), parsed.hostname.lower(), port
    except (UnicodeError, ValueError):
        return None


def _local_endpoint_match(url: str, trusted_endpoint: str) -> bool:
    candidate = _origin(url)
    trusted = _origin(trusted_endpoint) if trusted_endpoint else None
    return bool(
        candidate
        and trusted
        and trusted[1] in _LOOPBACK_HOSTS
        and candidate == trusted
        and candidate[0] in {"http", "https"}
    )


def _validate_download_url(url: str, *, trusted_endpoint: str) -> bool:
    """Return whether an explicitly configured loopback endpoint owns the URL."""

    if (
        not isinstance(url, str)
        or not url
        or url != url.strip()
        or any(character.isspace() or ord(character) < 0x20 for character in url)
        or "\\" in url
    ):
        raise VideoGenerationError("Invalid generated video URL")
    try:
        parsed = urlsplit(url)
        _port = parsed.port
    except (UnicodeError, ValueError):
        raise VideoGenerationError("Invalid generated video URL") from None
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise VideoGenerationError("Invalid generated video URL")
    local = _local_endpoint_match(url, trusted_endpoint)
    if parsed.scheme.lower() != "https" and not local:
        raise VideoGenerationError("Generated video URL must use HTTPS")
    if parsed.scheme.lower() not in {"http", "https"}:
        raise VideoGenerationError("Invalid generated video URL")
    return local


def _client_options(url: str, trusted_endpoint: str, timeout_seconds: float) -> dict:
    local = _validate_download_url(url, trusted_endpoint=trusted_endpoint)
    if local:
        return {"timeout": timeout_seconds, "follow_redirects": False, "trust_env": False}
    vetted_ips = validate_http_url_for_fetch(url)
    transport_kwargs: dict[str, object] = {}
    if trust_env():
        proxy_url = environment_proxy_url(url)
        if proxy_url is not None:
            transport_kwargs["proxy"] = proxy_url
    transport = pinned_transport(url, vetted_ips, **transport_kwargs)
    options: dict = {
        "timeout": timeout_seconds,
        "follow_redirects": False,
        "trust_env": trust_env(),
    }
    if transport is not None:
        options["transport"] = transport
    return options


def _remaining(deadline: float, *, job_id: str) -> float:
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise VideoGenerationPending(
            f"Video job {job_id} download timed out; check it again using its job ID",
            job_id=job_id,
        )
    return remaining


async def download_video_url(
    *,
    url: str,
    output_path: Path,
    job_id: str,
    max_bytes: int,
    deadline: float,
    provider: str,
    trusted_endpoint: str = "",
) -> int:
    """Save a signed MP4 URL; validate DNS and redirects without forwarding API keys."""

    destination = Path(output_path)
    if destination.suffix.lower() != ".mp4" or destination.exists():
        raise VideoGenerationError("Video output path must be a new .mp4 file", job_id=job_id)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise VideoGenerationError("Video byte limit must be positive", job_id=job_id)
    current_url = url
    temporary: Path | None = None
    try:
        for attempt in range(_MAX_REDIRECTS + 1):
            # DNS validation can block; bound it without blocking the event loop.
            try:
                async with asyncio.timeout(_remaining(deadline, job_id=job_id)):
                    options = await run_blocking_fetch_work(
                        _client_options,
                        current_url,
                        trusted_endpoint,
                        _remaining(deadline, job_id=job_id),
                    )
            except TimeoutError:
                raise VideoGenerationPending(
                    f"Video job {job_id} download timed out; check it again using its job ID",
                    job_id=job_id,
                ) from None
            except VideoGenerationError:
                raise
            except Exception:
                raise VideoGenerationError(
                    f"{provider} returned an unsafe video download URL", job_id=job_id
                ) from None

            async with httpx.AsyncClient(**options) as client:
                try:
                    async with asyncio.timeout(_remaining(deadline, job_id=job_id)):
                        async with client.stream(
                            "GET",
                            current_url,
                            headers={"Accept": "video/mp4", "Accept-Encoding": "identity"},
                        ) as response:
                            if response.status_code in _REDIRECT_CODES:
                                if attempt >= _MAX_REDIRECTS:
                                    raise VideoGenerationPending(
                                        f"Video job {job_id} redirected too often", job_id=job_id
                                    )
                                location = response.headers.get("location")
                                if not location:
                                    raise VideoGenerationError(
                                        f"{provider} video redirect was invalid", job_id=job_id
                                    )
                                current_url = urljoin(current_url, location)
                                _validate_download_url(
                                    current_url, trusted_endpoint=trusted_endpoint
                                )
                                continue
                            if response.status_code != 200:
                                if response.status_code in _TRANSIENT_CODES:
                                    raise VideoGenerationPending(
                                        f"Video job {job_id} content is temporarily unavailable",
                                        job_id=job_id,
                                    )
                                raise VideoGenerationError(
                                    f"{provider} video content returned "
                                    f"HTTP {response.status_code}",
                                    job_id=job_id,
                                )
                            mime = (
                                response.headers.get("content-type", "")
                                .split(";", 1)[0]
                                .strip()
                                .lower()
                            )
                            if mime not in {"video/mp4", "application/octet-stream"}:
                                raise VideoGenerationError(
                                    f"{provider} video content has unsupported type", job_id=job_id
                                )
                            length_header = response.headers.get("content-length")
                            content_length: int | None = None
                            if length_header is not None:
                                try:
                                    content_length = int(length_header)
                                except ValueError:
                                    raise VideoGenerationError(
                                        f"{provider} video content length is invalid", job_id=job_id
                                    ) from None
                                if content_length < 0 or content_length > max_bytes:
                                    raise VideoGenerationError(
                                        f"{provider} video exceeds the configured byte limit",
                                        job_id=job_id,
                                    )
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            with tempfile.NamedTemporaryFile(
                                mode="wb",
                                prefix=".video-",
                                suffix=".tmp",
                                dir=destination.parent,
                                delete=False,
                            ) as sink:
                                temporary = Path(sink.name)
                                count = 0
                                header = bytearray()
                                async for chunk in response.aiter_raw():
                                    count += len(chunk)
                                    if count > max_bytes:
                                        raise VideoGenerationError(
                                            f"{provider} video exceeds the configured byte limit",
                                            job_id=job_id,
                                        )
                                    if len(header) < 12:
                                        header.extend(chunk[: 12 - len(header)])
                                    sink.write(chunk)
                            if (
                                count < 12
                                or int.from_bytes(header[:4], "big") < 12
                                or header[4:8] != b"ftyp"
                            ):
                                raise VideoGenerationError(
                                    f"{provider} returned invalid MP4 content", job_id=job_id
                                )
                            if content_length is not None and count != content_length:
                                raise VideoGenerationError(
                                    f"{provider} video content length did not match the response",
                                    job_id=job_id,
                                )
                            _publish_video_without_clobber(temporary, destination, job_id=job_id)
                            return count
                except httpx.HTTPError:
                    raise VideoGenerationPending(
                        f"Video job {job_id} download is temporarily unavailable", job_id=job_id
                    ) from None
                except TimeoutError:
                    raise VideoGenerationPending(
                        f"Video job {job_id} download timed out; check it again using its job ID",
                        job_id=job_id,
                    ) from None
        raise VideoGenerationPending(f"Video job {job_id} redirected too often", job_id=job_id)
    except OSError:
        raise VideoGenerationPending(
            f"Video job {job_id} could not be saved; check storage and try its job ID again",
            job_id=job_id,
        ) from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
