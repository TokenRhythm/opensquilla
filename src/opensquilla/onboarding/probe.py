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

import asyncio
import copy
import hmac
import inspect
import os
import secrets
import time
import weakref
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import httpx
import structlog

from opensquilla.provider.app_attribution import is_host_or_subdomain
from opensquilla.provider.auxiliary_budget import (
    ensure_auxiliary_text_fits,
    resolve_auxiliary_request_budget,
)
from opensquilla.provider.failures import ProviderFailureKind, classify_provider_error
from opensquilla.provider.protocol import LLMProvider, ProviderModelListingResponseError
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
REACHABILITY_PROBE_TIMEOUT_SECONDS = 8.0
MODEL_PROBE_TIMEOUT_SECONDS = 60.0
_PROBE_STREAM_CLOSE_TIMEOUT_SECONDS = 1.0
_MODEL_LISTING_ERROR_BODY_INSPECTION_BYTES = 4096
PROBE_TIMEOUT_FAILURE_KIND = "probe_timeout"

_PROBE_CLEANUP_TASKS: set[asyncio.Task[Any]] = set()

ProviderProbeMode = Literal["reachability", "model"]
ProviderVerificationLevel = Literal["reachable", "model_verified", "none"]
ProviderProbeFailureStage = Literal["reachability", "model"]


def active_provider_probe_cleanup_tasks() -> int:
    """Return supervised probe tasks still releasing provider resources."""

    return sum(not task.done() for task in _PROBE_CLEANUP_TASKS)


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
    # Additive verification metadata. Older callers that construct successful
    # model-probe results without this field retain the legacy meaning.
    verification_level: ProviderVerificationLevel | None = None
    failure_stage: ProviderProbeFailureStage = "model"

    def __post_init__(self) -> None:
        if self.verification_level is None:
            verification_level: ProviderVerificationLevel = (
                "model_verified" if self.ok else "none"
            )
            object.__setattr__(self, "verification_level", verification_level)

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
            "verificationLevel": self.verification_level,
            "failureStage": self.failure_stage,
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
    mode: ProviderProbeMode = "model",
    timeout: float = _PROBE_TIMEOUT_SECONDS,
    reachability_timeout: float = REACHABILITY_PROBE_TIMEOUT_SECONDS,
    chat_stream_factory: Callable[
        [LLMProvider, list[Message], ChatConfig], AsyncIterator[StreamEvent]
    ]
    | None = None,
) -> ProviderProbeResult:
    """Check reachability or run a one-token live model probe.

    Raises ``ValueError`` for validation-level problems (unknown provider id,
    missing model for a model probe, or an unknown mode) so callers surface
    those as typed input errors; runtime reachability/credential failures come
    back as a not-ok result. Reachability uses a strict live model listing and
    falls back to the model probe when the adapter cannot prove the listing is
    live or the endpoint explicitly does not support it. A strict 2xx listing,
    including an empty catalog, proves that the endpoint responded.
    ``allow_default_api_key_env=False`` lets RPC callers suppress the registry
    env fallback when testing a different endpoint origin.
    """
    provider_id = (provider_id or "").strip()
    model = (model or "").strip()
    normalized_mode = str(mode or "model").strip()
    if normalized_mode not in {"reachability", "model"}:
        raise ValueError("Probe mode must be 'reachability' or 'model'.")
    mode = cast("ProviderProbeMode", normalized_mode)
    spec = get_provider_spec(provider_id)  # raises UnknownProviderError(ValueError)
    if mode == "model" and not model:
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
            failure_stage=mode,
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
            failure_stage=mode,
        )

    probe_start = time.monotonic()
    fallback_reachable = False
    if mode == "reachability":
        reachability_result, fallback_reachable = await _probe_provider_reachability(
            provider=provider,
            provider_id=provider_id,
            model=model,
            resolved_key=resolved_key,
            start=probe_start,
            timeout=reachability_timeout,
        )
        if reachability_result is not None:
            return reachability_result
        if not model:
            return ProviderProbeResult(
                ok=False,
                provider_id=provider_id,
                model=model,
                failure_kind=ProviderFailureKind.UNSUPPORTED_FEATURE.value,
                message=(
                    "Provider model listing could not verify reachability; "
                    "a model is required for the fallback generation test."
                ),
                latency_ms=int((time.monotonic() - probe_start) * 1000),
                verification_level="reachable" if fallback_reachable else "none",
                failure_stage="reachability",
            )

    request_budget = resolve_auxiliary_request_budget(
        provider,
        provider_id=provider_id,
        model=model,
        max_output_tokens=1,
    )
    cfg = ChatConfig(
        max_tokens=1,
        timeout=timeout,
        thinking=False,
        provider_request_max_chars=request_budget.provider_request_max_chars,
        provider_context_window_tokens=request_budget.context_window_tokens,
        provider_request_max_chars_explicit_cap=(
            request_budget.provider_request_max_chars_explicit_cap
        ),
    )
    messages = [Message(role="user", content="ping")]
    ensure_auxiliary_text_fits(
        messages,
        max_chars=request_budget.provider_request_max_chars,
        max_tokens=request_budget.max_input_tokens,
    )
    first_response_ms: int | None = None

    async def consume_stream() -> ProviderProbeResult:
        nonlocal first_response_ms
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
                    first_response_ms = int((time.monotonic() - probe_start) * 1000)
                if isinstance(event, ErrorEvent):
                    if _is_probe_timeout_signal(event.code, event.message):
                        failure_kind = PROBE_TIMEOUT_FAILURE_KIND
                    else:
                        status_code = int(event.code) if str(event.code).isdigit() else None
                        failure_kind = classify_provider_error(
                            provider_id,
                            status_code,
                            raw_code=event.code,
                            message=event.message,
                        ).value
                    return ProviderProbeResult(
                        ok=False,
                        provider_id=provider_id,
                        model=model,
                        failure_kind=failure_kind,
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
                        latency_ms=int((time.monotonic() - probe_start) * 1000),
                        first_response_ms=first_response_ms,
                        verification_level=(
                            "reachable" if fallback_reachable else "none"
                        ),
                        failure_stage="model",
                    )
                if isinstance(event, DoneEvent):
                    return ProviderProbeResult(
                        ok=True,
                        provider_id=provider_id,
                        model=model,
                        latency_ms=int((time.monotonic() - probe_start) * 1000),
                        first_response_ms=first_response_ms,
                        verification_level="model_verified",
                        failure_stage="model",
                    )
        finally:
            await _close_probe_stream(stream)

        return ProviderProbeResult(
            ok=False,
            provider_id=provider_id,
            model=model,
            failure_kind=ProviderFailureKind.MALFORMED_RESPONSE.value,
            message="Provider stream ended without a completion event.",
            latency_ms=int((time.monotonic() - probe_start) * 1000),
            first_response_ms=first_response_ms,
            verification_level="reachable" if fallback_reachable else "none",
            failure_stage="model",
        )

    consume_task = asyncio.create_task(consume_stream(), name="provider-model-probe")
    try:
        done, _ = await asyncio.wait({consume_task}, timeout=timeout)
        if consume_task not in done:
            await _cancel_and_supervise_probe_task(consume_task)
            raise TimeoutError
        return consume_task.result()
    except asyncio.CancelledError:
        await _cancel_and_supervise_probe_task(consume_task)
        raise
    except (TimeoutError, httpx.TimeoutException) as exc:
        detail = str(exc).strip()
        return ProviderProbeResult(
            ok=False,
            provider_id=provider_id,
            model=model,
            failure_kind=PROBE_TIMEOUT_FAILURE_KIND,
            message=(
                redact_error_text(detail, known_secrets=(resolved_key,))
                if detail
                else f"Provider model probe timed out after {timeout:g} seconds."
            ),
            code=PROBE_TIMEOUT_FAILURE_KIND,
            latency_ms=int((time.monotonic() - probe_start) * 1000),
            first_response_ms=first_response_ms,
            verification_level="reachable" if fallback_reachable else "none",
            failure_stage="model",
        )
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
            latency_ms=int((time.monotonic() - probe_start) * 1000),
            first_response_ms=first_response_ms,
            verification_level="reachable" if fallback_reachable else "none",
            failure_stage="model",
        )


def _consume_probe_cleanup_result(task: asyncio.Task[Any]) -> None:
    _PROBE_CLEANUP_TASKS.discard(task)
    if task.cancelled():
        return
    try:
        task.result()
    except Exception as exc:  # noqa: BLE001 - cleanup cannot change the probe result
        log.warning(
            "onboarding.provider_probe_cleanup_failed",
            exception_type=type(exc).__name__,
        )


def _supervise_probe_cleanup_task(task: asyncio.Task[Any]) -> None:
    _PROBE_CLEANUP_TASKS.add(task)
    task.add_done_callback(_consume_probe_cleanup_result)


async def _cancel_and_supervise_probe_task(task: asyncio.Task[Any]) -> None:
    if task.done():
        return
    task.cancel()
    _supervise_probe_cleanup_task(task)
    # Let an active stream enter its finally block and invoke aclose(). The
    # cleanup itself remains supervised so a slow close cannot extend the
    # user-visible probe deadline.
    await asyncio.sleep(0)


async def _close_probe_stream(stream: AsyncIterator[StreamEvent]) -> None:
    """Attempt stream cleanup without allowing it to extend the probe deadline."""

    aclose = getattr(stream, "aclose", None)
    if not callable(aclose):
        return
    close_result = aclose()
    if not inspect.isawaitable(close_result):
        return
    close_task = asyncio.ensure_future(close_result)
    try:
        done, _ = await asyncio.wait(
            {close_task},
            timeout=_PROBE_STREAM_CLOSE_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        if not close_task.done():
            close_task.cancel()
        _supervise_probe_cleanup_task(close_task)
        raise
    if close_task not in done:
        close_task.cancel()
        _supervise_probe_cleanup_task(close_task)
        log.warning("onboarding.provider_probe_stream_close_timed_out")
        return
    try:
        close_task.result()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - cleanup cannot change the probe result
        log.warning(
            "onboarding.provider_probe_stream_close_failed",
            exception_type=type(exc).__name__,
        )


def _is_probe_timeout_signal(code: object, message: object) -> bool:
    """Return whether an adapter-reported stream failure is a timeout."""
    normalized_code = str(code or "").strip().lower()
    if normalized_code in {
        "timeout",
        "request_timeout",
        "read_timeout",
        "write_timeout",
        "pool_timeout",
    }:
        return True
    if normalized_code not in {"", "request_error"}:
        return False
    normalized_message = str(message or "").lower()
    return "timed out" in normalized_message or "timeout" in normalized_message


def _is_unsupported_model_listing(
    exc: Exception,
    *,
    status_code: int | None,
    kind: ProviderFailureKind,
) -> bool:
    if isinstance(exc, NotImplementedError) or status_code in {404, 405, 501}:
        return True
    if kind is ProviderFailureKind.UNSUPPORTED_FEATURE:
        return True
    details = [str(exc).strip().lower()]
    response = getattr(exc, "response", None)
    if isinstance(response, httpx.Response):
        try:
            body = response.content[:_MODEL_LISTING_ERROR_BODY_INSPECTION_BYTES]
        except (httpx.ResponseNotRead, httpx.StreamConsumed):
            body = b""
        if body:
            encoding = response.encoding or "utf-8"
            try:
                details.append(body.decode(encoding, errors="replace").lower())
            except LookupError:
                details.append(body.decode("utf-8", errors="replace").lower())
    return any(
        marker in detail
        for detail in details
        for marker in (
            "list_models is not implemented",
            "model listing is not implemented",
            "model listing is unsupported",
            "models endpoint is not supported",
            "does not support model listing",
        )
    )


async def _probe_provider_reachability(
    *,
    provider: LLMProvider,
    provider_id: str,
    model: str,
    resolved_key: str,
    start: float,
    timeout: float,
) -> tuple[ProviderProbeResult | None, bool]:
    """Return a result and whether an HTTP response proved fallback reachability."""
    list_models: Any = provider.list_models
    try:
        supports_strict_listing = (
            "raise_on_error" in inspect.signature(list_models).parameters
        )
    except (TypeError, ValueError):
        supports_strict_listing = False
    if not supports_strict_listing:
        return None, False

    listing_task = asyncio.create_task(
        list_models(raise_on_error=True),
        name="provider-reachability-probe",
    )
    try:
        done, _ = await asyncio.wait({listing_task}, timeout=timeout)
        if listing_task not in done:
            await _cancel_and_supervise_probe_task(listing_task)
            raise TimeoutError
        listing_task.result()
    except asyncio.CancelledError:
        await _cancel_and_supervise_probe_task(listing_task)
        raise
    except (TimeoutError, httpx.TimeoutException) as exc:
        detail = str(exc).strip()
        return ProviderProbeResult(
            ok=False,
            provider_id=provider_id,
            model=model,
            failure_kind=PROBE_TIMEOUT_FAILURE_KIND,
            message=(
                redact_error_text(detail, known_secrets=(resolved_key,))
                if detail
                else f"Provider reachability check timed out after {timeout:g} seconds."
            ),
            code=PROBE_TIMEOUT_FAILURE_KIND,
            latency_ms=int((time.monotonic() - start) * 1000),
            verification_level="none",
            failure_stage="reachability",
        ), False
    except Exception as exc:  # noqa: BLE001 - probe failures are typed results
        status_code = _exception_status_code(exc)
        if isinstance(exc, ProviderModelListingResponseError):
            kind = ProviderFailureKind.MALFORMED_RESPONSE
        else:
            kind = classify_provider_error(
                provider_id,
                status_code,
                message=str(exc),
            )
        if _is_unsupported_model_listing(
            exc,
            status_code=status_code,
            kind=kind,
        ):
            return None, status_code is not None
        if status_code is not None and 500 <= status_code <= 599:
            kind = ProviderFailureKind.PROVIDER_OVERLOADED
        if kind is ProviderFailureKind.UNKNOWN and isinstance(exc, httpx.TransportError):
            kind = ProviderFailureKind.TRANSPORT_TRANSIENT
        return ProviderProbeResult(
            ok=False,
            provider_id=provider_id,
            model=model,
            failure_kind=kind.value,
            message=redact_error_text(str(exc), known_secrets=(resolved_key,)),
            code=str(status_code or ""),
            latency_ms=int((time.monotonic() - start) * 1000),
            verification_level="reachable" if status_code is not None else "none",
            failure_stage="reachability",
        ), False

    return ProviderProbeResult(
        ok=True,
        provider_id=provider_id,
        model=model,
        latency_ms=int((time.monotonic() - start) * 1000),
        verification_level="reachable",
        failure_stage="reachability",
    ), False


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
    # Additive live-catalog health metadata. ``None`` identifies providers
    # that do not participate in the shared catalog cache.
    catalog: dict[str, object] | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "failureKind": self.failure_kind,
            "detail": self.detail,
            "source": self.source,
            "models": [dict(m) for m in self.models],
            "catalog": dict(self.catalog) if self.catalog is not None else None,
        }


@dataclass
class _SavedDiscovery:
    result: ProviderModelsDiscoverResult
    checked_at: float
    last_good: ProviderModelsDiscoverResult | None = None
    last_good_at: float = 0


# Only saved connections enter this cache. Draft discovery remains isolated,
# and TokenRhythm keeps its existing entitlement-aware persistent coordinator.
# Process-keyed fingerprints cannot be used to cheaply guess credentials from
# cache keys; this cache has no cross-process persistence requirement.
_SAVED_DISCOVERY_KEY = secrets.token_bytes(32)
_saved_discoveries: dict[str, _SavedDiscovery] = {}
_saved_discovery_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)
_DISCOVERY_SUCCESS_TTL = 300.0
_DISCOVERY_FAILURE_TTL = 15.0
_DISCOVERY_LAST_GOOD_TTL = 3600.0
TRANSIENT_MODEL_DISCOVERY_FAILURES = frozenset({
    "transport_transient", "provider_overloaded", "rate_limited", "probe_timeout",
})


async def _discover_saved_or_draft_models(
    *, saved: bool, force: bool, **kwargs: Any,
) -> ProviderModelsDiscoverResult:
    if not saved:
        return await discover_provider_models(**kwargs)
    spec = get_provider_spec(kwargs["provider_id"])
    # Resolve environment values before fingerprinting: rotating an env-backed
    # credential must not reuse the previous account's catalog.
    key, _ = _resolve_probe_api_key(
        kwargs.get("api_key", ""), kwargs.get("api_key_env", ""),
        spec.env_key if kwargs.get("allow_default_api_key_env", True) else "",
    )
    kwargs = {**kwargs, "api_key": key, "api_key_env": "", "allow_default_api_key_env": False}
    fingerprint = hmac.new(
        _SAVED_DISCOVERY_KEY,
        repr((spec.provider_id, kwargs.get("base_url") or spec.default_base_url,
              key, kwargs.get("proxy", ""))).encode(),
        "sha256",
    ).hexdigest()
    lock = _saved_discovery_locks.get(fingerprint)
    if lock is None:
        lock = asyncio.Lock()
        _saved_discovery_locks[fingerprint] = lock
    # Force refresh is serialized after any older request for this identity.
    # A slow menu request cannot overwrite a newer explicit auth/empty result.
    async with lock:
        return await _refresh_saved_discovery(fingerprint, kwargs, force=force)


async def _refresh_saved_discovery(
    fingerprint: str, kwargs: dict[str, Any], *, force: bool,
) -> ProviderModelsDiscoverResult:
    previous = _saved_discoveries.get(fingerprint)
    now = time.monotonic()
    if previous is not None and not force:
        ttl = _DISCOVERY_SUCCESS_TTL if previous.result.ok else _DISCOVERY_FAILURE_TTL
        if now - previous.checked_at < ttl:
            return copy.deepcopy(previous.result)
    result = await discover_provider_models(**kwargs)
    now = time.monotonic()
    last_good = result if result.ok and result.source == "live" else None
    last_good_at = now if last_good is not None else 0
    if (
        not result.ok and result.failure_kind in TRANSIENT_MODEL_DISCOVERY_FAILURES
        and previous is not None and previous.last_good is not None
        and now - previous.last_good_at < _DISCOVERY_LAST_GOOD_TTL
    ):
        last_good, last_good_at = previous.last_good, previous.last_good_at
        # Retain the failure as a failure; consumers can use the same-identity
        # rows without mistaking a stale catalog for a successful auth check.
        result = ProviderModelsDiscoverResult(
            ok=False, provider_id=result.provider_id, failure_kind=result.failure_kind,
            detail=result.detail, source=last_good.source,
            models=copy.deepcopy(last_good.models), catalog=result.catalog,
        )
    # Auth/permission failures discard LKG. Bound memory for rotated accounts.
    _saved_discoveries.pop(fingerprint, None)
    if len(_saved_discoveries) >= 64:
        _saved_discoveries.pop(next(iter(_saved_discoveries)))
    _saved_discoveries[fingerprint] = _SavedDiscovery(
        copy.deepcopy(result), now, copy.deepcopy(last_good), last_good_at,
    )
    return result


def _provider_metadata_wire(
    info: ModelInfo,
    provider_id: str,
    catalog: object,
) -> dict[str, Any] | None:
    """Return one normalized provider metadata envelope, if available.

    New provider adapters attach the envelope directly to ``ModelInfo``.  A
    hydrated shared catalog may also own the same typed sidecar, so consult it
    as a compatibility fallback for cached rows constructed before the
    ``ModelInfo.metadata`` field was added.
    """

    if isinstance(info.metadata, dict):
        return dict(info.metadata)
    get_metadata = getattr(catalog, "get_provider_model_metadata", None)
    if not callable(get_metadata):
        return None
    typed = get_metadata(info.model_id, provider_id)
    to_wire = getattr(typed, "to_wire", None)
    if not callable(to_wire):
        return None
    wire = to_wire()
    return dict(wire) if isinstance(wire, Mapping) else None


def _metadata_capability(
    metadata: Mapping[str, Any] | None,
    capability: str,
) -> bool | None:
    """Resolve an explicit provider capability without collapsing ``False``.

    TokenRhythm's authenticated declaration is authoritative for the current
    credential.  Its public listing fills only facts the declaration omits.
    Returning ``None`` means neither source knows the value, at which point
    the legacy ``ModelInfo``/catalog fallback remains appropriate.
    """

    if metadata is None:
        return None
    for source_name in ("declared", "published"):
        source = metadata.get(source_name)
        if not isinstance(source, Mapping):
            continue
        capabilities = source.get("capabilities")
        if not isinstance(capabilities, Mapping):
            continue
        value = capabilities.get(capability)
        if isinstance(value, bool):
            return value
    return None


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
    metadata = _provider_metadata_wire(info, provider_id, catalog)
    override_window = catalog.user_context_window_override(info.model_id, provider=provider_id)
    if override_window is not None:
        context_window = override_window
    elif info.context_window > 0:
        context_window = info.context_window
    else:
        context_window = catalog.resolve_context_window(info.model_id, provider_id)
    max_output = info.max_output_tokens if info.max_output_tokens > 0 else entry.max_output_tokens
    tools = _metadata_capability(metadata, "tools")
    reasoning = _metadata_capability(metadata, "reasoning")
    vision = _metadata_capability(metadata, "vision")
    safe_tools = info.supports_tools or entry.supports_tools
    tools_enabled = False if tools is False else safe_tools
    safe_reasoning = info.supports_reasoning or entry.supports_reasoning
    reasoning_enabled = False if reasoning is False else safe_reasoning
    safe_vision = info.supports_vision or entry.supports_vision
    vision_enabled = False if vision is False else safe_vision
    capabilities: list[str] = ["chat"]
    if tools_enabled:
        capabilities.append("tools")
    if reasoning_enabled:
        capabilities.append("reasoning")
    if vision_enabled:
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
        # Provider adapters may attach a normalized, provider-owned metadata
        # envelope.  Keep it additive and opaque at this boundary: the
        # TokenRhythm catalog owns its published/declared schema and ordinary
        # providers continue to emit ``None``.
        "metadata": metadata,
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
        if isinstance(exc, ProviderModelListingResponseError):
            kind = ProviderFailureKind.MALFORMED_RESPONSE
        else:
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
    force_refresh: bool = False,
    persist_catalog: bool = False,
    catalog_config: object | None = None,
) -> ProviderModelsDiscoverResult:
    """Return endpoint-declared custom models or verified official catalogs.

    This is the selector-facing policy boundary. Unknown and unsupported
    provider ids remain validation errors, matching raw discovery. All other
    non-custom providers default to an empty, successful catalog *before* credential
    resolution or provider construction, preserving the manual model-id
    escape hatch without presenting guessed data as authoritative.

    A trusted provider id is not enough on its own: an operator-supplied
    OpenAI-compatible re-host can serve a completely different model set.
    Official-provider selection is allowed only when the effective base URL uses
    HTTPS and the provider's allowlisted official host (or one of its
    subdomains). Explicit custom providers query their configured endpoint;
    only declared capacity fields enter that provider's runtime metadata.
    """
    provider_id = (provider_id or "").strip()
    spec = get_provider_spec(provider_id)  # raises UnknownProviderError(ValueError)
    if not spec.runtime_supported:
        raise ValueError(f"Provider '{provider_id}' has no runtime support to discover.")

    if provider_id in {"custom", "custom_anthropic"}:
        from opensquilla.provider.model_capacity import (
            custom_capacity_identity,
            install_custom_capacity,
            resolve_model_capacities,
        )
        from opensquilla.provider.model_catalog import shared_catalog

        identity = custom_capacity_identity(catalog_config, provider_id)
        result = await _discover_saved_or_draft_models(
            saved=persist_catalog, force=force_refresh,
            provider_id=provider_id,
            api_key=api_key,
            api_key_env=api_key_env,
            base_url=base_url,
            proxy=proxy,
            allow_default_api_key_env=allow_default_api_key_env,
        )
        if persist_catalog and result.ok and catalog_config is not None:
            catalog = shared_catalog()
            install_custom_capacity(catalog, identity, provider_id, result.models)
            capacities = resolve_model_capacities(
                catalog,
                catalog_config,
                [{"provider": provider_id, "model": str(row["id"])} for row in result.models],
            )["models"]
            by_model = {capacity["model"]: capacity for capacity in capacities}
            for row in result.models:
                capacity = by_model[str(row["id"])]
                row["contextWindow"] = capacity["contextWindow"]["value"]
                row["maxOutputTokens"] = capacity["maxOutputTokens"]["value"]
        return result

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
        or not is_host_or_subdomain(effective_base_url, spec.compat.official_host)
    ):
        return ProviderModelsDiscoverResult(ok=True, provider_id=provider_id)

    # TokenRhythm's production API has two authoritative sources: its public
    # website catalog and the authenticated account entitlement list. Keep
    # that merged, persistent projection scoped to the canonical production
    # origin. Verified HTTPS subdomains (for example the provider's UAT
    # service) continue through the generic live-listing path below so their
    # credentials and model metadata never enter the production coordinator.
    tokenrhythm_production_catalog = False
    if spec.live_catalog_shape == "tokenrhythm":
        from opensquilla.provider.tokenrhythm_catalog import (
            is_official_tokenrhythm_endpoint,
        )

        tokenrhythm_production_catalog = is_official_tokenrhythm_endpoint(effective_base_url)

    if tokenrhythm_production_catalog:
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

        from opensquilla.gateway.model_catalog_refresh import (
            discover_tokenrhythm_models,
        )

        return await discover_tokenrhythm_models(
            provider_id=provider_id,
            api_key=resolved_key,
            base_url=effective_base_url,
            proxy=proxy,
            force=force_refresh,
            persist_entitlement=persist_catalog,
            config=catalog_config,
        )

    # The selector-facing host gate above already rejected plain HTTP,
    # foreign hosts, and lookalike suffixes. Treat an admitted TokenRhythm
    # non-production origin as its own declared catalog authority.

    discovery_provider_id = spec.selectable_model_discovery_provider_id or provider_id
    discover_kwargs: dict[str, Any] = {
        "provider_id": discovery_provider_id,
        "api_key": api_key,
        "api_key_env": api_key_env,
        # A sibling discovery provider owns a different protocol path. Its
        # registry default is the only trusted listing endpoint; never pass
        # the configured chat base path across protocols.
        "base_url": (base_url.strip() if discovery_provider_id == provider_id else ""),
        "proxy": proxy,
    }
    if not allow_default_api_key_env:
        discover_kwargs["allow_default_api_key_env"] = False
    result = await _discover_saved_or_draft_models(
        saved=persist_catalog, force=force_refresh, **discover_kwargs,
    )
    if result.provider_id == provider_id:
        return result
    return ProviderModelsDiscoverResult(
        ok=result.ok,
        provider_id=provider_id,
        failure_kind=result.failure_kind,
        detail=result.detail,
        source=result.source,
        models=result.models,
    )
