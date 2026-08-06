#!/usr/bin/env python3
"""Generate a short video via Seedance 2.0 (OpenRouter or Volcengine/BytePlus).

Two providers are supported with the same submit-then-poll lifecycle but
slightly different request/response shapes. Select with --provider.

  openrouter  (default)
    POST https://openrouter.ai/api/v1/videos
    Body:  {model, prompt, aspect_ratio, duration,
            frame_images?, input_references?}
    Resp:  {id, polling_url, status}
    Auth:  Authorization: Bearer $OPENROUTER_API_KEY
    Poll:  GET a same-origin polling_url, otherwise <base>/videos/<id>
    Done:  status in {completed}; download from top-level unsigned_urls[0]
    Models e.g. bytedance/seedance-2.0, bytedance/seedance-2.0-fast.

  volcengine                                  (CN region, official Ark)
  byteplus                                    (international, BytePlus ModelArk)
    POST https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks
         (or https://ark.ap-southeast.bytepluses.com/api/v3/...)
    Body:  {model, content: [{type:"text", text:"..."},
                              {type:"image_url", image_url:{url:"..."}}],
            resolution, ratio, duration, watermark:false}
    Resp:  {id}
    Auth:  Authorization: Bearer $ARK_API_KEY
    Poll:  GET <base>/contents/generations/tasks/<id>
    Done:  status in {succeeded}; download from content.video_url
    Models e.g. doubao-seedance-2-0-260128 (CN),
                dreamina-seedance-2-0-260128 (intl).

Usage:
    python generate_video.py --prompt "..." --filename "out.mp4" \\
        [--provider openrouter|volcengine|byteplus] \\
        [--input-image PATH] [--input-reference PATH] \\
        [--aspect-ratio 9:16] [--duration 5] [--resolution 720p] \\
        [--model MODEL_ID] [--api-key KEY] [--base-url URL]

On success stdout contains the absolute MP4 path followed by a sanitized
``VIDEO_GENERATION_RECEIPT`` JSON line. The same receipt is persisted beside
the video as ``<filename>.receipt.json``.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

import httpx

from opensquilla.env import trust_env as _trust_env
from opensquilla.tools.ssrf import (
    environment_proxy_url,
    validate_http_url_for_fetch,
)
from opensquilla.tools.ssrf import (
    pinned_transport as _pinned_transport,
)
from opensquilla.tools.types import SSRFBlockedError

# Contract with the bundled meta skill_exec wrapper: this non-zero exit is
# emitted only before a provider POST can be attempted. Do not use it after
# entering `_run_attempt`.
SAFE_NO_SUBMIT_EXIT_CODE = 78
# Exact bundled executor contract. These exits identify credential-account
# failures without trusting provider prose or serializing credentials. A paid
# submission may already exist, so the current run never retries; the parent
# only rotates a profile-pool key for a later explicit run.
PROVIDER_AUTH_INVALID_EXIT_CODE = 79
PROVIDER_INSUFFICIENT_CREDITS_EXIT_CODE = 80
PROVIDER_RATE_LIMITED_EXIT_CODE = 81

TERMINAL_STATES = {
    "completed", "succeeded",
    "failed", "cancelled", "expired",
}
SUCCESS_STATES = {"completed", "succeeded"}

# Submit and poll payloads are small control-plane JSON. Provider video bytes
# are streamed separately and bounded high enough for a 15-second 1080p clip.
MAX_PROVIDER_JSON_RESPONSE_BYTES = 1024 * 1024
MAX_VIDEO_DOWNLOAD_BYTES = 256 * 1024 * 1024
_RESPONSE_READ_CHUNK_BYTES = 64 * 1024
_VIDEO_READ_CHUNK_BYTES = 1024 * 1024
META_CAPABILITY_PROVIDER_ENV = "OPENSQUILLA_META_CAPABILITY_PROVIDER"
META_CAPABILITY_API_KEY_ENV = "OPENSQUILLA_META_CAPABILITY_API_KEY"
META_CAPABILITY_BASE_URL_ENV = "OPENSQUILLA_META_CAPABILITY_BASE_URL"
META_CAPABILITY_PROXY_ENV = "OPENSQUILLA_META_CAPABILITY_PROXY"
META_OPENROUTER_API_KEY_ENV = "OPENSQUILLA_META_OPENROUTER_API_KEY"
_MEDIA_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_MEDIA_REDIRECTS = 5

# Mirrors src/opensquilla/provider/openrouter_attribution.py — kept inline so
# this script can run as a standalone subprocess without importing the
# opensquilla package. Volcengine / BytePlus URLs DO NOT receive these
# headers (the predicate gates by host).
_OPENROUTER_APP_REFERER = "https://opensquilla.ai"
_OPENROUTER_APP_TITLE = "OpenSquilla"


def _is_openrouter_url(url: str | None) -> bool:
    if not url:
        return False
    raw = url.strip()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower()
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def _openrouter_attribution_headers(url: str | None) -> dict[str, str]:
    if not _is_openrouter_url(url):
        return {}
    return {
        "HTTP-Referer": _OPENROUTER_APP_REFERER,
        "X-Title": _OPENROUTER_APP_TITLE,
    }


# -------- provider config -----------------------------------------------------


@dataclass(frozen=True)
class Provider:
    name: str
    default_base_url: str
    default_model: str
    default_env: tuple[str, ...]
    submit_path: str
    polls_url_in_response: bool  # True = use submit response's polling_url;
                                 # False = construct from id
    build_payload: Callable[[Args], dict]
    extract_url: Callable[[dict], str | None]


def _build_openrouter_payload(args: Args) -> dict:
    user_prompt = args.prompt
    payload: dict = {
        "model": args.model,
        "prompt": user_prompt,
        "aspect_ratio": args.aspect_ratio,
        "duration": int(args.duration),
        # Seedance supports synchronized audio generation. Be explicit so a
        # real user run does not silently depend on a provider-side default.
        "generate_audio": True,
    }
    # frame_images locks the literal first/last frame.
    # input_references is a softer identity/style anchor — same picture can be
    # shared across multiple shots to keep the character consistent.
    # If both are provided OpenRouter prefers frame_images.
    if args.input_image:
        payload["frame_images"] = [
            {
                "type": "image_url",
                "image_url": {"url": _encode_input_image(args.input_image)},
                "frame_type": "first_frame",
            }
        ]
    elif args.input_references:
        refs = [r for r in args.input_references if r]
        if refs:
            payload["input_references"] = [
                {
                    "type": "image_url",
                    "image_url": {"url": _encode_input_image(r)},
                }
                for r in refs
            ]
    return payload


def _build_volcengine_payload(args: Args) -> dict:
    """Volcengine ARK / BytePlus ModelArk shape — content[] array."""
    content: list = [{"type": "text", "text": args.prompt}]
    # First-frame image
    if args.input_image:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": _encode_input_image(args.input_image)},
                # ARK uses a "role" field for first/last frame, optional
                "role": "first_frame",
            }
        )
    # Style/identity references (no role marker — just stacked images)
    for ref in args.input_references or []:
        if ref:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _encode_input_image(ref)},
                }
            )
    payload: dict = {
        "model": args.model,
        "content": content,
        "ratio": args.aspect_ratio,
        "resolution": args.resolution,
        "duration": int(args.duration),
        "watermark": False,
    }
    return payload


def _extract_openrouter_url(job: dict) -> str | None:
    """Top-level unsigned_urls[0] is the canonical OpenRouter path."""
    # Top-level url lists are where OpenRouter currently puts the video.
    for key in ("unsigned_urls", "urls"):
        urls = job.get(key) or []
        if isinstance(urls, list) and urls:
            first = urls[0]
            if isinstance(first, str):
                return first
            if isinstance(first, dict):
                first_url = first.get("url")
                if isinstance(first_url, str):
                    return first_url
    # Nested videos[] (older shape some routes still emit)
    videos = job.get("videos") or job.get("output") or []
    if isinstance(videos, dict):
        videos = [videos]
    for v in videos if isinstance(videos, list) else []:
        if not isinstance(v, dict):
            continue
        for key in ("url", "content_url", "download_url"):
            url = v.get(key)
            if isinstance(url, str) and url:
                return url
        for key in ("video_url", "videoUrl"):
            obj = v.get(key)
            if isinstance(obj, dict):
                nested_url = obj.get("url")
                if isinstance(nested_url, str):
                    return nested_url
            if isinstance(obj, str) and obj:
                return obj
    # Scalar top-level
    for key in ("content_url", "download_url", "url"):
        url = job.get(key)
        if isinstance(url, str) and url:
            return url
    return None


def _extract_volcengine_url(job: dict) -> str | None:
    """Volcengine puts the final URL at content.video_url."""
    content = job.get("content") or {}
    if isinstance(content, dict):
        url = content.get("video_url")
        if isinstance(url, str) and url:
            return url
    return _extract_openrouter_url(job)  # last-resort, schema can drift


PROVIDERS: dict[str, Provider] = {
    "openrouter": Provider(
        name="openrouter",
        default_base_url="https://openrouter.ai/api/v1",
        default_model="bytedance/seedance-2.0",
        default_env=("OPENROUTER_API_KEY",),
        submit_path="/videos",
        polls_url_in_response=True,
        build_payload=_build_openrouter_payload,
        extract_url=_extract_openrouter_url,
    ),
    "volcengine": Provider(
        name="volcengine",
        default_base_url="https://ark.cn-beijing.volces.com/api/v3",
        default_model="doubao-seedance-2-0-260128",
        default_env=("ARK_API_KEY", "VOLC_ARK_API_KEY"),
        submit_path="/contents/generations/tasks",
        polls_url_in_response=False,
        build_payload=_build_volcengine_payload,
        extract_url=_extract_volcengine_url,
    ),
    "byteplus": Provider(
        name="byteplus",
        default_base_url="https://ark.ap-southeast.bytepluses.com/api/v3",
        default_model="dreamina-seedance-2-0-260128",
        default_env=("ARK_API_KEY", "BYTEPLUS_API_KEY"),
        submit_path="/contents/generations/tasks",
        polls_url_in_response=False,
        build_payload=_build_volcengine_payload,
        extract_url=_extract_volcengine_url,
    ),
}


# -------- helpers -------------------------------------------------------------


@dataclass
class Args:
    """Typed mirror of argparse.Namespace, used by per-provider builders."""
    prompt: str
    model: str
    aspect_ratio: str
    duration: int
    resolution: str
    input_image: str
    input_references: list[str]


@dataclass(frozen=True)
class _ProviderConnection:
    """One atomic provider credential and endpoint selected before submit."""

    provider: str
    api_key: str = field(repr=False)
    base_url: str
    proxy: str = field(default="", repr=False)


def _encode_input_image(path: str) -> str:
    raw = Path(path).read_bytes()
    suffix = Path(path).suffix.lower().lstrip(".")
    mime = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }.get(suffix, "image/png")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _resolve_api_key(
    provided: str | None,
    env_names: Iterable[str],
    *,
    provider_name: str = "",
) -> str | None:
    if provided:
        key = provided.strip()
        if key:
            return key
    parent_provider = (os.environ.get(META_CAPABILITY_PROVIDER_ENV) or "").strip()
    parent_key = (os.environ.get(META_CAPABILITY_API_KEY_ENV) or "").strip()
    parent_base = (os.environ.get(META_CAPABILITY_BASE_URL_ENV) or "").strip()
    if (
        provider_name
        and parent_provider.lower() == provider_name.lower()
        and parent_key
        and parent_base
    ):
        return parent_key
    if provider_name == "openrouter":
        # This name is populated only by the parent MetaOrchestrator from the
        # active Gateway config. The child must not rediscover config from its
        # workspace cwd or honor a workspace-selected arbitrary api_key_env.
        parent_key = (os.environ.get(META_OPENROUTER_API_KEY_ENV) or "").strip()
        if parent_key:
            return parent_key
    for name in env_names:
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    return None


class _RequestError(RuntimeError):
    """A network/protocol failure whose message is safe to expose to users."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        provider_code: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.provider_code = _safe_policy_code(provider_code)
        self.retryable = retryable


def _raise_without_context(error: BaseException) -> NoReturn:
    """Raise a public error without retaining the untrusted exception chain.

    ``raise error from None`` suppresses traceback rendering but normally leaves
    the original exception reachable through ``error.__context__``.  Clearing
    both links in ``finally`` keeps signed URLs, response bodies, headers, and
    parser input unavailable through the public exception's cause/context chain.
    """

    try:
        raise error from None
    finally:
        error.__cause__ = None
        error.__context__ = None


def _status_is_retryable(status: int | None) -> bool:
    """Only throttling and server failures are safe transient classifications."""
    return status == 429 or (status is not None and 500 <= status <= 599)


@dataclass(frozen=True)
class _Origin:
    scheme: str
    host: str
    port: int


def _url_origin(url: str, *, trusted_base: bool = False) -> _Origin:
    """Return a canonical origin or a safe error without echoing the URL."""
    raw = str(url or "").strip()
    try:
        if not raw or any(char.isspace() or ord(char) < 0x20 for char in raw):
            raise ValueError
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").rstrip(".").lower()
        if (
            scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or "\\" in parsed.netloc
        ):
            raise ValueError
        if trusted_base and (parsed.query or parsed.fragment):
            raise ValueError
        explicit_port = parsed.port
        if explicit_port is not None and not 0 < explicit_port <= 65535:
            raise ValueError
        port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
    except (TypeError, ValueError):
        label = "trusted API base" if trusted_base else "authenticated endpoint"
        _raise_without_context(_RequestError(f"invalid {label} URL"))
    return _Origin(scheme=scheme, host=host, port=port)


def _require_trusted_api_url(url: str, trusted_base_url: str) -> None:
    """Fail closed unless an authenticated endpoint matches the API origin."""
    trusted = _url_origin(trusted_base_url, trusted_base=True)
    candidate = _url_origin(url)
    if candidate != trusted:
        raise _RequestError("refusing authenticated request outside trusted API origin")


def _clean_api_key(value: object) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in candidate):
        raise _RequestError("invalid API credential")
    return candidate


def _validated_api_base_url(value: str) -> str:
    candidate = str(value or "").strip()
    _url_origin(candidate, trusted_base=True)
    return candidate.rstrip("/")


def _require_same_api_origin(stored_base_url: str, candidate_base_url: str) -> None:
    if _url_origin(stored_base_url, trusted_base=True) != _url_origin(
        candidate_base_url,
        trusted_base=True,
    ):
        raise _RequestError(
            "refusing to send configured credential outside its API origin"
        )


def _validated_proxy_url(value: str) -> str:
    """Validate an explicit provider-API proxy without disclosing credentials."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    if any(char.isspace() or ord(char) < 0x20 for char in raw):
        raise _RequestError("invalid provider API proxy")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (TypeError, UnicodeError, ValueError):
        raise _RequestError("invalid provider API proxy") from None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
        or "\\" in parsed.netloc
        or (port is not None and not 0 < port <= 65535)
    ):
        raise _RequestError("invalid provider API proxy")
    return raw


def _resolve_provider_connection(
    provider: Provider,
    provided_key: str | None,
    requested_base_url: str,
) -> _ProviderConnection | None:
    """Resolve key, endpoint, and proxy as one pre-submit connection."""

    requested = str(requested_base_url or "").strip()
    if provided_key and provided_key.strip():
        return _ProviderConnection(
            provider=provider.name,
            api_key=_clean_api_key(provided_key),
            base_url=_validated_api_base_url(requested or provider.default_base_url),
        )

    parent_provider = (os.environ.get(META_CAPABILITY_PROVIDER_ENV) or "").strip()
    parent_key_raw = os.environ.get(META_CAPABILITY_API_KEY_ENV) or ""
    parent_base = (os.environ.get(META_CAPABILITY_BASE_URL_ENV) or "").strip()
    parent_proxy = (os.environ.get(META_CAPABILITY_PROXY_ENV) or "").strip()
    if any((parent_provider, parent_key_raw.strip(), parent_base, parent_proxy)):
        if not parent_provider or not parent_key_raw.strip() or not parent_base:
            raise _RequestError("incomplete parent provider connection")
        if parent_provider.lower() != provider.name:
            raise _RequestError("parent provider connection does not match selected provider")
        stored_base = _validated_api_base_url(parent_base)
        base_url = _validated_api_base_url(requested or stored_base)
        _require_same_api_origin(stored_base, base_url)
        return _ProviderConnection(
            provider=provider.name,
            api_key=_clean_api_key(parent_key_raw),
            base_url=base_url,
            proxy=_validated_proxy_url(parent_proxy),
        )

    if provider.name == "openrouter":
        legacy_parent_key = _clean_api_key(
            os.environ.get(META_OPENROUTER_API_KEY_ENV) or ""
        )
        if legacy_parent_key:
            base_url = _validated_api_base_url(
                requested or provider.default_base_url
            )
            _require_same_api_origin(provider.default_base_url, base_url)
            return _ProviderConnection(
                provider=provider.name,
                api_key=legacy_parent_key,
                base_url=base_url,
            )

    for env_name in provider.default_env:
        canonical_key = _clean_api_key(os.environ.get(env_name) or "")
        if not canonical_key:
            continue
        base_url = _validated_api_base_url(requested or provider.default_base_url)
        _require_same_api_origin(provider.default_base_url, base_url)
        return _ProviderConnection(
            provider=provider.name,
            api_key=canonical_key,
            base_url=base_url,
        )
    return None


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not forward Authorization through HTTP redirects."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _open_authenticated_request(
    request: urllib.request.Request,
    *,
    timeout: int,
    proxy_url: str = "",
) -> Any:
    """Open one authenticated request through only its selected API proxy."""
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else {}
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(proxies),
        _NoRedirectHandler(),
    )
    return opener.open(request, timeout=timeout)


def _declared_content_length(response: object) -> int | None:
    """Return a valid non-negative Content-Length without exposing headers."""

    headers = getattr(response, "headers", None)
    get_header = getattr(headers, "get", None)
    if not callable(get_header):
        return None
    value = get_header("Content-Length")
    if not isinstance(value, str):
        return None
    try:
        length = int(value.strip())
    except ValueError:
        return None
    return length if length >= 0 else None


def _read_limited_response(
    response: object,
    *,
    max_bytes: int,
    error_message: str,
) -> bytes:
    """Read an HTTP response with declared and cumulative byte bounds."""

    declared = _declared_content_length(response)
    if declared is not None and declared > max_bytes:
        raise _RequestError(error_message)
    read = getattr(response, "read", None)
    if not callable(read):
        raise _RequestError("provider returned an unreadable response")

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = read(min(_RESPONSE_READ_CHUNK_BYTES, max_bytes - total + 1))
        if not isinstance(chunk, bytes):
            raise _RequestError("provider returned an unreadable response")
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise _RequestError(error_message)
        chunks.append(chunk)
    return b"".join(chunks)


def _http_request(
    method: str,
    url: str,
    api_key: str,
    trusted_base_url: str,
    body: dict | None = None,
    timeout: int = 120,
    proxy_url: str = "",
) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers: dict[str, str] = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        _require_trusted_api_url(url, trusted_base_url)
        safe_proxy_url = _validated_proxy_url(proxy_url)
        headers.update(_openrouter_attribution_headers(url))
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        if safe_proxy_url:
            response = _open_authenticated_request(
                req,
                timeout=timeout,
                proxy_url=safe_proxy_url,
            )
        else:
            response = _open_authenticated_request(req, timeout=timeout)
        with response as resp:
            raw = _read_limited_response(
                resp,
                max_bytes=MAX_PROVIDER_JSON_RESPONSE_BYTES,
                error_message="provider JSON response exceeds size limit",
            )
    except _RequestError:
        raise
    except urllib.error.HTTPError as exc:
        # Never include the response body or request URL in the exception. Both
        # commonly contain provider diagnostics, request ids, or signed query
        # parameters that must not reach logs. Parse at most one strict policy
        # code for the sanitized failure receipt; discard everything else.
        provider_code = _provider_code_from_http_error(exc)
        _raise_without_context(
            _RequestError(
                f"HTTP {exc.code}",
                status=exc.code,
                provider_code=provider_code,
                # POST creates a potentially billed job and has no provider
                # idempotency contract. GET polling remains safe to retry.
                retryable=(method.upper() != "POST" and _status_is_retryable(exc.code)),
            )
        )
    except Exception:
        _raise_without_context(
            _RequestError(
                "network request failed",
                retryable=method.upper() != "POST",
            )
        )

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _raise_without_context(_RequestError("provider returned invalid JSON"))
    if not isinstance(parsed, dict):
        raise _RequestError("provider returned a non-object JSON response")
    return parsed


def _validated_download_target(url: str) -> tuple[str, list[str]]:
    """Return a public HTTPS URL and the exact IPs approved for connection."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").lower()
        if (
            parsed.scheme.lower() != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError
        parsed.port  # Validate a syntactically valid numeric port.
    except (TypeError, ValueError):
        _raise_without_context(_RequestError("invalid or insecure media URL"))
    url_host = f"[{host}]" if ":" in host else host
    netloc = f"{url_host}:{parsed.port}" if parsed.port is not None else url_host
    safe_url = urlunsplit(("https", netloc, parsed.path, parsed.query, ""))
    try:
        vetted_ips = validate_http_url_for_fetch(safe_url)
    except SSRFBlockedError:
        _raise_without_context(_RequestError("refusing non-public media host"))
    except (OSError, ValueError):
        _raise_without_context(
            _RequestError("media host resolution failed", retryable=True)
        )
    if not vetted_ips:
        raise _RequestError("media host has no usable address", retryable=True)
    return safe_url, vetted_ips


def _validate_download_url(url: str) -> str:
    """Return a fragment-free public HTTPS media URL or fail closed."""

    return _validated_download_target(url)[0]


def _remove_partial_download(destination: Path) -> None:
    """Best-effort cleanup for a private, unpublished provider candidate."""

    try:
        destination.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # Preserve the original safe network/size failure. The caller's
        # TemporaryDirectory cleanup gets another chance to remove the file.
        pass


async def _download_url_to_path_async(
    url: str,
    destination: Path,
    timeout: int,
) -> None:
    """Stream media through the shared DNS-pinned HTTPS transport."""

    current_url = url
    for redirect_count in range(_MAX_MEDIA_REDIRECTS + 1):
        safe_url, vetted_ips = _validated_download_target(current_url)
        # The shared transport connects to one vetted IP while preserving the
        # original hostname in Host, TLS SNI, and certificate verification.
        # Thus the HTTP client never performs an unguarded second DNS lookup.
        transport_kwargs: dict[str, object] = {}
        if _trust_env():
            proxy_url = environment_proxy_url(safe_url)
            if proxy_url is not None:
                transport_kwargs["proxy"] = proxy_url
        transport = _pinned_transport(safe_url, vetted_ips, **transport_kwargs)
        client_kwargs: dict[str, Any] = {
            "timeout": float(timeout),
            "follow_redirects": False,
            "trust_env": False,
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        async with httpx.AsyncClient(**client_kwargs) as client:
            async with client.stream(
                "GET",
                safe_url,
                headers={"Accept-Encoding": "identity"},
            ) as response:
                if response.status_code in _MEDIA_REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise _RequestError("provider media redirect was missing a target")
                    if redirect_count >= _MAX_MEDIA_REDIRECTS:
                        raise _RequestError("provider media returned too many redirects")
                    current_url = urljoin(safe_url, location)
                    continue
                if response.status_code >= 400:
                    raise _RequestError(
                        f"HTTP {response.status_code}",
                        status=response.status_code,
                        retryable=_status_is_retryable(response.status_code),
                    )
                declared = _declared_content_length(response)
                if declared is not None and declared > MAX_VIDEO_DOWNLOAD_BYTES:
                    raise _RequestError("provider media exceeds download size limit")
                total = 0
                with destination.open("wb") as output:
                    async for chunk in response.aiter_bytes(_VIDEO_READ_CHUNK_BYTES):
                        if not isinstance(chunk, bytes):
                            raise _RequestError("provider returned unreadable media")
                        total += len(chunk)
                        if total > MAX_VIDEO_DOWNLOAD_BYTES:
                            raise _RequestError("provider media exceeds download size limit")
                        output.write(chunk)
                return
    raise _RequestError("provider media returned too many redirects")


def _download_url_to_path(
    url: str,
    api_key: str,
    destination: Path,
    timeout: int,
) -> None:
    """Download provider media into a private candidate path.

    The caller validates this candidate with ffprobe before atomically
    publishing it at the user-visible output path.
    """
    del api_key  # Downloads are intentionally anonymous, including OpenRouter URLs.
    try:
        asyncio.run(_download_url_to_path_async(url, destination, timeout))
    except _RequestError:
        _remove_partial_download(destination)
        raise
    except Exception:
        _remove_partial_download(destination)
        _raise_without_context(_RequestError("media download failed", retryable=True))


@dataclass(frozen=True)
class _ProviderProblem:
    status: int | None
    code: str | None

    @property
    def retryable(self) -> bool:
        return _status_is_retryable(self.status)

    def summary(self, phase: str) -> str:
        details: list[str] = []
        if self.status is not None:
            details.append(f"HTTP {self.status}")
        if self.code:
            details.append(f"code={self.code}")
        suffix = f" ({', '.join(details)})" if details else ""
        return f"{phase} rejected by provider{suffix}"


_SAFE_ERROR_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}")
_SAFE_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_SAFE_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_SENSITIVE_TOKEN_PREFIXES = ("sk-", "sk_", "bearer")
_IDENTIFIER_CODE_PREFIXES = (
    "req-",
    "req_",
    "request-id-",
    "request_id_",
    "job-",
    "job_",
    "trace-",
    "trace_",
)
_PUBLIC_JOB_STATES = TERMINAL_STATES | {
    "created",
    "queued",
    "pending",
    "processing",
    "running",
    "in_progress",
}


def _safe_error_code(value: object) -> str | None:
    if isinstance(value, str):
        candidate = value.strip()
        if (
            _SAFE_ERROR_CODE.fullmatch(candidate)
            and not candidate.casefold().startswith(_SENSITIVE_TOKEN_PREFIXES)
        ):
            return candidate
    return None


def _safe_policy_code(value: object) -> str | None:
    """Return one bounded policy identifier, never a secret or request id."""

    candidate = _safe_error_code(value)
    if candidate is None:
        return None
    folded = candidate.casefold()
    if folded.startswith(_IDENTIFIER_CODE_PREFIXES):
        return None
    if not any(
        marker in folded
        for marker in ("policy", "privacy", "sensitive", "moderation", "safety", "filter")
    ):
        return None
    return candidate


def _safe_job_status(value: object, *, default: str) -> str:
    if not isinstance(value, str):
        return default
    candidate = value.strip().lower()
    return candidate if candidate in _PUBLIC_JOB_STATES else default


def _provider_problem(response: dict) -> _ProviderProblem | None:
    """Parse a provider error envelope without retaining its raw message."""
    error = response.get("error")
    if error in (None, "", False):
        return None

    status: int | None = None
    code: str | None = None
    messages: list[str] = []
    if isinstance(error, dict):
        for key in ("status", "status_code", "http_status", "code"):
            value = error.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                status = value
                break
            if isinstance(value, str) and value.strip().isdigit():
                status = int(value.strip())
                break
        for key in ("provider_code", "type", "code"):
            code = _safe_policy_code(error.get(key))
            if code:
                break
        for key in ("message", "detail"):
            value = error.get(key)
            if isinstance(value, str):
                messages.append(value)
    elif isinstance(error, str):
        messages.append(error)

    # Some OpenRouter routes return HTTP 200 with an inner error whose message
    # embeds the real HTTP status and provider policy code. Extract only those
    # strict tokens; never surface the message itself.
    for message in messages:
        if status is None:
            match = re.search(r"\bHTTP\s+(\d{3})\b", message)
            if match:
                status = int(match.group(1))
        if code is None:
            match = re.search(
                r"(?:[\"']?code[\"']?)\s*[:=]\s*[\"']?"
                r"([A-Za-z][A-Za-z0-9_.-]{0,127})",
                message,
            )
            if match:
                code = _safe_policy_code(match.group(1))
    return _ProviderProblem(status=status, code=code)


def _provider_code_from_http_error(exc: urllib.error.HTTPError) -> str | None:
    """Extract one allowlisted provider code without retaining HTTP error text."""

    try:
        raw = exc.read(64 * 1024)
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(payload, dict):
        return None
    problem = _provider_problem(payload)
    return problem.code if problem is not None else None


def _safe_job_id(value: object) -> str | None:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        candidate = str(value).strip()
        if (
            _SAFE_JOB_ID.fullmatch(candidate)
            and not candidate.casefold().startswith(_SENSITIVE_TOKEN_PREFIXES)
        ):
            return candidate
    return None


def _safe_model_id(value: str) -> str | None:
    candidate = value.strip()
    return candidate if _SAFE_MODEL_ID.fullmatch(candidate) else None


class _MediaValidationError(RuntimeError):
    """Downloaded media did not satisfy the public video contract."""


def _duration_value(data: object) -> float | None:
    if isinstance(data, bool) or not isinstance(data, (str, int, float)):
        return None
    try:
        value = float(data)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _probe_video(
    path: Path,
    *,
    expected_duration_s: float,
    duration_tolerance_s: float,
) -> dict[str, int | float | bool | str]:
    """Validate a candidate and return sanitized media metadata."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise _MediaValidationError("ffprobe is required to verify provider media")
    if not path.is_file() or path.stat().st_size <= 0:
        raise _MediaValidationError("downloaded media is empty")

    command = [
        ffprobe,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        _raise_without_context(
            _MediaValidationError("ffprobe could not inspect downloaded media")
        )
    if proc.returncode != 0:
        raise _MediaValidationError("downloaded file is not a readable video")
    try:
        probe = json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _raise_without_context(_MediaValidationError("ffprobe returned invalid metadata"))
    if not isinstance(probe, dict):
        raise _MediaValidationError("ffprobe returned invalid metadata")

    streams = probe.get("streams")
    if not isinstance(streams, list):
        raise _MediaValidationError("downloaded media has no video stream")
    video_stream = next(
        (
            stream
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "video"
        ),
        None,
    )
    if not isinstance(video_stream, dict):
        raise _MediaValidationError("downloaded media has no video stream")

    width = video_stream.get("width")
    height = video_stream.get("height")
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or isinstance(height, bool)
        or not 64 <= width <= 8192
        or not 64 <= height <= 8192
    ):
        raise _MediaValidationError("downloaded video has invalid dimensions")

    format_data = probe.get("format")
    format_duration = (
        _duration_value(format_data.get("duration"))
        if isinstance(format_data, dict)
        else None
    )
    duration = format_duration or _duration_value(video_stream.get("duration"))
    if duration is None:
        raise _MediaValidationError("downloaded video has no positive duration")
    if abs(duration - expected_duration_s) > duration_tolerance_s:
        raise _MediaValidationError("downloaded video duration is outside tolerance")

    codec = _safe_error_code(video_stream.get("codec_name"))
    metadata: dict[str, int | float | bool | str] = {
        "duration_s": round(duration, 3),
        "width": width,
        "height": height,
        "has_audio": any(
            isinstance(stream, dict) and stream.get("codec_type") == "audio"
            for stream in streams
        ),
    }
    if codec:
        metadata["video_codec"] = codec
    return metadata


def _poll(
    provider: Provider,
    base_url: str,
    api_key: str,
    job_id: str,
    polling_url: str | None,
    timeout_total: int,
    poll_interval: int,
    max_transient_retries: int = 0,
    retry_backoff_cap: int = 15,
    proxy_url: str = "",
) -> dict:
    deadline = time.time() + timeout_total
    last: dict = {}
    transient_failures = 0
    while time.time() < deadline:
        if provider.polls_url_in_response and polling_url:
            try:
                _require_trusted_api_url(polling_url, base_url)
            except _RequestError:
                # Ignore an untrusted provider-supplied endpoint and derive the
                # canonical same-origin job path instead.
                polling_url = None
                url = f"{base_url.rstrip('/')}{provider.submit_path}/{job_id}"
            else:
                url = polling_url
        else:
            url = f"{base_url.rstrip('/')}{provider.submit_path}/{job_id}"
        try:
            request_kwargs: dict[str, object] = {}
            if proxy_url:
                request_kwargs["proxy_url"] = proxy_url
            last = _http_request(
                "GET",
                url,
                api_key,
                trusted_base_url=base_url,
                timeout=60,
                **request_kwargs,
            )
        except _RequestError as exc:
            if not exc.retryable:
                _raise_without_context(
                    _AttemptError(
                        f"poll failed: {exc}",
                        retryable=False,
                        provider_status=exc.status,
                        provider_code=exc.provider_code,
                    )
                )
            if transient_failures >= max_transient_retries:
                _raise_without_context(
                    _AttemptError(
                        f"poll transient retry limit reached: {exc}",
                        retryable=False,
                        provider_status=exc.status,
                        provider_code=exc.provider_code,
                    )
                )
            transient_failures += 1
            backoff = min(2 ** transient_failures, retry_backoff_cap)
            print(
                f"  transient poll failure; retrying same job in {backoff}s",
                file=sys.stderr,
            )
            time.sleep(backoff)
            continue

        raw_status = last.get("status")
        status = raw_status.lower() if isinstance(raw_status, str) else ""
        if status in TERMINAL_STATES:
            print(f"  job {job_id} status={status}", file=sys.stderr)
            return last

        problem = _provider_problem(last)
        if problem is not None:
            if not problem.retryable:
                raise _attempt_error_from_problem("poll", problem)
            if transient_failures >= max_transient_retries:
                raise _AttemptError(
                    problem.summary("poll") + "; transient retry limit reached",
                    retryable=False,
                    provider_status=problem.status,
                    provider_code=problem.code,
                )
            transient_failures += 1
            backoff = min(2 ** transient_failures, retry_backoff_cap)
            print(
                f"  transient poll failure; retrying same job in {backoff}s",
                file=sys.stderr,
            )
            time.sleep(backoff)
            continue

        status_for_log = _safe_job_status(status, default="pending")
        # Do not print provider-controlled arbitrary status text.
        print(f"  job {job_id} status={status_for_log}", file=sys.stderr)
        time.sleep(poll_interval)
    raise RuntimeError(f"polling timeout after {timeout_total}s")


# -------- single attempt ------------------------------------------------------


def _is_policy_rejection_code(code: object) -> bool:
    return _safe_policy_code(code) is not None


class _AttemptError(RuntimeError):
    """Safe generation failure with an explicit submit-retry decision."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        provider_status: int | None = None,
        provider_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.provider_status = provider_status
        self.provider_code = _safe_policy_code(provider_code)

    @property
    def policy_rejected(self) -> bool:
        return _is_policy_rejection_code(self.provider_code)


def _credential_failure_exit_code(status: int | None) -> int | None:
    """Map only provider account failures to the parent runtime contract."""

    # A bare 403 is not proof that the credential is invalid: provider
    # permission and content-policy/guardrail failures also use that status.
    # Parking a pooled key on 403 could exhaust every healthy key.  Only the
    # authentication-specific 401 rotates the credential.
    if status == 401:
        return PROVIDER_AUTH_INVALID_EXIT_CODE
    if status == 402:
        return PROVIDER_INSUFFICIENT_CREDITS_EXIT_CODE
    if status == 429:
        return PROVIDER_RATE_LIMITED_EXIT_CODE
    return None


def _attempt_error_from_problem(phase: str, problem: _ProviderProblem) -> _AttemptError:
    return _AttemptError(
        problem.summary(phase),
        retryable=problem.retryable,
        provider_status=problem.status,
        provider_code=problem.code,
    )


def _http_error_is_retryable(message: str) -> bool:
    """Return whether an HTTP failure is plausibly transient."""
    match = re.search(r"\bHTTP\s+(\d{3})\b", message)
    if match is None:
        return False
    status = int(match.group(1))
    return _status_is_retryable(status)


def _run_attempt(
    *,
    provider: Provider,
    base_url: str,
    submit_url: str,
    api_key: str,
    payload: dict,
    timeout_total: int,
    poll_interval: int,
    download_path: Path,
    expected_duration_s: int,
    max_transient_retries: int = 0,
    retry_backoff_cap: int = 15,
    proxy_url: str = "",
) -> tuple[dict[str, int | float | bool | str], dict]:
    """Submit, poll one job, download to a candidate, and verify its media."""
    try:
        request_kwargs: dict[str, object] = {}
        if proxy_url:
            request_kwargs["proxy_url"] = proxy_url
        submit = _http_request(
            "POST",
            submit_url,
            api_key,
            trusted_base_url=base_url,
            body=payload,
            timeout=120,
            **request_kwargs,
        )
    except _RequestError as exc:
        # A failed POST is ambiguous: the provider may have accepted (and
        # billed) the generation before the response was lost. Without a
        # provider-supported idempotency key, automatically submitting again
        # can create duplicate paid jobs. Poll retries remain safe once a job
        # id is known, but an ambiguous submit must return control to the user.
        _raise_without_context(
            _AttemptError(
                f"submit failed: {exc}",
                retryable=False,
                provider_status=exc.status,
                provider_code=exc.provider_code,
            )
        )

    problem = _provider_problem(submit)
    if problem is not None:
        raise _AttemptError(
            problem.summary("submit"),
            retryable=False,
            provider_status=problem.status,
            provider_code=problem.code,
        )

    job_id = _safe_job_id(
        submit.get("id") or submit.get("task_id") or submit.get("job_id")
    )
    polling_value = submit.get("polling_url")
    polling_url = (
        polling_value
        if isinstance(polling_value, str)
        and polling_value.startswith(("http://", "https://"))
        else None
    )
    if not job_id:
        raise _AttemptError(
            "submit response missing a valid job id",
            retryable=False,
        )
    print(f"  job_id={job_id}", file=sys.stderr)

    try:
        final = _poll(
            provider, base_url, api_key, job_id, polling_url,
            timeout_total, poll_interval,
            max_transient_retries=max_transient_retries,
            retry_backoff_cap=retry_backoff_cap,
            proxy_url=proxy_url,
        )
    except _AttemptError:
        # A job id already exists. Never return a retryable error to the outer
        # submit loop, because that would create a second potentially billed
        # generation for a polling or policy failure.
        raise
    except RuntimeError as exc:
        _raise_without_context(_AttemptError(str(exc), retryable=False))

    raw_status = final.get("status")
    status = raw_status.lower() if isinstance(raw_status, str) else ""
    if status not in SUCCESS_STATES:
        terminal_problem = _provider_problem(final)
        safe_status = _safe_job_status(status, default="unknown")
        if terminal_problem is not None:
            raise _AttemptError(
                f"job ended with status={safe_status}; "
                f"{terminal_problem.summary('job')}",
                retryable=False,
                provider_status=terminal_problem.status,
                provider_code=terminal_problem.code,
            )
        raise _AttemptError(
            f"job ended with status={safe_status}",
            retryable=False,
        )

    success_problem = _provider_problem(final)
    if success_problem is not None:
        # The provider already returned a job id, so even a nominally transient
        # error envelope on the terminal response must never trigger a second
        # paid submission.
        raise _AttemptError(
            success_problem.summary("job"),
            retryable=False,
            provider_status=success_problem.status,
            provider_code=success_problem.code,
        )

    content_url = provider.extract_url(final)
    if not content_url:
        raise _AttemptError(
            "completed job has no content URL",
            retryable=False,
        )

    print("==> downloading provider media", file=sys.stderr)
    try:
        _download_url_to_path(content_url, api_key, download_path, timeout=600)
        validation = _probe_video(
            download_path,
            expected_duration_s=expected_duration_s,
            duration_tolerance_s=max(1.0, expected_duration_s * 0.2),
        )
    except (_RequestError, _MediaValidationError) as exc:
        # The paid job completed; a bad download must not cause re-submission.
        _raise_without_context(
            _AttemptError(
                f"provider media verification failed: {exc}",
                retryable=False,
            )
        )
    receipt: dict = {
        "status": "generated",
        "provider": provider.name,
        "model": str(payload.get("model") or provider.default_model),
        "job_id": job_id,
        "fallback": False,
    }
    return validation, receipt


def _persist_receipt(out_path: Path, receipt: dict) -> dict:
    """Atomically persist a strict, credential-free receipt allowlist."""
    receipt_path = out_path.with_suffix(out_path.suffix + ".receipt.json")
    provider = receipt.get("provider")
    model = _safe_model_id(str(receipt.get("model") or ""))
    job_id = _safe_job_id(receipt.get("job_id"))
    validation = receipt.get("validation")
    if provider not in PROVIDERS or model is None or job_id is None:
        raise RuntimeError("refusing to persist an invalid provider receipt")
    if not isinstance(validation, dict):
        raise RuntimeError("refusing to persist a receipt without media validation")

    def media_metadata(value: object) -> dict:
        if not isinstance(value, dict):
            raise RuntimeError("invalid media validation metadata")
        duration = value.get("duration_s")
        width = value.get("width")
        height = value.get("height")
        has_audio = value.get("has_audio")
        if (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not math.isfinite(float(duration))
            or duration <= 0
            or not isinstance(width, int)
            or isinstance(width, bool)
            or not isinstance(height, int)
            or isinstance(height, bool)
            or not 64 <= width <= 8192
            or not 64 <= height <= 8192
            or not isinstance(has_audio, bool)
        ):
            raise RuntimeError("invalid media validation metadata")
        safe: dict[str, int | float | bool | str] = {
            "duration_s": duration,
            "width": width,
            "height": height,
            "has_audio": has_audio,
        }
        codec = _safe_error_code(value.get("video_codec"))
        if codec:
            safe["video_codec"] = codec
        return safe

    expected_provider = validation.get("expected_provider_duration_s")
    expected_final = validation.get("expected_final_duration_s")
    if (
        not isinstance(expected_provider, (int, float))
        or isinstance(expected_provider, bool)
        or expected_provider <= 0
        or not isinstance(expected_final, (int, float))
        or isinstance(expected_final, bool)
        or expected_final <= 0
    ):
        raise RuntimeError("invalid expected duration metadata")
    public = {
        "status": "generated",
        "provider": provider,
        "model": model,
        "job_id": job_id,
        "fallback": False,
        "validation": {
            "expected_provider_duration_s": expected_provider,
            "expected_final_duration_s": expected_final,
            "trimmed": bool(validation.get("trimmed")),
            "provider_media": media_metadata(validation.get("provider_media")),
            "final_media": media_metadata(validation.get("final_media")),
        },
    }
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{receipt_path.name}.",
        suffix=".tmp",
        dir=str(receipt_path.parent),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        tmp_path.write_text(
            json.dumps(public, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, receipt_path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
    return public


def _persist_policy_rejection_receipt(
    out_path: Path,
    *,
    provider: Provider,
    model: str,
    policy_code: str | None,
) -> dict:
    """Persist only the bounded reason/code needed by fallback delivery UX."""

    receipt_path = out_path.with_suffix(out_path.suffix + ".receipt.json")
    safe_model = _safe_model_id(model)
    safe_code = _safe_policy_code(policy_code)
    if safe_model is None or safe_code is None:
        raise RuntimeError("refusing to persist an invalid policy-rejection receipt")
    if not _is_policy_rejection_code(safe_code):
        raise RuntimeError("refusing to persist a non-policy failure receipt")
    public = {
        "status": "policy_rejected",
        "provider": provider.name,
        "model": safe_model,
        "fallback": False,
        "reason": "provider_policy_rejected",
        "policy_code": safe_code,
    }
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{receipt_path.name}.",
        suffix=".tmp",
        dir=str(receipt_path.parent),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        tmp_path.write_text(
            json.dumps(public, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, receipt_path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
    return public


def _print_receipt(label: str, receipt: dict) -> None:
    # Preserve insertion order so status/provider/model/job remain visible even
    # if a downstream UI truncates the trailing local paths.
    print(f"{label}: {json.dumps(receipt, ensure_ascii=False)}")


def _provider_duration(provider: Provider, model_id: str, requested: int) -> int:
    """Map a user duration to the duration accepted by the provider.

    OpenRouter's Seedance 2.0 routes currently accept 4--15 seconds. Keep the
    public workflow's 3-second option by generating the shortest real clip and
    trimming it locally after download.
    """
    if (
        provider.name == "openrouter"
        and model_id.startswith("bytedance/seedance-2.0")
        and requested == 3
    ):
        return 4
    return requested


def _write_trimmed_video(
    source_path: Path,
    out_path: Path,
    *,
    duration_s: int,
) -> dict[str, int | float | bool | str]:
    """Re-encode an exact-duration candidate and verify the result."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to trim the provider clip")

    command = [
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-i",
        str(source_path),
        "-t",
        str(duration_s),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired):
        _raise_without_context(RuntimeError("could not trim provider clip"))
    if proc.returncode != 0 or not out_path.is_file() or out_path.stat().st_size == 0:
        # ffmpeg stderr can echo paths and URL-like metadata from the source;
        # keep the user-facing failure deliberately generic.
        raise RuntimeError(f"ffmpeg could not trim provider clip (exit {proc.returncode})")
    return _probe_video(
        out_path,
        expected_duration_s=duration_s,
        duration_tolerance_s=max(0.25, duration_s * 0.05),
    )


# -------- main ----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", "-p", required=True)
    parser.add_argument("--filename", "-f", required=True)
    parser.add_argument(
        "--provider", choices=tuple(PROVIDERS), default="openrouter",
        help="Backend API (default: openrouter)",
    )
    parser.add_argument("--input-image", "-i", default="")
    parser.add_argument(
        "--input-reference",
        dest="input_references",
        action="append",
        default=[],
        help="Style/identity reference image path; repeatable. Used only when --input-image is empty.",
    )
    parser.add_argument(
        "--aspect-ratio", default="9:16",
        choices=["9:16", "16:9", "1:1", "4:3", "3:4", "21:9"],
    )
    parser.add_argument("--duration", type=int, default=5)
    parser.add_argument(
        "--resolution", default="720p",
        choices=["480p", "720p", "1080p"],
        help="Output resolution (volcengine/byteplus only; ignored by openrouter)",
    )
    parser.add_argument(
        "--model", default="",
        help="Override the model id. Defaults to the provider's recommended model.",
    )
    parser.add_argument("--api-key", "-k", default="")
    parser.add_argument(
        "--base-url", default="",
        help="Override the provider's base URL.",
    )
    parser.add_argument("--poll-interval", type=int, default=5)
    parser.add_argument("--timeout-total", type=int, default=600)
    parser.add_argument(
        "--max-retries", type=int, default=0,
        help=(
            "Extra transient polling retries for an issued job (capped at 5); "
            "paid submits are never retried automatically."
        ),
    )
    parser.add_argument(
        "--retry-backoff-cap", type=int, default=15,
        help="Maximum sleep seconds between retries (exponential backoff is capped here).",
    )
    raw = parser.parse_args()

    if not 3 <= raw.duration <= 15:
        print(f"Error: --duration must be 3..15 (got {raw.duration})", file=sys.stderr)
        return SAFE_NO_SUBMIT_EXIT_CODE
    if raw.poll_interval < 0 or raw.timeout_total <= 0:
        print("Error: polling interval must be non-negative and timeout positive", file=sys.stderr)
        return SAFE_NO_SUBMIT_EXIT_CODE

    provider = PROVIDERS[raw.provider]
    model_id = raw.model or provider.default_model
    if _safe_model_id(model_id) is None:
        print("Error: --model contains unsupported characters", file=sys.stderr)
        return SAFE_NO_SUBMIT_EXIT_CODE
    try:
        connection = _resolve_provider_connection(
            provider,
            raw.api_key or None,
            raw.base_url,
        )
    except _RequestError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return SAFE_NO_SUBMIT_EXIT_CODE
    if connection is None:
        env_hint = " / ".join(provider.default_env)
        if provider.name == "openrouter":
            print(
                "Error: no OpenRouter API key found. Pass --api-key, set "
                "OPENROUTER_API_KEY, or configure an OpenRouter llm key in "
                "OpenSquilla config.",
                file=sys.stderr,
            )
        else:
            print(
                f"Error: no API key. Pass --api-key or set one of: {env_hint}.",
                file=sys.stderr,
            )
        return SAFE_NO_SUBMIT_EXIT_CODE
    base_url = connection.base_url

    if raw.input_image and not Path(raw.input_image).is_file():
        print(f"Error: --input-image not found: {raw.input_image}", file=sys.stderr)
        return SAFE_NO_SUBMIT_EXIT_CODE
    for ref in raw.input_references or []:
        if ref and not Path(ref).is_file():
            print(f"Error: --input-reference not found: {ref}", file=sys.stderr)
            return SAFE_NO_SUBMIT_EXIT_CODE

    requested_duration = int(raw.duration)
    provider_duration = _provider_duration(provider, model_id, requested_duration)
    args = Args(
        prompt=raw.prompt,
        model=model_id,
        aspect_ratio=raw.aspect_ratio,
        duration=provider_duration,
        resolution=raw.resolution,
        input_image=raw.input_image,
        input_references=raw.input_references or [],
    )
    payload = provider.build_payload(args)
    submit_url = base_url.rstrip("/") + provider.submit_path
    out_path = Path(raw.filename).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Keep retries finite even when the skill is invoked directly with an
    # unreasonable value. This caps accidental paid submissions and polling
    # traffic while preserving the documented 0..5 range.
    max_retries = min(max(0, raw.max_retries), 5)
    retry_backoff_cap = min(max(0, raw.retry_backoff_cap), 60)
    attempts = max_retries + 1
    last_error: str | None = None
    attempts_run = 0
    policy_failure: _AttemptError | None = None
    credential_failure_exit: int | None = None
    with tempfile.TemporaryDirectory(
        prefix="opensquilla-provider-video-",
        dir=str(out_path.parent),
    ) as tmp_dir:
        tmp_root = Path(tmp_dir)
        for attempt in range(1, attempts + 1):
            attempts_run = attempt
            print(
                f"==> attempt {attempt}/{attempts} provider={provider.name} "
                f"model={model_id} (requested={requested_duration}s, "
                f"provider={args.duration}s, {args.aspect_ratio}, {args.resolution})",
                file=sys.stderr,
            )
            provider_candidate = tmp_root / f"provider-{attempt}.mp4"
            try:
                provider_validation, receipt = _run_attempt(
                    provider=provider,
                    base_url=base_url,
                    submit_url=submit_url,
                    api_key=connection.api_key,
                    payload=payload,
                    timeout_total=raw.timeout_total,
                    poll_interval=raw.poll_interval,
                    download_path=provider_candidate,
                    expected_duration_s=provider_duration,
                    max_transient_retries=max_retries,
                    retry_backoff_cap=retry_backoff_cap,
                    proxy_url=connection.proxy,
                )
            except _AttemptError as exc:
                last_error = str(exc)
                print(f"  attempt {attempt} failed: {last_error}", file=sys.stderr)
                if not exc.retryable:
                    if exc.policy_rejected:
                        policy_failure = exc
                    else:
                        credential_failure_exit = _credential_failure_exit_code(
                            exc.provider_status
                        )
                    print("  non-retryable provider response; stopping", file=sys.stderr)
                    break
                if attempt < attempts:
                    backoff = min(2 ** attempt, retry_backoff_cap)
                    print(f"  retrying submit in {backoff}s", file=sys.stderr)
                    time.sleep(backoff)
                continue

            final_candidate = provider_candidate
            try:
                if provider_duration != requested_duration:
                    final_candidate = tmp_root / "final-trimmed.mp4"
                    final_validation = _write_trimmed_video(
                        provider_candidate,
                        final_candidate,
                        duration_s=requested_duration,
                    )
                else:
                    final_validation = provider_validation

                receipt["validation"] = {
                    "expected_provider_duration_s": provider_duration,
                    "expected_final_duration_s": requested_duration,
                    "trimmed": provider_duration != requested_duration,
                    "provider_media": provider_validation,
                    "final_media": final_validation,
                }
                # The candidate and destination share a directory/filesystem.
                # os.replace is atomic and works on Windows once all temporary
                # handles have been closed.
                os.replace(final_candidate, out_path)
                public_receipt = _persist_receipt(out_path, receipt)
            except (OSError, RuntimeError) as exc:
                print(f"Error: could not publish verified video: {exc}", file=sys.stderr)
                return 1
            print(str(out_path))
            _print_receipt("VIDEO_GENERATION_RECEIPT", public_receipt)
            return 0

    if policy_failure is not None:
        try:
            public_receipt = _persist_policy_rejection_receipt(
                out_path,
                provider=provider,
                model=model_id,
                policy_code=policy_failure.provider_code,
            )
        except (OSError, RuntimeError) as exc:
            print(f"Error: could not persist policy-rejection receipt: {exc}", file=sys.stderr)
        else:
            # Preserve the non-zero exit/failover behavior while giving the
            # parent one sanitized, current-invocation receipt line to bind to
            # the sidecar. Workspace sidecars alone are never trusted.
            _print_receipt("VIDEO_GENERATION_RECEIPT", public_receipt)
            print(
                "  sanitized provider-policy rejection recorded for delivery audit",
                file=sys.stderr,
            )
    if credential_failure_exit is not None:
        return credential_failure_exit
    print(
        f"Error: generation failed after {attempts_run} attempt(s). Last: {last_error}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
