"""Live credential/model probe + model discovery for LLM providers.

The cheapest class of misconfiguration — a bad API key, a typo'd model id, a
wrong base URL — used to surface only as an HTTP error in the middle of the
first chat. The probe runs a one-token chat turn against the candidate
configuration *before* it is saved, classifies any failure through the
standard provider taxonomy, and reports an actionable result.

Raw model discovery (:func:`discover_provider_models`) builds the same kind
of throwaway, never-persisted provider from candidate credentials and asks it
for its live model list, enriching each row from the layered model catalog.
Selector surfaces use :func:`discover_selectable_provider_models` instead;
that fail-closed wrapper admits only provider/host pairs whose listing has
been verified as an accurate source of user-selectable model ids.
"""

from __future__ import annotations

import inspect
import os
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, cast

import httpx
import structlog

from opensquilla.provider.app_attribution import is_provider_app_host
from opensquilla.provider.failures import ProviderFailureKind, classify_provider_error
from opensquilla.provider.protocol import LLMProvider
from opensquilla.provider.registry import get_provider_spec
from opensquilla.provider.selector import (
    ProviderBuildError,
    _exception_status_code,
    build_provider,
)
from opensquilla.provider.types import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    ModelInfo,
    ReasoningDeltaEvent,
    StreamEvent,
    TextDeltaEvent,
)
from opensquilla.redaction import redact_error_text

log = structlog.get_logger(__name__)

_PROBE_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class ProviderProbeResult:
    """Outcome of one live provider probe (never persisted)."""

    ok: bool
    provider_id: str
    model: str
    failure_kind: str = ""
    message: str = ""
    code: str = ""
    # Legacy end-to-end probe duration; 0 when the probe never reached the
    # network (missing key, build failure).
    latency_ms: int = 0
    # Time to the first non-empty model response. ``None`` means no text or
    # reasoning delta arrived before the probe completed or failed.
    first_response_ms: int | None = None

    @property
    def total_ms(self) -> int:
        """Explicit name for the legacy end-to-end ``latency_ms`` value."""
        return self.latency_ms

    def to_payload(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "providerId": self.provider_id,
            "model": self.model,
            "failureKind": self.failure_kind,
            "message": self.message,
            "code": self.code,
            "latencyMs": self.latency_ms,
            "firstResponseMs": self.first_response_ms,
            "totalMs": self.total_ms,
        }


def _resolve_probe_api_key(api_key: str, api_key_env: str, spec_env_key: str) -> tuple[str, str]:
    """Return (key, source-description) using the config precedence."""
    if api_key.strip():
        return api_key.strip(), "explicit"
    env_name = api_key_env.strip() or spec_env_key.strip()
    if env_name and env_name != "OAuth":
        return os.environ.get(env_name, "").strip(), f"${env_name}"
    return "", ""


async def probe_llm_provider(
    *,
    provider_id: str,
    model: str,
    api_key: str = "",
    api_key_env: str = "",
    base_url: str = "",
    proxy: str = "",
    allow_default_api_key_env: bool = True,
    timeout: float = _PROBE_TIMEOUT_SECONDS,
    chat_stream_factory: Callable[
        [LLMProvider, list[Message], ChatConfig], AsyncIterator[StreamEvent]
    ]
    | None = None,
) -> ProviderProbeResult:
    """Run a one-token live chat against the candidate provider config.

    Raises ``ValueError`` for validation-level problems (unknown provider id,
    missing model) so callers surface those as typed input errors; runtime
    reachability/credential failures come back as a not-ok result.
    ``allow_default_api_key_env=False`` lets RPC callers suppress the registry
    env fallback when testing a different endpoint origin.
    """
    provider_id = (provider_id or "").strip()
    model = (model or "").strip()
    spec = get_provider_spec(provider_id)  # raises UnknownProviderError(ValueError)
    if not model:
        raise ValueError("Model is required for a provider probe.")
    if not spec.runtime_supported:
        raise ValueError(f"Provider '{provider_id}' has no runtime support to probe.")

    default_env_key = spec.env_key if allow_default_api_key_env else ""
    resolved_key, key_source = _resolve_probe_api_key(
        api_key,
        api_key_env,
        default_env_key,
    )
    if spec.requires_api_key() and not resolved_key:
        checked = key_source or (default_env_key and f"${default_env_key}") or "no env key"
        return ProviderProbeResult(
            ok=False,
            provider_id=provider_id,
            model=model,
            failure_kind=ProviderFailureKind.AUTH_INVALID.value,
            message=f"No API key available (checked {checked}).",
        )

    try:
        provider = build_provider(
            provider_id,
            model,
            api_key=resolved_key,
            base_url=base_url.strip(),
            proxy=proxy.strip(),
        )
    except ProviderBuildError as exc:
        return ProviderProbeResult(
            ok=False,
            provider_id=provider_id,
            model=model,
            failure_kind=ProviderFailureKind.BAD_REQUEST.value,
            message=redact_error_text(str(exc), known_secrets=(resolved_key,)),
        )

    cfg = ChatConfig(max_tokens=1, timeout=timeout, thinking=False)
    messages = [Message(role="user", content="ping")]
    start = time.monotonic()
    first_response_ms: int | None = None
    try:
        stream = (
            chat_stream_factory(provider, messages, cfg)
            if chat_stream_factory is not None
            else provider.chat(messages, config=cfg)
        )
        try:
            async for event in stream:
                if (
                    first_response_ms is None
                    and isinstance(event, (TextDeltaEvent, ReasoningDeltaEvent))
                    and event.text
                ):
                    first_response_ms = int((time.monotonic() - start) * 1000)
                if isinstance(event, ErrorEvent):
                    status_code = int(event.code) if str(event.code).isdigit() else None
                    kind = classify_provider_error(
                        provider_id,
                        status_code,
                        raw_code=event.code,
                        message=event.message,
                    )
                    return ProviderProbeResult(
                        ok=False,
                        provider_id=provider_id,
                        model=model,
                        failure_kind=kind.value,
                        # Provider error bodies can echo credentials (bad keys,
                        # signed URLs) — never repeat them verbatim.
                        message=redact_error_text(
                            event.message,
                            known_secrets=(resolved_key,),
                        ),
                        code=redact_error_text(
                            str(event.code),
                            known_secrets=(resolved_key,),
                        ),
                        latency_ms=int((time.monotonic() - start) * 1000),
                        first_response_ms=first_response_ms,
                    )
                if isinstance(event, DoneEvent):
                    return ProviderProbeResult(
                        ok=True,
                        provider_id=provider_id,
                        model=model,
                        latency_ms=int((time.monotonic() - start) * 1000),
                        first_response_ms=first_response_ms,
                    )
        finally:
            aclose = getattr(stream, "aclose", None)
            if callable(aclose):
                await aclose()
    except Exception as exc:  # noqa: BLE001 - a probe never raises transport noise
        log.warning(
            "onboarding.provider_probe_failed",
            provider=provider_id,
            error=redact_error_text(str(exc), known_secrets=(resolved_key,)),
        )
        return ProviderProbeResult(
            ok=False,
            provider_id=provider_id,
            model=model,
            failure_kind=ProviderFailureKind.TRANSPORT_TRANSIENT.value,
            message=redact_error_text(str(exc), known_secrets=(resolved_key,)),
            latency_ms=int((time.monotonic() - start) * 1000),
            first_response_ms=first_response_ms,
        )

    return ProviderProbeResult(
        ok=False,
        provider_id=provider_id,
        model=model,
        failure_kind=ProviderFailureKind.MALFORMED_RESPONSE.value,
        message="Provider stream ended without a completion event.",
        latency_ms=int((time.monotonic() - start) * 1000),
        first_response_ms=first_response_ms,
    )


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderModelsDiscoverResult:
    """Outcome of one live model-discovery call (never persisted).

    ``source`` distinguishes a provider that genuinely listed models
    (``"live"``) from one that lists nothing or does not support listing
    (``"none"``, still ``ok=True``) — a classified failure is ``ok=False``
    with ``failure_kind``/``detail`` set instead.
    """

    ok: bool
    provider_id: str
    failure_kind: str = ""
    detail: str = ""
    source: str = "none"  # "live" | "none"
    models: list[dict[str, object]] = field(default_factory=list)

    def to_payload(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "failureKind": self.failure_kind,
            "detail": self.detail,
            "source": self.source,
            "models": [dict(m) for m in self.models],
        }


def _discover_model_row(info: ModelInfo, provider_id: str) -> dict[str, object]:
    """Adapt one live ``ModelInfo`` row, filling gaps from the layered catalog.

    The provider's own listing wins per field where it genuinely knows a
    value (``> 0`` limits, positive prices); ``shared_catalog().resolve_entry``
    fills the rest. A per-model ``[models.*]`` context_window override beats
    even the live listing, so discovery rows match what budgeting will
    actually use. ``capabilitySource`` names the catalog layer that resolved
    the entry, so clients can tell curated metadata from synthesized floors.
    """
    from opensquilla.provider.model_catalog import shared_catalog

    catalog = shared_catalog()
    entry = catalog.resolve_entry(info.model_id, provider=provider_id)
    override_window = catalog.user_context_window_override(info.model_id, provider=provider_id)
    if override_window is not None:
        context_window = override_window
    elif info.context_window > 0:
        context_window = info.context_window
    else:
        context_window = entry.context_window
    max_output = (
        info.max_output_tokens if info.max_output_tokens > 0 else entry.max_output_tokens
    )
    capabilities: list[str] = ["chat"]
    if info.supports_tools or entry.supports_tools:
        capabilities.append("tools")
    if info.supports_reasoning or entry.supports_reasoning:
        capabilities.append("reasoning")
    if info.supports_vision or entry.supports_vision:
        capabilities.append("vision")

    pricing: dict[str, float] | None = None
    if info.input_cost_per_1k > 0 or info.output_cost_per_1k > 0:
        pricing = {
            "inputPer1k": info.input_cost_per_1k,
            "outputPer1k": info.output_cost_per_1k,
        }
    elif entry.input_cost_per_mtok is not None or entry.output_cost_per_mtok is not None:
        # Catalog costs are canonical per-Mtok; the wire stays per-1k for
        # parity with models.list pricing rows.
        pricing = {
            "inputPer1k": (entry.input_cost_per_mtok or 0.0) / 1000.0,
            "outputPer1k": (entry.output_cost_per_mtok or 0.0) / 1000.0,
        }

    return {
        "id": info.model_id,
        "name": info.display_name or info.model_id,
        "contextWindow": context_window,
        "maxOutputTokens": max_output,
        "capabilities": capabilities,
        "pricing": pricing,
        "capabilitySource": entry.source,
    }


async def _list_models_for_discovery(provider: LLMProvider) -> list[ModelInfo]:
    """List the provider's models, surfacing failures where the adapter can.

    Runtime adapters historically swallow list-models errors and return an
    empty list, which is indistinguishable from a genuinely empty catalog.
    Adapters that grew the keyword-only ``raise_on_error`` parameter re-raise
    auth/transport failures when asked, so discovery can classify them; older
    adapters without the parameter keep the legacy swallow-errors behavior.
    """
    list_models: Any = provider.list_models
    try:
        accepts_raise = "raise_on_error" in inspect.signature(list_models).parameters
    except (TypeError, ValueError):  # C-implemented or exotic callables
        accepts_raise = False
    if accepts_raise:
        return cast("list[ModelInfo]", await list_models(raise_on_error=True))
    return cast("list[ModelInfo]", await list_models())


async def discover_provider_models(
    *,
    provider_id: str,
    api_key: str = "",
    api_key_env: str = "",
    base_url: str = "",
    proxy: str = "",
    allow_default_api_key_env: bool = True,
) -> ProviderModelsDiscoverResult:
    """List a candidate provider's live models without persisting anything.

    Builds the same throwaway provider as :func:`probe_llm_provider` (no
    model id is needed to list models) and classifies failures through the
    exact machinery ``ModelSelector.list_models_detailed`` uses, so a wrong
    key and an empty catalog stay distinguishable.

    Raises ``ValueError`` for validation-level problems (unknown provider id,
    no runtime support) so callers surface those as typed input errors.
    ``allow_default_api_key_env=False`` suppresses the registry env fallback
    for a candidate endpoint that must not inherit the active endpoint's key.
    """
    provider_id = (provider_id or "").strip()
    spec = get_provider_spec(provider_id)  # raises UnknownProviderError(ValueError)
    if not spec.runtime_supported:
        raise ValueError(f"Provider '{provider_id}' has no runtime support to discover.")

    default_env_key = spec.env_key if allow_default_api_key_env else ""
    resolved_key, key_source = _resolve_probe_api_key(
        api_key,
        api_key_env,
        default_env_key,
    )
    if spec.requires_api_key() and not resolved_key:
        checked = key_source or (default_env_key and f"${default_env_key}") or "no env key"
        return ProviderModelsDiscoverResult(
            ok=False,
            provider_id=provider_id,
            failure_kind=ProviderFailureKind.AUTH_INVALID.value,
            detail=f"No API key available (checked {checked}).",
        )

    try:
        provider = build_provider(
            provider_id,
            "",  # listing models needs no bound model id
            api_key=resolved_key,
            base_url=base_url.strip(),
            proxy=proxy.strip(),
        )
    except ProviderBuildError as exc:
        return ProviderModelsDiscoverResult(
            ok=False,
            provider_id=provider_id,
            failure_kind=ProviderFailureKind.BAD_REQUEST.value,
            detail=redact_error_text(str(exc), known_secrets=(resolved_key,)),
        )

    try:
        provider_models = await _list_models_for_discovery(provider)
    except Exception as exc:  # noqa: BLE001 - same classification as list_models_detailed
        kind = classify_provider_error(
            provider_id,
            _exception_status_code(exc),
            message=str(exc),
        )
        if kind is ProviderFailureKind.UNKNOWN and isinstance(exc, httpx.TransportError):
            # Raw socket noise ("connection refused", DNS failures) carries no
            # status code and often no classifiable message; it is transport
            # trouble by construction, exactly like the chat probe's guard.
            kind = ProviderFailureKind.TRANSPORT_TRANSIENT
        log.warning(
            "onboarding.models_discover_failed",
            provider=provider_id,
            kind=kind.value,
            error=redact_error_text(str(exc), known_secrets=(resolved_key,)),
        )
        return ProviderModelsDiscoverResult(
            ok=False,
            provider_id=provider_id,
            failure_kind=kind.value,
            # Provider error bodies can echo credentials (bad keys, signed
            # URLs) — never repeat them verbatim.
            detail=redact_error_text(str(exc), known_secrets=(resolved_key,)),
        )

    if not provider_models:
        # Distinct from a classified failure: the provider answered but lists
        # nothing (or does not support listing) — ok, just no live source.
        return ProviderModelsDiscoverResult(ok=True, provider_id=provider_id, source="none")
    return ProviderModelsDiscoverResult(
        ok=True,
        provider_id=provider_id,
        source="live",
        models=[_discover_model_row(m, provider_id) for m in provider_models],
    )


async def discover_selectable_provider_models(
    *,
    provider_id: str,
    api_key: str = "",
    api_key_env: str = "",
    base_url: str = "",
    proxy: str = "",
    allow_default_api_key_env: bool = True,
) -> ProviderModelsDiscoverResult:
    """Return only verified live catalogs suitable for a model picker.

    This is the selector-facing policy boundary. Unknown and unsupported
    provider ids remain validation errors, matching raw discovery. All other
    providers default to an empty, successful catalog *before* credential
    resolution or provider construction, preserving the manual model-id
    escape hatch without presenting guessed data as authoritative.

    A trusted provider id is not enough on its own: an operator-supplied
    OpenAI-compatible re-host can serve a completely different model set.
    Live selection is therefore allowed only when the effective base URL uses
    HTTPS and the provider's allowlisted official host (or one of its
    subdomains).
    """
    provider_id = (provider_id or "").strip()
    spec = get_provider_spec(provider_id)  # raises UnknownProviderError(ValueError)
    if not spec.runtime_supported:
        raise ValueError(f"Provider '{provider_id}' has no runtime support to discover.")

    if spec.selectable_model_catalog != "verified_live":
        return ProviderModelsDiscoverResult(ok=True, provider_id=provider_id)

    effective_base_url = base_url.strip() or spec.default_base_url
    try:
        uses_https = httpx.URL(effective_base_url).scheme == "https"
    except httpx.InvalidURL:
        uses_https = False
    if (
        not uses_https
        or not spec.compat.official_host
        or not is_provider_app_host(effective_base_url, spec.compat.official_host)
    ):
        return ProviderModelsDiscoverResult(ok=True, provider_id=provider_id)

    discover_kwargs: dict[str, Any] = {
        "provider_id": provider_id,
        "api_key": api_key,
        "api_key_env": api_key_env,
        "base_url": base_url.strip(),
        "proxy": proxy,
    }
    if not allow_default_api_key_env:
        discover_kwargs["allow_default_api_key_env"] = False
    return await discover_provider_models(**discover_kwargs)
