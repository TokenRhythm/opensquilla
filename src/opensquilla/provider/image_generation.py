"""Provider adapters for image generation."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from io import BytesIO
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image

from opensquilla.endpoint_identity import credential_env_for_endpoint
from opensquilla.env import trust_env as _trust_env
from opensquilla.provider.app_attribution import provider_app_headers
from opensquilla.provider.correlation_context import (
    bind_provider_request_correlation,
    current_provider_request_correlation,
)
from opensquilla.provider.error_redaction import redacted_httpx_error
from opensquilla.provider.image_generation_credentials import (
    ImageGenerationCredentialResolution,
    report_image_generation_pool_failure,
    resolve_image_generation_credential,
)
from opensquilla.provider.image_generation_policy import (
    conflicting_image_generation_endpoint_provider,
    is_valid_image_generation_base_url,
    parse_image_generation_model_ref,
    resolve_image_generation_base_url,
)
from opensquilla.provider.qwen_token_plan import (
    QWEN_TOKEN_PLAN_API_KEY_ENV,
    QWEN_TOKEN_PLAN_IMAGE_BASE_URL,
)
from opensquilla.provider.tokenrhythm_correlation import (
    is_tokenrhythm_correlation_target,
    redact_tokenrhythm_install_ids,
    tokenrhythm_correlation_headers,
    tokenrhythm_install_id_headers,
)
from opensquilla.provider.types import (
    ProviderRequestCorrelation,
    derive_provider_request_correlation,
)
from opensquilla.secrets import clean_header_secret


@dataclass
class ImageGenerationRequest:
    prompt: str
    model: str
    size: str
    output_format: str = "png"
    timeout_seconds: float = 180.0
    provider_request_correlation: ProviderRequestCorrelation | None = field(
        default=None,
        repr=False,
    )
    credential_session_key: str = field(default="", repr=False)
    credential_resolution: ImageGenerationCredentialResolution | None = field(
        default=None,
        repr=False,
    )


@dataclass
class ImageGenerationAttempt:
    provider: str
    model: str
    error: str


@dataclass
class ImageGenerationResult:
    image_bytes: bytes
    mime_type: str
    model: str
    provider: str
    revised_prompt: str | None = None
    attempts: list[ImageGenerationAttempt] = field(default_factory=list)


class ImageGenerationProvider(Protocol):
    provider_id: str
    default_model: str
    auth_env_vars: tuple[str, ...]

    async def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult: ...


ImageCredentialResolver = Callable[
    [ImageGenerationRequest],
    ImageGenerationCredentialResolution,
]


def _api_key_for_request(provider: object, request: ImageGenerationRequest) -> str:
    resolution = request.credential_resolution
    if resolution is not None:
        return clean_header_secret(
            resolution.api_key,
            label=f"{getattr(provider, 'provider_id', 'image')} image API key",
        )
    resolver = getattr(provider, "_credential_resolver", None)
    if callable(resolver):
        resolved = resolver(request)
        return clean_header_secret(
            resolved.api_key,
            label=f"{getattr(provider, 'provider_id', 'image')} image API key",
        )
    fallback = getattr(provider, "_resolve_api_key", None)
    return str(fallback() if callable(fallback) else "")


def _install_id_safe_exception(
    exc: Exception,
    *,
    api_key: str,
) -> Exception | None:
    """Return a safe replacement when an exception may retain provider secrets."""

    if isinstance(exc, httpx.HTTPError):
        # HTTPX errors retain their request and, for status errors, response
        # objects. The install id may therefore be present even when str(exc)
        # contains no echo of it.
        return redacted_httpx_error(exc, api_key=api_key)

    if isinstance(exc, json.JSONDecodeError):
        safe_document = redact_tokenrhythm_install_ids(exc.doc)
        if safe_document != exc.doc:
            # JSONDecodeError retains the complete response body in ``doc``
            # even though its normal string form only reports line/column.
            return RuntimeError("Image generation provider returned invalid JSON")

    raw_error = str(exc)
    safe_error = redact_tokenrhythm_install_ids(raw_error)
    if safe_error != raw_error:
        return RuntimeError(safe_error)

    retained_state = repr(getattr(exc, "__dict__", {}))
    if redact_tokenrhythm_install_ids(retained_state) != retained_state:
        return RuntimeError("Image generation provider request failed")
    return None


def _detach_exception(exc: Exception) -> Exception:
    """Detach traceback links before re-raising from a scrubbed boundary."""

    exc.__cause__ = None
    exc.__context__ = None
    exc.__traceback__ = None
    return exc


class OpenAIImageGenerationProvider:
    provider_id = "openai"
    default_model = "gpt-image-1"
    auth_env_vars: tuple[str, ...] = ("OPENAI_API_KEY",)

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str = "https://api.openai.com/v1",
        provider_kind: str = "openai",
    ) -> None:
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._base_url = base_url.rstrip("/")
        self._provider_kind = provider_kind

    def _resolve_api_key(self) -> str:
        return clean_header_secret(
            self._api_key or os.environ.get(self._api_key_env, ""),
            label=f"{self.provider_id} image API key",
        )

    def _api_url(self, path: str) -> str:
        if self._base_url.endswith("/v1") and path.startswith("/v1/"):
            return f"{self._base_url}{path[3:]}"
        return f"{self._base_url}{path}"

    async def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        _raise_for_conflicting_official_endpoint(self.provider_id, self._base_url)
        api_key = _api_key_for_request(self, request)
        if not api_key:
            raise RuntimeError(f"{self._api_key_env} is not set")
        protect_install_id = is_tokenrhythm_correlation_target(
            self._provider_kind,
            self._base_url,
        )

        payload = {
            "model": request.model,
            "prompt": request.prompt,
            "size": request.size,
            "output_format": request.output_format,
            "n": 1,
        }
        from opensquilla.engine.usage_http import reserve_direct_usage_call

        usage = await reserve_direct_usage_call(
            provider=self.provider_id,
            model=request.model,
            base_url=self._base_url,
        )
        safe_request_error: Exception | None = None
        cancelled_request_error: asyncio.CancelledError | None = None
        client: Any = None
        response: Any = None
        data: Any = None
        try:
            async with httpx.AsyncClient(
                timeout=request.timeout_seconds,
                trust_env=_trust_env(),
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    self._api_url("/v1/images/generations"),
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        **tokenrhythm_install_id_headers(
                            self._provider_kind,
                            self._base_url,
                        ),
                        **tokenrhythm_correlation_headers(
                            self._provider_kind,
                            self._base_url,
                            request.provider_request_correlation,
                        ),
                    },
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                await usage.finalize_openai_response(
                    data,
                    raw_json=str(getattr(response, "text", "") or ""),
                )
        except asyncio.CancelledError:
            if not protect_install_id:
                await usage.mark_unknown("cancelled")
                raise
            client = None
            response = None
            data = None
            payload = {}
            try:
                await usage.mark_unknown("cancelled")
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            cancelled_request_error = asyncio.CancelledError()
        except Exception as exc:
            if not protect_install_id:
                await usage.mark_unknown("direct_request_failed")
                raise
            safe_error = _install_id_safe_exception(exc, api_key=api_key)
            if safe_error is None:
                safe_error = _detach_exception(exc)
            client = None
            response = None
            data = None
            payload = {}
            try:
                await usage.mark_unknown("direct_request_failed")
            except asyncio.CancelledError:
                cancelled_request_error = asyncio.CancelledError()
            except Exception:
                safe_request_error = safe_error
            else:
                safe_request_error = safe_error

        if cancelled_request_error is not None:
            client = None
            response = None
            data = None
            payload = {}
            raise cancelled_request_error
        if safe_request_error is not None:
            client = None
            response = None
            data = None
            payload = {}
            # Raise outside the exception handler so the original exception is
            # not retained as a secret-bearing ``__context__``.
            raise safe_request_error

        if not protect_install_id:
            items = data.get("data") or []
            if not items:
                raise RuntimeError("Image generation provider returned no images")
            first = items[0]
            b64_json = first.get("b64_json")
            if not b64_json:
                raise RuntimeError(
                    "Image generation provider returned no b64_json"
                )
            image_bytes = base64.b64decode(b64_json)
            output_format = request.output_format.lower()
            mime_type = (
                "image/jpeg"
                if output_format in {"jpg", "jpeg"}
                else f"image/{output_format}"
            )
            return ImageGenerationResult(
                image_bytes=image_bytes,
                mime_type=mime_type,
                model=request.model,
                provider=self.provider_id,
                revised_prompt=first.get("revised_prompt"),
            )

        postprocess_error: Exception | None = None
        safe_items: Any = None
        safe_first: Any = None
        safe_b64_json: Any = None
        raw_revised_prompt: Any = None
        revised_prompt: str | None = None
        try:
            safe_items = data.get("data") or []
            if not safe_items:
                raise RuntimeError("Image generation provider returned no images")
            safe_first = safe_items[0]
            safe_b64_json = safe_first.get("b64_json")
            if not safe_b64_json:
                raise RuntimeError(
                    "Image generation provider returned no b64_json"
                )
            image_bytes = base64.b64decode(safe_b64_json)
            raw_revised_prompt = safe_first.get("revised_prompt")
            if isinstance(raw_revised_prompt, str):
                revised_prompt = redact_tokenrhythm_install_ids(raw_revised_prompt)
        except Exception as exc:
            safe_error = _install_id_safe_exception(exc, api_key=api_key)
            postprocess_error = safe_error or _detach_exception(exc)

        client = None
        response = None
        data = None
        payload = {}
        safe_items = None
        safe_first = None
        safe_b64_json = None
        raw_revised_prompt = None
        if postprocess_error is not None:
            raise postprocess_error

        output_format = request.output_format.lower()
        mime_type = (
            "image/jpeg"
            if output_format in {"jpg", "jpeg"}
            else f"image/{output_format}"
        )
        return ImageGenerationResult(
            image_bytes=image_bytes,
            mime_type=mime_type,
            model=request.model,
            provider=self.provider_id,
            revised_prompt=revised_prompt,
        )


class OpenRouterImageGenerationProvider:
    provider_id = "openrouter"
    default_model = "google/gemini-3.1-flash-image-preview"
    auth_env_vars: tuple[str, ...] = ("OPENROUTER_API_KEY",)

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_key_env: str = "OPENROUTER_API_KEY",
        base_url: str = "https://openrouter.ai/api/v1",
        provider_kind: str = "openrouter",
    ) -> None:
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._base_url = base_url.rstrip("/")
        self._provider_kind = provider_kind

    def _resolve_api_key(self) -> str:
        return clean_header_secret(
            self._api_key or os.environ.get(self._api_key_env, ""),
            label=f"{self.provider_id} image API key",
        )

    def _api_url(self, path: str) -> str:
        if self._base_url.endswith("/v1") and path.startswith("/v1/"):
            return f"{self._base_url}{path[3:]}"
        return f"{self._base_url}{path}"

    @staticmethod
    def _image_config_for_size(size: str) -> dict[str, str]:
        aspect_ratio = {
            "1024x1024": "1:1",
            "1536x1024": "3:2",
            "1024x1536": "2:3",
        }.get(size, "1:1")
        return {"aspect_ratio": aspect_ratio, "image_size": "1K"}

    async def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        _raise_for_conflicting_official_endpoint(self.provider_id, self._base_url)
        api_key = _api_key_for_request(self, request)
        if not api_key:
            raise RuntimeError(f"{self._api_key_env} is not set")
        protect_install_id = is_tokenrhythm_correlation_target(
            self._provider_kind,
            self._base_url,
        )

        payload = {
            "model": request.model,
            "messages": [{"role": "user", "content": request.prompt}],
            "modalities": ["image", "text"],
            "stream": False,
            "image_config": self._image_config_for_size(request.size),
        }
        from opensquilla.engine.usage_http import reserve_direct_usage_call

        usage = await reserve_direct_usage_call(
            provider=self.provider_id,
            model=request.model,
            base_url=self._base_url,
        )
        safe_request_error: Exception | None = None
        cancelled_request_error: asyncio.CancelledError | None = None
        client: Any = None
        response: Any = None
        data: Any = None
        try:
            async with httpx.AsyncClient(
                timeout=request.timeout_seconds,
                trust_env=_trust_env(),
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    self._api_url("/v1/chat/completions"),
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        **provider_app_headers(self._base_url),
                        **tokenrhythm_install_id_headers(
                            self._provider_kind,
                            self._base_url,
                        ),
                        **tokenrhythm_correlation_headers(
                            self._provider_kind,
                            self._base_url,
                            request.provider_request_correlation,
                        ),
                    },
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                await usage.finalize_openai_response(
                    data,
                    raw_json=str(getattr(response, "text", "") or ""),
                )
        except asyncio.CancelledError:
            if not protect_install_id:
                await usage.mark_unknown("cancelled")
                raise
            client = None
            response = None
            data = None
            payload = {}
            try:
                await usage.mark_unknown("cancelled")
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            cancelled_request_error = asyncio.CancelledError()
        except Exception as exc:
            if not protect_install_id:
                await usage.mark_unknown("direct_request_failed")
                raise
            safe_error = _install_id_safe_exception(exc, api_key=api_key)
            if safe_error is None:
                safe_error = _detach_exception(exc)
            client = None
            response = None
            data = None
            payload = {}
            try:
                await usage.mark_unknown("direct_request_failed")
            except asyncio.CancelledError:
                cancelled_request_error = asyncio.CancelledError()
            except Exception:
                safe_request_error = safe_error
            else:
                safe_request_error = safe_error

        if cancelled_request_error is not None:
            client = None
            response = None
            data = None
            payload = {}
            raise cancelled_request_error
        if safe_request_error is not None:
            client = None
            response = None
            data = None
            payload = {}
            raise safe_request_error

        if not protect_install_id:
            image_url = _extract_openrouter_image_url(data)
            if not image_url:
                raise RuntimeError("Image generation provider returned no images")
            mime_type, image_bytes = _decode_data_url(image_url)
            return ImageGenerationResult(
                image_bytes=image_bytes,
                mime_type=mime_type,
                model=request.model,
                provider=self.provider_id,
            )

        postprocess_error: Exception | None = None
        safe_image_url: str | None = None
        try:
            safe_image_url = _extract_openrouter_image_url(data)
            if not safe_image_url:
                raise RuntimeError("Image generation provider returned no images")
            mime_type, image_bytes = _decode_data_url(safe_image_url)
        except Exception as exc:
            safe_error = _install_id_safe_exception(exc, api_key=api_key)
            postprocess_error = safe_error or _detach_exception(exc)

        client = None
        response = None
        data = None
        payload = {}
        safe_image_url = None
        if postprocess_error is not None:
            raise postprocess_error

        return ImageGenerationResult(
            image_bytes=image_bytes,
            mime_type=mime_type,
            model=request.model,
            provider=self.provider_id,
        )


class TokenRhythmImageGenerationProvider:
    """OpenAI Images-compatible adapter for TokenRhythm image models."""

    provider_id = "tokenrhythm"
    default_model = "qwen-image-2.0"
    auth_env_vars: tuple[str, ...] = ("TOKENRHYTHM_API_KEY",)

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_key_env: str = "TOKENRHYTHM_API_KEY",
        base_url: str = "https://tokenrhythm.studio/v1",
    ) -> None:
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._base_url = base_url.rstrip("/")

    def _resolve_api_key(self) -> str:
        return clean_header_secret(
            self._api_key or os.environ.get(self._api_key_env, ""),
            label=f"{self.provider_id} image API key",
        )

    def _api_url(self, path: str) -> str:
        if self._base_url.endswith("/v1") and path.startswith("/v1/"):
            return f"{self._base_url}{path[3:]}"
        return f"{self._base_url}{path}"

    async def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        _raise_for_conflicting_official_endpoint(self.provider_id, self._base_url)
        api_key = _api_key_for_request(self, request)
        if not api_key:
            raise RuntimeError(f"{self._api_key_env} is not set")

        payload = {
            "model": request.model,
            "prompt": request.prompt,
            "size": request.size,
            "n": 1,
        }
        from opensquilla.engine.usage_http import reserve_direct_usage_call

        usage = await reserve_direct_usage_call(
            provider=self.provider_id,
            model=request.model,
            base_url=self._base_url,
        )
        safe_request_error: Exception | None = None
        cancelled_request_error: asyncio.CancelledError | None = None
        client: Any = None
        response: Any = None
        data: Any = None
        try:
            async with httpx.AsyncClient(
                timeout=request.timeout_seconds,
                trust_env=_trust_env(),
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    self._api_url("/v1/images/generations"),
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        **provider_app_headers(self._base_url),
                        **tokenrhythm_install_id_headers(
                            "tokenrhythm",
                            self._base_url,
                        ),
                        **tokenrhythm_correlation_headers(
                            "tokenrhythm",
                            self._base_url,
                            request.provider_request_correlation,
                        ),
                    },
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                await usage.finalize_openai_response(
                    data,
                    raw_json=str(getattr(response, "text", "") or ""),
                    allow_billing_only=True,
                )
        except asyncio.CancelledError:
            client = None
            response = None
            data = None
            payload = {}
            try:
                await usage.mark_unknown("cancelled")
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            cancelled_request_error = asyncio.CancelledError()
        except Exception as exc:
            safe_error = _install_id_safe_exception(exc, api_key=api_key)
            if safe_error is None:
                safe_error = _detach_exception(exc)
            client = None
            response = None
            data = None
            payload = {}
            try:
                await usage.mark_unknown("direct_request_failed")
            except asyncio.CancelledError:
                cancelled_request_error = asyncio.CancelledError()
            except Exception:
                safe_request_error = safe_error
            else:
                safe_request_error = safe_error

        if cancelled_request_error is not None:
            client = None
            response = None
            data = None
            payload = {}
            raise cancelled_request_error
        if safe_request_error is not None:
            client = None
            response = None
            data = None
            payload = {}
            raise safe_request_error

        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            client = None
            response = None
            data = None
            payload = {}
            items = None
            raise RuntimeError("Image generation provider returned no images")
        first: Any = items[0]
        b64_json: Any = first.get("b64_json")
        image_url: Any = first.get("url")
        raw_revised_prompt = first.get("revised_prompt")
        revised_prompt = (
            redact_tokenrhythm_install_ids(raw_revised_prompt)
            if isinstance(raw_revised_prompt, str)
            else None
        )
        client = None
        response = None
        data = None
        payload = {}
        items = None
        first = None
        raw_revised_prompt = None
        if isinstance(b64_json, str) and b64_json:
            invalid_b64_json = False
            try:
                image_bytes = base64.b64decode(b64_json)
            except (ValueError, TypeError):
                invalid_b64_json = True
            b64_json = None
            image_url = None
            if invalid_b64_json:
                raise RuntimeError(
                    "Image generation provider returned invalid b64_json"
                )
            output_format = request.output_format.lower()
            mime_type = (
                "image/jpeg"
                if output_format in {"jpg", "jpeg"}
                else f"image/{output_format}"
            )
        else:
            if not isinstance(image_url, str) or not image_url:
                b64_json = None
                image_url = None
                raise RuntimeError(
                    "Image generation provider returned neither b64_json nor url"
                )
            download_error: Exception | None = None
            download_cancelled: asyncio.CancelledError | None = None
            if image_url.startswith("data:"):
                try:
                    mime_type, image_bytes = _decode_data_url(image_url)
                except Exception as exc:
                    safe_error = _install_id_safe_exception(exc, api_key=api_key)
                    download_error = safe_error or _detach_exception(exc)
            else:
                try:
                    mime_type, image_bytes = await _download_tokenrhythm_image(
                        image_url,
                        timeout_seconds=request.timeout_seconds,
                    )
                except asyncio.CancelledError:
                    download_cancelled = asyncio.CancelledError()
                except Exception as exc:
                    safe_error = _install_id_safe_exception(exc, api_key=api_key)
                    download_error = safe_error or _detach_exception(exc)
            b64_json = None
            image_url = None
            if download_cancelled is not None:
                raise download_cancelled
            if download_error is not None:
                raise download_error

        return ImageGenerationResult(
            image_bytes=image_bytes,
            mime_type=mime_type,
            model=request.model,
            provider=self.provider_id,
            revised_prompt=revised_prompt,
        )


class QwenTokenPlanImageGenerationProvider:
    """Native adapter for the Token Plan multimodal-generation API."""

    provider_id = "qwen_token_plan"
    default_model = "wan2.7-image"
    auth_env_vars: tuple[str, ...] = (QWEN_TOKEN_PLAN_API_KEY_ENV,)
    _generation_path = "/services/aigc/multimodal-generation/generation"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_key_env: str = QWEN_TOKEN_PLAN_API_KEY_ENV,
        base_url: str = QWEN_TOKEN_PLAN_IMAGE_BASE_URL,
    ) -> None:
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._base_url = base_url.rstrip("/")

    def _resolve_api_key(self) -> str:
        return clean_header_secret(
            self._api_key or os.environ.get(self._api_key_env, ""),
            label=f"{self.provider_id} image API key",
        )

    @staticmethod
    def _wire_size(size: str) -> str:
        normalized = str(size or "").strip().lower().replace("*", "x")
        dimensions = normalized.split("x")
        if (
            len(dimensions) != 2
            or not all(part.isdigit() for part in dimensions)
            or any(int(part) <= 0 for part in dimensions)
        ):
            raise RuntimeError(f"Invalid Token Plan image size: {size!r}")
        return "*".join(dimensions)

    async def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        _raise_for_conflicting_official_endpoint(self.provider_id, self._base_url)
        api_key = _api_key_for_request(self, request)
        if not api_key:
            raise RuntimeError(f"{self._api_key_env} is not set")

        payload = {
            "model": request.model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"text": request.prompt}],
                    }
                ]
            },
            "parameters": {
                "size": self._wire_size(request.size),
                "n": 1,
                "thinking_mode": False,
            },
        }
        from opensquilla.engine.usage_http import reserve_direct_usage_call

        usage = await reserve_direct_usage_call(
            provider=self.provider_id,
            model=request.model,
            base_url=self._base_url,
        )
        try:
            async with httpx.AsyncClient(
                timeout=request.timeout_seconds,
                trust_env=_trust_env(),
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    f"{self._base_url}{self._generation_path}",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                await usage.finalize_openai_response(
                    data,
                    raw_json=str(getattr(response, "text", "") or ""),
                )
        except asyncio.CancelledError:
            await usage.mark_unknown("cancelled")
            raise
        except Exception:
            await usage.mark_unknown("direct_request_failed")
            raise

        image_url = _extract_qwen_token_plan_image_url(data)
        if not image_url:
            raise RuntimeError("Image generation provider returned no images")
        mime_type, image_bytes = await _download_qwen_token_plan_image(
            image_url,
            timeout_seconds=request.timeout_seconds,
        )
        return ImageGenerationResult(
            image_bytes=image_bytes,
            mime_type=mime_type,
            model=request.model,
            provider=self.provider_id,
        )


def _extract_openrouter_image_url(data: dict) -> str | None:
    for choice in data.get("choices") or []:
        message = choice.get("message") or {}
        for image in message.get("images") or []:
            image_url = image.get("image_url") or image.get("imageUrl") or {}
            url = image_url.get("url")
            if isinstance(url, str) and url:
                return url
    return None


def _extract_qwen_token_plan_image_url(data: dict) -> str | None:
    output = data.get("output") or {}
    for choice in output.get("choices") or []:
        message = choice.get("message") or {}
        for item in message.get("content") or []:
            if not isinstance(item, dict):
                continue
            image_url = item.get("image") or item.get("image_url")
            if isinstance(image_url, str) and image_url:
                return image_url
    return None


_GENERATED_IMAGE_DOWNLOAD_LIMIT = 20 * 1024 * 1024
_GENERATED_IMAGE_REDIRECT_LIMIT = 3


async def _download_qwen_token_plan_image(
    image_url: str,
    *,
    timeout_seconds: float,
) -> tuple[str, bytes]:
    """Download one signed result URL without exposing it in failures."""

    return await _download_generated_image(
        image_url,
        timeout_seconds=timeout_seconds,
        provider_label="Token Plan",
    )


async def _download_tokenrhythm_image(
    image_url: str,
    *,
    timeout_seconds: float,
) -> tuple[str, bytes]:
    """Download one TokenRhythm result URL through the shared SSRF guard."""

    return await _download_generated_image(
        image_url,
        timeout_seconds=timeout_seconds,
        provider_label="TokenRhythm",
    )


async def _download_generated_image(
    image_url: str,
    *,
    timeout_seconds: float,
    provider_label: str,
) -> tuple[str, bytes]:
    """Download one signed generated-image URL without exposing it in failures."""

    from opensquilla.tools.ssrf import (
        environment_proxy_url,
        pinned_transport,
        validate_http_url_for_fetch,
    )

    current_url = image_url
    for redirect_count in range(_GENERATED_IMAGE_REDIRECT_LIMIT + 1):
        try:
            parsed = urlsplit(current_url)
            if (
                parsed.scheme.lower() != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
            ):
                raise ValueError("unsafe generated image URL")
            vetted_ips = validate_http_url_for_fetch(current_url)

            transport_kwargs: dict[str, object] = {}
            if _trust_env():
                proxy_url = environment_proxy_url(current_url)
                if proxy_url is not None:
                    transport_kwargs["proxy"] = proxy_url
            transport = pinned_transport(current_url, vetted_ips, **transport_kwargs)
            client_kwargs: dict[str, object] = {
                "timeout": timeout_seconds,
                "follow_redirects": False,
                "trust_env": _trust_env(),
            }
            if transport is not None:
                client_kwargs["transport"] = transport

            async with httpx.AsyncClient(**client_kwargs) as client:  # type: ignore[arg-type]
                async with client.stream("GET", current_url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("redirect without location")
                        current_url = urljoin(current_url, location)
                        continue

                    response.raise_for_status()
                    content_length = response.headers.get("content-length")
                    if (
                        content_length is not None
                        and content_length.isdigit()
                        and int(content_length) > _GENERATED_IMAGE_DOWNLOAD_LIMIT
                    ):
                        raise ValueError("generated image exceeds download limit")
                    image_bytes = bytearray()
                    async for chunk in response.aiter_bytes():
                        image_bytes.extend(chunk)
                        if len(image_bytes) > _GENERATED_IMAGE_DOWNLOAD_LIMIT:
                            raise ValueError("generated image exceeds download limit")
                    content_type = (
                        response.headers.get("content-type", "image/png")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RuntimeError(
                f"Failed to securely download the generated {provider_label} image"
            ) from None

        if not image_bytes:
            raise RuntimeError(f"{provider_label} returned an empty generated image")
        mime_type = content_type if content_type.startswith("image/") else "image/png"
        return mime_type, bytes(image_bytes)

    raise RuntimeError(f"{provider_label} generated image exceeded the redirect limit")


def _decode_data_url(data_url: str) -> tuple[str, bytes]:
    prefix, sep, encoded = data_url.partition(",")
    if not sep or ";base64" not in prefix:
        raise RuntimeError("Image generation provider returned unsupported image URL")
    mime_type = prefix.removeprefix("data:").split(";", 1)[0] or "image/png"
    return mime_type, base64.b64decode(encoded)


def _raise_for_conflicting_official_endpoint(provider_id: str, base_url: str) -> None:
    if not is_valid_image_generation_base_url(base_url):
        raise RuntimeError(
            f"Image generation provider {provider_id!r} has invalid endpoint "
            f"{base_url!r}; set an absolute http:// or https:// image base URL"
        )
    conflicting_provider = conflicting_image_generation_endpoint_provider(
        provider_id,
        base_url,
    )
    if conflicting_provider is None:
        return
    raise RuntimeError(
        f"Image generation provider {provider_id!r} cannot use "
        f"{conflicting_provider!r}'s official endpoint {base_url!r}; "
        f"set the {provider_id!r} image base URL before retrying"
    )


_IMAGE_OUTPUT_FORMATS: dict[str, tuple[str, str]] = {
    "png": ("PNG", "image/png"),
    "jpg": ("JPEG", "image/jpeg"),
    "jpeg": ("JPEG", "image/jpeg"),
    "webp": ("WEBP", "image/webp"),
}


def _normalize_generated_image(
    result: ImageGenerationResult,
    output_format: str,
) -> ImageGenerationResult:
    normalized_format = output_format.strip().lower()
    format_spec = _IMAGE_OUTPUT_FORMATS.get(normalized_format)
    if format_spec is None:
        raise RuntimeError(f"Unsupported image output format: {output_format!r}")
    pillow_format, mime_type = format_spec

    try:
        with Image.open(BytesIO(result.image_bytes)) as image:
            image.verify()
        with Image.open(BytesIO(result.image_bytes)) as image:
            image.load()
            output_image: Image.Image = image
            if pillow_format == "JPEG" and image.mode != "RGB":
                if image.mode in {"RGBA", "LA"} or (
                    image.mode == "P" and "transparency" in image.info
                ):
                    rgba = image.convert("RGBA")
                    try:
                        output_image = Image.new("RGB", image.size, "white")
                        output_image.paste(rgba, mask=rgba.getchannel("A"))
                    finally:
                        rgba.close()
                else:
                    output_image = image.convert("RGB")

            buffer = BytesIO()
            try:
                output_image.save(buffer, format=pillow_format)
            finally:
                if output_image is not image:
                    output_image.close()
    except Exception as exc:
        raise RuntimeError(
            f"Image generation provider returned invalid image bytes: {exc}"
        ) from exc

    return replace(
        result,
        image_bytes=buffer.getvalue(),
        mime_type=mime_type,
    )


_PROVIDERS: dict[str, ImageGenerationProvider] = {}


def _get_config_attr(config: object | None, name: str, default: str = "") -> str:
    value = getattr(config, name, default) if config is not None else default
    return value if isinstance(value, str) else default


def _field_was_set(config: object | None, name: str) -> bool:
    fields_set = getattr(config, "model_fields_set", None)
    return isinstance(fields_set, set) and name in fields_set


def _resolve_configured_base_url(
    *,
    provider_id: str,
    provider_config: object | None,
    llm_config: object | None,
    default_base_url: str,
    gateway_config: object | None = None,
) -> str:
    return resolve_image_generation_base_url(
        provider_id=provider_id,
        provider_config=provider_config,
        llm_config=llm_config,
        default_base_url=default_base_url,
        gateway_config=gateway_config,
    )


def _credential_resolver_for_provider(
    *,
    provider_id: str,
    provider_config: object | None,
    default_env_key: str,
    default_base_url: str,
    effective_base_url: str,
    gateway_config: object | None,
    llm_config: object | None,
) -> ImageCredentialResolver:
    def resolve(request: ImageGenerationRequest) -> ImageGenerationCredentialResolution:
        correlation = request.provider_request_correlation
        session_key = (
            request.credential_session_key
            or (correlation.session_id if correlation is not None else "")
            or (correlation.execution_id if correlation is not None else "")
            or "image-generation"
        )
        return resolve_image_generation_credential(
            provider_id=provider_id,
            provider_config=provider_config,
            default_env_key=default_env_key,
            default_base_url=default_base_url,
            effective_base_url=effective_base_url,
            gateway_config=gateway_config,
            llm_config=llm_config,
            model=request.model,
            runtime=True,
            session_key=session_key,
        )

    return resolve


def _register_configured_provider(
    provider: ImageGenerationProvider,
    *,
    credential_resolver: ImageCredentialResolver,
) -> None:
    setattr(provider, "_credential_resolver", credential_resolver)
    register_image_generation_provider(provider)


def register_image_generation_provider(provider: ImageGenerationProvider) -> None:
    _PROVIDERS[provider.provider_id] = provider


def reset_image_generation_providers(
    image_config: object | None = None,
    *,
    llm_config: object | None = None,
    gateway_config: object | None = None,
) -> None:
    _PROVIDERS.clear()
    providers_config = getattr(image_config, "providers", None)
    openai_config = getattr(providers_config, "openai", None)
    openrouter_config = getattr(providers_config, "openrouter", None)
    tokenrhythm_config = getattr(providers_config, "tokenrhythm", None)
    qwen_token_plan_config = getattr(providers_config, "qwen_token_plan", None)

    openai_base_url = _resolve_configured_base_url(
        provider_id="openai",
        provider_config=openai_config,
        llm_config=llm_config,
        default_base_url="https://api.openai.com/v1",
        gateway_config=gateway_config,
    )
    openai_api_key_env = credential_env_for_endpoint(
        configured_env=_get_config_attr(openai_config, "api_key_env", "OPENAI_API_KEY"),
        configured_explicitly=_field_was_set(openai_config, "api_key_env"),
        default_env="OPENAI_API_KEY",
        default_base_url="https://api.openai.com/v1",
        effective_base_url=openai_base_url,
    )
    openai_credential_resolver = _credential_resolver_for_provider(
        provider_id="openai",
        provider_config=openai_config,
        default_env_key="OPENAI_API_KEY",
        default_base_url="https://api.openai.com/v1",
        effective_base_url=openai_base_url,
        gateway_config=gateway_config,
        llm_config=llm_config,
    )
    openai_initial_credential = resolve_image_generation_credential(
        provider_id="openai",
        provider_config=openai_config,
        default_env_key="OPENAI_API_KEY",
        default_base_url="https://api.openai.com/v1",
        effective_base_url=openai_base_url,
        gateway_config=gateway_config,
        llm_config=llm_config,
    )
    _register_configured_provider(
        OpenAIImageGenerationProvider(
            api_key=openai_initial_credential.api_key,
            api_key_env=openai_api_key_env,
            base_url=openai_base_url,
        ),
        credential_resolver=openai_credential_resolver,
    )
    openrouter_base_url = _resolve_configured_base_url(
        provider_id="openrouter",
        provider_config=openrouter_config,
        llm_config=llm_config,
        default_base_url="https://openrouter.ai/api/v1",
        gateway_config=gateway_config,
    )
    openrouter_api_key_env = credential_env_for_endpoint(
        configured_env=_get_config_attr(
            openrouter_config,
            "api_key_env",
            "OPENROUTER_API_KEY",
        ),
        configured_explicitly=_field_was_set(openrouter_config, "api_key_env"),
        default_env="OPENROUTER_API_KEY",
        default_base_url="https://openrouter.ai/api/v1",
        effective_base_url=openrouter_base_url,
    )
    openrouter_credential_resolver = _credential_resolver_for_provider(
        provider_id="openrouter",
        provider_config=openrouter_config,
        default_env_key="OPENROUTER_API_KEY",
        default_base_url="https://openrouter.ai/api/v1",
        effective_base_url=openrouter_base_url,
        gateway_config=gateway_config,
        llm_config=llm_config,
    )
    openrouter_initial_credential = resolve_image_generation_credential(
        provider_id="openrouter",
        provider_config=openrouter_config,
        default_env_key="OPENROUTER_API_KEY",
        default_base_url="https://openrouter.ai/api/v1",
        effective_base_url=openrouter_base_url,
        gateway_config=gateway_config,
        llm_config=llm_config,
    )
    _register_configured_provider(
        OpenRouterImageGenerationProvider(
            api_key=openrouter_initial_credential.api_key,
            api_key_env=openrouter_api_key_env,
            base_url=openrouter_base_url,
        ),
        credential_resolver=openrouter_credential_resolver,
    )
    tokenrhythm_base_url = _resolve_configured_base_url(
        provider_id="tokenrhythm",
        provider_config=tokenrhythm_config,
        llm_config=llm_config,
        default_base_url="https://tokenrhythm.studio/v1",
        gateway_config=gateway_config,
    )
    tokenrhythm_api_key_env = credential_env_for_endpoint(
        configured_env=_get_config_attr(
            tokenrhythm_config,
            "api_key_env",
            "TOKENRHYTHM_API_KEY",
        ),
        configured_explicitly=_field_was_set(tokenrhythm_config, "api_key_env"),
        default_env="TOKENRHYTHM_API_KEY",
        default_base_url="https://tokenrhythm.studio/v1",
        effective_base_url=tokenrhythm_base_url,
    )
    tokenrhythm_credential_resolver = _credential_resolver_for_provider(
        provider_id="tokenrhythm",
        provider_config=tokenrhythm_config,
        default_env_key="TOKENRHYTHM_API_KEY",
        default_base_url="https://tokenrhythm.studio/v1",
        effective_base_url=tokenrhythm_base_url,
        gateway_config=gateway_config,
        llm_config=llm_config,
    )
    tokenrhythm_initial_credential = resolve_image_generation_credential(
        provider_id="tokenrhythm",
        provider_config=tokenrhythm_config,
        default_env_key="TOKENRHYTHM_API_KEY",
        default_base_url="https://tokenrhythm.studio/v1",
        effective_base_url=tokenrhythm_base_url,
        gateway_config=gateway_config,
        llm_config=llm_config,
    )
    _register_configured_provider(
        TokenRhythmImageGenerationProvider(
            api_key=tokenrhythm_initial_credential.api_key,
            api_key_env=tokenrhythm_api_key_env,
            base_url=tokenrhythm_base_url,
        ),
        credential_resolver=tokenrhythm_credential_resolver,
    )
    qwen_token_plan_base_url = _resolve_configured_base_url(
        provider_id="qwen_token_plan",
        provider_config=qwen_token_plan_config,
        llm_config=llm_config,
        default_base_url=QWEN_TOKEN_PLAN_IMAGE_BASE_URL,
        gateway_config=gateway_config,
    )
    qwen_token_plan_api_key_env = credential_env_for_endpoint(
        configured_env=_get_config_attr(
            qwen_token_plan_config,
            "api_key_env",
            QWEN_TOKEN_PLAN_API_KEY_ENV,
        ),
        configured_explicitly=_field_was_set(
            qwen_token_plan_config,
            "api_key_env",
        ),
        default_env=QWEN_TOKEN_PLAN_API_KEY_ENV,
        default_base_url=QWEN_TOKEN_PLAN_IMAGE_BASE_URL,
        effective_base_url=qwen_token_plan_base_url,
    )
    qwen_token_plan_credential_resolver = _credential_resolver_for_provider(
        provider_id="qwen_token_plan",
        provider_config=qwen_token_plan_config,
        default_env_key=QWEN_TOKEN_PLAN_API_KEY_ENV,
        default_base_url=QWEN_TOKEN_PLAN_IMAGE_BASE_URL,
        effective_base_url=qwen_token_plan_base_url,
        gateway_config=gateway_config,
        llm_config=llm_config,
    )
    qwen_token_plan_initial_credential = resolve_image_generation_credential(
        provider_id="qwen_token_plan",
        provider_config=qwen_token_plan_config,
        default_env_key=QWEN_TOKEN_PLAN_API_KEY_ENV,
        default_base_url=QWEN_TOKEN_PLAN_IMAGE_BASE_URL,
        effective_base_url=qwen_token_plan_base_url,
        gateway_config=gateway_config,
        llm_config=llm_config,
    )
    _register_configured_provider(
        QwenTokenPlanImageGenerationProvider(
            api_key=qwen_token_plan_initial_credential.api_key,
            api_key_env=qwen_token_plan_api_key_env,
            base_url=qwen_token_plan_base_url,
        ),
        credential_resolver=qwen_token_plan_credential_resolver,
    )


def list_image_generation_providers() -> list[ImageGenerationProvider]:
    return list(_PROVIDERS.values())


def get_image_generation_provider(provider_id: str) -> ImageGenerationProvider | None:
    return _PROVIDERS.get(provider_id)


def _is_image_generation_correlation(
    correlation: ProviderRequestCorrelation,
) -> bool:
    return correlation.call_kind in {
        "auxiliary.image_generation",
        "auxiliary.image_generation.provider_fallback",
    }


def _provider_fallback_correlation(
    correlation: ProviderRequestCorrelation | None,
) -> ProviderRequestCorrelation | None:
    if correlation is None or correlation.call_kind.endswith(".provider_fallback"):
        return correlation
    return derive_provider_request_correlation(
        correlation,
        call_kind=f"{correlation.call_kind}.provider_fallback",
    )


async def generate_with_fallbacks(
    *,
    request: ImageGenerationRequest,
    candidates: list[str],
) -> ImageGenerationResult:
    correlation_base = (
        request.provider_request_correlation
        or current_provider_request_correlation()
    )
    correlation = correlation_base
    if correlation is not None and not _is_image_generation_correlation(correlation):
        correlation = derive_provider_request_correlation(
            correlation,
            execution_id=uuid.uuid4().hex,
            call_kind="auxiliary.image_generation",
        )
    request = replace(request, provider_request_correlation=correlation)
    attempts: list[ImageGenerationAttempt] = []
    last_error: Exception | None = None
    for candidate_index, candidate in enumerate(candidates):
        try:
            provider_id, model = parse_image_generation_model_ref(candidate)
        except ValueError as exc:
            raw_error = str(exc)
            safe_error = redact_tokenrhythm_install_ids(raw_error)
            attempts.append(
                ImageGenerationAttempt(provider="", model=candidate, error=safe_error)
            )
            last_error = exc if safe_error == raw_error else RuntimeError(safe_error)
            continue
        provider = get_image_generation_provider(provider_id)
        if provider is None:
            error = f"No image generation provider registered for {provider_id}"
            attempts.append(ImageGenerationAttempt(provider_id, model, error))
            last_error = RuntimeError(error)
            continue
        call_correlation = (
            correlation
            if candidate_index == 0
            else _provider_fallback_correlation(correlation)
        )
        call_request = replace(
            request,
            model=model,
            provider_request_correlation=call_correlation,
        )
        credential_resolution: ImageGenerationCredentialResolution | None = None
        try:
            credential_resolver = getattr(provider, "_credential_resolver", None)
            if callable(credential_resolver):
                credential_resolution = credential_resolver(call_request)
                call_request = replace(
                    call_request,
                    credential_resolution=credential_resolution,
                )
            with bind_provider_request_correlation(call_correlation):
                result = await provider.generate(call_request)
            if not result.image_bytes:
                raise RuntimeError("Image generation provider returned empty image")
            result = _normalize_generated_image(result, request.output_format)
            result.attempts = attempts
            return result
        except Exception as exc:  # noqa: BLE001 - failures are summarized for fallback
            report_image_generation_pool_failure(credential_resolution, exc)
            raw_error = str(exc)
            safe_error = redact_tokenrhythm_install_ids(raw_error)
            attempts.append(ImageGenerationAttempt(provider_id, model, safe_error))
            last_error = exc if safe_error == raw_error else RuntimeError(safe_error)

    if len(attempts) <= 1 and last_error is not None:
        raise last_error
    summary = " | ".join(
        f"{attempt.provider}/{attempt.model}: {attempt.error}" for attempt in attempts
    )
    message = redact_tokenrhythm_install_ids(
        f"All image generation models failed ({len(attempts)}): {summary}"
    )
    raise RuntimeError(message)


reset_image_generation_providers()
