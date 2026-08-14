"""Typed normalization for TokenRhythm's public and authenticated catalogs.

The public ``/api/models`` document describes what TokenRhythm publishes on
its website.  The authenticated ``/v1/models`` document describes what the
current credential may use.  They are deliberately kept as separate typed
records: public data must not grant entitlement, and compatibility projections
must not erase an explicit ``False`` or replace a published value with a
runtime safety budget.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import structlog

from opensquilla.env import trust_env as _trust_env
from opensquilla.secrets import clean_header_secret

from .app_attribution import provider_app_headers
from .error_redaction import redacted_httpx_error
from .fx import TOKENRHYTHM_CNY_PER_USD
from .tokenrhythm_correlation import (
    redact_tokenrhythm_install_ids,
    tokenrhythm_install_id_headers,
)

log = structlog.get_logger(__name__)
_TOKENS_PER_MTOK = Decimal("1000000")
TOKENRHYTHM_PUBLIC_CATALOG_URL = "https://tokenrhythm.studio/api/models"
TOKENRHYTHM_API_BASE_URL = "https://tokenrhythm.studio/v1"
_TOKENRHYTHM_OFFICIAL_API_HOST = "tokenrhythm.studio"
_RESPONSE_CAPABILITY_KEYS = (
    "stream",
    "tools",
    "background",
    "compact",
    "webSearch",
    "mcp",
    "codeInterpreter",
    "imageGeneration",
    "fileSearch",
    "cancel",
)
_ENVELOPE_FIELDS = frozenset({"code", "message", "data", "traceId", "object"})
_PUBLISHED_ROW_FIELDS = frozenset(
    {
        "id",
        "name",
        "type",
        "status",
        "contextWindow",
        "maxOutputTokens",
        "capabilities",
        "protocolCapabilities",
        "modalities",
        "provider",
        "providerDisplayName",
        "supportsStream",
        "supportsTools",
        "reasoningMode",
        "reasoningDefault",
        "reasoningSupportedEfforts",
        "reasoningSupportsMaxTokens",
        "supportsReasoning",
        "supportsVision",
        "supportsAnthropic",
        "supportsResponses",
        "supportsStreaming",
        "supportsEmbeddings",
        "supportsResponses",
        "abilities",
        "inputPrice",
        "outputPrice",
        "cacheReadPrice",
        "discountInputPrice",
        "discountOutputPrice",
        "discountCacheReadPrice",
        "hasDiscount",
        "effectiveInputPrice",
        "effectiveOutputPrice",
        "effectiveCacheReadPrice",
        "billingMode",
        "pricePerImage",
        "billingUnit",
        "currency",
        "price",
        "latency",
        "context",
    }
)
_DECLARED_ROW_FIELDS = _PUBLISHED_ROW_FIELDS | frozenset(
    {
        "object",
        "created",
        "owned_by",
        "context_length",
        "context_window",
        "max_completion_tokens",
        "top_provider",
        "supported_parameters",
        "supports_tools",
        "supports_reasoning",
        "supports_vision",
        "supports_anthropic",
        "supports_responses",
        "supports_streaming",
        "responses",
        "responses_modes",
        "responses_capabilities",
    }
)
_CAPABILITY_FIELDS = frozenset(
    {
        "openai",
        "anthropic",
        "stream",
        "streaming",
        "tools",
        "reasoning",
        "cacheRead",
        "embeddings",
        "vision",
        "audio",
        "video",
        "file",
        "responses",
    }
)
_PROTOCOL_FIELDS = frozenset({"openai", "anthropic", "responses"})
_CHAT_PROTOCOL_FIELDS = frozenset(
    {"available", "stream", "tools", "reasoning", "cacheRead", "embeddings", "vision"}
)
_RESPONSES_PROTOCOL_FIELDS = frozenset(
    {"available", "modes", "capabilities"}
) | frozenset(_RESPONSE_CAPABILITY_KEYS)
_TOP_PROVIDER_FIELDS = frozenset(
    {"context_length", "max_completion_tokens", "is_moderated"}
)


def canonical_tokenrhythm_base_url(value: str) -> str:
    """Return a stable, credential-free spelling of a provider base URL.

    Official TokenRhythm origin-only and ``/v1`` spellings are normalized to
    the same API base so gateway refresh, onboarding draft detection, and
    runtime fallback lookup cannot drift on case, default port, or a trailing
    slash. Any other origin/path or URL decoration fails closed as ``""``.
    """

    try:
        parsed = urlsplit(str(value or "").strip())
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except (UnicodeError, ValueError):
        return ""
    if (
        not scheme
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return ""
    path = parsed.path.rstrip("/")
    if not (
        scheme == "https"
        and host == _TOKENRHYTHM_OFFICIAL_API_HOST
        and (port is None or port == 443)
        and path in {"", "/v1"}
    ):
        return ""
    return urlunsplit((scheme, host, "/v1", "", ""))


def _identity_digest(domain: str, *parts: str) -> str:
    digest = hashlib.sha256()
    digest.update(f"opensquilla:{domain}:v1".encode())
    for part in parts:
        digest.update(b"\0")
        digest.update(part.encode())
    return digest.hexdigest()


def tokenrhythm_authority_identity(
    *,
    provider: str,
    base_url: str,
    api_key: str,
) -> str | None:
    """Return the secret-free identity of one official authenticated authority.

    ``None`` means the deployment is not eligible for the official dual-source
    catalog.  The digest itself is suitable only for private snapshot lookup;
    callers must never serialize it into route plans, turn metadata, or logs.
    """

    provider_id = str(provider or "").strip().lower()
    key = str(api_key or "").strip()
    canonical_base = canonical_tokenrhythm_base_url(base_url)
    if (
        provider_id != "tokenrhythm"
        or not key
        or not canonical_base
        or not is_official_tokenrhythm_endpoint(canonical_base)
    ):
        return None
    return _identity_digest(
        "tokenrhythm-authority",
        provider_id,
        canonical_base,
        key,
    )


def tokenrhythm_transport_fingerprint(
    authority_identity: str,
    *,
    proxy: str,
) -> str:
    """Return a private refresh identity for one authority and proxy."""

    proxy_digest = _identity_digest(
        "tokenrhythm-proxy", str(proxy or "").strip()
    )
    return _identity_digest(
        "tokenrhythm-transport", authority_identity, proxy_digest
    )


def tokenrhythm_public_transport_fingerprint(*, proxy: str) -> str:
    """Return a private refresh identity for the keyless public request."""

    proxy_digest = _identity_digest(
        "tokenrhythm-proxy", str(proxy or "").strip()
    )
    return _identity_digest("tokenrhythm-public-transport", proxy_digest)


def _log_unknown_fields(
    value: object,
    known: frozenset[str],
    *,
    source: str,
    scope: str,
    known_secret: str = "",
) -> None:
    if not isinstance(value, Mapping):
        return
    unknown = sorted(
        {
            _redact_schema_field_name(str(key), known_secret=known_secret)[:80]
            for key in value
            if isinstance(key, str) and key not in known
        }
    )[:32]
    if unknown:
        log.debug(
            "tokenrhythm_catalog.schema_drift",
            source=source,
            scope=scope,
            fields=unknown,
        )


def _redact_schema_field_name(value: str, *, known_secret: str) -> str:
    redacted = redact_tokenrhythm_install_ids(value)
    return redacted.replace(known_secret, "***") if known_secret else redacted


def _log_row_schema(
    row: Mapping[str, Any],
    *,
    source: str,
    known_secret: str = "",
) -> None:
    known = _PUBLISHED_ROW_FIELDS if source == "published" else _DECLARED_ROW_FIELDS
    _log_unknown_fields(
        row,
        known,
        source=source,
        scope="row",
        known_secret=known_secret,
    )
    capabilities = row.get("capabilities")
    if isinstance(capabilities, Mapping):
        _log_unknown_fields(
            capabilities,
            _CAPABILITY_FIELDS,
            source=source,
            scope="row.capabilities",
            known_secret=known_secret,
        )
    protocols = row.get("protocolCapabilities")
    if isinstance(protocols, Mapping):
        _log_unknown_fields(
            protocols,
            _PROTOCOL_FIELDS,
            source=source,
            scope="row.protocolCapabilities",
            known_secret=known_secret,
        )
        for protocol_name in ("openai", "anthropic", "responses"):
            protocol = protocols.get(protocol_name)
            if isinstance(protocol, Mapping):
                protocol_fields = (
                    _RESPONSES_PROTOCOL_FIELDS
                    if protocol_name == "responses"
                    else _CHAT_PROTOCOL_FIELDS
                )
                _log_unknown_fields(
                    protocol,
                    protocol_fields,
                    source=source,
                    scope=f"row.protocolCapabilities.{protocol_name}",
                    known_secret=known_secret,
                )
    direct_responses = row.get("responses")
    if isinstance(direct_responses, Mapping):
        _log_unknown_fields(
            direct_responses,
            _RESPONSES_PROTOCOL_FIELDS | frozenset({"capabilities"}),
            source=source,
            scope="row.responses",
            known_secret=known_secret,
        )
    top_provider = row.get("top_provider")
    if isinstance(top_provider, Mapping):
        _log_unknown_fields(
            top_provider,
            _TOP_PROVIDER_FIELDS,
            source=source,
            scope="row.top_provider",
            known_secret=known_secret,
        )


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _first_bool(*values: object) -> bool | None:
    for value in values:
        parsed = _bool(value)
        if parsed is not None:
            return parsed
    return None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer() or not math.isfinite(value):
            return None
        value = int(value)
    elif isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            return None
    if not isinstance(value, int) or value < 0:
        return None
    return value


def _positive_int(value: object) -> int | None:
    parsed = _nonnegative_int(value)
    return parsed if parsed is not None and parsed > 0 else None


def _decimal_string(value: object) -> str | None:
    """Validate a non-negative decimal without round-tripping through float."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0 or (parsed and parsed.adjusted() > 308):
        return None
    return raw


def _string_tuple(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _text(item)
        if text is None or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return tuple(result)


def is_official_tokenrhythm_endpoint(value: str) -> bool:
    """Return whether credentials may be sent to the registry API root.

    Only the configured ``https://tokenrhythm.studio/v1`` root (plus its
    origin-only spelling, normalized by :func:`canonical_tokenrhythm_base_url`)
    is trusted.  A same-host custom path is a different authority and must not
    inherit website metadata or receive authenticated discovery requests.
    """

    try:
        return canonical_tokenrhythm_base_url(value) == TOKENRHYTHM_API_BASE_URL
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class TokenRhythmCapabilities:
    tools: bool | None = None
    reasoning: bool | None = None
    vision: bool | None = None
    anthropic: bool | None = None
    responses: bool | None = None
    streaming: bool | None = None

    @classmethod
    def from_source(cls, row: Mapping[str, Any]) -> TokenRhythmCapabilities:
        capabilities = _mapping(row.get("capabilities"))
        raw_capability_names = row.get("capabilities")
        capability_names = (
            {str(item) for item in raw_capability_names}
            if isinstance(raw_capability_names, list)
            else set()
        )
        protocols = _mapping(row.get("protocolCapabilities"))
        anthropic = _mapping(protocols.get("anthropic"))
        responses = _mapping(protocols.get("responses"))
        supported = row.get("supported_parameters")
        supported_names = (
            {str(item) for item in supported}
            if isinstance(supported, list)
            else set()
        )
        tools = _first_bool(
            capabilities.get("tools"),
            row.get("supportsTools"),
            row.get("supports_tools"),
        )
        if tools is None and {"tools", "tool_choice"}.intersection(supported_names):
            tools = True
        if tools is None and "tools" in capability_names:
            tools = True
        reasoning = _first_bool(
            capabilities.get("reasoning"),
            row.get("supportsReasoning"),
            row.get("supports_reasoning"),
        )
        if reasoning is None and {"reasoning", "reasoning_effort"}.intersection(
            supported_names
        ):
            reasoning = True
        if reasoning is None and "reasoning" in capability_names:
            reasoning = True
        vision = _first_bool(
            capabilities.get("vision"),
            row.get("supportsVision"),
            row.get("supports_vision"),
        )
        if vision is None and "vision" in capability_names:
            vision = True
        return cls(
            tools=tools,
            reasoning=reasoning,
            vision=vision,
            anthropic=_first_bool(
                capabilities.get("anthropic"),
                anthropic.get("available"),
                row.get("supportsAnthropic"),
                row.get("supports_anthropic"),
                True if "anthropic" in capability_names else None,
            ),
            responses=_first_bool(
                capabilities.get("responses"),
                responses.get("available"),
                row.get("supportsResponses"),
                row.get("supports_responses"),
                True if "responses" in capability_names else None,
            ),
            streaming=_first_bool(
                capabilities.get("stream"),
                capabilities.get("streaming"),
                row.get("supportsStream"),
                row.get("supports_streaming"),
                True if {"stream", "streaming"}.intersection(capability_names) else None,
            ),
        )

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> TokenRhythmCapabilities:
        return cls(
            tools=_bool(value.get("tools")),
            reasoning=_bool(value.get("reasoning")),
            vision=_bool(value.get("vision")),
            anthropic=_bool(value.get("anthropic")),
            responses=_bool(value.get("responses")),
            streaming=_bool(value.get("streaming")),
        )

    def to_wire(self) -> dict[str, bool | None]:
        return {
            "tools": self.tools,
            "reasoning": self.reasoning,
            "vision": self.vision,
            "anthropic": self.anthropic,
            "responses": self.responses,
            "streaming": self.streaming,
        }


@dataclass(frozen=True, slots=True)
class TokenRhythmResponseCapabilityStates:
    stream: bool | None = None
    tools: bool | None = None
    background: bool | None = None
    compact: bool | None = None
    web_search: bool | None = None
    mcp: bool | None = None
    code_interpreter: bool | None = None
    image_generation: bool | None = None
    file_search: bool | None = None
    cancel: bool | None = None

    @classmethod
    def from_source(
        cls,
        value: Mapping[str, Any],
        enabled_names: tuple[str, ...] | None,
    ) -> TokenRhythmResponseCapabilityStates:
        enabled = set(enabled_names or ())

        def state(key: str) -> bool | None:
            if key in value:
                return _bool(value.get(key))
            return True if key in enabled else None

        return cls(
            stream=state("stream"),
            tools=state("tools"),
            background=state("background"),
            compact=state("compact"),
            web_search=state("webSearch"),
            mcp=state("mcp"),
            code_interpreter=state("codeInterpreter"),
            image_generation=state("imageGeneration"),
            file_search=state("fileSearch"),
            cancel=state("cancel"),
        )

    @classmethod
    def from_wire(
        cls,
        value: Mapping[str, Any],
        enabled_names: tuple[str, ...],
    ) -> TokenRhythmResponseCapabilityStates:
        return cls.from_source(value, enabled_names)

    def to_wire(self) -> dict[str, bool | None]:
        return {
            "stream": self.stream,
            "tools": self.tools,
            "background": self.background,
            "compact": self.compact,
            "webSearch": self.web_search,
            "mcp": self.mcp,
            "codeInterpreter": self.code_interpreter,
            "imageGeneration": self.image_generation,
            "fileSearch": self.file_search,
            "cancel": self.cancel,
        }

    def enabled_names(self) -> tuple[str, ...]:
        wire = self.to_wire()
        return tuple(key for key in _RESPONSE_CAPABILITY_KEYS if wire[key] is True)


@dataclass(frozen=True, slots=True)
class TokenRhythmResponses:
    modes: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    capability_states: TokenRhythmResponseCapabilityStates = (
        TokenRhythmResponseCapabilityStates()
    )

    @classmethod
    def from_source(cls, row: Mapping[str, Any]) -> TokenRhythmResponses | None:
        protocols = _mapping(row.get("protocolCapabilities"))
        protocol_responses = protocols.get("responses")
        direct_responses = row.get("responses")
        if isinstance(protocol_responses, Mapping):
            response_row = protocol_responses
            response_present = True
        elif isinstance(direct_responses, Mapping):
            response_row = direct_responses
            response_present = True
        else:
            response_row = {}
            response_present = False
        top_modes = _string_tuple(row.get("responses_modes"))
        top_capabilities = _string_tuple(row.get("responses_capabilities"))
        if not response_present and top_modes is None and top_capabilities is None:
            return None
        response_modes = (
            _string_tuple(response_row.get("modes")) if "modes" in response_row else None
        )
        modes = response_modes if response_modes is not None else (top_modes or ())
        response_capabilities = (
            _string_tuple(response_row.get("capabilities"))
            if "capabilities" in response_row
            else None
        )
        enabled_capabilities = (
            response_capabilities
            if response_capabilities is not None
            else (top_capabilities or ())
        )
        capability_states = TokenRhythmResponseCapabilityStates.from_source(
            response_row,
            enabled_capabilities,
        )
        capabilities = list(capability_states.enabled_names())
        for name in enabled_capabilities:
            if name not in _RESPONSE_CAPABILITY_KEYS and name not in capabilities:
                capabilities.append(name)
        return cls(
            modes=modes,
            capabilities=tuple(capabilities),
            capability_states=capability_states,
        )

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> TokenRhythmResponses:
        capabilities = _string_tuple(value.get("capabilities")) or ()
        capability_states = TokenRhythmResponseCapabilityStates.from_wire(
            _mapping(value.get("capabilityStates")),
            capabilities,
        )
        normalized_capabilities = list(capability_states.enabled_names())
        for name in capabilities:
            if name not in _RESPONSE_CAPABILITY_KEYS and name not in normalized_capabilities:
                normalized_capabilities.append(name)
        return cls(
            modes=_string_tuple(value.get("modes")) or (),
            capabilities=tuple(normalized_capabilities),
            capability_states=capability_states,
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "modes": list(self.modes),
            "capabilities": list(self.capabilities),
            "capabilityStates": self.capability_states.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class TokenRhythmPriceBuckets:
    input: str | None = None
    output: str | None = None
    cache_read: str | None = None

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> TokenRhythmPriceBuckets:
        return cls(
            input=_decimal_string(value.get("input")),
            output=_decimal_string(value.get("output")),
            cache_read=_decimal_string(value.get("cacheRead")),
        )

    def to_wire(self) -> dict[str, str | None]:
        return {
            "input": self.input,
            "output": self.output,
            "cacheRead": self.cache_read,
        }


@dataclass(frozen=True, slots=True)
class TokenRhythmPricing:
    currency: str | None
    billing_mode: str | None
    billing_unit: int | None
    has_discount: bool | None
    price_per_image: str | None
    standard: TokenRhythmPriceBuckets
    discount: TokenRhythmPriceBuckets
    effective: TokenRhythmPriceBuckets

    @classmethod
    def from_source(cls, row: Mapping[str, Any]) -> TokenRhythmPricing | None:
        pricing_keys = {
            "currency",
            "billingMode",
            "billingUnit",
            "hasDiscount",
            "pricePerImage",
            "inputPrice",
            "outputPrice",
            "cacheReadPrice",
            "discountInputPrice",
            "discountOutputPrice",
            "discountCacheReadPrice",
            "effectiveInputPrice",
            "effectiveOutputPrice",
            "effectiveCacheReadPrice",
        }
        if not pricing_keys.intersection(row):
            return None
        return cls(
            currency=_text(row.get("currency")),
            billing_mode=_text(row.get("billingMode")),
            billing_unit=_positive_int(row.get("billingUnit")),
            has_discount=_bool(row.get("hasDiscount")),
            price_per_image=_decimal_string(row.get("pricePerImage")),
            standard=TokenRhythmPriceBuckets(
                input=_decimal_string(row.get("inputPrice")),
                output=_decimal_string(row.get("outputPrice")),
                cache_read=_decimal_string(row.get("cacheReadPrice")),
            ),
            discount=TokenRhythmPriceBuckets(
                input=_decimal_string(row.get("discountInputPrice")),
                output=_decimal_string(row.get("discountOutputPrice")),
                cache_read=_decimal_string(row.get("discountCacheReadPrice")),
            ),
            effective=TokenRhythmPriceBuckets(
                input=_decimal_string(row.get("effectiveInputPrice")),
                output=_decimal_string(row.get("effectiveOutputPrice")),
                cache_read=_decimal_string(row.get("effectiveCacheReadPrice")),
            ),
        )

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> TokenRhythmPricing:
        return cls(
            currency=_text(value.get("currency")),
            billing_mode=_text(value.get("billingMode")),
            billing_unit=_positive_int(value.get("billingUnit")),
            has_discount=_bool(value.get("hasDiscount")),
            price_per_image=_decimal_string(value.get("pricePerImage")),
            standard=TokenRhythmPriceBuckets.from_wire(_mapping(value.get("standard"))),
            discount=TokenRhythmPriceBuckets.from_wire(_mapping(value.get("discount"))),
            effective=TokenRhythmPriceBuckets.from_wire(_mapping(value.get("effective"))),
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "billingMode": self.billing_mode,
            "billingUnit": self.billing_unit,
            "hasDiscount": self.has_discount,
            "pricePerImage": self.price_per_image,
            "standard": self.standard.to_wire(),
            "discount": self.discount.to_wire(),
            "effective": self.effective.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class TokenRhythmPublishedModel:
    name: str
    provider_display_name: str | None
    model_type: str | None
    status: str | None
    modalities: tuple[str, ...] | None
    context_window: int | None
    max_output_tokens: int | None
    reasoning_mode: str | None
    reasoning_default: str | None
    reasoning_supported_efforts: tuple[str, ...] | None
    reasoning_supports_max_tokens: bool | None
    capabilities: TokenRhythmCapabilities
    responses: TokenRhythmResponses | None
    pricing: TokenRhythmPricing | None

    @classmethod
    def from_source(
        cls, model_id: str, row: Mapping[str, Any]
    ) -> TokenRhythmPublishedModel:
        return cls(
            name=_text(row.get("name")) or model_id,
            provider_display_name=_text(row.get("providerDisplayName")),
            model_type=_text(row.get("type")),
            status=_text(row.get("status")),
            modalities=_string_tuple(row.get("modalities")),
            context_window=_positive_int(row.get("contextWindow")),
            max_output_tokens=_positive_int(row.get("maxOutputTokens")),
            reasoning_mode=_text(row.get("reasoningMode")),
            reasoning_default=_text(row.get("reasoningDefault")),
            reasoning_supported_efforts=_string_tuple(
                row.get("reasoningSupportedEfforts")
            ),
            reasoning_supports_max_tokens=_bool(
                row.get("reasoningSupportsMaxTokens")
            ),
            capabilities=TokenRhythmCapabilities.from_source(row),
            responses=TokenRhythmResponses.from_source(row),
            pricing=TokenRhythmPricing.from_source(row),
        )

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> TokenRhythmPublishedModel:
        return cls(
            name=_text(value.get("name")) or "",
            provider_display_name=_text(value.get("providerDisplayName")),
            model_type=_text(value.get("modelType")),
            status=_text(value.get("status")),
            modalities=_string_tuple(value.get("modalities")),
            context_window=_positive_int(value.get("contextWindow")),
            max_output_tokens=_positive_int(value.get("maxOutputTokens")),
            reasoning_mode=_text(value.get("reasoningMode")),
            reasoning_default=_text(value.get("reasoningDefault")),
            reasoning_supported_efforts=(
                _string_tuple(value.get("reasoningSupportedEfforts"))
                if isinstance(value.get("reasoningSupportedEfforts"), list)
                else None
            ),
            reasoning_supports_max_tokens=_bool(
                value.get("reasoningSupportsMaxTokens")
            ),
            capabilities=TokenRhythmCapabilities.from_wire(
                _mapping(value.get("capabilities"))
            ),
            responses=(
                TokenRhythmResponses.from_wire(_mapping(value.get("responses")))
                if isinstance(value.get("responses"), Mapping)
                else None
            ),
            pricing=(
                TokenRhythmPricing.from_wire(_mapping(value.get("pricing")))
                if isinstance(value.get("pricing"), Mapping)
                else None
            ),
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "providerDisplayName": self.provider_display_name,
            "modelType": self.model_type,
            "status": self.status,
            "modalities": list(self.modalities) if self.modalities is not None else None,
            "contextWindow": self.context_window,
            "maxOutputTokens": self.max_output_tokens,
            "reasoningMode": self.reasoning_mode,
            "reasoningDefault": self.reasoning_default,
            "reasoningSupportedEfforts": (
                list(self.reasoning_supported_efforts)
                if self.reasoning_supported_efforts is not None
                else None
            ),
            "reasoningSupportsMaxTokens": self.reasoning_supports_max_tokens,
            "capabilities": self.capabilities.to_wire(),
            "responses": self.responses.to_wire() if self.responses is not None else None,
            "pricing": self.pricing.to_wire() if self.pricing is not None else None,
        }


@dataclass(frozen=True, slots=True)
class TokenRhythmDeclaredModel:
    context_window: int | None
    max_output_tokens: int | None
    capabilities: TokenRhythmCapabilities
    responses: TokenRhythmResponses | None
    pricing: TokenRhythmPricing | None
    display_name: str = ""
    model_type: str | None = None
    status: str | None = None

    @classmethod
    def from_source(
        cls, model_id: str, row: Mapping[str, Any]
    ) -> TokenRhythmDeclaredModel:
        context = None
        for key in ("context_length", "context_window", "contextWindow"):
            if key in row:
                context = _positive_int(row.get(key))
                if context is not None:
                    break
        max_output = None
        if "max_completion_tokens" in row:
            max_output = _positive_int(row.get("max_completion_tokens"))
        if max_output is None:
            top_provider = _mapping(row.get("top_provider"))
            max_output = _positive_int(top_provider.get("max_completion_tokens"))
        return cls(
            context_window=context,
            max_output_tokens=max_output,
            capabilities=TokenRhythmCapabilities.from_source(row),
            responses=TokenRhythmResponses.from_source(row),
            pricing=TokenRhythmPricing.from_source(row),
            display_name=_text(row.get("name")) or model_id,
            model_type=_text(row.get("type")),
            status=_text(row.get("status")),
        )

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> TokenRhythmDeclaredModel:
        return cls(
            context_window=_positive_int(value.get("contextWindow")),
            max_output_tokens=_positive_int(value.get("maxOutputTokens")),
            capabilities=TokenRhythmCapabilities.from_wire(
                _mapping(value.get("capabilities"))
            ),
            responses=(
                TokenRhythmResponses.from_wire(_mapping(value.get("responses")))
                if isinstance(value.get("responses"), Mapping)
                else None
            ),
            pricing=(
                TokenRhythmPricing.from_wire(_mapping(value.get("pricing")))
                if isinstance(value.get("pricing"), Mapping)
                else None
            ),
            display_name=_text(value.get("displayName")) or "",
            model_type=_text(value.get("modelType")),
            status=_text(value.get("status")),
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "displayName": self.display_name,
            "modelType": self.model_type,
            "status": self.status,
            "contextWindow": self.context_window,
            "maxOutputTokens": self.max_output_tokens,
            "capabilities": self.capabilities.to_wire(),
            "responses": self.responses.to_wire() if self.responses is not None else None,
            "pricing": self.pricing.to_wire() if self.pricing is not None else None,
        }


@dataclass(frozen=True, slots=True)
class TokenRhythmModelMetadata:
    published: TokenRhythmPublishedModel | None
    declared: TokenRhythmDeclaredModel | None
    schema_version: int = 1

    def to_wire(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "published": self.published.to_wire() if self.published is not None else None,
            "declared": self.declared.to_wire() if self.declared is not None else None,
        }


@dataclass(frozen=True, slots=True)
class TokenRhythmCatalogModel:
    model_id: str
    display_name: str
    metadata: TokenRhythmModelMetadata

    @property
    def context_window(self) -> int:
        declared = self.metadata.declared
        published = self.metadata.published
        for value in (
            declared.context_window if declared is not None else None,
            published.context_window if published is not None else None,
        ):
            if value is not None and value > 0:
                return value
        return 0

    @property
    def max_output_tokens(self) -> int:
        declared = self.metadata.declared
        published = self.metadata.published
        for value in (
            declared.max_output_tokens if declared is not None else None,
            published.max_output_tokens if published is not None else None,
        ):
            if value is not None and value > 0:
                return value
        return 0


class TokenRhythmCatalogEntries(dict[str, dict[str, Any]]):
    """Compatibility catalog fields carrying their typed published sidecar."""

    def __init__(
        self,
        entries: Mapping[str, Mapping[str, Any]],
        *,
        published: Mapping[str, TokenRhythmPublishedModel],
    ) -> None:
        super().__init__((model_id, dict(fields)) for model_id, fields in entries.items())
        self.published = dict(published)


def parse_tokenrhythm_published(
    payload: Mapping[str, Any],
    known_secret: str = "",
) -> dict[str, TokenRhythmPublishedModel]:
    _log_unknown_fields(
        payload,
        _ENVELOPE_FIELDS,
        source="published",
        scope="envelope",
        known_secret=known_secret,
    )
    data = payload.get("data")
    if not isinstance(data, list):
        return {}
    result: dict[str, TokenRhythmPublishedModel] = {}
    for raw_row in data:
        if not isinstance(raw_row, Mapping):
            continue
        _log_row_schema(
            raw_row,
            source="published",
            known_secret=known_secret,
        )
        model_id = _text(raw_row.get("id"))
        if model_id is None:
            continue
        result[model_id] = TokenRhythmPublishedModel.from_source(model_id, raw_row)
    return result


def parse_tokenrhythm_declared(
    payload: Mapping[str, Any],
    known_secret: str = "",
) -> dict[str, TokenRhythmDeclaredModel]:
    _log_unknown_fields(
        payload,
        _ENVELOPE_FIELDS,
        source="declared",
        scope="envelope",
        known_secret=known_secret,
    )
    data = payload.get("data")
    if not isinstance(data, list):
        return {}
    result: dict[str, TokenRhythmDeclaredModel] = {}
    for raw_row in data:
        if not isinstance(raw_row, Mapping):
            continue
        _log_row_schema(
            raw_row,
            source="declared",
            known_secret=known_secret,
        )
        model_id = _text(raw_row.get("id"))
        if model_id is None:
            continue
        result[model_id] = TokenRhythmDeclaredModel.from_source(model_id, raw_row)
    return result


def _validated_catalog_envelope(
    payload: object,
    *,
    source: str,
    known_secret: str,
) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError(f"invalid TokenRhythm {source} catalog envelope")
    data = payload.get("data")
    valid_data = isinstance(data, list) and all(
        isinstance(row, Mapping) and _text(row.get("id")) is not None
        for row in data
    )
    raw_code = payload.get("code")
    successful = "code" not in payload or raw_code == "0" or (
        isinstance(raw_code, int) and not isinstance(raw_code, bool) and raw_code == 0
    )
    if not valid_data or not successful:
        # Valid documents are inspected once by the parser. Failed envelopes
        # never reach it, so inspect their key names here without logging any
        # values or response bodies.
        _log_unknown_fields(
            payload,
            _ENVELOPE_FIELDS,
            source=source,
            scope="envelope",
            known_secret=known_secret,
        )
    if not valid_data:
        raise ValueError(f"invalid TokenRhythm {source} catalog envelope")
    if not successful:
        raise ValueError(f"unsuccessful TokenRhythm {source} catalog envelope")
    return payload


def _redact_catalog_fetch_text(value: str, *, api_key: str) -> str:
    redacted = redact_tokenrhythm_install_ids(value)
    return redacted.replace(api_key, "***") if api_key else redacted


async def _fetch_tokenrhythm_catalog[CatalogModelT](
    url: str,
    *,
    headers: dict[str, str],
    api_key: str,
    proxy: str,
    timeout: float,
    source: str,
    parser: Callable[[Mapping[str, Any], str], dict[str, CatalogModelT]],
) -> dict[str, CatalogModelT]:
    """Fetch one typed catalog without retaining request secrets on failures."""

    safe_request_error: Exception | None = None
    cancelled_request_error: asyncio.CancelledError | None = None
    client: Any = None
    response: Any = None
    payload: Any = None
    parsed: dict[str, CatalogModelT] | None = None
    raw_message = ""
    raw_state = ""
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            trust_env=_trust_env(),
            proxy=proxy or None,
            follow_redirects=False,
        ) as client:
            # Recompute immediately before the physical request so a live
            # privacy change or proxy setting suppresses the optional header.
            headers.update(
                tokenrhythm_install_id_headers(
                    "tokenrhythm",
                    url,
                    proxy=proxy or None,
                )
            )
            response = await client.get(url, headers=dict(headers))
            response.raise_for_status()
            payload = response.json(parse_float=Decimal)
            parsed = parser(
                _validated_catalog_envelope(
                    payload,
                    source=source,
                    known_secret=api_key,
                ),
                api_key,
            )
    except asyncio.CancelledError:
        cancelled_request_error = asyncio.CancelledError()
    except httpx.HTTPError as exc:
        safe_request_error = redacted_httpx_error(exc, api_key=api_key)
    except json.JSONDecodeError as exc:
        safe_document = _redact_catalog_fetch_text(exc.doc, api_key=api_key)
        if safe_document == exc.doc:
            exc.__cause__ = None
            exc.__context__ = None
            exc.__traceback__ = None
            safe_request_error = exc
        else:
            safe_request_error = RuntimeError(
                f"TokenRhythm {source} catalog returned invalid JSON"
            )
    except Exception as exc:
        raw_message = str(exc)
        safe_message = _redact_catalog_fetch_text(raw_message, api_key=api_key)
        raw_state = repr(getattr(exc, "__dict__", {}))
        safe_state = _redact_catalog_fetch_text(raw_state, api_key=api_key)
        if safe_message != raw_message or safe_state != raw_state:
            safe_request_error = RuntimeError(
                safe_message
                if safe_message != raw_message
                else f"TokenRhythm {source} catalog parsing failed"
            )
        else:
            exc.__cause__ = None
            exc.__context__ = None
            exc.__traceback__ = None
            safe_request_error = exc

    headers.clear()
    client = None
    response = None
    payload = None
    raw_message = ""
    raw_state = ""
    api_key = ""
    proxy = ""
    url = ""
    if cancelled_request_error is not None:
        parsed = None
        raise cancelled_request_error
    if safe_request_error is not None:
        parsed = None
        raise safe_request_error
    return parsed or {}


async def fetch_tokenrhythm_published(
    url: str = TOKENRHYTHM_PUBLIC_CATALOG_URL,
    *,
    proxy: str = "",
    timeout: float = 5.0,
) -> dict[str, TokenRhythmPublishedModel]:
    """Fetch and normalize the keyless public website catalog."""

    headers = provider_app_headers(url)
    try:
        return await _fetch_tokenrhythm_catalog(
            url,
            headers=headers,
            api_key="",
            proxy=proxy,
            timeout=timeout,
            source="published",
            parser=parse_tokenrhythm_published,
        )
    finally:
        headers.clear()
        proxy = ""
        url = ""


async def fetch_tokenrhythm_declared(
    base_url: str = TOKENRHYTHM_API_BASE_URL,
    *,
    api_key: str,
    proxy: str = "",
    timeout: float = 10.0,
) -> dict[str, TokenRhythmDeclaredModel]:
    """Fetch the current key's declaration without exposing raw rows.

    Credentials are sent only to TokenRhythm's official HTTPS host. Custom
    OpenAI-compatible endpoints continue to use their generic provider path.
    """
    canonical_base = ""
    clean_key = ""
    url = ""
    headers: dict[str, str] = {}
    try:
        canonical_base = canonical_tokenrhythm_base_url(base_url)
        if not canonical_base or not is_official_tokenrhythm_endpoint(canonical_base):
            raise ValueError(
                "TokenRhythm catalog credentials require the official HTTPS host"
            )
        clean_key = clean_header_secret(api_key, label="TokenRhythm API key")
        url = f"{canonical_base}/models"
        headers = {"Authorization": f"Bearer {clean_key}"}
        headers.update(provider_app_headers(canonical_base))
        return await _fetch_tokenrhythm_catalog(
            url,
            headers=headers,
            api_key=clean_key,
            proxy=proxy,
            timeout=timeout,
            source="declared",
            parser=parse_tokenrhythm_declared,
        )
    finally:
        headers.clear()
        clean_key = ""
        api_key = ""
        proxy = ""
        url = ""
        canonical_base = ""
        base_url = ""


def merge_tokenrhythm_catalog(
    published: Mapping[str, TokenRhythmPublishedModel],
    declared: Mapping[str, TokenRhythmDeclaredModel],
) -> dict[str, TokenRhythmCatalogModel]:
    """Merge metadata for authenticated ids only; public rows grant no access."""
    published_by_id = {model_id.lower(): value for model_id, value in published.items()}
    result: dict[str, TokenRhythmCatalogModel] = {}
    for model_id, declared_model in declared.items():
        published_model = published_by_id.get(model_id.lower())
        declared_status = (declared_model.status or "").lower()
        if (
            declared_model.model_type is not None
            and declared_model.model_type.lower() != "chat"
        ) or declared_status not in ("", "online", "testing"):
            continue
        published_status = (
            (published_model.status or "").lower()
            if published_model is not None
            else ""
        )
        if published_model is not None and (
            (
                published_model.model_type is not None
                and published_model.model_type.lower() != "chat"
            )
            or published_status not in ("", "online", "testing")
        ):
            continue
        result[model_id] = TokenRhythmCatalogModel(
            model_id=model_id,
            display_name=(
                published_model.name
                if published_model is not None and published_model.name
                else declared_model.display_name or model_id
            ),
            metadata=TokenRhythmModelMetadata(
                published=published_model,
                declared=declared_model,
            ),
        )
    return result


def _price_per_mtok(pricing: TokenRhythmPricing, raw: str | None) -> float | None:
    if (
        raw is None
        or (pricing.currency or "").upper() != "CNY"
        or pricing.billing_unit is None
    ):
        return None
    try:
        converted = (
            Decimal(raw)
            * (_TOKENS_PER_MTOK / Decimal(pricing.billing_unit))
            / TOKENRHYTHM_CNY_PER_USD
        )
        value = float(converted)
    except (InvalidOperation, ValueError, ZeroDivisionError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def tokenrhythm_published_catalog_entries(
    published: Mapping[str, TokenRhythmPublishedModel],
) -> TokenRhythmCatalogEntries:
    entries: dict[str, dict[str, Any]] = {}
    for model_id, model in published.items():
        status = (model.status or "").lower()
        if status not in ("", "online", "testing"):
            continue
        if model.model_type is not None and model.model_type.lower() != "chat":
            continue
        fields: dict[str, Any] = {}
        if model.name:
            fields["display_name"] = model.name
        if model.context_window is not None and model.context_window > 0:
            fields["context_window"] = model.context_window
        if model.max_output_tokens is not None and model.max_output_tokens > 0:
            # Published value stays exact. Runtime safety is applied by the
            # provider-scoped resolver, never by normalization.
            fields["max_output_tokens"] = model.max_output_tokens
        if model.capabilities.tools is not None:
            fields["supports_tools"] = model.capabilities.tools
        if model.capabilities.vision is not None:
            fields["supports_vision"] = model.capabilities.vision
        pricing = model.pricing
        if pricing is not None:
            for bucket_name, field_name in (
                ("input", "input_cost_per_mtok"),
                ("output", "output_cost_per_mtok"),
                ("cache_read", "cache_read_cost_per_mtok"),
            ):
                effective_value = getattr(pricing.effective, bucket_name)
                discount_value = getattr(pricing.discount, bucket_name)
                standard_value = getattr(pricing.standard, bucket_name)
                raw = effective_value
                if raw is None and pricing.has_discount is True:
                    raw = discount_value
                if raw is None:
                    raw = standard_value
                cost = _price_per_mtok(pricing, raw)
                if cost is not None:
                    fields[field_name] = cost
        if fields:
            entries[model_id] = fields
    return TokenRhythmCatalogEntries(entries, published=published)


def tokenrhythm_published_to_wire(
    models: Mapping[str, TokenRhythmPublishedModel],
) -> dict[str, dict[str, Any]]:
    return {model_id: model.to_wire() for model_id, model in models.items()}


def tokenrhythm_published_from_wire(
    payload: Mapping[str, Any],
) -> dict[str, TokenRhythmPublishedModel]:
    return {
        str(model_id): TokenRhythmPublishedModel.from_wire(value)
        for model_id, value in payload.items()
        if isinstance(value, Mapping)
    }


def tokenrhythm_declared_to_wire(
    models: Mapping[str, TokenRhythmDeclaredModel],
) -> dict[str, dict[str, Any]]:
    return {model_id: model.to_wire() for model_id, model in models.items()}


def tokenrhythm_declared_from_wire(
    payload: Mapping[str, Any],
) -> dict[str, TokenRhythmDeclaredModel]:
    return {
        str(model_id): TokenRhythmDeclaredModel.from_wire(value)
        for model_id, value in payload.items()
        if isinstance(value, Mapping)
    }


__all__ = [
    "TOKENRHYTHM_API_BASE_URL",
    "TOKENRHYTHM_PUBLIC_CATALOG_URL",
    "TokenRhythmCapabilities",
    "TokenRhythmCatalogEntries",
    "TokenRhythmCatalogModel",
    "TokenRhythmDeclaredModel",
    "TokenRhythmModelMetadata",
    "TokenRhythmPriceBuckets",
    "TokenRhythmPricing",
    "TokenRhythmPublishedModel",
    "TokenRhythmResponseCapabilityStates",
    "TokenRhythmResponses",
    "canonical_tokenrhythm_base_url",
    "fetch_tokenrhythm_declared",
    "fetch_tokenrhythm_published",
    "is_official_tokenrhythm_endpoint",
    "merge_tokenrhythm_catalog",
    "parse_tokenrhythm_declared",
    "parse_tokenrhythm_published",
    "tokenrhythm_declared_from_wire",
    "tokenrhythm_declared_to_wire",
    "tokenrhythm_authority_identity",
    "tokenrhythm_public_transport_fingerprint",
    "tokenrhythm_published_catalog_entries",
    "tokenrhythm_published_from_wire",
    "tokenrhythm_published_to_wire",
    "tokenrhythm_transport_fingerprint",
]
