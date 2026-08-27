"""Media built-in tools: image, image_generate, pdf, tts."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from opensquilla.artifacts import (
    DEFAULT_ARTIFACT_DISK_BUDGET_BYTES,
    DEFAULT_ARTIFACT_MAX_BYTES,
    ArtifactBudgetError,
    ArtifactStore,
    artifact_payload,
)
from opensquilla.engine.usage_accounting import (
    account_provider_stream,
    current_usage_accounting_scope,
    provider_accounts_physical_usage,
)
from opensquilla.env import trust_env as _trust_env
from opensquilla.provider.audio import (
    AudioGenerationResult,
    DubbingDownloadRequest,
    DubbingRequest,
    DubbingStatusRequest,
    ElevenLabsAudioProductionProvider,
    ElevenLabsSharedVoicesRequest,
    ElevenLabsSubscriptionRequest,
    ElevenLabsTextToSpeechRequest,
    ElevenLabsVoicesListRequest,
    MusicGenerationRequest,
    MusicGenerationResult,
    VoiceCloneRequest,
    VoiceConversionRequest,
    VoiceConversionResult,
    resolve_elevenlabs_api_key_env,
)
from opensquilla.provider.auxiliary_budget import (
    ensure_auxiliary_text_fits,
    resolve_auxiliary_request_budget,
)
from opensquilla.provider.correlation_context import (
    bind_provider_request_correlation,
    current_provider_request_correlation,
)
from opensquilla.provider.image_generation import (
    ImageGenerationRequest,
    generate_with_fallbacks,
    get_image_generation_provider,
    list_image_generation_providers,
    parse_image_generation_model_ref,
    reset_image_generation_providers,
)
from opensquilla.provider.image_generation_catalog import (
    get_image_generation_provider_catalog_entry,
)
from opensquilla.provider.image_generation_credentials import (
    resolve_image_generation_credential,
)
from opensquilla.provider.image_generation_policy import (
    conflicting_image_generation_endpoint_provider,
    is_valid_image_generation_base_url,
)
from opensquilla.provider.protocol import provider_metadata
from opensquilla.provider.types import ChatConfig, derive_provider_request_correlation
from opensquilla.sandbox.operation_runtime import SandboxToolDescriptor
from opensquilla.tools.path_aliases import resolve_workspace_alias
from opensquilla.tools.path_policy import reject_foreign_host_path
from opensquilla.tools.registry import tool
from opensquilla.tools.run_mode import full_host_access_active
from opensquilla.tools.ssrf import validate_http_url_for_fetch
from opensquilla.tools.types import (
    CallerKind,
    SafeToolError,
    SSRFBlockedError,
    ToolError,
    UnsupportedURLSchemeError,
    current_tool_context,
)

_SUPPORTED_IMAGE_FORMATS = {"png", "jpg", "jpeg", "gif", "webp"}
_SUPPORTED_AUDIO_FORMATS = {"aac", "flac", "m4a", "mp3", "mp4", "mpeg", "ogg", "wav", "webm"}
_IMAGE_SIZE_LIMIT = 20 * 1024 * 1024  # 20 MB
_AUDIO_SIZE_LIMIT = 100 * 1024 * 1024  # 100 MB
_PDF_RENDER_SCALE = 2.0
_PDF_TEXT_LIMIT = 50_000
_MAX_REDIRECTS = 5
_VISION_ANALYSIS_TIMEOUT_SECONDS = 180.0
_image_generation_config: Any | None = None
_audio_config: Any | None = None
_media_gateway_config: Any | None = None
_media_llm_config: Any | None = None
_media_squilla_router_config: Any | None = None


def configure_image_generation(
    config: Any | None,
    *,
    gateway_config: Any | None = None,
    llm_config: Any | None = None,
    squilla_router_config: Any | None = None,
) -> None:
    global _image_generation_config, _media_gateway_config
    global _media_llm_config, _media_squilla_router_config
    _image_generation_config = config
    _media_gateway_config = gateway_config
    _media_llm_config = llm_config
    _media_squilla_router_config = squilla_router_config
    reset_image_generation_providers(
        config,
        llm_config=llm_config,
        gateway_config=gateway_config,
    )


def configure_audio(config: Any | None) -> None:
    global _audio_config
    _audio_config = config


# ---------------------------------------------------------------------------
# image
# ---------------------------------------------------------------------------


@tool(
    name="image",
    description=(
        "Analyze an image using a vision-capable model. "
        "Accepts only a real local file path or HTTP(S) URL. "
        "Do not call this tool for images already attached to the current chat turn; "
        "use the attachment content directly. "
        "Returns the model's text analysis of the image."
    ),
    params={
        "path": {
            "type": "string",
            "description": (
                "Real local file path or HTTP(S) URL to the image. "
                "Do not pass a chat attachment display name or a filename visible "
                "inside a screenshot."
            ),
        },
        "prompt": {
            "type": "string",
            "description": "What to analyze or describe about the image.",
        },
    },
    required=["path", "prompt"],
    sandbox=SandboxToolDescriptor.media(kind="media.analyze"),
    execution_timeout_seconds=_VISION_ANALYSIS_TIMEOUT_SECONDS,
)
async def image(path: str, prompt: str = "Describe this image") -> str:
    if not prompt or not prompt.strip():
        raise ToolError("Prompt must not be empty")

    is_url = path.startswith("http://") or path.startswith("https://")

    if is_url:
        url_block = _sensitive_media_url_block("image", path)
        if url_block is not None:
            return json.dumps(url_block)
        image_bytes, media_type = await _fetch_image_url(path)
    else:
        p = _resolve_media_path(path)
        path_block = _sensitive_media_path_block("image", p, path)
        if path_block is not None:
            return json.dumps(path_block)
        image_bytes, media_type = await _read_image_file(path)

    # Validate not corrupt using Pillow
    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes))
        img.verify()
    except Exception as exc:
        raise SafeToolError(f"Image appears corrupt or unreadable: {exc}") from exc

    # Try provider vision call; graceful fallback if unavailable
    b64_data = base64.b64encode(image_bytes).decode()
    try:
        description = await _call_vision_provider(b64_data, media_type, prompt)
        model_used = "provider"
    except ToolError:
        raise
    except Exception:
        return json.dumps(
            {
                "status": "not_available",
                "note": "Vision provider not configured or unavailable",
                "path": path,
            }
        )

    return json.dumps({"description": description, "model": model_used, "path": path})


async def _read_image_file(path: str) -> tuple[bytes, str]:
    p = _resolve_media_path(path)
    if not p.exists():
        raise SafeToolError(
            f"Image path is not accessible by the image tool: {path}. "
            "Pass a real local file path or HTTP(S) URL. If this is a chat attachment, "
            "answer from the attached image directly instead of calling the image tool."
        )
    ext = p.suffix.lstrip(".").lower()
    if ext == "pdf":
        loop = asyncio.get_event_loop()
        rendered_bytes = await loop.run_in_executor(None, _render_pdf_first_page_png, p)
        if len(rendered_bytes) > _IMAGE_SIZE_LIMIT:
            raise SafeToolError("Rendered PDF page exceeds 20MB image size limit")
        return rendered_bytes, "image/png"
    if ext not in _SUPPORTED_IMAGE_FORMATS:
        raise SafeToolError(
            f"Unsupported image format: {ext}. "
            f"Supported: {', '.join(sorted(_SUPPORTED_IMAGE_FORMATS))}"
        )
    loop = asyncio.get_event_loop()
    image_bytes: bytes = await loop.run_in_executor(None, p.read_bytes)
    if len(image_bytes) > _IMAGE_SIZE_LIMIT:
        raise SafeToolError("Image exceeds 20MB size limit")
    media_type = _ext_to_mime(ext)
    return image_bytes, media_type


def _render_pdf_first_page_png(path: Path) -> bytes:
    try:
        import pypdfium2 as pdfium  # type: ignore[import-untyped]
    except Exception as exc:  # pragma: no cover - dependency is provided by pdfplumber
        raise ToolError("PDF image analysis requires pypdfium2 to render pages") from exc

    pdf = None
    page = None
    bitmap = None
    try:
        pdf = pdfium.PdfDocument(str(path))
        if len(pdf) < 1:
            raise ToolError(f"PDF has no pages: {path}")
        page = pdf[0]
        bitmap = page.render(scale=_PDF_RENDER_SCALE)
        image = bitmap.to_pil()
        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"Failed to render PDF first page: {path}") from exc
    finally:
        for obj in (bitmap, page, pdf):
            close = getattr(obj, "close", None)
            if close is not None:
                close()


def _resolve_media_path(path: str) -> Path:
    ctx = current_tool_context.get()
    reject_foreign_host_path(path, platform=os.name)
    candidate = Path(path).expanduser()
    workspace = Path(ctx.workspace_dir).expanduser() if ctx and ctx.workspace_dir else None
    alias = resolve_workspace_alias(candidate, workspace)
    if alias is not None:
        return alias
    if candidate.is_absolute():
        return candidate.resolve(strict=False)
    if ctx and ctx.workspace_dir:
        return (Path(ctx.workspace_dir).expanduser() / candidate).resolve(strict=False)
    return candidate.resolve(strict=False)


def _sensitive_media_path_block(tool_name: str, resolved: Path, original_path: str) -> dict | None:
    from opensquilla.sandbox.sensitive_paths import build_block_envelope, is_sensitive_path
    from opensquilla.tools.builtin import filesystem

    if full_host_access_active() or filesystem._sandbox_path_access_enabled():
        return None
    sensitive = is_sensitive_path(str(resolved))
    if sensitive is None:
        return None
    return build_block_envelope(f"{tool_name} {original_path}", sensitive, tool_name=tool_name)


def _sensitive_media_url_block(tool_name: str, url: str) -> dict | None:
    from opensquilla.tools.builtin.web import _sensitive_url_marker

    marker = _sensitive_url_marker(url)
    if marker is None:
        return None
    return {
        "status": "blocked",
        "reason": "sensitive_payload",
        "tool": tool_name,
        "sensitive_payload": marker,
        "message": (
            "Refusing to fetch a media URL whose query string appears to contain "
            "secrets or host account data."
        ),
        "retryable": False,
    }


async def _fetch_image_url(url: str) -> tuple[bytes, str]:
    import httpx

    from opensquilla.tools.ssrf import environment_proxy_url, pinned_transport

    vetted: dict[str, list[str]] = {"ips": []}

    def _check_image_url(candidate_url: str) -> None:
        marker = _sensitive_media_url_block("image", candidate_url)
        if marker is not None:
            raise ToolError("Blocked: URL contains sensitive data")
        try:
            vetted["ips"] = validate_http_url_for_fetch(candidate_url)
        except UnsupportedURLSchemeError as exc:
            raise ToolError("Only HTTP/HTTPS URLs are supported for image fetch") from exc
        except SSRFBlockedError as exc:
            raise ToolError(str(exc)) from exc
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    try:
        current_url = url
        for _redirect_count in range(_MAX_REDIRECTS + 1):
            _check_image_url(current_url)
            # Pin the connection to the address that just passed the guard so a
            # second (rebinding) DNS resolution cannot land on a private IP.
            transport_kwargs: dict[str, object] = {}
            if _trust_env():
                proxy_url = environment_proxy_url(current_url)
                if proxy_url is not None:
                    transport_kwargs["proxy"] = proxy_url
            transport = pinned_transport(current_url, vetted["ips"], **transport_kwargs)
            client_kwargs: dict[str, object] = {
                "timeout": 30.0,
                "follow_redirects": False,
                "trust_env": _trust_env(),
            }
            if transport is not None:
                client_kwargs["transport"] = transport
            async with httpx.AsyncClient(**client_kwargs) as client:  # type: ignore[arg-type]
                resp = await client.get(current_url)
            if resp.status_code not in {301, 302, 303, 307, 308}:
                break
            location = resp.headers.get("location")
            if not location:
                break
            current_url = urljoin(current_url, location)
        else:
            raise ToolError(f"Too many redirects (>{_MAX_REDIRECTS})")
        resp.raise_for_status()
        image_bytes = resp.content
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"Failed to fetch image from URL: {exc}") from exc

    if len(image_bytes) > _IMAGE_SIZE_LIMIT:
        raise ToolError("Image exceeds 20MB size limit")

    # Detect format from content-type or URL extension
    content_type = resp.headers.get("content-type", "")
    final_parsed = urlparse(current_url)
    ext = _mime_to_ext(content_type) or Path(final_parsed.path).suffix.lstrip(".").lower()
    if ext not in _SUPPORTED_IMAGE_FORMATS:
        raise ToolError(
            f"Unsupported image format: {ext}. "
            f"Supported: {', '.join(sorted(_SUPPORTED_IMAGE_FORMATS))}"
        )
    return image_bytes, _ext_to_mime(ext)


def _ext_to_mime(ext: str) -> str:
    mapping = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
    }
    return mapping.get(ext, "image/png")


def _mime_to_ext(content_type: str) -> str:
    ct = content_type.split(";")[0].strip().lower()
    mapping = {
        "image/png": "png",
        "image/jpeg": "jpeg",
        "image/gif": "gif",
        "image/webp": "webp",
    }
    return mapping.get(ct, "")


async def _complete_from_stream(provider: Any, messages: list, config: Any = None) -> str:
    """Consume a chat() stream and return the assembled text response."""
    correlation = current_provider_request_correlation()
    budget = None
    if config is None:
        config = ChatConfig(provider_request_correlation=correlation)
    elif (
        correlation is not None
        and getattr(config, "provider_request_correlation", None) is None
    ):
        config = config.model_copy(
            update={"provider_request_correlation": correlation},
        )
    if int(getattr(config, "provider_request_max_chars", 0) or 0) <= 0:
        budget = resolve_auxiliary_request_budget(
            provider,
            max_output_tokens=int(getattr(config, "max_tokens", 0) or 0),
        )
        config = config.model_copy(
            update={
                "max_tokens": budget.max_output_tokens,
                "provider_request_max_chars": budget.provider_request_max_chars,
            }
        )
    if budget is None:
        budget = resolve_auxiliary_request_budget(
            provider,
            max_output_tokens=int(getattr(config, "max_tokens", 0) or 0),
            provider_request_max_chars=int(
                getattr(config, "provider_request_max_chars", 0) or 0
            ),
        )
    config = config.model_copy(
        update={
            "max_tokens": budget.max_output_tokens,
            "provider_request_max_chars": budget.provider_request_max_chars,
        }
    )
    ensure_auxiliary_text_fits(
        messages,
        max_chars=budget.provider_request_max_chars,
        max_tokens=budget.max_input_tokens,
        system=str(getattr(config, "system", "") or ""),
    )
    scope = current_usage_accounting_scope()
    close_stream = None
    if scope is None:
        stream = provider.chat(messages=messages, config=config)
    elif provider_accounts_physical_usage(provider):
        stream = provider.chat(messages=messages, config=config)
        close_stream = stream
    else:
        metadata = provider_metadata(provider)
        stream = account_provider_stream(
            lambda: provider.chat(messages=messages, config=config),
            provider=metadata.provider_id or metadata.provider_name or metadata.provider_kind,
            model=metadata.model,
        )
        close_stream = stream
    text_parts: list[str] = []
    try:
        async for event in stream:
            if hasattr(event, "text"):
                text_parts.append(event.text)
            elif hasattr(event, "delta") and isinstance(event.delta, str):
                text_parts.append(event.delta)
            elif getattr(event, "kind", None) == "error":
                code = getattr(event, "code", "") or "provider_error"
                message = getattr(event, "message", "") or "Provider stream failed"
                raise RuntimeError(f"Provider stream error ({code}): {message}")
    finally:
        aclose = getattr(close_stream, "aclose", None)
        if callable(aclose):
            await aclose()
    return "".join(text_parts)


async def _call_vision_provider(b64_data: str, media_type: str, prompt: str) -> str:
    """Send image to provider vision API. Raises if provider not available."""
    try:
        from opensquilla.provider.selector import ModelSelector, SelectorConfig
        from opensquilla.provider.types import ContentBlockImage, ContentBlockText, Message

        cfg = _resolve_vision_provider_config(default_model="openai/gpt-4o-mini")
        selector = ModelSelector(SelectorConfig(primary=cfg))
        provider = selector.resolve()
    except Exception as exc:
        raise RuntimeError(f"Provider not available: {exc}") from exc

    vision_message = Message(
        role="user",
        content=[
            ContentBlockImage(media_type=media_type, data=b64_data),
            ContentBlockText(text=prompt),
        ],
    )
    correlation = derive_provider_request_correlation(
        current_provider_request_correlation(),
        execution_id=uuid.uuid4().hex,
        call_kind="auxiliary.media",
    )
    with bind_provider_request_correlation(correlation):
        return await _complete_from_stream(provider, [vision_message])


# ---------------------------------------------------------------------------
# image_generate
# ---------------------------------------------------------------------------


@tool(
    name="image_generate",
    description=(
        "Generate an image from a text prompt using a configured image provider. "
        "On web and channel surfaces, the generated image is registered as an artifact "
        "for that surface to deliver; do not call publish_artifact again for the returned path. "
        "For code, HTML, SVG, canvas, or screenshot based image artifacts, use "
        "the appropriate code/runtime/rendering tool instead."
    ),
    params={
        "prompt": {
            "type": "string",
            "description": "Text description of the image to generate.",
        },
        "size": {
            "type": "string",
            "description": 'Image dimensions. One of "1024x1024", "1536x1024", "1024x1536".',
            "enum": ["1024x1024", "1536x1024", "1024x1536"],
        },
        "model": {
            "type": "string",
            "description": 'Optional provider/model identifier, e.g. "openai/gpt-image-1".',
        },
        "filename": {
            "type": "string",
            "description": "Optional output filename or relative path.",
        },
    },
    required=["prompt"],
    sandbox=SandboxToolDescriptor.media(kind="media.generate_image"),
)
async def image_generate(
    prompt: str,
    size: str | None = None,
    model: str | None = None,
    filename: str | None = None,
) -> str:
    return await _image_generate_impl(prompt=prompt, size=size, model=model, filename=filename)


async def _image_generate_impl(
    *,
    prompt: str,
    size: str | None,
    model: str | None,
    filename: str | None,
) -> str:
    if not prompt or not prompt.strip():
        raise ToolError("Prompt must not be empty")

    config = _resolve_image_generation_config()
    effective_size = size if size is not None else getattr(config, "size", "1024x1024")
    valid_sizes = {"1024x1024", "1536x1024", "1024x1536"}
    if effective_size not in valid_sizes:
        raise ToolError(
            f"Invalid size: {effective_size}. Must be {' | '.join(sorted(valid_sizes))}"
        )

    if not getattr(config, "enabled", False):
        raise ToolError("Image generation is disabled")
    if not _image_generation_binding_is_active(config):
        raise ToolError(
            "Image generation is inactive because its bound LLM provider is not active"
        )

    candidates = _resolve_image_generation_candidates(model, config)
    if not candidates:
        raise ToolError("Image generation is not configured")

    output_format = getattr(config, "output_format", "png")
    target = _resolve_generated_image_path(filename, output_format)
    tool_context = current_tool_context.get()
    try:
        result = await generate_with_fallbacks(
            request=ImageGenerationRequest(
                prompt=prompt,
                model=candidates[0],
                size=effective_size,
                output_format=output_format,
                timeout_seconds=float(getattr(config, "timeout_seconds", 180.0)),
                credential_session_key=(
                    str(tool_context.session_key or "") if tool_context is not None else ""
                ),
            ),
            candidates=candidates,
        )
    except Exception as exc:
        raise ToolError(f"Image generation failed: {exc}") from exc

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(result.image_bytes)
    payload: dict[str, Any] = {
        "status": "ok",
        "path": str(target),
        "provider": result.provider,
        "model": result.model,
        "mime_type": result.mime_type,
        "size_bytes": len(result.image_bytes),
        "revised_prompt": result.revised_prompt,
    }
    artifact = _publish_generated_image_artifact(target, result.mime_type)
    if artifact is not None:
        payload["artifact"] = {k: v for k, v in artifact.items() if k != "download_url"}
        payload["artifact"]["registered_for_delivery"] = True
        payload["artifact"]["delivery_managed_by_surface"] = True
        payload["note"] = (
            "The generated image is registered for the current chat surface. "
            "Do not call publish_artifact again for this same file unless the user explicitly "
            "asks for a separate copy."
        )
    return json.dumps(payload)


def _publish_generated_image_artifact(target: Path, mime_type: str) -> dict[str, Any] | None:
    ctx = current_tool_context.get()
    if (
        ctx is None
        or ctx.caller_kind is CallerKind.SUBAGENT
        or not ctx.artifact_media_root
        or not ctx.artifact_session_id
        or not ctx.session_key
    ):
        return None

    store = ArtifactStore(ctx.artifact_media_root)
    try:
        ref = store.publish_file(
            target,
            session_id=ctx.artifact_session_id,
            session_key=ctx.session_key,
            name=target.name,
            mime=mime_type or "image/png",
            source="image_generate",
            max_bytes=ctx.artifact_max_bytes
            if ctx.artifact_max_bytes is not None
            else DEFAULT_ARTIFACT_MAX_BYTES,
            disk_budget_bytes=ctx.artifact_disk_budget_bytes
            if ctx.artifact_disk_budget_bytes is not None
            else DEFAULT_ARTIFACT_DISK_BUDGET_BYTES,
        )
    except ArtifactBudgetError as exc:
        raise ToolError(str(exc)) from exc
    except FileNotFoundError as exc:
        raise ToolError(f"artifact storage path is unavailable: {exc}") from exc
    payload = artifact_payload(ref)
    ctx.published_artifacts.append(payload)
    return payload


def _resolve_image_generation_config() -> Any:
    if _image_generation_config is not None:
        return _image_generation_config
    from opensquilla.gateway.config import ImageGenerationConfig

    return ImageGenerationConfig()


def _resolve_image_generation_candidates(model: str | None, config: Any) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(raw: str | None) -> None:
        if raw and raw not in seen:
            seen.add(raw)
            candidates.append(raw)

    add(model)
    add(getattr(config, "primary", None))
    for fallback in getattr(config, "fallbacks", []) or []:
        add(fallback)
    primary = getattr(config, "primary", None)
    fallbacks = getattr(config, "fallbacks", []) or []
    has_explicit_model_routing = (
        bool(model) or bool(fallbacks) or bool(primary and primary != "openai/gpt-image-1")
    )
    if not has_explicit_model_routing:
        for provider in list_image_generation_providers():
            if _image_generation_provider_has_auth(provider):
                add(f"{provider.provider_id}/{provider.default_model}")
    return candidates


def _image_generation_binding_is_active(config: Any) -> bool:
    """Whether a system-owned route still has its bound provider credential."""

    if str(getattr(config, "binding", "custom") or "custom") != "follow_llm":
        return True
    try:
        provider_id, _model = parse_image_generation_model_ref(
            str(getattr(config, "primary", "") or "")
        )
    except ValueError:
        return False
    provider = get_image_generation_provider(provider_id)
    if provider is None:
        return False
    try:
        spec = get_image_generation_provider_catalog_entry(provider_id)
        provider_config = getattr(
            getattr(config, "providers", None),
            provider_id,
            None,
        )
        resolution = resolve_image_generation_credential(
            provider_id=provider_id,
            provider_config=provider_config,
            default_env_key=spec.env_key,
            default_base_url=spec.default_base_url,
            effective_base_url=spec.default_base_url,
            gateway_config=_media_gateway_config,
            llm_config=_media_llm_config,
            model=spec.default_model,
            include_image_credentials=False,
        )
    except (KeyError, ValueError):
        return False
    return resolution.available and resolution.owner in {"primary", "profile"}


def image_generation_available(config: Any | None = None) -> bool:
    """Return whether image generation has at least one configured provider."""
    resolved_config = config if config is not None else _resolve_image_generation_config()
    if not getattr(resolved_config, "enabled", False) or not _image_generation_binding_is_active(
        resolved_config
    ):
        return False

    for candidate in _resolve_image_generation_candidates(None, resolved_config):
        try:
            provider_id, _model = parse_image_generation_model_ref(candidate)
        except ValueError:
            continue
        provider = get_image_generation_provider(provider_id)
        if provider is not None and _image_generation_provider_has_auth(provider):
            return True
    return False


def _image_generation_provider_has_auth(provider: Any) -> bool:
    provider_id = str(getattr(provider, "provider_id", "") or "")
    missing_base_url = object()
    configured_base_url = getattr(provider, "_base_url", missing_base_url)
    # Third-party image providers are not required to expose an HTTP endpoint
    # by the public protocol. Built-in HTTP adapters do, and retain endpoint
    # validation before they are surfaced as available.
    if configured_base_url is not missing_base_url:
        base_url = str(configured_base_url or "")
        if not is_valid_image_generation_base_url(base_url):
            return False
        if conflicting_image_generation_endpoint_provider(provider_id, base_url) is not None:
            return False

    resolve_api_key = getattr(provider, "_resolve_api_key", None)
    if callable(resolve_api_key):
        try:
            return bool(resolve_api_key())
        except Exception:  # noqa: BLE001 - capability checks must be non-fatal
            return False

    auth_env_vars = tuple(getattr(provider, "auth_env_vars", ()) or ())
    if not auth_env_vars:
        return True
    return any(bool(os.environ.get(env_var)) for env_var in auth_env_vars)


def _resolve_generated_image_path(filename: str | None, output_format: str) -> Path:
    ext = "jpg" if output_format == "jpeg" else output_format
    raw = filename or f"generated-image-{uuid.uuid4().hex[:12]}.{ext}"
    ctx = current_tool_context.get()
    reject_foreign_host_path(raw, platform=os.name)
    root = (
        Path(ctx.workspace_dir).expanduser().resolve(strict=False)
        if ctx and ctx.workspace_dir
        else Path.cwd()
    )
    candidate = Path(raw).expanduser()
    if candidate.suffix.lower() != f".{ext}":
        candidate = candidate.with_suffix(f".{ext}")

    target = candidate if candidate.is_absolute() else root / candidate
    resolved = target.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ToolError(f"Image output path is outside workspace: {filename}") from exc
    return resolved


# ---------------------------------------------------------------------------
# pdf
# ---------------------------------------------------------------------------


@tool(
    name="pdf",
    description=(
        "Extract text from a PDF file, optionally filtered by page range. "
        "If a prompt is supplied, the extracted text is sent to the LLM for analysis."
    ),
    params={
        "path": {
            "type": "string",
            "description": "File path to the PDF.",
        },
        "pages": {
            "type": "string",
            "description": (
                'Page range to extract: "1-5", "3", or "1,3,5-10". Omit for all pages.'
            ),
        },
        "prompt": {
            "type": "string",
            "description": "Optional analysis prompt. Sends extracted text to the LLM.",
        },
    },
    required=["path"],
    sandbox=SandboxToolDescriptor.media(kind="media.read_pdf"),
)
async def pdf(
    path: str,
    pages: str | None = None,
    prompt: str | None = None,
) -> str:
    p = _resolve_media_path(path)
    path_block = _sensitive_media_path_block("pdf", p, path)
    if path_block is not None:
        return json.dumps(path_block)
    if not p.exists():
        raise SafeToolError(f"PDF file not found: {path} (resolved={p})")

    try:
        import pdfplumber
    except ImportError as exc:
        raise SafeToolError("pdfplumber is not installed") from exc

    loop = asyncio.get_event_loop()

    def _extract() -> dict[str, Any]:
        try:
            with pdfplumber.open(str(p)) as doc:
                total_pages = len(doc.pages)

                # Resolve page indices (0-based)
                if pages:
                    indices = _parse_page_range(pages, total_pages)
                else:
                    indices = list(range(total_pages))

                texts: list[str] = []
                for idx in indices:
                    page_text = doc.pages[idx].extract_text() or ""
                    texts.append(page_text)

                extracted = "\n\n".join(t for t in texts if t)
                return {"total_pages": total_pages, "text": extracted}
        except ToolError:
            raise
        except Exception as exc:
            err_msg = str(exc).lower()
            if "password" in err_msg or "encrypted" in err_msg:
                raise SafeToolError("PDF is password-protected") from exc
            raise SafeToolError(f"File is not a valid PDF: {path} (resolved={p})") from exc

    result = await loop.run_in_executor(None, _extract)
    total_pages: int = result["total_pages"]
    extracted_text: str = result["text"]

    if not extracted_text.strip():
        raise SafeToolError("No extractable text found - PDF may be image-only")

    # Truncate
    truncated = len(extracted_text) > _PDF_TEXT_LIMIT
    if truncated:
        extracted_text = extracted_text[:_PDF_TEXT_LIMIT]

    page_desc = pages if pages else f"1-{total_pages}"

    if prompt and prompt.strip():
        # Send to LLM for analysis
        analysis = await _call_llm_with_text(extracted_text, prompt)
        return json.dumps(
            {
                "path": path,
                "pages": page_desc,
                "total_pages": total_pages,
                "analysis": analysis,
                "truncated": truncated,
            }
        )

    return json.dumps(
        {
            "path": path,
            "pages": page_desc,
            "total_pages": total_pages,
            "text": extracted_text,
            "truncated": truncated,
        }
    )


def _parse_page_range(pages: str, total: int) -> list[int]:
    """Parse page range string to 0-based index list."""
    indices: list[int] = []
    segments = [s.strip() for s in pages.split(",")]
    for seg in segments:
        if not seg:
            continue
        if "-" in seg:
            parts = seg.split("-", 1)
            if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
                raise SafeToolError(f"Invalid page range: {pages}")
            start, end = int(parts[0]), int(parts[1])
            if start < 1 or end < start:
                raise SafeToolError(f"Invalid page range: {pages}")
            for n in range(start, end + 1):
                if n > total:
                    raise SafeToolError(f"Page {n} exceeds document length ({total} pages)")
                indices.append(n - 1)
        elif re.match(r"^\d+$", seg):
            n = int(seg)
            if n < 1:
                raise SafeToolError(f"Invalid page range: {pages}")
            if n > total:
                raise SafeToolError(f"Page {n} exceeds document length ({total} pages)")
            indices.append(n - 1)
        else:
            raise SafeToolError(f"Invalid page range: {pages}")
    return indices


async def _call_llm_with_text(text: str, prompt: str) -> str:
    """Send extracted text to LLM with analysis prompt. Graceful fallback."""
    try:
        from opensquilla.provider.selector import ModelSelector, SelectorConfig
        from opensquilla.provider.types import Message

        cfg = _resolve_provider_config("LLM", default_model="openai/gpt-4o-mini")
        selector = ModelSelector(SelectorConfig(primary=cfg))
        provider = selector.resolve()
        message = Message(role="user", content=f"{prompt}\n\n---\n{text}")
        correlation = derive_provider_request_correlation(
            current_provider_request_correlation(),
            execution_id=uuid.uuid4().hex,
            call_kind="auxiliary.media",
        )
        with bind_provider_request_correlation(correlation):
            return await _complete_from_stream(provider, [message])
    except Exception:
        return f"[LLM analysis not available] Extracted text ({len(text)} chars) ready."


def _config_value(config: Any | None, key: str, default: Any = "") -> Any:
    if config is None:
        return default
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def _has_explicit_scope_override(scope: str) -> bool:
    return bool(
        os.environ.get(f"OPENSQUILLA_{scope}_PROVIDER")
        or os.environ.get(f"OPENSQUILLA_{scope}_MODEL")
    )


def _configured_image_tier(router_config: Any | None) -> Any | None:
    tiers = _config_value(router_config, "tiers", {})
    if not isinstance(tiers, dict):
        return None

    preferred = tiers.get("image_model")
    if _config_value(preferred, "supports_image", False):
        return preferred

    for tier in tiers.values():
        if _config_value(tier, "supports_image", False):
            return tier
    return None


def _configured_provider_config(provider_name: str, model: str):
    from opensquilla.provider.selector import ProviderConfig

    provider_name = str(provider_name or "").strip().lower() or "openrouter"
    if _media_gateway_config is not None:
        from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config
        from opensquilla.provider.deployment import resolve_provider_deployment

        # Resolve the active provider on a throwaway copy so environment
        # materialization and provenance tracking cannot mutate the live
        # gateway config from a media-tool lookup. The shared deployment
        # resolver then uses this as the inherited primary or independently
        # resolves a demoted provider from ``llm_profiles``.
        scratch = _media_gateway_config.model_copy(deep=True)
        runtime = resolve_llm_runtime_config(scratch)
        inherited = ProviderConfig(
            provider=runtime.provider,
            model=model,
            api_key=runtime.api_key,
            base_url=runtime.base_url,
            proxy=runtime.proxy,
            provider_routing=runtime.provider_routing,
        )
        resolution = resolve_provider_deployment(
            _media_gateway_config,
            provider_name,
            model,
            inherited_provider_config=inherited,
        )
        if resolution.provider_config is not None:
            return resolution.provider_config

    llm_provider = str(_config_value(_media_llm_config, "provider", "") or "").strip().lower()
    use_llm_config = provider_name == llm_provider

    api_key = str(_config_value(_media_llm_config, "api_key", "") or "") if use_llm_config else ""
    if use_llm_config and not api_key:
        api_key_env = str(_config_value(_media_llm_config, "api_key_env", "") or "")
        if api_key_env:
            api_key = os.environ.get(api_key_env, "")

    base_url = str(_config_value(_media_llm_config, "base_url", "") or "") if use_llm_config else ""
    proxy = str(_config_value(_media_llm_config, "proxy", "") or "") if use_llm_config else ""
    provider_routing = (
        _config_value(_media_llm_config, "provider_routing", {}) if use_llm_config else {}
    )
    if not isinstance(provider_routing, dict):
        provider_routing = {}

    if provider_name == "anthropic":
        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        base_url = base_url or os.environ.get("ANTHROPIC_BASE_URL", "")
    elif provider_name == "openrouter":
        api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "") or os.environ.get(
            "OPENAI_API_KEY", ""
        )
        base_url = base_url or os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    else:
        api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        base_url = base_url or os.environ.get("OPENAI_BASE_URL", "")

    return ProviderConfig(
        provider=provider_name,
        model=model,
        api_key=api_key,
        base_url=base_url,
        proxy=proxy or os.environ.get("OPENSQUILLA_LLM_PROXY", ""),
        provider_routing=provider_routing,
    )


def _resolve_vision_provider_config(*, default_model: str):
    if not _has_explicit_scope_override("VISION"):
        tier = _configured_image_tier(_media_squilla_router_config)
        model = str(_config_value(tier, "model", "") or "")
        if tier is not None and model:
            provider_name = str(
                _config_value(tier, "provider", _config_value(_media_llm_config, "provider", ""))
                or "openrouter"
            )
            return _configured_provider_config(provider_name, model)
    return _resolve_provider_config("VISION", default_model=default_model)


def _resolve_provider_config(scope: str, *, default_model: str):
    from opensquilla.provider.selector import ProviderConfig

    provider_name = (
        os.environ.get(f"OPENSQUILLA_{scope}_PROVIDER")
        or os.environ.get("OPENSQUILLA_LLM_PROVIDER")
        or "openrouter"
    )
    model = (
        os.environ.get(f"OPENSQUILLA_{scope}_MODEL")
        or os.environ.get("OPENSQUILLA_LLM_MODEL")
        or default_model
    )

    if provider_name == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    elif provider_name == "openrouter":
        api_key = os.environ.get("OPENROUTER_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
        base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    else:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        base_url = os.environ.get("OPENAI_BASE_URL", "")

    return ProviderConfig(
        provider=provider_name,
        model=model,
        api_key=api_key,
        base_url=base_url,
        proxy=os.environ.get("OPENSQUILLA_LLM_PROXY", ""),
    )


# ---------------------------------------------------------------------------
# tts
# ---------------------------------------------------------------------------


def _resolve_audio_config() -> Any:
    if _audio_config is not None:
        return _audio_config
    from opensquilla.gateway.config import AudioConfig

    return AudioConfig()


def _audio_provider_config(config: Any) -> Any:
    providers = getattr(config, "providers", None)
    return getattr(providers, "elevenlabs", None)


def _audio_configured(config: Any) -> bool:
    if not getattr(config, "enabled", False):
        return False
    provider_config = _audio_provider_config(config)
    if provider_config is None:
        return False
    api_key = str(getattr(provider_config, "api_key", "") or "")
    api_key_env = resolve_elevenlabs_api_key_env(provider_config)
    return bool(api_key or os.environ.get(api_key_env))


def _elevenlabs_provider(config: Any) -> ElevenLabsAudioProductionProvider:
    provider_config = _audio_provider_config(config)
    api_key_env = resolve_elevenlabs_api_key_env(provider_config)
    return ElevenLabsAudioProductionProvider(
        api_key=str(getattr(provider_config, "api_key", "") or "") or None,
        api_key_env=api_key_env,
        base_url=str(getattr(provider_config, "base_url", "") or "https://api.elevenlabs.io"),
    )


def _audio_not_available_payload(
    *,
    tool_name: str,
    missing_capability: str,
    note: str,
) -> str:
    return json.dumps(
        {
            "status": "not_available",
            "tool": tool_name,
            "provider": "elevenlabs",
            "missing_capability": missing_capability,
            "note": note,
        }
    )


def _consent_required_payload(*, tool_name: str, note: str) -> str:
    return json.dumps(
        {
            "status": "consent_required",
            "tool": tool_name,
            "provider": "elevenlabs",
            "note": note,
        }
    )


def _has_consent_metadata(consent_metadata: dict[str, Any] | None) -> bool:
    if not isinstance(consent_metadata, dict):
        return False
    consent = consent_metadata.get("consent")
    if isinstance(consent, bool):
        return consent
    if isinstance(consent, str):
        return consent.strip().lower() in {"1", "true", "yes", "y", "confirmed"}
    return bool(consent_metadata.get("speaker") and consent_metadata.get("source"))


def _audio_mime_type(path: Path) -> str:
    ext = path.suffix.lstrip(".").lower()
    mapping = {
        "aac": "audio/aac",
        "flac": "audio/flac",
        "m4a": "audio/mp4",
        "mp3": "audio/mpeg",
        "mp4": "audio/mp4",
        "mpeg": "audio/mpeg",
        "ogg": "audio/ogg",
        "wav": "audio/wav",
        "webm": "audio/webm",
    }
    return mapping.get(ext, "application/octet-stream")


async def _resolve_supported_audio_file_for_tool(
    *, tool_name: str, path: str
) -> tuple[Path, bytes, str]:
    resolved = _resolve_media_path(path)
    path_block = _sensitive_media_path_block(tool_name, resolved, path)
    if path_block is not None:
        raise SafeToolError(path_block["message"])
    if not resolved.exists():
        raise SafeToolError(f"Audio file not found: {path} (resolved={resolved})")
    ext = resolved.suffix.lstrip(".").lower()
    if ext not in _SUPPORTED_AUDIO_FORMATS:
        raise ToolError(
            f"Unsupported audio format: {ext}. "
            f"Supported: {', '.join(sorted(_SUPPORTED_AUDIO_FORMATS))}"
        )
    loop = asyncio.get_event_loop()
    audio_bytes: bytes = await loop.run_in_executor(None, resolved.read_bytes)
    if len(audio_bytes) > _AUDIO_SIZE_LIMIT:
        raise ToolError("Audio file exceeds 100MB size limit")
    return resolved, audio_bytes, _audio_mime_type(resolved)


def _audio_extension(response_format: str, mime_type: str) -> str:
    normalized = (response_format or "").lower()
    if normalized.startswith("mp3"):
        return "mp3"
    if normalized.startswith("pcm"):
        return "pcm16"
    if normalized in {"wav", "flac", "opus"}:
        return normalized
    mime = mime_type.split(";", 1)[0].lower()
    return {
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/flac": "flac",
        "audio/ogg": "ogg",
        "audio/opus": "opus",
        "audio/l16": "pcm16",
    }.get(mime, "mp3")


def _resolve_generated_audio_path(
    output_path: str | None,
    *,
    response_format: str,
    mime_type: str,
    prefix: str,
) -> Path:
    ext = _audio_extension(response_format, mime_type)
    raw = output_path or f"{prefix}-{uuid.uuid4().hex[:12]}.{ext}"
    reject_foreign_host_path(raw, platform=os.name)
    ctx = current_tool_context.get()
    root = (
        Path(ctx.workspace_dir).expanduser().resolve(strict=False)
        if ctx and ctx.workspace_dir
        else Path.cwd()
    )
    candidate = Path(raw).expanduser()
    if not candidate.suffix:
        candidate = candidate.with_suffix(f".{ext}")
    target = candidate if candidate.is_absolute() else root / candidate
    resolved = target.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ToolError(f"Audio output path is outside workspace: {output_path}") from exc
    return resolved


def _publish_generated_audio_artifact(
    target: Path,
    mime_type: str,
    *,
    source: str,
) -> dict[str, Any] | None:
    ctx = current_tool_context.get()
    if (
        ctx is None
        or ctx.caller_kind is CallerKind.SUBAGENT
        or not ctx.artifact_media_root
        or not ctx.artifact_session_id
        or not ctx.session_key
    ):
        return None
    store = ArtifactStore(ctx.artifact_media_root)
    try:
        ref = store.publish_file(
            target,
            session_id=ctx.artifact_session_id,
            session_key=ctx.session_key,
            name=target.name,
            mime=mime_type or "application/octet-stream",
            source=source,
            max_bytes=ctx.artifact_max_bytes
            if ctx.artifact_max_bytes is not None
            else DEFAULT_ARTIFACT_MAX_BYTES,
            disk_budget_bytes=ctx.artifact_disk_budget_bytes
            if ctx.artifact_disk_budget_bytes is not None
            else DEFAULT_ARTIFACT_DISK_BUDGET_BYTES,
        )
    except ArtifactBudgetError as exc:
        raise ToolError(str(exc)) from exc
    except FileNotFoundError as exc:
        raise ToolError(f"artifact storage path is unavailable: {exc}") from exc
    payload = artifact_payload(ref)
    ctx.published_artifacts.append(payload)
    return payload


def _write_generated_audio_payload(
    result: AudioGenerationResult | VoiceConversionResult | MusicGenerationResult,
    *,
    output_path: str | None,
    prefix: str,
    artifact_source: str,
    extra: dict[str, Any] | None = None,
) -> str:
    target = _resolve_generated_audio_path(
        output_path,
        response_format=result.response_format,
        mime_type=result.mime_type,
        prefix=prefix,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(result.audio_bytes)
    payload: dict[str, Any] = {
        "status": "ok",
        "path": str(target),
        "provider": result.provider,
        "model": result.model,
        "response_format": result.response_format,
        "mime_type": result.mime_type,
        "size_bytes": len(result.audio_bytes),
    }
    voice = getattr(result, "voice", None)
    if voice:
        payload["voice"] = voice
    generation_id = getattr(result, "generation_id", None)
    if generation_id:
        payload["generation_id"] = generation_id
    if extra:
        payload.update(extra)
    artifact = _publish_generated_audio_artifact(
        target,
        result.mime_type,
        source=artifact_source,
    )
    if artifact is not None:
        payload["artifact"] = {k: v for k, v in artifact.items() if k != "download_url"}
        payload["artifact"]["registered_for_delivery"] = True
        payload["artifact"]["delivery_managed_by_surface"] = True
    return json.dumps(payload)


def _bounded_float(
    name: str,
    value: float | int | None,
    *,
    minimum: float = 0.0,
    maximum: float = 1.0,
) -> float | None:
    if value is None:
        return None
    numeric = float(value)
    if numeric < minimum or numeric > maximum:
        raise ToolError(f"{name} must be between {minimum:g} and {maximum:g}")
    return numeric


def _tts_voice_settings(
    *,
    speed: float,
    stability: float | None,
    similarity_boost: float | None,
    style: float | None,
    use_speaker_boost: bool | None,
    tts_config: Any,
) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    resolved_stability = _bounded_float(
        "Stability",
        stability
        if stability is not None
        else getattr(tts_config, "stability", None),
    )
    resolved_similarity = _bounded_float(
        "Similarity boost",
        similarity_boost
        if similarity_boost is not None
        else getattr(tts_config, "similarity_boost", None),
    )
    resolved_style = _bounded_float(
        "Style",
        style if style is not None else getattr(tts_config, "style", None),
    )
    if resolved_stability is not None:
        settings["stability"] = resolved_stability
    if resolved_similarity is not None:
        settings["similarity_boost"] = resolved_similarity
    if resolved_style is not None:
        settings["style"] = resolved_style
    resolved_boost = (
        use_speaker_boost
        if use_speaker_boost is not None
        else getattr(tts_config, "use_speaker_boost", None)
    )
    if resolved_boost is not None:
        settings["use_speaker_boost"] = bool(resolved_boost)
    settings["speed"] = speed
    return settings


def _shared_voice_summary(voice: dict[str, Any]) -> dict[str, Any]:
    raw_labels = voice.get("labels")
    labels = raw_labels if isinstance(raw_labels, dict) else {}
    summary = {
        "name": voice.get("name"),
        "voice_id": voice.get("voice_id"),
        "public_owner_id": voice.get("public_owner_id"),
        "language": voice.get("language") or labels.get("language"),
        "accent": voice.get("accent") or labels.get("accent"),
        "locale": voice.get("locale") or labels.get("locale"),
        "gender": voice.get("gender") or labels.get("gender"),
        "age": voice.get("age") or labels.get("age"),
        "category": voice.get("category"),
        "description": voice.get("description"),
    }
    return {key: value for key, value in summary.items() if value not in (None, "")}


def _provider_quota_exceeded(error: RuntimeError) -> bool:
    text = str(error).lower()
    return "quota_exceeded" in text or (
        "credits remaining" in text and "required" in text
    )


def _short_song_preview_lyrics(lyrics: str) -> str:
    lines = [line.strip() for line in lyrics.splitlines() if line.strip()]
    preview: list[str] = []
    for line in lines:
        preview.append(line)
        if len(preview) >= 6 or len("\n".join(preview)) >= 120:
            break
    return "\n".join(preview).strip() or lyrics.strip()


@tool(
    name="voice_clone",
    description=(
        "Clone a voice from a local audio sample through ElevenLabs. "
        "Requires explicit consent_metadata for the sampled speaker."
    ),
    params={
        "sample_audio": {"type": "string", "description": "Local audio sample path."},
        "name": {"type": "string", "description": "Name for the cloned voice."},
        "description": {"type": "string", "description": "Optional voice description."},
        "consent_metadata": {
            "type": "object",
            "description": "Consent proof, e.g. {'speaker': 'me', 'consent': true}.",
        },
    },
    required=["sample_audio", "name"],
    sandbox=SandboxToolDescriptor.media(kind="media.voice_clone"),
)
async def voice_clone(
    sample_audio: str,
    name: str,
    description: str | None = None,
    consent_metadata: dict[str, Any] | None = None,
) -> str:
    if not name or not name.strip():
        raise ToolError("Voice name must not be empty")
    if not _has_consent_metadata(consent_metadata):
        return _consent_required_payload(
            tool_name="voice_clone",
            note="Voice cloning requires explicit consent metadata for the target voice.",
        )
    config = _resolve_audio_config()
    if not _audio_configured(config):
        return _audio_not_available_payload(
            tool_name="voice_clone",
            missing_capability="voice_cloning",
            note="ElevenLabs voice-cloning provider is disabled or not configured.",
        )
    resolved, audio_bytes, mime_type = await _resolve_supported_audio_file_for_tool(
        tool_name="voice_clone",
        path=sample_audio,
    )
    try:
        result = await _elevenlabs_provider(config).clone_voice(
            VoiceCloneRequest(
                sample_audio_bytes=audio_bytes,
                sample_filename=resolved.name,
                sample_mime_type=mime_type,
                name=name.strip(),
                description=description.strip() if description else None,
            )
        )
    except RuntimeError as exc:
        return _audio_not_available_payload(
            tool_name="voice_clone",
            missing_capability="voice_cloning",
            note=str(exc),
        )
    return json.dumps(
        {
            "status": "ok",
            "provider": result.provider,
            "voice_id": result.voice_id,
            "name": result.name,
            "preview_url": result.preview_url,
            "requires_verification": result.requires_verification,
            "source_path": str(resolved),
        }
    )


@tool(
    name="voice_convert",
    description=(
        "Convert a local source audio file into a target ElevenLabs voice. "
        "Requires explicit consent_metadata for the source speaker."
    ),
    params={
        "source_audio": {"type": "string", "description": "Local source audio path."},
        "target_voice": {"type": "string", "description": "ElevenLabs target voice id."},
        "output_path": {"type": "string", "description": "Optional output audio path."},
        "consent_metadata": {
            "type": "object",
            "description": "Consent proof, e.g. {'speaker': 'me', 'consent': true}.",
        },
    },
    required=["source_audio", "target_voice"],
    sandbox=SandboxToolDescriptor.media(kind="media.voice_convert"),
)
async def voice_convert(
    source_audio: str,
    target_voice: str,
    output_path: str | None = None,
    consent_metadata: dict[str, Any] | None = None,
) -> str:
    if not target_voice or not target_voice.strip():
        raise ToolError("Target voice must not be empty")
    if not _has_consent_metadata(consent_metadata):
        return _consent_required_payload(
            tool_name="voice_convert",
            note="Voice conversion requires explicit consent metadata for the source voice.",
        )
    config = _resolve_audio_config()
    if not _audio_configured(config):
        return _audio_not_available_payload(
            tool_name="voice_convert",
            missing_capability="voice_conversion",
            note="ElevenLabs voice-conversion provider is disabled or not configured.",
        )
    provider_config = _audio_provider_config(config)
    model_id = str(
        getattr(provider_config, "voice_conversion_model", "")
        or "eleven_multilingual_sts_v2"
    )
    output_format = str(
        getattr(provider_config, "music_output_format", "") or "mp3_44100_128"
    )
    resolved, audio_bytes, mime_type = await _resolve_supported_audio_file_for_tool(
        tool_name="voice_convert",
        path=source_audio,
    )
    try:
        result = await _elevenlabs_provider(config).convert_voice(
            VoiceConversionRequest(
                source_audio_bytes=audio_bytes,
                source_filename=resolved.name,
                source_mime_type=mime_type,
                target_voice=target_voice.strip(),
                model_id=model_id,
                output_format=output_format,
            )
        )
    except RuntimeError as exc:
        return _audio_not_available_payload(
            tool_name="voice_convert",
            missing_capability="voice_conversion",
            note=str(exc),
        )
    return _write_generated_audio_payload(
        result,
        output_path=output_path,
        prefix="voice-converted",
        artifact_source="voice_convert",
        extra={"source_path": str(resolved)},
    )


@tool(
    name="dubbing_generate",
    description="Submit a local audio/video file for ElevenLabs dubbing.",
    params={
        "source_media": {"type": "string", "description": "Local source media path."},
        "target_language": {"type": "string", "description": "Target language code."},
        "source_language": {"type": "string", "description": "Optional source language code."},
        "name": {"type": "string", "description": "Optional dubbing job name."},
        "num_speakers": {"type": "integer", "description": "Optional speaker count."},
    },
    required=["source_media", "target_language"],
    sandbox=SandboxToolDescriptor.media(kind="media.dubbing_generate"),
)
async def dubbing_generate(
    source_media: str,
    target_language: str,
    source_language: str | None = None,
    name: str | None = None,
    num_speakers: int | None = None,
) -> str:
    if not target_language or not target_language.strip():
        raise ToolError("Target language must not be empty")
    config = _resolve_audio_config()
    if not _audio_configured(config):
        return _audio_not_available_payload(
            tool_name="dubbing_generate",
            missing_capability="advanced_dubbing",
            note="ElevenLabs dubbing provider is disabled or not configured.",
        )
    resolved, audio_bytes, mime_type = await _resolve_supported_audio_file_for_tool(
        tool_name="dubbing_generate",
        path=source_media,
    )
    try:
        result = await _elevenlabs_provider(config).create_dubbing(
            DubbingRequest(
                source_bytes=audio_bytes,
                filename=resolved.name,
                mime_type=mime_type,
                target_language=target_language.strip(),
                source_language=source_language.strip() if source_language else None,
                name=name.strip() if name else None,
                num_speakers=num_speakers,
                watermark=True,
            )
        )
    except RuntimeError as exc:
        return _audio_not_available_payload(
            tool_name="dubbing_generate",
            missing_capability="advanced_dubbing",
            note=str(exc),
        )
    return json.dumps(
        {
            "status": "ok",
            "provider": result.provider,
            "dubbing_id": result.dubbing_id,
            "dubbing_status": result.status,
            "source_path": str(resolved),
            "source_language": result.source_language,
            "target_language": result.target_language,
            "note": (
                "Dubbing job submitted; call dubbing_status or dubbing_download "
                "to fetch completion."
            ),
        }
    )


@tool(
    name="dubbing_status",
    description="Check the status of an ElevenLabs dubbing job.",
    params={"dubbing_id": {"type": "string", "description": "ElevenLabs dubbing job id."}},
    required=["dubbing_id"],
    sandbox=SandboxToolDescriptor.media(kind="media.dubbing_status"),
)
async def dubbing_status(dubbing_id: str) -> str:
    if not dubbing_id or not dubbing_id.strip():
        raise ToolError("Dubbing id must not be empty")
    config = _resolve_audio_config()
    if not _audio_configured(config):
        return _audio_not_available_payload(
            tool_name="dubbing_status",
            missing_capability="advanced_dubbing",
            note="ElevenLabs dubbing provider is disabled or not configured.",
        )
    try:
        result = await _elevenlabs_provider(config).get_dubbing_status(
            DubbingStatusRequest(dubbing_id=dubbing_id.strip())
        )
    except RuntimeError as exc:
        return _audio_not_available_payload(
            tool_name="dubbing_status",
            missing_capability="advanced_dubbing",
            note=str(exc),
        )
    return json.dumps(
        {
            "status": "ok",
            "provider": result.provider,
            "dubbing_id": result.dubbing_id,
            "dubbing_status": result.status,
            "raw": result.raw,
        }
    )


_DUBBING_READY_STATUSES = {"dubbed", "done", "complete", "completed", "ready"}
_DUBBING_FAILED_STATUSES = {"failed", "error", "cancelled", "canceled"}


@tool(
    name="dubbing_download",
    description="Download completed ElevenLabs dubbing audio, optionally polling until ready.",
    params={
        "dubbing_id": {"type": "string", "description": "ElevenLabs dubbing job id."},
        "language_code": {"type": "string", "description": "Dubbed language code."},
        "output_path": {"type": "string", "description": "Optional output audio path."},
        "wait_for_completion": {"type": "boolean", "description": "Poll until ready."},
        "poll_interval_seconds": {"type": "number", "description": "Polling interval."},
        "timeout_seconds": {"type": "number", "description": "Max wait time."},
    },
    required=["dubbing_id", "language_code"],
    sandbox=SandboxToolDescriptor.media(kind="media.dubbing_download"),
)
async def dubbing_download(
    dubbing_id: str,
    language_code: str,
    output_path: str | None = None,
    wait_for_completion: bool = True,
    poll_interval_seconds: float = 5.0,
    timeout_seconds: float = 300.0,
) -> str:
    if not dubbing_id or not dubbing_id.strip():
        raise ToolError("Dubbing id must not be empty")
    if not language_code or not language_code.strip():
        raise ToolError("Language code must not be empty")
    config = _resolve_audio_config()
    if not _audio_configured(config):
        return _audio_not_available_payload(
            tool_name="dubbing_download",
            missing_capability="advanced_dubbing",
            note="ElevenLabs dubbing provider is disabled or not configured.",
        )
    provider = _elevenlabs_provider(config)
    final_status = "unknown"
    if wait_for_completion:
        deadline = asyncio.get_event_loop().time() + max(timeout_seconds, 0.0)
        while True:
            status_result = await provider.get_dubbing_status(
                DubbingStatusRequest(dubbing_id=dubbing_id.strip())
            )
            final_status = status_result.status
            normalized = final_status.strip().lower()
            if normalized in _DUBBING_READY_STATUSES:
                break
            if normalized in _DUBBING_FAILED_STATUSES:
                raise ToolError(f"Dubbing job {dubbing_id} failed with status {final_status}")
            if asyncio.get_event_loop().time() >= deadline:
                raise ToolError(
                    f"Dubbing job {dubbing_id} was not ready before timeout; "
                    f"last status={final_status}"
                )
            await asyncio.sleep(max(poll_interval_seconds, 0.1))
    try:
        download = await provider.download_dubbing_audio(
            DubbingDownloadRequest(
                dubbing_id=dubbing_id.strip(),
                language_code=language_code.strip(),
            )
        )
    except RuntimeError as exc:
        return _audio_not_available_payload(
            tool_name="dubbing_download",
            missing_capability="advanced_dubbing",
            note=str(exc),
        )
    result = AudioGenerationResult(
        audio_bytes=download.audio_bytes,
        provider=download.provider,
        model="dubbing",
        voice=None,
        response_format="mp3",
        mime_type=download.mime_type,
    )
    return _write_generated_audio_payload(
        result,
        output_path=output_path,
        prefix="dubbed-audio",
        artifact_source="dubbing_download",
        extra={
            "dubbing_id": download.dubbing_id,
            "language_code": download.language_code,
            "dubbing_status": final_status,
        },
    )


@tool(
    name="music_generate",
    description="Generate instrumental music through ElevenLabs.",
    params={
        "prompt": {"type": "string", "description": "Music prompt."},
        "style": {"type": "string", "description": "Optional style hint."},
        "duration_seconds": {"type": "number", "description": "Optional duration."},
        "output_path": {"type": "string", "description": "Optional output audio path."},
    },
    required=["prompt"],
    sandbox=SandboxToolDescriptor.media(kind="media.music_generate"),
)
async def music_generate(
    prompt: str,
    style: str | None = None,
    duration_seconds: float | None = None,
    output_path: str | None = None,
) -> str:
    if not prompt or not prompt.strip():
        raise ToolError("Prompt must not be empty")
    config = _resolve_audio_config()
    if not _audio_configured(config):
        return _audio_not_available_payload(
            tool_name="music_generate",
            missing_capability="music_generation",
            note="ElevenLabs music provider is disabled or not configured.",
        )
    provider_config = _audio_provider_config(config)
    final_prompt = prompt.strip()
    if style and style.strip():
        final_prompt = f"{final_prompt}\nStyle: {style.strip()}"
    try:
        result = await _elevenlabs_provider(config).generate_music(
            MusicGenerationRequest(
                prompt=final_prompt,
                model_id=str(getattr(provider_config, "music_model", "") or "music_v1"),
                output_format=str(
                    getattr(provider_config, "music_output_format", "")
                    or "mp3_44100_128"
                ),
                duration_seconds=duration_seconds,
                force_instrumental=True,
            )
        )
    except RuntimeError as exc:
        return _audio_not_available_payload(
            tool_name="music_generate",
            missing_capability="music_generation",
            note=str(exc),
        )
    return _write_generated_audio_payload(
        result,
        output_path=output_path,
        prefix="generated-music",
        artifact_source="music_generate",
    )


@tool(
    name="song_generate",
    description="Generate a song with sung vocals through ElevenLabs music generation.",
    params={
        "lyrics": {"type": "string", "description": "Original lyrics to sing."},
        "vocal_style": {"type": "string", "description": "Optional vocal style."},
        "backing_style": {"type": "string", "description": "Optional backing style."},
        "duration_seconds": {"type": "number", "description": "Optional duration."},
        "output_path": {"type": "string", "description": "Optional output audio path."},
    },
    required=["lyrics"],
    sandbox=SandboxToolDescriptor.media(kind="media.song_generate"),
)
async def song_generate(
    lyrics: str,
    vocal_style: str | None = None,
    backing_style: str | None = None,
    duration_seconds: float | None = None,
    output_path: str | None = None,
) -> str:
    if not lyrics or not lyrics.strip():
        raise ToolError("Lyrics must not be empty")
    config = _resolve_audio_config()
    if not _audio_configured(config):
        return _audio_not_available_payload(
            tool_name="song_generate",
            missing_capability="singing_generation",
            note="ElevenLabs music provider is disabled or not configured.",
        )
    provider_config = _audio_provider_config(config)
    prompt_parts = ["Generate a complete song with sung vocals."]
    if vocal_style and vocal_style.strip():
        prompt_parts.append(f"Vocal style: {vocal_style.strip()}")
    if backing_style and backing_style.strip():
        prompt_parts.append(f"Backing style: {backing_style.strip()}")
    provider = _elevenlabs_provider(config)
    lyrics_text = lyrics.strip()
    model_id = str(getattr(provider_config, "music_model", "") or "music_v1")
    output_format = str(
        getattr(provider_config, "music_output_format", "") or "mp3_44100_128"
    )
    try:
        result = await provider.generate_music(
            MusicGenerationRequest(
                prompt="\n".join(prompt_parts),
                lyrics=lyrics_text,
                model_id=model_id,
                output_format=output_format,
                duration_seconds=duration_seconds,
                force_instrumental=False,
            )
        )
    except RuntimeError as exc:
        if _provider_quota_exceeded(exc):
            preview_lyrics = _short_song_preview_lyrics(lyrics_text)
            preview_duration = min(float(duration_seconds or 8.0), 8.0)
            try:
                result = await provider.generate_music(
                    MusicGenerationRequest(
                        prompt="\n".join([*prompt_parts, "Fallback: short preview demo."]),
                        lyrics=preview_lyrics,
                        model_id=model_id,
                        output_format=output_format,
                        duration_seconds=preview_duration,
                        force_instrumental=False,
                    )
                )
            except RuntimeError as retry_exc:
                return _audio_not_available_payload(
                    tool_name="song_generate",
                    missing_capability="singing_generation",
                    note=str(retry_exc),
                )
            return _write_generated_audio_payload(
                result,
                output_path=output_path,
                prefix="generated-song",
                artifact_source="song_generate",
                extra={
                    "quota_retry": {
                        "strategy": "short_preview",
                        "duration_seconds": preview_duration,
                        "lyrics_truncated": preview_lyrics != lyrics_text,
                        "original_note": str(exc),
                    }
                },
            )
        return _audio_not_available_payload(
            tool_name="song_generate",
            missing_capability="singing_generation",
            note=str(exc),
        )
    return _write_generated_audio_payload(
        result,
        output_path=output_path,
        prefix="generated-song",
        artifact_source="song_generate",
    )


@tool(
    name="audio_provider_capabilities",
    description="Report configured ElevenLabs audio provider capabilities.",
    params={
        "probe_live": {
            "type": "boolean",
            "description": "When true, call read-only ElevenLabs subscription and voices APIs.",
        }
    },
    required=[],
    sandbox=SandboxToolDescriptor.media(kind="media.audio_capabilities"),
)
async def audio_provider_capabilities(probe_live: bool = False) -> str:
    config = _resolve_audio_config()
    configured = _audio_configured(config)
    payload: dict[str, Any] = {
        "status": "ok",
        "provider": "elevenlabs",
        "configured": configured,
        "capabilities": {
            "text_to_speech": {"status": "available" if configured else "unavailable"},
            "voice_search": {"status": "available" if configured else "unavailable"},
            "voice_conversion": {"status": "available" if configured else "unavailable"},
            "advanced_dubbing": {"status": "available" if configured else "unavailable"},
            "dubbing_download": {"status": "available" if configured else "unavailable"},
            "voice_cloning": {"status": "unknown" if configured else "unavailable"},
            "music_generation": {"status": "unknown" if configured else "unavailable"},
            "singing_generation": {"status": "unknown" if configured else "unavailable"},
        },
    }
    if not configured or not probe_live:
        return json.dumps(payload)
    provider = _elevenlabs_provider(config)
    try:
        subscription = await provider.get_subscription(ElevenLabsSubscriptionRequest())
        voices = await provider.list_voices(ElevenLabsVoicesListRequest())
    except RuntimeError as exc:
        payload["probe_error"] = str(exc)
        return json.dumps(payload)
    tier = (subscription.tier or "").strip().lower()
    paid = tier not in {"", "free"}
    payload["subscription"] = {
        "tier": subscription.tier,
        "status": subscription.status,
    }
    payload["voice_count"] = len(voices.voices)
    for key in ("voice_cloning", "music_generation", "singing_generation"):
        payload["capabilities"][key] = (
            {"status": "available"}
            if paid
            else {"status": "unavailable", "reason": "paid_plan_required"}
        )
    return json.dumps(payload)


@tool(
    name="voice_search",
    description=(
        "Search ElevenLabs shared voices by language, locale, accent, gender, age, "
        "category, or free-text query before choosing a voice for TTS."
    ),
    params={
        "language": {
            "type": "string",
            "description": "Language code such as zh, en, ja, ko, fr, de, es, or pt.",
        },
        "accent": {
            "type": "string",
            "description": (
                "Desired accent label, e.g. beijing mandarin, british, american, "
                "mexican, taiwan mandarin."
            ),
        },
        "locale": {
            "type": "string",
            "description": "Optional locale hint such as zh-CN, zh-TW, en-GB, or es-MX.",
        },
        "gender": {"type": "string", "description": "Optional gender filter."},
        "age": {"type": "string", "description": "Optional age filter."},
        "category": {"type": "string", "description": "Optional voice category filter."},
        "search": {"type": "string", "description": "Optional free-text search query."},
        "page_size": {
            "type": "integer",
            "description": "Number of voices to return (1 to 50, default 10).",
            "minimum": 1,
            "maximum": 50,
        },
    },
    required=[],
    sandbox=SandboxToolDescriptor.media(kind="media.voice_search"),
)
async def voice_search(
    language: str = "",
    accent: str = "",
    locale: str = "",
    gender: str = "",
    age: str = "",
    category: str = "",
    search: str = "",
    page_size: int = 10,
) -> str:
    config = _resolve_audio_config()
    if not _audio_configured(config):
        return _audio_not_available_payload(
            tool_name="voice_search",
            missing_capability="voice_search",
            note="ElevenLabs voice search provider is disabled or not configured.",
        )
    try:
        result = await _elevenlabs_provider(config).search_shared_voices(
            ElevenLabsSharedVoicesRequest(
                language=language.strip() or None,
                accent=accent.strip() or None,
                locale=locale.strip() or None,
                gender=gender.strip() or None,
                age=age.strip() or None,
                category=category.strip() or None,
                search=search.strip() or None,
                page_size=page_size,
            )
        )
    except RuntimeError as exc:
        return _audio_not_available_payload(
            tool_name="voice_search",
            missing_capability="voice_search",
            note=str(exc),
        )
    return json.dumps(
        {
            "status": "ok",
            "provider": result.provider,
            "voices": [_shared_voice_summary(voice) for voice in result.voices],
            "has_more": result.raw.get("has_more"),
            "total_count": result.raw.get("total_count"),
        }
    )


@tool(
    name="tts",
    description=(
        "Synthesize text to speech audio using a TTS provider. "
        "Returns an explicit not_available envelope when no TTS provider is configured."
    ),
    params={
        "text": {
            "type": "string",
            "description": "Text to synthesize (max 4096 characters).",
        },
        "voice": {
            "type": "string",
            "description": (
                "ElevenLabs voice identifier. Uses audio.tts.voice when omitted."
            ),
        },
        "output_path": {
            "type": "string",
            "description": "Output file path. Auto-generated if omitted.",
        },
        "language_code": {
            "type": "string",
            "description": (
                "Optional BCP-47 language/locale hint such as zh, zh-CN, "
                "en-US, en-GB, ja-JP, ko-KR, es-MX, or fr-FR."
            ),
        },
        "speed": {
            "type": "number",
            "description": "Playback speed multiplier (0.25 to 4.0, default 1.0).",
            "minimum": 0.25,
            "maximum": 4.0,
        },
        "stability": {
            "type": "number",
            "description": "Optional ElevenLabs stability voice setting (0.0 to 1.0).",
            "minimum": 0.0,
            "maximum": 1.0,
        },
        "similarity_boost": {
            "type": "number",
            "description": (
                "Optional ElevenLabs similarity boost voice setting (0.0 to 1.0)."
            ),
            "minimum": 0.0,
            "maximum": 1.0,
        },
        "style": {
            "type": "number",
            "description": "Optional ElevenLabs style exaggeration setting (0.0 to 1.0).",
            "minimum": 0.0,
            "maximum": 1.0,
        },
        "use_speaker_boost": {
            "type": "boolean",
            "description": "Optional ElevenLabs speaker boost setting.",
        },
    },
    required=["text"],
    sandbox=SandboxToolDescriptor.media(kind="media.tts"),
)
async def tts(
    text: str,
    voice: str = "",
    output_path: str | None = None,
    language_code: str = "",
    speed: float = 1.0,
    stability: float | None = None,
    similarity_boost: float | None = None,
    style: float | None = None,
    use_speaker_boost: bool | None = None,
) -> str:
    if not text or not text.strip():
        raise ToolError("Text must not be empty")

    if len(text) > 4096:
        raise ToolError(f"Text exceeds 4096 character limit ({len(text)} chars)")

    if speed < 0.25 or speed > 4.0:
        raise ToolError("Speed must be between 0.25 and 4.0")

    config = _resolve_audio_config()
    if not _audio_configured(config):
        return _audio_not_available_payload(
            tool_name="tts",
            missing_capability="text_to_speech",
            note="ElevenLabs audio provider is disabled or not configured.",
        )
    tts_config = getattr(config, "tts", None)
    model_id = str(getattr(tts_config, "model", "") or "eleven_multilingual_v2")
    resolved_voice = str(voice or getattr(tts_config, "voice", "") or "").strip()
    if not resolved_voice:
        raise ToolError("Voice must not be empty")
    resolved_language_code = str(
        language_code or getattr(tts_config, "language_code", "") or ""
    ).strip()
    voice_settings = _tts_voice_settings(
        speed=speed,
        stability=stability,
        similarity_boost=similarity_boost,
        style=style,
        use_speaker_boost=use_speaker_boost,
        tts_config=tts_config,
    )
    output_format = str(getattr(tts_config, "output_format", "") or "mp3_44100_128")
    timeout_seconds = float(getattr(tts_config, "timeout_seconds", 120.0) or 120.0)
    provider = _elevenlabs_provider(config)
    try:
        result = await provider.text_to_speech(
            ElevenLabsTextToSpeechRequest(
                text=text,
                voice=resolved_voice,
                model_id=model_id,
                output_format=output_format,
                timeout_seconds=timeout_seconds,
                language_code=resolved_language_code or None,
                voice_settings=voice_settings,
            )
        )
    except RuntimeError as exc:
        return _audio_not_available_payload(
            tool_name="tts",
            missing_capability="text_to_speech",
            note=str(exc),
        )
    return _write_generated_audio_payload(
        result,
        output_path=output_path,
        prefix="speech",
        artifact_source="tts",
    )
