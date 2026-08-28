"""Provider context-state and prompt-cache capability profiles."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

ANTHROPIC_COMPACTION_STATE_KIND = "anthropic_compaction_block"
OPENAI_RESPONSES_COMPACTED_WINDOW_STATE_KIND = "openai_responses_compacted_window"


class PromptCacheSupport(StrEnum):
    NONE = "none"
    IMPLICIT = "implicit"
    EXPLICIT = "explicit"
    AUTOMATIC = "automatic"


class NativeCompactionSupport(StrEnum):
    NONE = "none"
    STANDALONE = "standalone"


class ProviderStateContinuityDecision(StrEnum):
    KEEP_PROVIDER = "keep_provider"
    USE_PORTABLE_FALLBACK = "use_portable_fallback"
    DISCARD_PROVIDER_STATE = "discard_provider_state"
    REBUILD_FROM_CANONICAL_TRANSCRIPT = "rebuild_from_canonical_transcript"


@dataclass(frozen=True)
class ProviderContextCapabilities:
    provider: str
    model: str
    prompt_cache: PromptCacheSupport = PromptCacheSupport.NONE
    native_compaction: NativeCompactionSupport = NativeCompactionSupport.NONE
    native_compaction_state_kind: str | None = None
    supports_cache_breakpoints: bool = False
    state_portable_across_providers: bool = False
    min_cache_tokens: int | None = None
    cache_ttl_options: tuple[int, ...] = ()

    @property
    def supports_explicit_prompt_cache(self) -> bool:
        return self.prompt_cache == PromptCacheSupport.EXPLICIT and self.supports_cache_breakpoints


@dataclass(frozen=True)
class ProviderContextProfile:
    """Static context-capability profile carried on a ``ProviderSpec``.

    Only branches keyed purely on the provider identity live here. Branches
    that also consult the request host (Gemini's ``generativelanguage``
    endpoint, OpenAI's ``api.openai.com`` guard) deliberately stay as code in
    :func:`provider_context_capabilities` — expressing them as spec profiles
    would grant cache behavior to custom-base-url deployments that do not
    serve it.
    """

    prompt_cache: PromptCacheSupport = PromptCacheSupport.NONE
    native_compaction: NativeCompactionSupport = NativeCompactionSupport.NONE
    native_compaction_state_kind: str | None = None
    supports_cache_breakpoints: bool = False
    state_portable_across_providers: bool = False
    min_cache_tokens: int | None = None
    cache_ttl_options: tuple[int, ...] = ()
    # Optional per-model hook: (model-id prefix, cache support) pairs, first
    # match wins, ``prompt_cache`` is the fallback for unmatched models. When
    # the table is non-empty, cache-breakpoint support follows the resolved
    # level (only explicit-cache models accept cache_control breakpoints).
    prompt_cache_model_prefix_table: tuple[tuple[str, PromptCacheSupport], ...] = ()
    # Optional per-model-name hook: like the prefix table above, but matched
    # against the model BASENAME (id after stripping any "vendor/" prefix),
    # and consulted only when the full-id prefix table misses. First match
    # wins; ``prompt_cache`` remains the fallback for unmatched models.
    prompt_cache_model_name_prefix_table: tuple[tuple[str, PromptCacheSupport], ...] = ()


ANTHROPIC_CONTEXT_PROFILE = ProviderContextProfile(
    prompt_cache=PromptCacheSupport.EXPLICIT,
    native_compaction=NativeCompactionSupport.NONE,
    supports_cache_breakpoints=True,
    state_portable_across_providers=False,
)

OPENAI_RESPONSES_CONTEXT_PROFILE = ProviderContextProfile(
    prompt_cache=PromptCacheSupport.AUTOMATIC,
    native_compaction=NativeCompactionSupport.STANDALONE,
    native_compaction_state_kind=OPENAI_RESPONSES_COMPACTED_WINDOW_STATE_KIND,
    state_portable_across_providers=False,
)

OPENROUTER_CONTEXT_PROFILE = ProviderContextProfile(
    # Fallback for model ids outside the prefix table below.
    prompt_cache=PromptCacheSupport.IMPLICIT,
    state_portable_across_providers=False,
    prompt_cache_model_prefix_table=(
        ("anthropic/", PromptCacheSupport.EXPLICIT),
        ("google/", PromptCacheSupport.EXPLICIT),
        ("deepseek/", PromptCacheSupport.EXPLICIT),
        ("x-ai/", PromptCacheSupport.EXPLICIT),
        ("z-ai/", PromptCacheSupport.IMPLICIT),
    ),
    prompt_cache_model_name_prefix_table=(
        ("qwen3.6-flash", PromptCacheSupport.EXPLICIT),
        ("qwen3.5-flash", PromptCacheSupport.EXPLICIT),
        ("qwen3-coder", PromptCacheSupport.EXPLICIT),
    ),
)


@dataclass(frozen=True)
class ProviderStateContinuityDiagnostic:
    decision: ProviderStateContinuityDecision
    candidate_provider: str
    candidate_model: str
    provider_state_loss_risk: bool = False
    active_state_kind: str | None = None
    active_state_provider: str | None = None
    portable_fallback_available: bool = False
    reason: str = ""

    def as_metadata(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "candidate_provider": self.candidate_provider,
            "candidate_model": self.candidate_model,
            "provider_state_loss_risk": self.provider_state_loss_risk,
            "active_state_kind": self.active_state_kind,
            "active_state_provider": self.active_state_provider,
            "portable_fallback_available": self.portable_fallback_available,
            "reason": self.reason,
        }


def _registered_context_profile(provider: str) -> ProviderContextProfile | None:
    """Spec-carried profile for a provider id, or None when host guards apply."""
    # Local import: the registry imports this module for the profile
    # definitions attached to its specs.
    from .registry import UnknownProviderError, get_provider_spec

    try:
        return get_provider_spec(provider).context_profile
    except UnknownProviderError:
        return None


def _profile_prompt_cache(profile: ProviderContextProfile, model_l: str) -> PromptCacheSupport:
    for prefix, support in profile.prompt_cache_model_prefix_table:
        if model_l.startswith(prefix):
            return support
    # Basename table is consulted only after the full-id prefix table misses.
    # getattr keeps profiles built without the field working.
    name_table: tuple[tuple[str, PromptCacheSupport], ...] = getattr(
        profile, "prompt_cache_model_name_prefix_table", ()
    )
    if name_table:
        model_name = model_l.rsplit("/", 1)[-1]
        for prefix, support in name_table:
            if model_name.startswith(prefix):
                return support
    return profile.prompt_cache


def _capabilities_from_profile(
    profile: ProviderContextProfile,
    *,
    provider: str,
    model: str,
    model_l: str,
) -> ProviderContextCapabilities:
    prompt_cache = profile.prompt_cache
    supports_cache_breakpoints = profile.supports_cache_breakpoints
    if profile.prompt_cache_model_prefix_table or getattr(
        profile, "prompt_cache_model_name_prefix_table", ()
    ):
        prompt_cache = _profile_prompt_cache(profile, model_l)
        # Per-model resolution: only explicit-cache models accept
        # cache_control breakpoints.
        supports_cache_breakpoints = prompt_cache == PromptCacheSupport.EXPLICIT
    return ProviderContextCapabilities(
        provider=provider,
        model=model,
        prompt_cache=prompt_cache,
        native_compaction=profile.native_compaction,
        native_compaction_state_kind=profile.native_compaction_state_kind,
        supports_cache_breakpoints=supports_cache_breakpoints,
        state_portable_across_providers=profile.state_portable_across_providers,
        min_cache_tokens=profile.min_cache_tokens,
        cache_ttl_options=profile.cache_ttl_options,
    )


def _gemini_min_cache_tokens(model_l: str) -> int | None:
    if "flash" in model_l:
        return 1024
    if "pro" in model_l:
        return 4096
    return None


def provider_context_capabilities(
    *,
    provider_kind: str,
    model: str,
    base_url: str = "",
) -> ProviderContextCapabilities:
    provider = provider_kind.strip().lower()
    model_l = model.strip().lower()
    base_l = base_url.strip().lower()

    profile = _registered_context_profile(provider)
    if profile is not None:
        return _capabilities_from_profile(
            profile, provider=provider, model=model, model_l=model_l
        )

    # The two branches below stay code, not spec profiles: each is gated on
    # the request host, and keying them on the provider id alone would grant
    # cache behavior to custom-base-url deployments that do not serve it.
    if provider == "gemini" or "generativelanguage.googleapis.com" in base_l:
        return ProviderContextCapabilities(
            provider=provider,
            model=model,
            prompt_cache=PromptCacheSupport.IMPLICIT,
            native_compaction=NativeCompactionSupport.NONE,
            min_cache_tokens=_gemini_min_cache_tokens(model_l),
            state_portable_across_providers=False,
        )

    if provider == "openai" and "api.openai.com" in base_l:
        return ProviderContextCapabilities(
            provider=provider,
            model=model,
            prompt_cache=PromptCacheSupport.AUTOMATIC,
            state_portable_across_providers=False,
        )

    return ProviderContextCapabilities(provider=provider, model=model)


def supports_openrouter_explicit_prompt_cache(model: str) -> bool:
    return provider_context_capabilities(
        provider_kind="openrouter",
        model=model,
    ).supports_explicit_prompt_cache


def _state_value(state: Any, field: str, default: Any = None) -> Any:
    if isinstance(state, dict):
        return state.get(field, default)
    return getattr(state, field, default)


def _state_int_value(state: Any, field: str) -> int | None:
    value = _state_value(state, field, None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _state_order_key(index: int, state: Any) -> tuple[int, int, int]:
    created_at = _state_int_value(state, "created_at")
    state_id = _state_int_value(state, "id")
    return (
        created_at if created_at is not None else -1,
        state_id if state_id is not None else -1,
        index,
    )


def _active_context_states(
    context_states: list[Any],
    *,
    now_ms: int | None = None,
) -> list[Any]:
    indexed = [
        (index, state)
        for index, state in enumerate(context_states)
        if _is_active_context_state(state, now_ms=now_ms)
    ]
    indexed.sort(key=lambda item: _state_order_key(item[0], item[1]))
    return [state for _, state in indexed]


def _latest_portable_context_state(active_states: list[Any]) -> Any | None:
    return next(
        (
            state
            for state in reversed(active_states)
            if bool(_state_value(state, "portable", False))
        ),
        None,
    )


def _latest_native_context_state(active_states: list[Any]) -> Any | None:
    return next(
        (
            state
            for state in reversed(active_states)
            if not bool(_state_value(state, "portable", False))
        ),
        None,
    )


def _is_active_context_state(state: Any, *, now_ms: int | None = None) -> bool:
    if not bool(_state_value(state, "valid", True)):
        return False
    expires_at = _state_int_value(state, "expires_at")
    return not (now_ms is not None and expires_at is not None and expires_at <= now_ms)


def provider_state_continuity_diagnostic(
    *,
    context_states: list[Any],
    candidate_provider: str,
    candidate_model: str,
    now_ms: int | None = None,
) -> ProviderStateContinuityDiagnostic:
    provider = candidate_provider.strip().lower()
    model = candidate_model.strip()
    active_states = _active_context_states(context_states, now_ms=now_ms)
    if not active_states:
        return ProviderStateContinuityDiagnostic(
            decision=ProviderStateContinuityDecision.REBUILD_FROM_CANONICAL_TRANSCRIPT,
            candidate_provider=provider,
            candidate_model=model,
            reason="no_active_context_state",
        )

    portable_state = _latest_portable_context_state(active_states)
    native_state = _latest_native_context_state(active_states)
    if native_state is None:
        return ProviderStateContinuityDiagnostic(
            decision=ProviderStateContinuityDecision.USE_PORTABLE_FALLBACK,
            candidate_provider=provider,
            candidate_model=model,
            portable_fallback_available=portable_state is not None,
            reason="portable_context_state_available",
        )

    state_provider = str(_state_value(native_state, "provider", "")).strip().lower()
    state_kind = str(_state_value(native_state, "state_kind", "") or "")
    if state_provider == provider:
        return ProviderStateContinuityDiagnostic(
            decision=ProviderStateContinuityDecision.KEEP_PROVIDER,
            candidate_provider=provider,
            candidate_model=model,
            active_state_kind=state_kind,
            active_state_provider=state_provider,
            portable_fallback_available=portable_state is not None,
            reason="candidate_provider_matches_latest_native_state",
        )

    if portable_state is not None:
        return ProviderStateContinuityDiagnostic(
            decision=ProviderStateContinuityDecision.USE_PORTABLE_FALLBACK,
            candidate_provider=provider,
            candidate_model=model,
            provider_state_loss_risk=True,
            active_state_kind=state_kind,
            active_state_provider=state_provider,
            portable_fallback_available=True,
            reason="latest_native_state_provider_switch_with_portable_fallback",
        )

    return ProviderStateContinuityDiagnostic(
        decision=ProviderStateContinuityDecision.DISCARD_PROVIDER_STATE,
        candidate_provider=provider,
        candidate_model=model,
        provider_state_loss_risk=True,
        active_state_kind=state_kind,
        active_state_provider=state_provider,
        reason="latest_native_state_provider_switch_without_portable_fallback",
    )
