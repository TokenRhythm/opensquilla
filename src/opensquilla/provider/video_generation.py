"""OpenRouter text-to-video generation with bounded polling and MP4 delivery."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from opensquilla.endpoint_identity import base_url_matches_official_api
from opensquilla.env import trust_env
from opensquilla.provider.error_redaction import (
    redact_upstream_error_text,
    redacted_httpx_error,
)
from opensquilla.provider.video_generation_policy import is_valid_video_generation_base_url
from opensquilla.secrets import clean_header_secret

_DEFAULT_MAX_BYTES = 100 * 1024 * 1024
_MAX_JSON_BYTES = 4 * 1024 * 1024
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}\Z")
_ASPECT_RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3", "21:9", "9:21"}
_RESOLUTIONS = {"480p", "720p", "1080p", "1K", "2K", "4K"}


class VideoGenerationError(RuntimeError):
    """A video request failed; ``job_id`` is present after accepted submission."""

    def __init__(
        self,
        message: str,
        *,
        job_id: str | None = None,
        recoverable: bool = False,
    ) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.recoverable = recoverable


class VideoGenerationPending(VideoGenerationError):  # noqa: N818
    """The accepted job can be checked again without another paid submission."""

    def __init__(self, message: str, *, job_id: str) -> None:
        super().__init__(message, job_id=job_id, recoverable=True)


class VideoGenerationSubmissionUnknown(VideoGenerationError):  # noqa: N818
    """The POST outcome is unknown; an automatic second POST could charge twice."""

    def __init__(
        self, message: str = "Video submission outcome is unknown; do not resubmit automatically"
    ) -> None:
        super().__init__(message)


@dataclass(frozen=True)
class VideoGenerationResult:
    job_id: str
    output_path: Path
    model: str
    bytes_written: int
    mime_type: str = "video/mp4"
    provider: str = "openrouter"
    generation_id: str | None = None


def _api_root(base_url: str) -> str:
    if not is_valid_video_generation_base_url(base_url):
        raise VideoGenerationError("Invalid OpenRouter video API base URL")
    return base_url.rstrip("/")


def _safe_job_id(value: object) -> str:
    if not isinstance(value, str) or not _JOB_ID.fullmatch(value):
        raise VideoGenerationError("OpenRouter video response contains an invalid job ID")
    return value


def _polling_url(api_root: str, job_id: str, raw_url: object) -> str:
    expected = f"{api_root}/videos/{job_id}"
    if not isinstance(raw_url, str) or not raw_url or raw_url != raw_url.strip():
        raise VideoGenerationError(
            "OpenRouter video response has no valid polling URL", job_id=job_id, recoverable=True
        )
    if any(character.isspace() or ord(character) < 0x20 for character in raw_url):
        raise VideoGenerationError(
            "OpenRouter video polling URL is invalid", job_id=job_id, recoverable=True
        )
    try:
        candidate = urljoin(f"{api_root}/", raw_url)
        actual = urlsplit(candidate)
        target = urlsplit(expected)
        actual_port = actual.port or (443 if actual.scheme == "https" else 80)
        target_port = target.port or (443 if target.scheme == "https" else 80)
    except (UnicodeError, ValueError) as exc:
        raise VideoGenerationError(
            "OpenRouter video polling URL is invalid", job_id=job_id, recoverable=True
        ) from exc
    if (
        actual.scheme != target.scheme
        or actual.hostname != target.hostname
        or actual_port != target_port
        or actual.path != target.path
        or actual.query
        or actual.fragment
        or actual.username is not None
        or actual.password is not None
        or "\\" in actual.netloc
    ):
        raise VideoGenerationError(
            "OpenRouter video polling URL left the job endpoint", job_id=job_id, recoverable=True
        )
    return expected


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
) -> None:
    if not isinstance(model, str) or not _MODEL_ID.fullmatch(model):
        raise VideoGenerationError("Invalid OpenRouter video model")
    if not isinstance(prompt, str) or not prompt.strip():
        raise VideoGenerationError("Video prompt must not be empty")
    if duration is not None and (
        isinstance(duration, bool) or not isinstance(duration, int) or duration < 1
    ):
        raise VideoGenerationError("Video duration must be a positive whole number")
    if max_duration_seconds is not None:
        if (
            isinstance(max_duration_seconds, bool)
            or not isinstance(max_duration_seconds, int)
            or max_duration_seconds < 1
        ):
            raise VideoGenerationError("Video duration limit must be a positive whole number")
        if duration is not None and duration > max_duration_seconds:
            raise VideoGenerationError("Video duration exceeds the configured limit")
    if aspect_ratio is not None and aspect_ratio not in _ASPECT_RATIOS:
        raise VideoGenerationError("Unsupported video aspect ratio")
    if resolution is not None and resolution not in _RESOLUTIONS:
        raise VideoGenerationError("Unsupported video resolution")
    if output_path.suffix.lower() != ".mp4" or output_path.exists():
        raise VideoGenerationError("Video output path must be a new .mp4 file")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise VideoGenerationError("Video timeout must be positive and finite")
    if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
        raise VideoGenerationError("Video polling interval must be positive and finite")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise VideoGenerationError("Video byte limit must be positive")


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
            raise VideoGenerationError("OpenRouter video JSON response exceeds the size limit")
    try:
        result = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        if isinstance(exc, json.JSONDecodeError):
            exc.doc = ""
        raise VideoGenerationError("OpenRouter video endpoint returned invalid JSON") from None
    if not isinstance(result, dict):
        raise VideoGenerationError("OpenRouter video endpoint returned invalid JSON")
    return result


def _error_text(value: object, *, api_key: str) -> str:
    if isinstance(value, str) and value.strip():
        return redact_upstream_error_text(value.strip(), api_key=api_key)
    return "unknown provider error"


def _publish_video_without_clobber(temporary: Path, output_path: Path, *, job_id: str) -> None:
    """Expose a complete MP4 without replacing a concurrent workspace file."""

    try:
        # Both paths are in the same directory. A successful hard link makes
        # the fully written file visible in one filesystem operation.
        os.link(temporary, output_path)
        return
    except FileExistsError:
        raise VideoGenerationError("Video output path already exists", job_id=job_id) from None
    except OSError:
        # Filesystems and Windows versions report unsupported hard links with
        # different error codes. Exclusive creation below still fails closed
        # when the destination already exists or cannot be written.
        pass

    # Some filesystems do not support hard links. O_EXCL still claims the name
    # atomically, and a failed copy removes only the name claimed by this call.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(output_path, flags, 0o600)
    except FileExistsError:
        raise VideoGenerationError("Video output path already exists", job_id=job_id) from None
    try:
        with os.fdopen(descriptor, "wb") as target, temporary.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise


async def _validate_model_options(
    client: httpx.AsyncClient,
    *,
    api_root: str,
    headers: dict[str, str],
    api_key: str,
    model: str,
    duration: int | None,
    max_duration_seconds: int,
    aspect_ratio: str | None,
    resolution: str | None,
    deadline: float,
) -> int:
    try:
        async with asyncio.timeout(_remaining(deadline)):
            async with client.stream(
                "GET", f"{api_root}/videos/models", headers=headers
            ) as response:
                if response.status_code != 200:
                    raise VideoGenerationError(
                        f"OpenRouter video model catalog returned HTTP {response.status_code}"
                    )
                payload = await _read_json(response)
    except httpx.HTTPError as exc:
        redacted_httpx_error(exc, api_key=api_key)
        raise VideoGenerationError("Could not check OpenRouter video model capabilities") from None
    except TimeoutError:
        raise VideoGenerationError("OpenRouter video model capability check timed out") from None
    entries = payload.get("data")
    if not isinstance(entries, list):
        raise VideoGenerationError("OpenRouter video model catalog is malformed")
    entry = next(
        (item for item in entries if isinstance(item, dict) and item.get("id") == model),
        None,
    )
    if entry is None:
        raise VideoGenerationError(f"OpenRouter video model {model} is not available")
    supported_durations = entry.get("supported_durations")
    if supported_durations is None:
        selected_duration = duration if duration is not None else min(5, max_duration_seconds)
    else:
        if (
            not isinstance(supported_durations, list)
            or not supported_durations
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 1
                for value in supported_durations
            )
        ):
            raise VideoGenerationError(
                f"OpenRouter video model {model} has invalid supported durations"
            )
        if duration is None:
            bounded_durations = [
                value for value in supported_durations if value <= max_duration_seconds
            ]
            if not bounded_durations:
                raise VideoGenerationError(
                    f"OpenRouter video model {model} has no duration within the configured limit"
                )
            selected_duration = min(bounded_durations)
        else:
            selected_duration = duration
            if selected_duration not in supported_durations:
                raise VideoGenerationError(
                    f"OpenRouter video model {model} does not support durations {duration}"
                )
    for field, requested in (
        ("supported_aspect_ratios", aspect_ratio),
        ("supported_resolutions", resolution),
    ):
        if requested is None:
            continue
        supported = entry.get(field)
        if supported is None:
            continue
        if not isinstance(supported, list) or requested not in supported:
            raise VideoGenerationError(
                f"OpenRouter video model {model} does not support "
                f"{field.removeprefix('supported_')} "
                f"{requested}"
            )
    return selected_duration


async def _submit(
    client: httpx.AsyncClient,
    *,
    api_root: str,
    headers: dict[str, str],
    api_key: str,
    model: str,
    prompt: str,
    duration: int,
    aspect_ratio: str | None,
    resolution: str | None,
    deadline: float,
) -> tuple[str, str, dict[str, object]]:
    body: dict[str, object] = {"model": model, "prompt": prompt, "duration": duration}
    if aspect_ratio is not None:
        body["aspect_ratio"] = aspect_ratio
    if resolution is not None:
        body["resolution"] = resolution
    try:
        async with asyncio.timeout(_remaining(deadline)):
            async with client.stream(
                "POST", f"{api_root}/videos", headers=headers, json=body
            ) as response:
                if response.status_code != 202:
                    if response.status_code >= 500:
                        raise VideoGenerationSubmissionUnknown()
                    raise VideoGenerationError(
                        f"OpenRouter video submission returned HTTP {response.status_code}"
                    )
                try:
                    payload = await _read_json(response)
                except VideoGenerationError:
                    raise VideoGenerationSubmissionUnknown() from None
    except httpx.HTTPError as exc:
        redacted_httpx_error(exc, api_key=api_key)
        raise VideoGenerationSubmissionUnknown() from None
    except TimeoutError:
        raise VideoGenerationSubmissionUnknown() from None
    try:
        job_id = _safe_job_id(payload.get("id"))
    except VideoGenerationError:
        raise VideoGenerationSubmissionUnknown() from None
    polling_url = _polling_url(api_root, job_id, payload.get("polling_url"))
    return job_id, polling_url, payload


async def _poll_until_complete(
    client: httpx.AsyncClient,
    *,
    polling_url: str,
    job_id: str,
    headers: dict[str, str],
    api_key: str,
    deadline: float,
    poll_interval_seconds: float,
    initial: dict[str, object] | None = None,
) -> dict[str, object]:
    payload = initial
    while True:
        if payload is None:
            try:
                async with asyncio.timeout(_remaining(deadline, job_id=job_id)):
                    async with client.stream("GET", polling_url, headers=headers) as response:
                        if response.status_code != 200:
                            if response.status_code in {429, 500, 502, 503, 504}:
                                raise VideoGenerationPending(
                                    f"Video job {job_id} status is temporarily unavailable",
                                    job_id=job_id,
                                )
                            raise VideoGenerationError(
                                f"OpenRouter video status returned HTTP {response.status_code}",
                                job_id=job_id,
                            )
                        try:
                            payload = await _read_json(response)
                        except VideoGenerationError as exc:
                            raise VideoGenerationError(
                                str(exc), job_id=job_id, recoverable=True
                            ) from None
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
        if payload.get("id") != job_id:
            raise VideoGenerationError(
                "OpenRouter video status returned a different job ID", job_id=job_id
            )
        status = payload.get("status")
        if status == "completed":
            return payload
        if status in {"failed", "cancelled", "expired"}:
            reason = _error_text(payload.get("error"), api_key=api_key)
            raise VideoGenerationError(f"Video job {job_id} {status}: {reason}", job_id=job_id)
        if status not in {"pending", "in_progress"}:
            raise VideoGenerationError(
                f"OpenRouter video job {job_id} returned an unknown status", job_id=job_id
            )
        payload = None
        await asyncio.sleep(min(poll_interval_seconds, _remaining(deadline, job_id=job_id)))


async def _download(
    client: httpx.AsyncClient,
    *,
    api_root: str,
    job_id: str,
    headers: dict[str, str],
    api_key: str,
    output_path: Path,
    max_bytes: int,
    deadline: float,
) -> int:
    content_url = f"{api_root}/videos/{job_id}/content"
    temporary: Path | None = None
    try:
        async with asyncio.timeout(_remaining(deadline, job_id=job_id)):
            async with client.stream(
                "GET",
                content_url,
                headers={**headers, "Accept": "video/mp4", "Accept-Encoding": "identity"},
            ) as response:
                if response.status_code != 200:
                    if response.status_code in {429, 500, 502, 503, 504}:
                        raise VideoGenerationPending(
                            f"Video job {job_id} content is temporarily unavailable", job_id=job_id
                        )
                    raise VideoGenerationError(
                        f"OpenRouter video content returned HTTP {response.status_code}",
                        job_id=job_id,
                    )
                mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if mime not in {"video/mp4", "application/octet-stream"}:
                    raise VideoGenerationError(
                        f"OpenRouter video content has unsupported type {mime or '<missing>'}",
                        job_id=job_id,
                    )
                length_header = response.headers.get("content-length")
                content_length: int | None = None
                if length_header is not None:
                    try:
                        content_length = int(length_header)
                    except ValueError:
                        raise VideoGenerationError(
                            "OpenRouter video content length is invalid", job_id=job_id
                        ) from None
                    if content_length < 0 or content_length > max_bytes:
                        raise VideoGenerationError(
                            "OpenRouter video exceeds the configured byte limit", job_id=job_id
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
                                "OpenRouter video exceeds the configured byte limit", job_id=job_id
                            )
                        if len(header) < 12:
                            header.extend(chunk[: 12 - len(header)])
                        sink.write(chunk)
                if count < 12 or int.from_bytes(header[:4], "big") < 12 or header[4:8] != b"ftyp":
                    raise VideoGenerationError(
                        "OpenRouter returned invalid MP4 content", job_id=job_id
                    )
                if content_length is not None and count != content_length:
                    raise VideoGenerationError(
                        "OpenRouter video content length did not match the response",
                        job_id=job_id,
                    )
                _publish_video_without_clobber(temporary, output_path, job_id=job_id)
                return count
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
                # The completed output may already be published through a
                # hard link. A failed cleanup must not erase that result.
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
    initial: dict[str, object] | None = None,
    polling_url: str | None = None,
) -> VideoGenerationResult:
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    completed = await _poll_until_complete(
        client,
        polling_url=polling_url or f"{api_root}/videos/{job_id}",
        job_id=job_id,
        headers=headers,
        api_key=api_key,
        deadline=deadline,
        poll_interval_seconds=poll_interval_seconds,
        initial=initial,
    )
    bytes_written = await _download(
        client,
        api_root=api_root,
        job_id=job_id,
        headers=headers,
        api_key=api_key,
        output_path=output_path,
        max_bytes=max_bytes,
        deadline=deadline,
    )
    generation_id = completed.get("generation_id")
    return VideoGenerationResult(
        job_id=job_id,
        output_path=output_path,
        model=model,
        bytes_written=bytes_written,
        generation_id=generation_id if isinstance(generation_id, str) else None,
    )


async def generate_openrouter_video(
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
    poll_interval_seconds: float = 30.0,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> VideoGenerationResult:
    """Submit exactly one paid request, then poll and save its first MP4 output."""

    api_root = _api_root(base_url)
    secret = clean_header_secret(api_key, label="OpenRouter video API key")
    if not secret:
        raise VideoGenerationError("OpenRouter video API key is missing")
    destination = Path(output_path)
    _validate_request(
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
    headers = {"Authorization": f"Bearer {secret}", "Accept": "application/json"}
    async with httpx.AsyncClient(
        timeout=None, trust_env=trust_env(), follow_redirects=False
    ) as client:
        if base_url_matches_official_api("https://openrouter.ai/api/v1", api_root):
            selected_duration = await _validate_model_options(
                client,
                api_root=api_root,
                headers=headers,
                api_key=secret,
                model=model,
                duration=duration,
                max_duration_seconds=max_duration_seconds,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                deadline=deadline,
            )
        else:
            # Compatible gateways may expose the job API without OpenRouter's
            # optional model catalog. The explicit operator route owns checks.
            selected_duration = duration if duration is not None else min(5, max_duration_seconds)
        job_id, polling_url, submitted = await _submit(
            client,
            api_root=api_root,
            headers=headers,
            api_key=secret,
            model=model,
            prompt=prompt,
            duration=selected_duration,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            deadline=deadline,
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
            initial=submitted,
            polling_url=polling_url,
        )


async def resume_openrouter_video(
    *,
    base_url: str,
    api_key: str,
    job_id: str,
    model: str,
    output_path: Path,
    timeout_seconds: float = 600.0,
    poll_interval_seconds: float = 30.0,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> VideoGenerationResult:
    """Complete a known job using GET requests only; never submit a new job."""

    api_root = _api_root(base_url)
    secret = clean_header_secret(api_key, label="OpenRouter video API key")
    if not secret:
        raise VideoGenerationError("OpenRouter video API key is missing")
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
