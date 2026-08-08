#!/usr/bin/env python3
"""Generate images via OpenRouter (default google/gemini-3.1-flash-image-preview).

Pipeline: OpenRouter `/api/v1/chat/completions` with
`modalities=["image", "text"]`. The response carries a base64 data URL
in `choices[0].message.images[0].image_url.url`.

Compatibility knobs (raw CLI users can opt in):

  --max-retries N           budget used only for failures proven to occur
                            before a paid submit (default 0).
  --fallback-model M ...    used only after such a safe pre-submit failure.
                            Provider responses never cause an automatic
                            second paid request.
  --placeholder-on-fail     when every model fails (typically moderation
                            refusing the prompt), emit a 720x1280
                            solid-colour PNG with a "Scene placeholder"
                            label so downstream merge steps still have a
                            file in this slot. Off by default.

Usage:
    python generate_image.py --prompt "..." --filename "out.png" \\
        [--input-image PATH] [--aspect-ratio 9:16] [--image-size 1K|2K|4K] \\
        [--model google/gemini-3.1-flash-image-preview] \\
        [--max-retries 1] [--fallback-model google/gemini-3-pro-image-preview] \\
        [--placeholder-on-fail] [--api-key KEY]

Auth:
    1. --api-key argument
    2. Parent-injected atomic OpenSquilla provider connection during a
       meta-skill run
    3. Legacy parent-injected OpenRouter credential (official endpoint only)
    4. OPENROUTER_API_KEY environment variable for direct CLI use

Output: prints the absolute path of the saved PNG, followed by a sanitized
``IMAGE_GENERATION_RECEIPT`` JSON line. The same receipt is persisted beside
the PNG as ``<filename>.receipt.json``.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import io
import json
import math
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlparse, urlsplit

# Contract with the bundled meta skill_exec wrapper: this non-zero exit is
# emitted only before a provider POST can be attempted.
SAFE_NO_SUBMIT_EXIT_CODE = 78
# Reserved exits consumed only by OpenSquilla's exact bundled skill executor.
# They are emitted after a provider request returned a credential-account
# failure, so replay remains unsafe even though the next explicitly-authorized
# run should rotate a profile-pool credential.
PROVIDER_AUTH_INVALID_EXIT_CODE = 79
PROVIDER_INSUFFICIENT_CREDITS_EXIT_CODE = 80
PROVIDER_RATE_LIMITED_EXIT_CODE = 81

DEFAULT_MODEL = "google/gemini-3.1-flash-image-preview"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
META_CAPABILITY_PROVIDER_ENV = "OPENSQUILLA_META_CAPABILITY_PROVIDER"
META_CAPABILITY_API_KEY_ENV = "OPENSQUILLA_META_CAPABILITY_API_KEY"
META_CAPABILITY_BASE_URL_ENV = "OPENSQUILLA_META_CAPABILITY_BASE_URL"
META_CAPABILITY_PROXY_ENV = "OPENSQUILLA_META_CAPABILITY_PROXY"
META_OPENROUTER_API_KEY_ENV = "OPENSQUILLA_META_OPENROUTER_API_KEY"

# Image responses embed the output as base64 inside JSON. These limits are
# deliberately above normal 4K provider output while bounding both the wire
# response and the decoded allocation if an upstream endpoint misbehaves.
MAX_OPENROUTER_RESPONSE_BYTES = 48 * 1024 * 1024
MAX_DECODED_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_DIMENSION = 16_384
MAX_IMAGE_PIXELS = 64 * 1024 * 1024
_RESPONSE_READ_CHUNK_BYTES = 64 * 1024

# Mirrors src/opensquilla/provider/openrouter_attribution.py — kept inline so
# this script can run as a standalone subprocess without importing the
# opensquilla package. Keep the three constants and the predicate in sync if
# the canonical helper changes.
_OPENROUTER_APP_REFERER = "https://opensquilla.ai"
_OPENROUTER_APP_TITLE = "OpenSquilla"
_SAFE_POLICY_CODE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}")
_SAFE_MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_SAFE_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_EMBEDDED_CODE_RE = re.compile(
    r"(?:[\"']?(?:provider_code|policy_code|code)[\"']?)"
    r"\s*[:=]\s*[\"']?([A-Za-z][A-Za-z0-9_.-]{0,127})",
    re.IGNORECASE,
)


def _safe_policy_code(value: object) -> str | None:
    """Return one bounded policy identifier, never provider prose."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if _SAFE_POLICY_CODE_RE.fullmatch(candidate) is None:
        return None
    folded = candidate.casefold()
    if folded.startswith(
        (
            "sk-",
            "sk_",
            "bearer",
            "req-",
            "req_",
            "request-id-",
            "request_id_",
            "job-",
            "job_",
            "trace-",
            "trace_",
        )
    ):
        return None
    if not any(
        marker in folded
        for marker in (
            "policy",
            "privacy",
            "sensitive",
            "moderation",
            "safety",
            "filter",
        )
    ):
        return None
    return candidate


def _policy_code_from_text(value: object) -> str | None:
    """Extract only a named, syntactically safe policy code from text."""

    if not isinstance(value, str):
        return None
    text = value[: 64 * 1024]
    direct = _safe_policy_code(text)
    if direct is not None:
        return direct
    for match in _EMBEDDED_CODE_RE.finditer(text):
        candidate = _safe_policy_code(match.group(1))
        if candidate is not None:
            return candidate
    return None


def _policy_code_from_payload(payload: object, *, _depth: int = 0) -> str | None:
    """Inspect only bounded error fields and discard all provider messages."""

    if _depth > 5:
        return None
    if isinstance(payload, dict):
        for key in ("provider_code", "policy_code", "type", "code"):
            candidate = _safe_policy_code(payload.get(key))
            if candidate is not None:
                return candidate
        for key in ("message", "detail", "raw"):
            candidate = _policy_code_from_text(payload.get(key))
            if candidate is not None:
                return candidate
        for key in ("error", "metadata", "data", "details", "cause"):
            candidate = _policy_code_from_payload(payload.get(key), _depth=_depth + 1)
            if candidate is not None:
                return candidate
    elif isinstance(payload, list):
        for item in payload[:20]:
            candidate = _policy_code_from_payload(item, _depth=_depth + 1)
            if candidate is not None:
                return candidate
    return None


class _ImageRequestError(RuntimeError):
    """A provider failure whose public message contains no provider diagnostics."""

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

    @property
    def policy_rejected(self) -> bool:
        return self.provider_code is not None


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


@dataclass(frozen=True)
class _Origin:
    scheme: str
    host: str
    port: int


@dataclass(frozen=True)
class _ProviderConnection:
    """One atomic provider credential and endpoint selected before submit."""

    provider: str
    api_key: str = field(repr=False)
    base_url: str
    proxy: str = field(default="", repr=False)


def _raise_without_context(error: BaseException) -> NoReturn:
    """Raise a public error without retaining the untrusted exception chain.

    ``from None`` suppresses traceback rendering but normally leaves the
    original exception reachable through ``__context__``. Clearing both links
    keeps signed URLs, response bodies, and parser input unavailable through
    the public exception's cause/context chain.
    """

    try:
        raise error from None
    finally:
        error.__cause__ = None
        error.__context__ = None


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


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward an authenticated image request through a redirect."""

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


def _read_limited_response(response: object, *, max_bytes: int) -> bytes:
    """Read an HTTP response with declared and cumulative byte bounds."""

    declared = _declared_content_length(response)
    if declared is not None and declared > max_bytes:
        raise _ImageRequestError("OpenRouter response exceeds size limit")

    read = getattr(response, "read", None)
    if not callable(read):
        raise _ImageRequestError("OpenRouter returned an unreadable response")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = read(min(_RESPONSE_READ_CHUNK_BYTES, max_bytes - total + 1))
        if not isinstance(chunk, bytes):
            raise _ImageRequestError("OpenRouter returned an unreadable response")
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise _ImageRequestError("OpenRouter response exceeds size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _clean_api_key(value: object) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in candidate):
        raise _ImageRequestError("invalid API credential")
    return candidate


def _api_origin(value: str) -> _Origin:
    """Validate a credential-bearing API base without echoing its value."""

    raw = str(value or "").strip()
    if not raw or any(char.isspace() or ord(char) < 0x20 for char in raw):
        raise _ImageRequestError("invalid authenticated API URL")
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").rstrip(".").lower()
        explicit_port = parsed.port
        if explicit_port is not None and not 0 < explicit_port <= 65535:
            raise ValueError
        port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
    except (TypeError, UnicodeError, ValueError):
        raise _ImageRequestError("invalid authenticated API URL") from None
    if (
        scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\\" in parsed.netloc
    ):
        raise _ImageRequestError("invalid authenticated API URL")
    return _Origin(scheme=scheme, host=host, port=port)


def _validated_api_base_url(value: str) -> str:
    candidate = str(value or "").strip()
    _api_origin(candidate)
    return candidate.rstrip("/")


def _require_same_api_origin(stored_base_url: str, candidate_base_url: str) -> None:
    if _api_origin(stored_base_url) != _api_origin(candidate_base_url):
        raise _ImageRequestError(
            "refusing to send configured credential outside its API origin"
        )


def _validated_proxy_url(value: str) -> str:
    """Validate an explicit provider-API proxy without disclosing credentials."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    if any(char.isspace() or ord(char) < 0x20 for char in raw):
        raise _ImageRequestError("invalid provider API proxy")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (TypeError, UnicodeError, ValueError):
        raise _ImageRequestError("invalid provider API proxy") from None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
        or "\\" in parsed.netloc
        or (port is not None and not 0 < port <= 65535)
    ):
        raise _ImageRequestError("invalid provider API proxy")
    return raw


def _resolve_provider_connection(
    provided_key: str | None,
    requested_base_url: str,
) -> _ProviderConnection | None:
    """Resolve key, endpoint, and proxy as one pre-submit connection."""

    requested = str(requested_base_url or "").strip()
    if provided_key and provided_key.strip():
        return _ProviderConnection(
            provider="openrouter",
            api_key=_clean_api_key(provided_key),
            base_url=_validated_api_base_url(requested or DEFAULT_BASE_URL),
        )

    parent_provider = (os.environ.get(META_CAPABILITY_PROVIDER_ENV) or "").strip()
    parent_key_raw = os.environ.get(META_CAPABILITY_API_KEY_ENV) or ""
    parent_base = (os.environ.get(META_CAPABILITY_BASE_URL_ENV) or "").strip()
    parent_proxy = (os.environ.get(META_CAPABILITY_PROXY_ENV) or "").strip()
    if any((parent_provider, parent_key_raw.strip(), parent_base, parent_proxy)):
        if not parent_provider or not parent_key_raw.strip() or not parent_base:
            raise _ImageRequestError("incomplete parent provider connection")
        if parent_provider.lower() != "openrouter":
            raise _ImageRequestError("parent provider connection does not match openrouter")
        stored_base = _validated_api_base_url(parent_base)
        base_url = _validated_api_base_url(requested or stored_base)
        _require_same_api_origin(stored_base, base_url)
        return _ProviderConnection(
            provider="openrouter",
            api_key=_clean_api_key(parent_key_raw),
            base_url=base_url,
            proxy=_validated_proxy_url(parent_proxy),
        )

    # Compatibility with parent runtimes that injected only this OpenRouter-
    # specific name. Its credential is deliberately bound to the official
    # endpoint; it cannot authorize a cross-origin CLI override.
    legacy_parent_key = _clean_api_key(
        os.environ.get(META_OPENROUTER_API_KEY_ENV) or ""
    )
    if legacy_parent_key:
        base_url = _validated_api_base_url(requested or DEFAULT_BASE_URL)
        _require_same_api_origin(DEFAULT_BASE_URL, base_url)
        return _ProviderConnection(
            provider="openrouter",
            api_key=legacy_parent_key,
            base_url=base_url,
        )

    canonical_key = _clean_api_key(os.environ.get("OPENROUTER_API_KEY") or "")
    if canonical_key:
        base_url = _validated_api_base_url(requested or DEFAULT_BASE_URL)
        _require_same_api_origin(DEFAULT_BASE_URL, base_url)
        return _ProviderConnection(
            provider="openrouter",
            api_key=canonical_key,
            base_url=base_url,
        )
    return None


def resolve_api_key(provided: str | None) -> str | None:
    if provided:
        key = _clean_api_key(provided)
        if key:
            return key
    parent_provider = (os.environ.get(META_CAPABILITY_PROVIDER_ENV) or "").strip()
    parent_key = (os.environ.get(META_CAPABILITY_API_KEY_ENV) or "").strip()
    parent_base = (os.environ.get(META_CAPABILITY_BASE_URL_ENV) or "").strip()
    if parent_provider.lower() == "openrouter" and parent_key and parent_base:
        return _clean_api_key(parent_key)
    # Only the parent runtime may translate active Gateway config into this
    # volatile name. Never rediscover config from cwd: the cwd is the user's
    # workspace and can contain an untrusted opensquilla.toml.
    legacy_parent_key = _clean_api_key(
        os.environ.get(META_OPENROUTER_API_KEY_ENV) or ""
    )
    if legacy_parent_key:
        return legacy_parent_key
    val = _clean_api_key(os.environ.get("OPENROUTER_API_KEY") or "")
    return val or None


def encode_input_image(path: str) -> str:
    raw = Path(path).read_bytes()
    suffix = Path(path).suffix.lower().lstrip(".")
    mime = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "gif": "image/gif",
    }.get(suffix, "image/png")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def build_payload(prompt: str, input_image: str | None, aspect_ratio: str, image_size: str, model: str) -> dict:
    user_content: list = [{"type": "text", "text": prompt}]
    if input_image:
        user_content.append(
            {
                "type": "image_url",
                "image_url": {"url": encode_input_image(input_image)},
            }
        )
    return {
        "model": model,
        "messages": [{"role": "user", "content": user_content}],
        "modalities": ["image", "text"],
        "stream": False,
        "image_config": {
            "aspect_ratio": aspect_ratio,
            "image_size": image_size,
        },
    }


def extract_image_url(data: dict) -> str | None:
    for choice in data.get("choices") or []:
        message = choice.get("message") or {}
        for image in message.get("images") or []:
            image_url = image.get("image_url") or image.get("imageUrl") or {}
            url = image_url.get("url")
            if isinstance(url, str) and url:
                return url
    return None


def extract_finish_reason(data: dict) -> str | None:
    """OpenRouter signals moderation refusals via native_finish_reason."""
    for choice in data.get("choices") or []:
        for key in ("native_finish_reason", "finish_reason"):
            val = choice.get(key)
            if isinstance(val, str) and val:
                return val
    return None


def decode_data_url(data_url: str) -> bytes:
    prefix, sep, encoded = data_url.partition(",")
    metadata = prefix.split(";")
    mime = metadata[0].removeprefix("data:").lower()
    parameters = {part.lower() for part in metadata[1:]}
    expected_format = {
        "image/png": "PNG",
        "image/jpeg": "JPEG",
        "image/webp": "WEBP",
    }.get(mime)
    if (
        not sep
        or not prefix.lower().startswith("data:")
        or "base64" not in parameters
        or expected_format is None
    ):
        raise ValueError("OpenRouter returned a non-base64 image URL")
    max_encoded_bytes = 4 * ((MAX_DECODED_IMAGE_BYTES + 2) // 3)
    if len(encoded) > max_encoded_bytes:
        raise _ImageRequestError("OpenRouter image output exceeds size limit")
    decoded = base64.b64decode(encoded, validate=True)
    if len(decoded) > MAX_DECODED_IMAGE_BYTES:
        raise _ImageRequestError("OpenRouter image output exceeds size limit")

    # A provider request id proves only that a request existed; it does not
    # prove the returned bytes are a usable image. Verify the declared MIME,
    # decoded format, dimensions, complete payload, and pixel allocation before
    # a generated receipt can be emitted. Normalize supported formats to the
    # promised PNG artifact so filename, content, and downstream decoders agree.
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        raise _ImageRequestError("local image validation is unavailable") from None
    try:
        with Image.open(io.BytesIO(decoded)) as probe:
            actual_format = str(probe.format or "").upper()
            width, height = probe.size
            frame_count = int(getattr(probe, "n_frames", 1) or 1)
            if actual_format != expected_format:
                raise _ImageRequestError(
                    "upstream image MIME does not match decoded format"
                )
            if (
                width <= 0
                or height <= 0
                or width > MAX_IMAGE_DIMENSION
                or height > MAX_IMAGE_DIMENSION
                or width * height > MAX_IMAGE_PIXELS
                or frame_count != 1
            ):
                raise _ImageRequestError("upstream image dimensions are invalid")
            probe.verify()

        with Image.open(io.BytesIO(decoded)) as image:
            image.load()
            normalized = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            output = io.BytesIO()
            normalized.save(output, format="PNG")
            png_bytes = output.getvalue()
    except _ImageRequestError:
        raise
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise _ImageRequestError("upstream image provider returned invalid image data") from None

    if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise _ImageRequestError("upstream image provider returned invalid image data")
    if len(png_bytes) > MAX_DECODED_IMAGE_BYTES:
        raise _ImageRequestError("OpenRouter image output exceeds size limit")
    return png_bytes


def post_chat_completions(
    base_url: str,
    api_key: str,
    payload: dict,
    timeout: int,
    *,
    proxy_url: str = "",
) -> dict:
    trusted_base_url = _validated_api_base_url(base_url)
    url = trusted_base_url + "/chat/completions"
    if _api_origin(url) != _api_origin(trusted_base_url):
        raise _ImageRequestError("refusing authenticated request outside trusted API origin")
    safe_proxy_url = _validated_proxy_url(proxy_url)
    body = json.dumps(payload).encode("utf-8")
    headers: dict[str, str] = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    headers.update(_openrouter_attribution_headers(url))
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
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
                max_bytes=MAX_OPENROUTER_RESPONSE_BYTES,
            )
    except _ImageRequestError:
        raise
    except urllib.error.HTTPError as exc:
        # The body, URL, request id, and provider prose may contain sensitive
        # diagnostics. Parse at most one strict policy code and discard the
        # original exception instead of chaining it into logs.
        try:
            error_raw = exc.read(64 * 1024)
            error_payload = json.loads(error_raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            error_payload = None
        provider_code = _policy_code_from_payload(error_payload)
        _raise_without_context(
            _ImageRequestError(
                f"OpenRouter HTTP {exc.code}",
                status=exc.code,
                provider_code=provider_code,
                # This is a non-idempotent generation POST. Even an HTTP error
                # can arrive after an upstream route accepted a paid job, so
                # automatic submit retries and model fallbacks are unsafe.
                retryable=False,
            )
        )
    except (OSError, TimeoutError, urllib.error.URLError, ValueError):
        _raise_without_context(
            _ImageRequestError(
                "OpenRouter network request failed",
                # A lost response is ambiguous: the provider may have accepted
                # and billed the request before the transport failed.
                retryable=False,
            )
        )

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _raise_without_context(_ImageRequestError("OpenRouter returned invalid JSON"))
    if not isinstance(parsed, dict):
        raise _ImageRequestError("OpenRouter returned non-object JSON")
    return parsed


def _try_one_attempt(
    *,
    base_url: str,
    api_key: str,
    prompt: str,
    input_image: str | None,
    aspect_ratio: str,
    image_size: str,
    model: str,
    timeout: int,
    proxy_url: str = "",
) -> tuple[bytes, dict]:
    """Single network round-trip with only sanitized public failures."""
    payload = build_payload(prompt, input_image, aspect_ratio, image_size, model)
    data = post_chat_completions(
        base_url,
        api_key,
        payload,
        timeout,
        proxy_url=proxy_url,
    )
    policy_code = _policy_code_from_payload(data)
    if data.get("error") not in (None, "", False):
        if policy_code is not None:
            raise _ImageRequestError(
                "upstream image provider rejected generation",
                provider_code=policy_code,
                retryable=False,
            )
        raise _ImageRequestError(
            "upstream image provider rejected generation",
            retryable=False,
        )
    image_url = extract_image_url(data)
    if not image_url:
        policy_code = policy_code or _safe_policy_code(extract_finish_reason(data))
        if policy_code is not None:
            raise _ImageRequestError(
                "upstream image provider rejected generation",
                provider_code=policy_code,
                retryable=False,
            )
        raise _ImageRequestError("upstream image provider returned no image")
    try:
        image_bytes = decode_data_url(image_url)
    except _ImageRequestError:
        raise
    except (ValueError, TypeError, binascii.Error):
        _raise_without_context(
            _ImageRequestError("upstream image provider returned invalid image data")
        )
    return image_bytes, data


def _safe_usage(data: dict) -> dict[str, int | float]:
    """Copy only numeric billing counters into a public receipt."""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return {}
    safe: dict[str, int | float] = {}
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cost",
        "total_cost",
    ):
        value = usage.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and value >= 0
        ):
            safe[key] = value
    return safe


def _safe_model_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _SAFE_MODEL_ID_RE.fullmatch(candidate) else None


def _safe_request_id(value: object) -> str | None:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    candidate = str(value).strip()
    if candidate.lower().startswith(("sk-", "sk_", "bearer")):
        return None
    return candidate if _SAFE_REQUEST_ID_RE.fullmatch(candidate) else None


def _generated_receipt(data: dict, *, model: str) -> dict:
    safe_model = _safe_model_id(model)
    if safe_model is None:
        raise RuntimeError("refusing to persist an invalid image model id")
    receipt: dict = {
        "status": "generated_unverified",
        "provider": "openrouter",
        "model": safe_model,
        "placeholder": False,
    }
    request_id = _safe_request_id(
        data.get("id") or data.get("request_id") or data.get("generation_id")
    )
    if request_id is not None:
        receipt["status"] = "generated"
        receipt["request_id"] = request_id
    else:
        receipt["reason"] = "provider_response_missing_request_id"
    usage = _safe_usage(data)
    if usage:
        receipt["usage"] = usage
    return receipt


def _policy_rejection_receipt(
    *,
    model: str,
    policy_code: str,
    placeholder: bool,
) -> dict:
    safe_model = _safe_model_id(model)
    safe_code = _safe_policy_code(policy_code)
    if safe_model is None or safe_code is None:
        raise RuntimeError("refusing to persist an invalid policy-rejection receipt")
    return {
        "status": "policy_rejected",
        "provider": "openrouter",
        "model": safe_model,
        "reason": "provider_policy_rejected",
        "policy_code": safe_code,
        "placeholder": placeholder,
    }


def _public_receipt(receipt: dict) -> dict:
    """Copy only fields allowed by the image-generation receipt contract."""

    status = receipt.get("status")
    model = _safe_model_id(receipt.get("model"))
    if model is None:
        raise RuntimeError("refusing to persist an invalid image model id")
    placeholder = receipt.get("placeholder")
    if not isinstance(placeholder, bool):
        raise RuntimeError("refusing to persist an invalid placeholder marker")

    if status in {"generated", "generated_unverified"}:
        public: dict = {
            "status": status,
            "provider": "openrouter",
            "model": model,
            "placeholder": False,
        }
        request_id = _safe_request_id(receipt.get("request_id"))
        if status == "generated":
            if request_id is None:
                raise RuntimeError("refusing generated receipt without provider request id")
            public["request_id"] = request_id
        else:
            public["reason"] = "provider_response_missing_request_id"
        usage = _safe_usage({"usage": receipt.get("usage")})
        if usage:
            public["usage"] = usage
        return public

    if status == "policy_rejected":
        return _policy_rejection_receipt(
            model=model,
            policy_code=str(receipt.get("policy_code") or ""),
            placeholder=placeholder,
        )

    if status == "placeholder":
        return {
            "status": "placeholder",
            "provider": "local",
            "model": model,
            "placeholder": True,
            "reason": "all_model_attempts_failed",
        }
    raise RuntimeError("refusing to persist an unknown image receipt status")


def _persist_receipt(out_path: Path, receipt: dict) -> tuple[Path, dict]:
    """Atomically persist and return a strict credential-free receipt."""

    receipt_path = out_path.with_suffix(out_path.suffix + ".receipt.json")
    public = _public_receipt(receipt)
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
        # Same-directory replacement is atomic and works on Windows after the
        # mkstemp handle is closed.
        os.replace(tmp_path, receipt_path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
    return receipt_path, public


def _print_receipt(label: str, receipt: dict) -> None:
    # Preserve insertion order so status/provider/model/id remain visible even
    # if a downstream UI truncates the trailing local paths.
    print(f"{label}: {json.dumps(receipt, ensure_ascii=False)}")


def _write_placeholder_png(out_path: Path, prompt: str, aspect_ratio: str) -> None:
    """Last-resort 720x1280 solid-colour PNG with a short label so merge can run."""
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Pillow not installed; cannot write placeholder. "
            "pip install pillow or disable --placeholder-on-fail."
        ) from exc

    width, height = {
        "9:16": (720, 1280),
        "16:9": (1280, 720),
        "1:1": (1024, 1024),
        "3:2": (1080, 720),
        "2:3": (720, 1080),
        "4:3": (1024, 768),
        "3:4": (768, 1024),
    }.get(aspect_ratio, (720, 1280))

    img = Image.new("RGB", (width, height), color=(28, 30, 38))
    draw = ImageDraw.Draw(img)
    title = "Scene placeholder"
    subtitle = "(image model refused this prompt)"
    snippet = prompt.strip().split("\n", 1)[0][:120]
    font_title: Any
    font_body: Any
    try:
        font_title = ImageFont.truetype("arial.ttf", 36)
        font_body = ImageFont.truetype("arial.ttf", 22)
    except Exception:
        font_title = ImageFont.load_default()
        font_body = ImageFont.load_default()

    def _center(text: str, y: int, font: Any) -> None:
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        draw.text(((width - text_w) // 2, y), text, fill=(220, 220, 230), font=font)

    _center(title, height // 2 - 80, font_title)
    _center(subtitle, height // 2 - 30, font_body)
    # Wrap snippet to ~40 chars per line
    line = ""
    y = height // 2 + 30
    for word in snippet.split():
        if len(line) + len(word) + 1 > 40:
            _center(line, y, font_body)
            y += 28
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        _center(line, y, font_body)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", "-p", required=True)
    parser.add_argument("--filename", "-f", required=True, help="Output filename (.png)")
    parser.add_argument("--input-image", "-i", help="Optional reference image path")
    parser.add_argument("--aspect-ratio", default="1:1", choices=["1:1", "3:2", "2:3", "16:9", "9:16", "4:3", "3:4"])
    parser.add_argument("--image-size", default="1K", choices=["1K", "2K", "4K"])
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--max-retries", type=int, default=0,
        help=(
            "Compatibility retry budget for failures explicitly proven to happen "
            "before a paid submit (capped at 5). Provider responses and ambiguous "
            "generation POST failures always stop."
        ),
    )
    parser.add_argument(
        "--fallback-model", action="append", default=[],
        help=(
            "Compatibility fallback used only after a proven safe pre-submit failure. "
            "Provider responses never trigger another paid request."
        ),
    )
    parser.add_argument(
        "--placeholder-on-fail", default="no", choices=["yes", "no"],
        help="When every model refuses, write a solid-colour placeholder PNG instead of exiting non-zero. Default no.",
    )
    parser.add_argument(
        "--retry-backoff-cap", type=int, default=8,
        help="Maximum sleep seconds between retries (exponential backoff capped here).",
    )
    parser.add_argument("--api-key", "-k")
    parser.add_argument(
        "--base-url",
        default="",
        help=(
            "Override the API base. Parent-injected and canonical environment "
            "credentials permit only same-origin path changes."
        ),
    )
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    try:
        connection = _resolve_provider_connection(args.api_key, args.base_url)
    except _ImageRequestError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return SAFE_NO_SUBMIT_EXIT_CODE
    if connection is None:
        print(
            "Error: no OpenRouter API key found. Pass --api-key, set "
            "OPENROUTER_API_KEY, or configure an OpenRouter llm key in "
            "OpenSquilla config.",
            file=sys.stderr,
        )
        return SAFE_NO_SUBMIT_EXIT_CODE

    if args.input_image and not Path(args.input_image).is_file():
        print(f"Error: --input-image not found: {args.input_image}", file=sys.stderr)
        return SAFE_NO_SUBMIT_EXIT_CODE

    # Build the compatibility attempt schedule. Production provider failures are
    # deliberately non-retryable; only a caller-classified pre-submit failure may
    # advance to another entry without risking a duplicate paid request.
    max_retries = min(max(0, args.max_retries), 5)
    retry_backoff_cap = min(max(0, args.retry_backoff_cap), 60)
    fallback_models = [m for m in (args.fallback_model or []) if m]
    schedule: list[tuple[str, int, int]] = []  # (model, attempt_index_in_model, total_for_model)
    for i in range(1 + max_retries):
        schedule.append((args.model, i + 1, 1 + max_retries))
    for fm in fallback_models:
        schedule.append((fm, 1, 1))

    out_path = Path(args.filename).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    last_error: str | None = None
    credential_failure_exit: int | None = None
    policy_failure: tuple[str, str] | None = None
    for attempt_idx, (model, n, total) in enumerate(schedule, start=1):
        print(
            f"==> [{attempt_idx}/{len(schedule)}] model={model} (attempt {n}/{total})",
            file=sys.stderr,
        )
        try:
            image_bytes, response = _try_one_attempt(
                base_url=connection.base_url,
                api_key=connection.api_key,
                prompt=args.prompt,
                input_image=args.input_image,
                aspect_ratio=args.aspect_ratio,
                image_size=args.image_size,
                model=model,
                timeout=args.timeout,
                proxy_url=connection.proxy,
            )
        except _ImageRequestError as exc:
            last_error = f"[{model} #{n}] {exc}"
            print(f"  {last_error}", file=sys.stderr)
            if exc.policy_rejected and exc.provider_code is not None:
                policy_failure = (model, exc.provider_code)
            else:
                credential_failure_exit = _credential_failure_exit_code(exc.status)
            if not exc.retryable:
                print(
                    "  non-retryable provider response; stopping",
                    file=sys.stderr,
                )
                break
            if attempt_idx < len(schedule):
                backoff = min(2 ** n, retry_backoff_cap)
                print(f"  sleeping {backoff}s before next attempt", file=sys.stderr)
                time.sleep(backoff)
            continue
        except Exception:  # noqa: BLE001 - never surface arbitrary provider diagnostics
            last_error = f"[{model} #{n}] image generation failed"
            print(f"  {last_error}", file=sys.stderr)
            # An unexpected failure may have happened after the paid POST was
            # accepted. Do not submit another request without an idempotency
            # contract from the provider.
            print("  non-retryable provider response; stopping", file=sys.stderr)
            break
        try:
            out_path.write_bytes(image_bytes)
            _, receipt = _persist_receipt(
                out_path,
                _generated_receipt(response, model=model),
            )
        except OSError as exc:
            print(f"Error: could not save image receipt: {exc}", file=sys.stderr)
            return 1
        print(str(out_path))
        _print_receipt("IMAGE_GENERATION_RECEIPT", receipt)
        return 0

    if policy_failure is not None:
        rejected_model, policy_code = policy_failure
        wrote_placeholder = False
        if args.placeholder_on_fail == "yes":
            try:
                _write_placeholder_png(out_path, args.prompt, args.aspect_ratio)
                wrote_placeholder = True
            except Exception:  # noqa: BLE001 - keep provider/local internals out of output
                print("Error: local placeholder generation failed", file=sys.stderr)
        try:
            _, receipt = _persist_receipt(
                out_path,
                _policy_rejection_receipt(
                    model=rejected_model,
                    policy_code=policy_code,
                    placeholder=wrote_placeholder,
                ),
            )
        except (OSError, RuntimeError) as exc:
            print(f"Error: could not save policy-rejection receipt: {exc}", file=sys.stderr)
            return 1

        if wrote_placeholder:
            print(str(out_path))
            _print_receipt("IMAGE_GENERATION_RECEIPT", receipt)
            return 0
        # A policy refusal is still a conclusive paid-provider outcome. Emit
        # the same sanitized receipt on this invocation's captured stdout so
        # the parent can bind the sidecar to the current failed subprocess.
        _print_receipt("IMAGE_GENERATION_RECEIPT", receipt)
        print(
            "Error: upstream image provider rejected generation "
            f"(policy_code={policy_code})",
            file=sys.stderr,
        )
        return 1

    # Credential/account failures must not be hidden behind a local
    # placeholder. The parent uses this machine-owned exit to park only the
    # profile-pool key used by this run; it never retries the paid request in
    # the same run.
    if credential_failure_exit is not None:
        return credential_failure_exit

    # All real model attempts failed. Maybe fall back to a placeholder PNG.
    if args.placeholder_on_fail == "yes":
        print(
            f"All {len(schedule)} model attempt(s) failed; writing placeholder PNG. Last error: {last_error}",
            file=sys.stderr,
        )
        try:
            _write_placeholder_png(out_path, args.prompt, args.aspect_ratio)
        except Exception as exc:  # noqa: BLE001
            print(f"Error: placeholder generation failed: {exc}", file=sys.stderr)
            return 1
        try:
            _, receipt = _persist_receipt(
                out_path,
                {
                    "status": "placeholder",
                    "provider": "local",
                    "model": args.model,
                    "placeholder": True,
                    "reason": "all_model_attempts_failed",
                },
            )
        except OSError as exc:
            print(f"Error: could not save placeholder receipt: {exc}", file=sys.stderr)
            return 1
        print(str(out_path))
        _print_receipt("IMAGE_GENERATION_RECEIPT", receipt)
        return 0

    print(
        f"Error: all {len(schedule)} model attempt(s) failed. Last: {last_error}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
