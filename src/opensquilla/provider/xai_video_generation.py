"""xAI text-to-video requests with resumable polling and bounded MP4 delivery."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from pathlib import Path

import httpx

from opensquilla.env import trust_env
from opensquilla.provider.error_redaction import redact_upstream_error_text, redacted_httpx_error
from opensquilla.provider.video_download import download_video_url
from opensquilla.provider.video_generation import (
    VideoGenerationError,
    VideoGenerationPending,
    VideoGenerationResult,
    VideoGenerationSubmissionUnknown,
    VideoJobAcceptedCallback,
)
from opensquilla.provider.video_generation_policy import is_valid_video_generation_base_url
from opensquilla.secrets import clean_header_secret

XAI_VIDEO_BASE_URL = "https://api.x.ai/v1"
XAI_VIDEO_MODELS = frozenset(
    {"grok-imagine-video-1.5", "grok-imagine-video-1.5-lite", "grok-imagine-video"}
)
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ASPECT_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9", "5:2"}
_RESOLUTIONS = {"480p", "720p", "1080p"}
_MAX_JSON_BYTES = 4 * 1024 * 1024
_DEFAULT_MAX_BYTES = 100 * 1024 * 1024


def _api_root(base_url: str) -> str:
    if not is_valid_video_generation_base_url(base_url):
        raise VideoGenerationError("Invalid xAI video API base URL")
    return base_url.rstrip("/")


def _safe_job_id(value: object) -> str:
    if not isinstance(value, str) or not _JOB_ID.fullmatch(value) or value in {".", ".."}:
        raise VideoGenerationError("xAI video response contains an invalid request ID")
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
) -> int:
    if not isinstance(model, str) or not _MODEL_ID.fullmatch(model):
        raise VideoGenerationError("Invalid xAI video model")
    if not isinstance(prompt, str) or not prompt.strip():
        raise VideoGenerationError("Video prompt must not be empty")
    if max_duration_seconds is not None and (
        isinstance(max_duration_seconds, bool)
        or not isinstance(max_duration_seconds, int)
        or max_duration_seconds < 1
    ):
        raise VideoGenerationError("Video duration limit must be a positive whole number")
    if duration is not None and (
        isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 15
    ):
        raise VideoGenerationError("xAI video duration must be between 1 and 15 seconds")
    if (
        duration is not None
        and max_duration_seconds is not None
        and duration > max_duration_seconds
    ):
        raise VideoGenerationError("Video duration exceeds the configured limit")
    if aspect_ratio is not None and aspect_ratio not in _ASPECT_RATIOS:
        raise VideoGenerationError("Unsupported xAI video aspect ratio")
    if resolution is not None and resolution not in _RESOLUTIONS:
        raise VideoGenerationError("Unsupported xAI video resolution")
    if model == "grok-imagine-video" and resolution == "1080p":
        raise VideoGenerationError("The selected xAI video model supports at most 720p")
    if output_path.suffix.lower() != ".mp4" or output_path.exists():
        raise VideoGenerationError("Video output path must be a new .mp4 file")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise VideoGenerationError("Video timeout must be positive and finite")
    if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
        raise VideoGenerationError("Video polling interval must be positive and finite")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise VideoGenerationError("Video byte limit must be positive")
    if duration is not None:
        return duration
    return min(8, max_duration_seconds or 8)


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
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > _MAX_JSON_BYTES:
            raise VideoGenerationError("xAI video JSON response exceeds the size limit")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise VideoGenerationError("xAI video endpoint returned invalid JSON") from None
    if not isinstance(value, dict):
        raise VideoGenerationError("xAI video endpoint returned invalid JSON")
    return value


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
    body: dict[str, object] = {"model": model, "prompt": prompt, "duration": duration}
    if aspect_ratio is not None:
        body["aspect_ratio"] = aspect_ratio
    if resolution is not None:
        body["resolution"] = resolution
    try:
        async with asyncio.timeout(_remaining(deadline)):
            async with client.stream(
                "POST",
                f"{api_root}/videos/generations",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=body,
            ) as response:
                if response.status_code not in {200, 202}:
                    if response.status_code >= 500:
                        raise VideoGenerationSubmissionUnknown()
                    raise VideoGenerationError(
                        f"xAI video submission returned HTTP {response.status_code}"
                    )
                try:
                    payload = await _read_json(response)
                except VideoGenerationError:
                    raise VideoGenerationSubmissionUnknown() from None
                try:
                    job_id = _safe_job_id(payload.get("request_id"))
                except VideoGenerationError:
                    raise VideoGenerationSubmissionUnknown() from None
                if on_job_accepted is not None:
                    on_job_accepted(job_id)
    except httpx.HTTPError as exc:
        redacted_httpx_error(exc, api_key=api_key)
        raise VideoGenerationSubmissionUnknown() from None
    except TimeoutError:
        raise VideoGenerationSubmissionUnknown() from None
    return job_id


def _error_message(value: object, *, api_key: str) -> str:
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
                    f"{api_root}/videos/{job_id}",
                    headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                ) as response:
                    if response.status_code != 200:
                        if response.status_code in {408, 429, 500, 502, 503, 504}:
                            raise VideoGenerationPending(
                                f"Video job {job_id} status is temporarily unavailable",
                                job_id=job_id,
                            )
                        raise VideoGenerationError(
                            f"xAI video status returned HTTP {response.status_code}", job_id=job_id
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

        status = str(payload.get("status", "")).strip().lower()
        if status == "done":
            return payload
        if status in {"failed", "expired", "cancelled", "canceled", "error"}:
            reason = _error_message(payload.get("error"), api_key=api_key)
            raise VideoGenerationError(f"Video job {job_id} failed: {reason}", job_id=job_id)
        if status not in {"pending", "queued", "running", "processing", "submitted"}:
            raise VideoGenerationError("xAI video job returned invalid status", job_id=job_id)
        await asyncio.sleep(min(poll_interval_seconds, _remaining(deadline, job_id=job_id)))


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
    payload = await _poll_until_complete(
        client,
        api_root=api_root,
        api_key=api_key,
        job_id=job_id,
        deadline=deadline,
        poll_interval_seconds=poll_interval_seconds,
    )
    video = payload.get("video")
    url = video.get("url") if isinstance(video, dict) else None
    if not isinstance(url, str) or not url:
        raise VideoGenerationPending(
            f"Video job {job_id} has no downloadable result yet", job_id=job_id
        )
    count = await download_video_url(
        url=url,
        output_path=output_path,
        job_id=job_id,
        max_bytes=max_bytes,
        deadline=deadline,
        provider="xAI",
        trusted_endpoint=api_root,
    )
    return VideoGenerationResult(
        job_id=job_id,
        output_path=output_path,
        model=model,
        bytes_written=count,
        provider="xai",
    )


async def generate_xai_video(
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
    """Submit one xAI video request and save the result after polling."""

    api_root = _api_root(base_url)
    secret = clean_header_secret(api_key, label="xAI video API key")
    if not secret:
        raise VideoGenerationError("xAI video API key is missing")
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


async def resume_xai_video(
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
    """Finish an accepted xAI video request with GET requests only."""

    api_root = _api_root(base_url)
    secret = clean_header_secret(api_key, label="xAI video API key")
    if not secret:
        raise VideoGenerationError("xAI video API key is missing")
    safe_job_id = _safe_job_id(job_id)
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
