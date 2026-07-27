"""Shared validation policy for image-generation routing and endpoints."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from opensquilla.endpoint_identity import base_url_allows_credential_reuse

IMAGE_GENERATION_OFFICIAL_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}
_PROVIDER_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _has_unsafe_model_ref_character(value: str) -> bool:
    return any(
        character == "\\"
        or character.isspace()
        or ord(character) < 0x20
        or ord(character) == 0x7F
        for character in value
    )


def parse_image_generation_model_ref(raw: str) -> tuple[str, str]:
    """Parse a syntactically safe ``provider/model[/...]`` image route.

    ``openrouter/auto`` is OpenRouter's wire-model name. Preserve that nested
    name even when an older config uses the ambiguous short route; callers that
    serialize the parsed result produce the unambiguous canonical reference
    ``openrouter/openrouter/auto``.
    """
    if not isinstance(raw, str):
        raise ValueError(f"Invalid image generation model ref: {raw!r}")
    normalized = raw.strip()
    provider, sep, model = normalized.partition("/")
    model_segments = model.split("/") if model else []
    if (
        not sep
        or not _PROVIDER_TOKEN_RE.fullmatch(provider)
        or not model_segments
        or any(not segment for segment in model_segments)
        or _has_unsafe_model_ref_character(model)
    ):
        raise ValueError(f"Invalid image generation model ref: {raw!r}")
    if provider == "openrouter" and model == "auto":
        return provider, "openrouter/auto"
    return provider, model


def conflicting_image_generation_endpoint_provider(
    provider_id: str,
    base_url: str,
) -> str | None:
    """Return the other known provider whose official origin owns ``base_url``.

    Unknown custom compatible endpoints are intentionally allowed. Only an
    endpoint on another registered image provider's official HTTP origin is a
    deterministic provider mismatch.
    """
    candidate = str(base_url or "").strip()
    if not candidate:
        return None
    for known_provider_id, official_base_url in (
        IMAGE_GENERATION_OFFICIAL_BASE_URLS.items()
    ):
        if known_provider_id == provider_id:
            continue
        if base_url_allows_credential_reuse(official_base_url, candidate):
            return known_provider_id
    return None


def is_valid_image_generation_base_url(value: str) -> bool:
    """Return whether ``value`` is an absolute HTTP(S) endpoint.

    Image providers pass this value straight to ``httpx``.  Validate it at
    configuration/status boundaries so an invalid legacy value is actionable
    instead of presenting as Ready and failing only when the tool is called.
    """

    untrimmed = str(value or "")
    raw = untrimmed.strip()
    if (
        not raw
        or raw != untrimmed
        or any(character.isspace() or ord(character) < 0x20 for character in raw)
        or "?" in raw
        or "#" in raw
    ):
        return False
    try:
        parsed = urlsplit(raw)
        # Accessing ``port`` validates both syntax and range.
        _port = parsed.port
        return (
            parsed.scheme.lower() in {"http", "https"}
            and bool(parsed.hostname)
            and "\\" not in parsed.netloc
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
    except (UnicodeError, ValueError):
        return False


def resolve_image_generation_base_url(
    *,
    provider_id: str,
    provider_config: object | None,
    llm_config: object | None,
    default_base_url: str,
) -> str:
    """Resolve the image endpoint, including same-provider LLM inheritance.

    Image-provider defaults are materialized by the config schema.  A base URL
    absent from the stored image provider config therefore needs provenance,
    not a value comparison, before an operator-selected LLM endpoint may be
    inherited.
    """

    base_url = _config_string(provider_config, "base_url", default_base_url) or default_base_url
    if not _field_was_set(provider_config, "base_url") and _llm_provider_matches(
        llm_config,
        provider_id,
    ):
        return _llm_chosen_base_url(llm_config) or base_url
    return base_url


def image_generation_llm_endpoint_allows_credential_reuse(
    *,
    provider_id: str,
    llm_config: object | None,
    default_base_url: str,
    effective_base_url: str,
) -> bool:
    """Whether the active LLM credential belongs to this image endpoint."""

    if not _llm_provider_matches(llm_config, provider_id):
        return False
    llm_base_url = _llm_chosen_base_url(llm_config) or default_base_url
    return base_url_allows_credential_reuse(llm_base_url, effective_base_url)


def _config_string(config: object | None, name: str, default: str = "") -> str:
    value = getattr(config, name, default) if config is not None else default
    return value if isinstance(value, str) else default


def _field_was_set(config: object | None, name: str) -> bool:
    fields_set = getattr(config, "model_fields_set", None)
    return isinstance(fields_set, set) and name in fields_set


def _llm_provider_matches(llm_config: object | None, provider_id: str) -> bool:
    return _config_string(llm_config, "provider").strip().lower() == provider_id


def _llm_chosen_base_url(llm_config: object | None) -> str:
    value = _config_string(llm_config, "base_url")
    if not value or llm_config is None:
        return ""
    fields = getattr(type(llm_config), "model_fields", None)
    field = fields.get("base_url") if isinstance(fields, dict) else None
    default = str(getattr(field, "default", "") or "") if field is not None else ""
    return "" if value == default else value


__all__ = [
    "IMAGE_GENERATION_OFFICIAL_BASE_URLS",
    "conflicting_image_generation_endpoint_provider",
    "image_generation_llm_endpoint_allows_credential_reuse",
    "is_valid_image_generation_base_url",
    "parse_image_generation_model_ref",
    "resolve_image_generation_base_url",
]
