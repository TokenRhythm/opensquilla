"""DashScope text-to-video jobs for standard and Token Plan endpoints."""

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
from opensquilla.provider.video_generation import (
    VideoGenerationError,
    VideoGenerationPending,
    VideoGenerationResult,
    VideoGenerationSubmissionUnknown,
    VideoJobAcceptedCallback,
)
from opensquilla.provider.video_generation_policy import is_valid_video_generation_base_url
from opensquilla.secrets import clean_header_secret

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")
_TASK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MAX_JSON_BYTES = 4 * 1024 * 1024
_DEFAULT_MAX_BYTES = 100 * 1024 * 1024
_TRANSIENT_HTTP = {429, 500, 502, 503, 504}
_LEGACY_SIZES = {
    ("720p", "16:9"): "1280*720",
    ("720p", "9:16"): "720*1280",
    ("1080p", "16:9"): "1920*1080",
    ("1080p", "9:16"): "1080*1920",
}


def _api_root(base_url: str) -> str:
    if not is_valid_video_generation_base_url(base_url):
        raise VideoGenerationError("Invalid DashScope video API base URL")
    return base_url.rstrip("/")


def _remaining(deadline: float, *, job_id: str | None = None) -> float:
    remaining = deadline - time.monotonic()
    if remaining > 0:
        return remaining
    if job_id:
        raise VideoGenerationPending(
            f"Video job {job_id} is still pending; check it again using its job ID",
            job_id=job_id,
        )
    raise VideoGenerationError("Video request timed out before submission")


async def _read_json(response: httpx.Response) -> dict[str, object]:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > _MAX_JSON_BYTES:
            raise VideoGenerationError("DashScope video JSON response exceeds the size limit")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise VideoGenerationError("DashScope video endpoint returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise VideoGenerationError("DashScope video endpoint returned invalid JSON")
    return payload


def _safe_task_id(value: object) -> str:
    if not isinstance(value, str) or not _TASK.fullmatch(value) or value in {".", ".."}:
        raise VideoGenerationError("DashScope video response contains an invalid task ID")
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
    if not isinstance(model, str) or not _MODEL.fullmatch(model) or model in {".", ".."}:
        raise VideoGenerationError("Invalid DashScope video model ID")
    minimum = 3 if model.startswith("happyhorse-") else 2
    if not isinstance(prompt, str) or not prompt.strip():
        raise VideoGenerationError("Video prompt must not be empty")
    if max_duration_seconds is not None and (
        isinstance(max_duration_seconds, bool)
        or not isinstance(max_duration_seconds, int)
        or not minimum <= max_duration_seconds <= 60
    ):
        raise VideoGenerationError(
            f"DashScope video duration limit must be at least {minimum} seconds"
        )
    if duration is not None and (
        isinstance(duration, bool) or not isinstance(duration, int) or not minimum <= duration <= 15
    ):
        raise VideoGenerationError(f"DashScope video duration must be {minimum} to 15 seconds")
    if (
        duration is not None
        and max_duration_seconds is not None
        and duration > max_duration_seconds
    ):
        raise VideoGenerationError("Video duration exceeds the configured limit")
    if aspect_ratio is not None and aspect_ratio not in {"16:9", "9:16"}:
        raise VideoGenerationError("Unsupported DashScope video aspect ratio")
    if resolution is not None and resolution not in {"720p", "1080p"}:
        raise VideoGenerationError("Unsupported DashScope video resolution")
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
    return duration if duration is not None else min(5, max_duration_seconds)


async def _submit(
    client: httpx.AsyncClient,
    *,
    api_root: str,
    api_key: str,
    model: str,
    prompt: str,
    duration: int,
    aspect_ratio: str,
    resolution: str,
    deadline: float,
    on_job_accepted: VideoJobAcceptedCallback | None = None,
) -> str:
    parameters: dict[str, object] = {"duration": duration}
    if model.startswith(("wan2.6-", "wan2.5-", "wan2.2-", "wanx2.1-")):
        parameters["size"] = _LEGACY_SIZES[(resolution, aspect_ratio)]
    else:
        parameters.update({"resolution": resolution.upper(), "ratio": aspect_ratio})
    body = {"model": model, "input": {"prompt": prompt}, "parameters": parameters}
    try:
        async with asyncio.timeout(_remaining(deadline)):
            async with client.stream(
                "POST",
                f"{api_root}/services/aigc/video-generation/video-synthesis",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "X-DashScope-Async": "enable",
                    "Accept": "application/json",
                },
                json=body,
            ) as response:
                if response.status_code not in {200, 202}:
                    if response.status_code >= 500:
                        raise VideoGenerationSubmissionUnknown()
                    raise VideoGenerationError(
                        f"DashScope video submission returned HTTP {response.status_code}"
                    )
                try:
                    payload = await _read_json(response)
                except VideoGenerationError:
                    raise VideoGenerationSubmissionUnknown() from None
                output = payload.get("output")
                try:
                    job_id = _safe_task_id(
                        output.get("task_id") if isinstance(output, dict) else None
                    )
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


async def _poll_until_complete(
    client: httpx.AsyncClient,
    *,
    api_root: str,
    api_key: str,
    job_id: str,
    deadline: float,
    poll_interval_seconds: float,
) -> str:
    while True:
        try:
            async with asyncio.timeout(_remaining(deadline, job_id=job_id)):
                async with client.stream(
                    "GET",
                    f"{api_root}/tasks/{job_id}",
                    headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                ) as response:
                    if response.status_code != 200:
                        if response.status_code in _TRANSIENT_HTTP:
                            raise VideoGenerationPending(
                                f"Video job {job_id} status is temporarily unavailable",
                                job_id=job_id,
                            )
                        raise VideoGenerationError(
                            f"DashScope video status returned HTTP {response.status_code}",
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
        output = payload.get("output")
        if not isinstance(output, dict):
            raise VideoGenerationPending("DashScope video status has no output", job_id=job_id)
        if "task_id" in output and output["task_id"] != job_id:
            raise VideoGenerationError(
                "DashScope video status returned a different task", job_id=job_id
            )
        status = output.get("task_status")
        if status == "SUCCEEDED":
            video_url = output.get("video_url")
            if not isinstance(video_url, str) or not video_url:
                raise VideoGenerationPending("DashScope video has no download URL", job_id=job_id)
            return video_url
        if status in {"FAILED", "CANCELED", "UNKNOWN"}:
            reason = payload.get("message")
            safe_reason = (
                redact_upstream_error_text(reason, api_key=api_key)
                if isinstance(reason, str) and reason
                else "unknown provider error"
            )
            raise VideoGenerationError(
                f"Video job {job_id} {status.lower()}: {safe_reason}", job_id=job_id
            )
        if status not in {"PENDING", "RUNNING"}:
            raise VideoGenerationError(
                "DashScope video job returned an unknown status", job_id=job_id
            )
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
    provider: str,
) -> VideoGenerationResult:
    from opensquilla.provider.video_download import download_video_url

    url = await _poll_until_complete(
        client,
        api_root=api_root,
        api_key=api_key,
        job_id=job_id,
        deadline=deadline,
        poll_interval_seconds=poll_interval_seconds,
    )
    bytes_written = await download_video_url(
        url=url,
        output_path=output_path,
        job_id=job_id,
        max_bytes=max_bytes,
        deadline=deadline,
        provider=provider,
        trusted_endpoint=api_root,
    )
    return VideoGenerationResult(
        job_id=job_id,
        output_path=output_path,
        model=model,
        bytes_written=bytes_written,
        provider=provider,
    )


async def generate_qwen_video(
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
    provider: str = "qwen",
    on_job_accepted: VideoJobAcceptedCallback | None = None,
) -> VideoGenerationResult:
    api_root = _api_root(base_url)
    secret = clean_header_secret(api_key, label="DashScope video API key")
    if not secret:
        raise VideoGenerationError("DashScope video API key is missing")
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
            aspect_ratio=aspect_ratio or "16:9",
            resolution=resolution or "720p",
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
            provider=provider,
        )


async def resume_qwen_video(
    *,
    base_url: str,
    api_key: str,
    job_id: str,
    model: str,
    output_path: Path,
    timeout_seconds: float = 600.0,
    poll_interval_seconds: float = 10.0,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    provider: str = "qwen",
) -> VideoGenerationResult:
    api_root = _api_root(base_url)
    secret = clean_header_secret(api_key, label="DashScope video API key")
    if not secret:
        raise VideoGenerationError("DashScope video API key is missing")
    safe_job_id = _safe_task_id(job_id)
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
            provider=provider,
        )
