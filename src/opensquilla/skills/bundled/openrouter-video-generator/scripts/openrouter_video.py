#!/usr/bin/env python3
"""OpenRouter video entrypoint for meta-skill ``skill_exec`` steps."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request

from opensquilla.skills.bundled._provider_http import (
    ProviderHTTPError,
    download_public_https_bytes,
    open_authenticated_request,
    read_limited_response,
    resolve_authenticated_url,
    same_http_origin,
)

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "expired"}
PUBLIC_JOB_STATUSES = TERMINAL_STATUSES | {
    "in_progress",
    "pending",
    "processing",
    "queued",
    "running",
}
SAFE_NO_SUBMIT_EXIT_CODE = 78
META_CAPABILITY_LEASE_REQUIRED_ENV = "OPENSQUILLA_META_CAPABILITY_LEASE_REQUIRED"
META_CAPABILITY_PROVIDER_ENV = "OPENSQUILLA_META_CAPABILITY_PROVIDER"
META_CAPABILITY_API_KEY_ENV = "OPENSQUILLA_META_CAPABILITY_API_KEY"
META_CAPABILITY_BASE_URL_ENV = "OPENSQUILLA_META_CAPABILITY_BASE_URL"
META_CAPABILITY_PROXY_ENV = "OPENSQUILLA_META_CAPABILITY_PROXY"
MAX_PROVIDER_JSON_RESPONSE_BYTES = 1024 * 1024
MAX_VIDEO_DOWNLOAD_BYTES = 256 * 1024 * 1024
MIN_VIDEO_DOWNLOAD_BYTES = 1024
_SAFE_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_SENSITIVE_TOKEN_PREFIXES = ("sk-", "sk_", "bearer")


def _safe_filename(value: str, default: str) -> str:
    name = Path(value or default).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    if not name:
        name = default
    if not name.lower().endswith(".mp4"):
        name = re.sub(r"\.[A-Za-z0-9]+$", "", name) + ".mp4"
    return name


def _preview(text: str) -> str:
    return " ".join(text.split())[:80]


def _safe_job_id(
    value: object,
    *,
    secrets: tuple[str, ...] = (),
) -> str | None:
    """Return one bounded public job identifier, never a key-like value."""

    if isinstance(value, (str, int)) and not isinstance(value, bool):
        candidate = str(value).strip()
        leaks_secret = False
        for raw_secret in secrets:
            secret = str(raw_secret or "").strip()
            if not secret:
                continue
            if hmac.compare_digest(candidate.encode(), secret.encode()):
                leaks_secret = True
                break
            # Provider-controlled identifiers are persisted in run output.
            # Reject meaningful fragments in either direction so a reflected
            # custom-prefix key or JWT cannot bypass prefix-only heuristics.
            if (
                (len(secret) >= 8 and secret in candidate)
                or (len(candidate) >= 8 and candidate in secret)
            ):
                leaks_secret = True
                break
        if (
            _SAFE_JOB_ID.fullmatch(candidate)
            and not candidate.casefold().startswith(_SENSITIVE_TOKEN_PREFIXES)
            and not leaks_secret
        ):
            return candidate
    return None


def _safe_job_status(value: object) -> str:
    """Map provider-controlled status text to the public workflow vocabulary."""

    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in PUBLIC_JOB_STATUSES:
            return candidate
    return "unknown"


def _print_record(label: str, payload: dict[str, object]) -> None:
    print(f"{label}: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}")


def _failure(label: str, filename: str, **extra: object) -> None:
    payload: dict[str, object] = {
        "replacement_slot": f"project/assets/video/{filename}",
    }
    payload.update(extra)
    _print_record(label, payload)


def _failure_reason(exc: BaseException) -> str:
    if isinstance(exc, URLError):
        return exc.reason.__class__.__name__
    return exc.__class__.__name__


def _is_probable_mp4(payload: bytes) -> bool:
    """Reject obvious non-video/error payloads before publishing an MP4 path.

    This intentionally performs a dependency-free container sanity check. It
    does not replace a decoder, but it prevents JSON/HTML/error text or random
    bytes from being persisted and advertised as browser-playable video.
    """

    if len(payload) < MIN_VIDEO_DOWNLOAD_BYTES:
        return False
    offset = 0
    box_types: set[bytes] = set()
    first = True
    while offset < len(payload):
        if offset + 8 > len(payload):
            return False
        box_size = int.from_bytes(payload[offset : offset + 4], "big")
        box_type = payload[offset + 4 : offset + 8]
        header_size = 8
        if box_size == 1:
            if offset + 16 > len(payload):
                return False
            box_size = int.from_bytes(payload[offset + 8 : offset + 16], "big")
            header_size = 16
        elif box_size == 0:
            box_size = len(payload) - offset
        if box_size < header_size or offset + box_size > len(payload):
            return False
        if first and box_type != b"ftyp":
            return False
        first = False
        box_types.add(box_type)
        offset += box_size
    return b"mdat" in box_types and bool({b"moov", b"moof"} & box_types)


def _runtime_connection(args: argparse.Namespace) -> tuple[str, str, str, bool]:
    """Resolve a MetaSkill lease or the direct-CLI compatibility inputs."""

    lease_required = os.environ.get(META_CAPABILITY_LEASE_REQUIRED_ENV) == "1"
    if lease_required:
        provider = os.environ.get(META_CAPABILITY_PROVIDER_ENV, "").strip().lower()
        if provider != "openrouter":
            return "", "", "", True
        return (
            os.environ.get(META_CAPABILITY_API_KEY_ENV, "").strip(),
            os.environ.get(META_CAPABILITY_BASE_URL_ENV, "").strip().rstrip("/"),
            os.environ.get(META_CAPABILITY_PROXY_ENV, "").strip(),
            True,
        )
    api_key_env = args.api_key_env.strip() or "OPENROUTER_API_KEY"
    return (
        str(args.api_key.strip() or os.environ.get(api_key_env, "")),
        args.base_url.strip().rstrip("/"),
        "",
        False,
    )


def _open_url(request: Request, *, timeout: float, proxy: str):
    return open_authenticated_request(
        request,
        timeout=timeout,
        proxy=proxy,
    )


def _request_json(
    url: str,
    *,
    key: str,
    method: str = "GET",
    body: dict[str, object] | None = None,
    timeout: float = 60.0,
    proxy: str = "",
) -> dict[str, object]:
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    with _open_url(req, timeout=timeout, proxy=proxy) as resp:
        raw = read_limited_response(
            resp,
            max_bytes=MAX_PROVIDER_JSON_RESPONSE_BYTES,
            error_message="provider JSON response exceeds size limit",
        )
        parsed = json.loads(raw.decode("utf-8", "replace"))
    if not isinstance(parsed, dict):
        raise ValueError("response_not_object")
    return parsed


def _resolve_url(url: str, *, base_url: str) -> str:
    return urljoin(f"{base_url.rstrip('/')}/", url)


def _same_origin(url: str, *, base_url: str) -> bool:
    return same_http_origin(url, base_url)


def _download(
    url: str,
    *,
    key: str,
    base_url: str,
    timeout: float = 120.0,
    proxy: str = "",
) -> bytes:
    resolved_url = _resolve_url(url, base_url=base_url)
    return download_public_https_bytes(
        resolved_url,
        timeout=timeout,
        max_bytes=MAX_VIDEO_DOWNLOAD_BYTES,
        proxy=proxy,
        authorization=f"Bearer {key}" if key else "",
        authorization_base_url=base_url,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="https://openrouter.ai/api/v1")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--api-key-env", default="OPENROUTER_API_KEY")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--filename", default="intro.mp4")
    parser.add_argument("--duration", type=int, default=10)
    parser.add_argument("--aspect-ratio", default="16:9")
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--max-wait", type=float, default=300.0)
    args = parser.parse_args()

    filename = _safe_filename(args.filename, "intro.mp4")
    prompt = sys.stdin.read().strip()
    if not prompt:
        prompt = "Create a short browser-playable video for this webpage."

    key, base_url, proxy, lease_required = _runtime_connection(args)
    missing = []
    if not key:
        missing.append(
            "provider_connection:openrouter"
            if lease_required
            else (args.api_key_env.strip() or "OPENROUTER_API_KEY")
        )
    if not base_url:
        missing.append("provider_endpoint:openrouter")
    if not args.model:
        missing.append("awesome_webpage.openrouter.models.video_generation")
    if not args.output_dir:
        missing.append("awesome_webpage.output_dir")
    if missing:
        _failure("VIDEO_CONFIG_NEEDED", filename, missing=missing)
        return SAFE_NO_SUBMIT_EXIT_CODE if lease_required else 0

    output_dir = Path(args.output_dir).expanduser()
    output_path = output_dir / filename
    local_path = f"project/assets/video/{filename}"
    try:
        submit = _request_json(
            f"{base_url}/videos",
            key=key,
            method="POST",
            body={
                "model": args.model,
                "prompt": prompt,
                "duration": args.duration,
                "aspect_ratio": args.aspect_ratio,
            },
            proxy=proxy,
        )
    except HTTPError as exc:
        _failure("VIDEO_GENERATION_FAILED", filename, phase="submit", status=exc.code)
        return 0
    except (URLError, TimeoutError, ValueError) as exc:
        _failure(
            "VIDEO_GENERATION_FAILED",
            filename,
            phase="submit",
            reason=_failure_reason(exc),
        )
        return 0

    job_id = _safe_job_id(submit.get("id"), secrets=(key,))
    if job_id is None:
        _failure(
            "VIDEO_GENERATION_FAILED",
            filename,
            phase="submit",
            reason="invalid_job_id",
        )
        return 0
    try:
        poll_url = resolve_authenticated_url(
            str(submit.get("polling_url") or f"videos/{job_id}"),
            base_url=base_url,
        )
    except ProviderHTTPError:
        _failure(
            "VIDEO_GENERATION_FAILED",
            filename,
            phase="poll",
            reason="unsafe_polling_url",
            job_id=job_id,
        )
        return 0
    last = submit
    status = _safe_job_status(last.get("status"))
    deadline = time.time() + max(1.0, args.max_wait)
    while status not in TERMINAL_STATUSES and time.time() < deadline:
        time.sleep(max(1.0, args.poll_interval))
        try:
            last = _request_json(poll_url, key=key, proxy=proxy)
        except HTTPError as exc:
            _failure(
                "VIDEO_GENERATION_FAILED",
                filename,
                phase="poll",
                status=exc.code,
                job_id=job_id,
            )
            return 0
        except (URLError, TimeoutError, ValueError) as exc:
            _failure(
                "VIDEO_GENERATION_FAILED",
                filename,
                phase="poll",
                reason=_failure_reason(exc),
                job_id=job_id,
            )
            return 0
        status = _safe_job_status(last.get("status"))

    if status != "completed":
        _failure("VIDEO_GENERATION_FAILED", filename, status=status, job_id=job_id)
        return 0

    urls = last.get("unsigned_urls") or last.get("urls") or []
    if not isinstance(urls, list) or not urls:
        _failure("VIDEO_MODEL_UNSUPPORTED", filename, reason="no_download_url", job_id=job_id)
        return 0

    try:
        download_url = _resolve_url(str(urls[0]), base_url=base_url)
        body = _download(
            download_url,
            key=key,
            base_url=base_url,
            proxy=proxy,
        )
    except HTTPError as exc:
        _failure(
            "VIDEO_GENERATION_FAILED",
            filename,
            phase="download",
            status=exc.code,
            job_id=job_id,
        )
        return 0
    except (URLError, TimeoutError, ProviderHTTPError) as exc:
        _failure(
            "VIDEO_GENERATION_FAILED",
            filename,
            phase="download",
            reason=_failure_reason(exc),
            job_id=job_id,
        )
        return 0

    if not _is_probable_mp4(body):
        _failure(
            "VIDEO_GENERATION_FAILED",
            filename,
            phase="validate",
            reason="invalid_video_payload",
            job_id=job_id,
        )
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(body)
    _print_record(
        "VIDEO_READY",
        {
            "local_path": local_path,
            "mime": "video/mp4",
            "duration_s": args.duration,
            "resolution": None,
            "prompt_preview": _preview(prompt),
            "job_id": job_id,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
