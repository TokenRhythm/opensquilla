"""Gemini Veo text-to-video requests with bounded polling and MP4 delivery."""

from __future__ import annotations

import asyncio
import math
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from opensquilla.endpoint_identity import base_url_allows_credential_reuse
from opensquilla.env import trust_env
from opensquilla.provider.error_redaction import (
    redact_upstream_error_text,
    redacted_httpx_error,
)
from opensquilla.provider.video_generation import (
    VideoGenerationError,
    VideoGenerationPending,
    VideoGenerationResult,
    VideoGenerationSubmissionUnknown,
    VideoJobAcceptedCallback,
    _publish_video_without_clobber,
    read_video_json,
)
from opensquilla.provider.video_generation_policy import is_valid_video_generation_base_url
from opensquilla.secrets import clean_header_secret

GEMINI_VIDEO_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_VIDEO_MODELS = frozenset(
    {
        "veo-3.1-generate-preview",
        "veo-3.1-fast-generate-preview",
        "veo-3.1-lite-generate-preview",
    }
)
_OPERATION = re.compile(r"(?:models/([A-Za-z0-9._-]+)/)?operations/([A-Za-z0-9._-]{1,512})\Z")
_DURATIONS = (4, 6, 8)
_MAX_JSON_BYTES = 4 * 1024 * 1024
_DEFAULT_MAX_BYTES = 100 * 1024 * 1024
_REDIRECT_CODES = {301, 302, 303, 307, 308}


def _api_root(base_url: str) -> str:
    if not is_valid_video_generation_base_url(base_url):
        raise VideoGenerationError("Invalid Gemini video API base URL")
    return base_url.rstrip("/")


def _safe_operation(value: object, *, model: str) -> str:
    if not isinstance(value, str):
        raise VideoGenerationError("Gemini video response contains an invalid operation name")
    match = _OPERATION.fullmatch(value)
    if (
        match is None
        or match.group(2) in {".", ".."}
        or (match.group(1) is not None and match.group(1) != model)
    ):
        raise VideoGenerationError("Gemini video response contains an invalid operation name")
    return value


def _validate_request(
    *,
    model: str,
    prompt: str,
    duration: int | None,
    max_duration_seconds: int | None,
    aspect_ratio: str | None,
    resolution: str | None,
    output_path: Path,
    timeout_seconds: float,
    poll_interval_seconds: float,
    max_bytes: int,
) -> int | None:
    if model not in GEMINI_VIDEO_MODELS:
        raise VideoGenerationError("Unsupported Gemini Veo video model")
    if not isinstance(prompt, str) or not prompt.strip():
        raise VideoGenerationError("Video prompt must not be empty")
    if max_duration_seconds is not None and (
        isinstance(max_duration_seconds, bool)
        or not isinstance(max_duration_seconds, int)
        or max_duration_seconds < 1
    ):
        raise VideoGenerationError("Video duration limit must be a positive whole number")
    if duration is not None and (
        isinstance(duration, bool) or not isinstance(duration, int) or duration not in _DURATIONS
    ):
        raise VideoGenerationError("Gemini Veo duration must be 4, 6, or 8 seconds")
    if (
        duration is not None
        and max_duration_seconds is not None
        and duration > max_duration_seconds
    ):
        raise VideoGenerationError("Video duration exceeds the configured limit")
    if aspect_ratio is not None and aspect_ratio not in {"16:9", "9:16"}:
        raise VideoGenerationError("Unsupported Gemini Veo aspect ratio")
    if resolution is not None and resolution not in {"720p", "1080p"}:
        raise VideoGenerationError("Unsupported Gemini Veo resolution")
    if output_path.suffix.lower() != ".mp4" or output_path.exists():
        raise VideoGenerationError("Video output path must be a new .mp4 file")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise VideoGenerationError("Video timeout must be positive and finite")
    if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
        raise VideoGenerationError("Video polling interval must be positive and finite")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise VideoGenerationError("Video byte limit must be positive")
    if max_duration_seconds is None:
        return None
    if duration is None:
        options = [item for item in _DURATIONS if item <= max_duration_seconds]
        if resolution == "1080p":
            options = [item for item in options if item == 8]
        if not options:
            raise VideoGenerationError("Gemini Veo has no duration within the configured limit")
        return min(options)
    if resolution == "1080p" and duration != 8:
        raise VideoGenerationError("Gemini Veo 1080p requires an 8-second duration")
    return duration


def _remaining(deadline: float, *, job_id: str | None = None) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        if job_id:
            raise VideoGenerationPending(
                f"Video job {job_id} is still pending; check it again using its job ID",
                job_id=job_id,
            )
        raise VideoGenerationError("Video request timed out before submission")
    return seconds


async def _read_json(response: httpx.Response) -> dict[str, object]:
    return await read_video_json(response, max_bytes=_MAX_JSON_BYTES, label="Gemini video")


async def _submit(
    client: httpx.AsyncClient,
    *,
    api_root: str,
    api_key: str,
    model: str,
    prompt: str,
    duration: int,
    aspect_ratio: str | None,
    resolution: str | None,
    deadline: float,
    on_job_accepted: VideoJobAcceptedCallback | None = None,
) -> str:
    parameters: dict[str, object] = {"durationSeconds": str(duration)}
    if aspect_ratio is not None:
        parameters["aspectRatio"] = aspect_ratio
    if resolution is not None:
        parameters["resolution"] = resolution
    body = {"instances": [{"prompt": prompt}], "parameters": parameters}
    accepted_job_id: str | None = None
    try:
        async with asyncio.timeout(_remaining(deadline)):
            async with client.stream(
                "POST",
                f"{api_root}/models/{model}:predictLongRunning",
                headers={"x-goog-api-key": api_key, "Accept": "application/json"},
                json=body,
            ) as response:
                if response.status_code not in {200, 202}:
                    if response.status_code >= 500:
                        raise VideoGenerationSubmissionUnknown()
                    raise VideoGenerationError(
                        f"Gemini video submission returned HTTP {response.status_code}"
                    )
                try:
                    payload = await _read_json(response)
                except VideoGenerationError:
                    raise VideoGenerationSubmissionUnknown() from None
                try:
                    job_id = _safe_operation(payload.get("name"), model=model)
                except VideoGenerationError:
                    raise VideoGenerationSubmissionUnknown() from None
                accepted_job_id = job_id
                if on_job_accepted is not None:
                    on_job_accepted(job_id)
    except httpx.HTTPError as exc:
        redacted_httpx_error(exc, api_key=api_key)
        if accepted_job_id is not None:
            raise VideoGenerationPending(
                "Video submission was accepted; check it again using its job ID",
                job_id=accepted_job_id,
            ) from None
        raise VideoGenerationSubmissionUnknown() from None
    except TimeoutError:
        if accepted_job_id is not None:
            raise VideoGenerationPending(
                "Video submission was accepted; check it again using its job ID",
                job_id=accepted_job_id,
            ) from None
        raise VideoGenerationSubmissionUnknown() from None
    return job_id


def _error_text(value: object, *, api_key: str) -> str:
    if isinstance(value, dict):
        value = value.get("message")
    if isinstance(value, str) and value.strip():
        return redact_upstream_error_text(value.strip(), api_key=api_key)
    return "unknown provider error"


async def _poll_until_complete(
    client: httpx.AsyncClient,
    *,
    api_root: str,
    api_key: str,
    job_id: str,
    deadline: float,
    poll_interval_seconds: float,
) -> dict[str, object]:
    while True:
        try:
            async with asyncio.timeout(_remaining(deadline, job_id=job_id)):
                async with client.stream(
                    "GET",
                    f"{api_root}/{job_id}",
                    headers={"x-goog-api-key": api_key, "Accept": "application/json"},
                ) as response:
                    if response.status_code != 200:
                        if response.status_code in {429, 500, 502, 503, 504}:
                            raise VideoGenerationPending(
                                f"Video job {job_id} status is temporarily unavailable",
                                job_id=job_id,
                            )
                        raise VideoGenerationError(
                            f"Gemini video status returned HTTP {response.status_code}",
                            job_id=job_id,
                        )
                    try:
                        payload = await _read_json(response)
                    except VideoGenerationError as exc:
                        raise VideoGenerationPending(str(exc), job_id=job_id) from None
        except httpx.HTTPError as exc:
            redacted_httpx_error(exc, api_key=api_key)
            raise VideoGenerationPending(
                f"Video job {job_id} status is temporarily unavailable", job_id=job_id
            ) from None
        except TimeoutError:
            raise VideoGenerationPending(
                f"Video job {job_id} is still pending; check it again using its job ID",
                job_id=job_id,
            ) from None
        if "name" in payload and payload["name"] != job_id:
            raise VideoGenerationError(
                "Gemini video status returned a different job", job_id=job_id
            )
        done = payload.get("done", False)
        if done is True:
            if payload.get("error") is not None:
                reason = _error_text(payload["error"], api_key=api_key)
                raise VideoGenerationError(f"Video job {job_id} failed: {reason}", job_id=job_id)
            return payload
        if done is not False:
            raise VideoGenerationError("Gemini video job returned invalid status", job_id=job_id)
        await asyncio.sleep(min(poll_interval_seconds, _remaining(deadline, job_id=job_id)))


def _safe_download_url(raw_url: object, *, api_root: str, job_id: str) -> str:
    if not isinstance(raw_url, str) or raw_url != raw_url.strip() or not raw_url:
        raise VideoGenerationError("Gemini video has no valid download URI", job_id=job_id)
    if any(ch.isspace() or ord(ch) < 0x20 for ch in raw_url) or "\\" in raw_url:
        raise VideoGenerationError("Gemini video download URI is invalid", job_id=job_id)
    try:
        parsed = urlsplit(raw_url)
        _port = parsed.port
    except (UnicodeError, ValueError):
        raise VideoGenerationError("Gemini video download URI is invalid", job_id=job_id) from None
    if (
        not base_url_allows_credential_reuse(api_root, raw_url)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not _is_file_path(parsed.path, api_root=api_root)
    ):
        raise VideoGenerationError(
            "Gemini video download URI left the configured API", job_id=job_id, recoverable=True
        )
    return raw_url


def _is_file_path(path: str, *, api_root: str) -> bool:
    root_path = urlsplit(api_root).path.rstrip("/")
    if not path.startswith(f"{root_path}/files/") or "%" in path:
        return False
    return all(segment not in {"", ".", ".."} for segment in path.split("/")[1:])


def _redirect_url(
    raw_url: object, *, current_url: str, api_root: str, job_id: str
) -> tuple[str, bool]:
    if not isinstance(raw_url, str) or not raw_url or raw_url != raw_url.strip():
        raise VideoGenerationPending("Gemini video redirect is invalid", job_id=job_id)
    if any(ch.isspace() or ord(ch) < 0x20 for ch in raw_url) or "\\" in raw_url:
        raise VideoGenerationPending("Gemini video redirect is invalid", job_id=job_id)
    try:
        url = urljoin(current_url, raw_url)
        parsed = urlsplit(url)
        port = parsed.port
    except (UnicodeError, ValueError):
        raise VideoGenerationPending("Gemini video redirect is invalid", job_id=job_id) from None
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise VideoGenerationPending("Gemini video redirect is unsafe", job_id=job_id)
    host = (parsed.hostname or "").lower()
    configured_api = base_url_allows_credential_reuse(api_root, url)
    google_media = (
        parsed.scheme == "https"
        and port in {None, 443}
        and (host == "storage.googleapis.com" or host.endswith(".googleusercontent.com"))
    )
    if not configured_api and not google_media:
        raise VideoGenerationPending(
            "Gemini video redirect left trusted media hosts", job_id=job_id
        )
    if configured_api and not _is_file_path(parsed.path, api_root=api_root):
        raise VideoGenerationPending("Gemini video redirect left the file endpoint", job_id=job_id)
    return url, configured_api


async def _download(
    client: httpx.AsyncClient,
    *,
    api_root: str,
    api_key: str,
    job_id: str,
    completed: dict[str, object],
    output_path: Path,
    max_bytes: int,
    deadline: float,
) -> int:
    response_body = completed.get("response")
    video_response = (
        response_body.get("generateVideoResponse") if isinstance(response_body, dict) else None
    )
    samples = video_response.get("generatedSamples") if isinstance(video_response, dict) else None
    first = samples[0] if isinstance(samples, list) and samples else None
    video = first.get("video") if isinstance(first, dict) else None
    raw_uri = video.get("uri") if isinstance(video, dict) else None
    url = _safe_download_url(raw_uri, api_root=api_root, job_id=job_id)
    send_key = True
    temporary: Path | None = None
    try:
        for redirect_count in range(4):
            headers = {"Accept": "video/mp4", "Accept-Encoding": "identity"}
            if send_key:
                headers["x-goog-api-key"] = api_key
            async with asyncio.timeout(_remaining(deadline, job_id=job_id)):
                async with client.stream("GET", url, headers=headers) as response:
                    if response.status_code in _REDIRECT_CODES:
                        if redirect_count >= 3:
                            raise VideoGenerationPending(
                                "Gemini video redirected too often", job_id=job_id
                            )
                        url, send_key = _redirect_url(
                            response.headers.get("location"),
                            current_url=url,
                            api_root=api_root,
                            job_id=job_id,
                        )
                        continue
                    if response.status_code != 200:
                        if response.status_code in {429, 500, 502, 503, 504}:
                            raise VideoGenerationPending(
                                f"Video job {job_id} content is temporarily unavailable",
                                job_id=job_id,
                            )
                        raise VideoGenerationError(
                            f"Gemini video content returned HTTP {response.status_code}",
                            job_id=job_id,
                        )
                    mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if mime not in {"video/mp4", "application/octet-stream"}:
                        raise VideoGenerationError(
                            f"Gemini video content has unsupported type {mime or '<missing>'}",
                            job_id=job_id,
                        )
                    content_length: int | None = None
                    length_header = response.headers.get("content-length")
                    if length_header is not None:
                        try:
                            content_length = int(length_header)
                        except ValueError:
                            raise VideoGenerationError(
                                "Gemini video content length is invalid", job_id=job_id
                            ) from None
                        if content_length < 0 or content_length > max_bytes:
                            raise VideoGenerationError(
                                "Gemini video exceeds the configured byte limit", job_id=job_id
                            )
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        prefix=".video-",
                        suffix=".tmp",
                        dir=output_path.parent,
                        delete=False,
                    ) as sink:
                        temporary = Path(sink.name)
                        count = 0
                        header = bytearray()
                        async for chunk in response.aiter_raw():
                            count += len(chunk)
                            if count > max_bytes:
                                raise VideoGenerationError(
                                    "Gemini video exceeds the configured byte limit", job_id=job_id
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
                            "Gemini returned invalid MP4 content", job_id=job_id
                        )
                    if content_length is not None and count != content_length:
                        raise VideoGenerationError(
                            "Gemini video content length did not match the response", job_id=job_id
                        )
                    _publish_video_without_clobber(temporary, output_path, job_id=job_id)
                    return count
        raise VideoGenerationPending("Gemini video redirected too often", job_id=job_id)
    except httpx.HTTPError as exc:
        redacted_httpx_error(exc, api_key=api_key)
        raise VideoGenerationPending(
            f"Video job {job_id} download is temporarily unavailable", job_id=job_id
        ) from None
    except TimeoutError:
        raise VideoGenerationPending(
            f"Video job {job_id} download timed out; check it again using its job ID",
            job_id=job_id,
        ) from None
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


async def _complete_job(
    client: httpx.AsyncClient,
    *,
    api_root: str,
    api_key: str,
    job_id: str,
    model: str,
    output_path: Path,
    max_bytes: int,
    deadline: float,
    poll_interval_seconds: float,
) -> VideoGenerationResult:
    completed = await _poll_until_complete(
        client,
        api_root=api_root,
        api_key=api_key,
        job_id=job_id,
        deadline=deadline,
        poll_interval_seconds=poll_interval_seconds,
    )
    count = await _download(
        client,
        api_root=api_root,
        api_key=api_key,
        job_id=job_id,
        completed=completed,
        output_path=output_path,
        max_bytes=max_bytes,
        deadline=deadline,
    )
    return VideoGenerationResult(
        job_id=job_id,
        output_path=output_path,
        model=model,
        bytes_written=count,
        provider="gemini",
    )


async def generate_gemini_video(
    *,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    duration: int | None,
    max_duration_seconds: int,
    aspect_ratio: str | None,
    resolution: str | None,
    output_path: Path,
    timeout_seconds: float = 600.0,
    poll_interval_seconds: float = 10.0,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    on_job_accepted: VideoJobAcceptedCallback | None = None,
) -> VideoGenerationResult:
    """Submit one Veo request, poll its operation, then save the first MP4."""

    api_root = _api_root(base_url)
    secret = clean_header_secret(api_key, label="Gemini video API key")
    if not secret:
        raise VideoGenerationError("Gemini video API key is missing")
    destination = Path(output_path)
    selected_duration = _validate_request(
        model=model,
        prompt=prompt,
        duration=duration,
        max_duration_seconds=max_duration_seconds,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        output_path=destination,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        max_bytes=max_bytes,
    )
    assert selected_duration is not None
    deadline = time.monotonic() + timeout_seconds
    async with httpx.AsyncClient(
        timeout=None, trust_env=trust_env(), follow_redirects=False
    ) as client:
        job_id = await _submit(
            client,
            api_root=api_root,
            api_key=secret,
            model=model,
            prompt=prompt.strip(),
            duration=selected_duration,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            deadline=deadline,
            on_job_accepted=on_job_accepted,
        )
        return await _complete_job(
            client,
            api_root=api_root,
            api_key=secret,
            job_id=job_id,
            model=model,
            output_path=destination,
            max_bytes=max_bytes,
            deadline=deadline,
            poll_interval_seconds=poll_interval_seconds,
        )


async def resume_gemini_video(
    *,
    base_url: str,
    api_key: str,
    job_id: str,
    model: str,
    output_path: Path,
    timeout_seconds: float = 600.0,
    poll_interval_seconds: float = 10.0,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> VideoGenerationResult:
    """Finish a known Veo operation with GET requests only."""

    api_root = _api_root(base_url)
    secret = clean_header_secret(api_key, label="Gemini video API key")
    if not secret:
        raise VideoGenerationError("Gemini video API key is missing")
    safe_job_id = _safe_operation(job_id, model=model)
    destination = Path(output_path)
    _validate_request(
        model=model,
        prompt="resume",
        duration=None,
        max_duration_seconds=None,
        aspect_ratio=None,
        resolution=None,
        output_path=destination,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        max_bytes=max_bytes,
    )
    deadline = time.monotonic() + timeout_seconds
    async with httpx.AsyncClient(
        timeout=None, trust_env=trust_env(), follow_redirects=False
    ) as client:
        return await _complete_job(
            client,
            api_root=api_root,
            api_key=secret,
            job_id=safe_job_id,
            model=model,
            output_path=destination,
            max_bytes=max_bytes,
            deadline=deadline,
            poll_interval_seconds=poll_interval_seconds,
        )
