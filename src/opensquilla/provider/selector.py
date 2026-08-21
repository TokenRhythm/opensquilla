"""Model selector with fallback chain and config-driven provider resolution."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from opensquilla.redaction import redact_error_text

from .anthropic import AnthropicProvider
from .compat_policy import OpenAICompatPolicy
from .credentials import credential_provider_hint, endpoint_provider_hint
from .failures import classify_provider_error
from .ollama import OllamaProvider
from .openai import OpenAIProvider
from .openai_codex import OpenAICodexProvider
from .openai_responses import OpenAIResponsesProvider
from .protocol import LLMProvider, ProviderPlugin, resolve_failover_chain
from .registry import (
    AuthHeaderStyle,
    ProviderSpec,
    UnknownProviderError,
    get_provider_spec,
)
from .types import ModelInfo


@dataclass
class ProviderConfig:
    """Runtime configuration for a single provider."""

    provider: str  # "anthropic" | "openai" | "ollama"
    model: str
    api_key: str = field(default="", repr=False)
    base_url: str = ""
    org_id: str = ""
    proxy: str = ""  # explicit HTTP proxy URL
    provider_routing: dict[str, str] = field(default_factory=dict)
    # False for cross-provider tier execution: provider-bound continuity
    # state minted elsewhere (thinking blocks / thought signatures) must not
    # be replayed to a provider that did not produce it.
    replay_provider_state: bool = True


@dataclass
class SelectorConfig:
    """Full model selection config: primary + ordered fallback chain."""

    primary: ProviderConfig
    fallbacks: list[ProviderConfig] = field(default_factory=list)


@dataclass(frozen=True)
class ProviderListError:
    """One provider's failure while listing models, classified and redacted.

    ``model_hint`` is the chain link's configured model — an operator anchor
    for *which* configured provider row failed, since several links can share
    a provider id. ``kind`` is a :class:`ProviderFailureKind` value; ``detail``
    is credential-masked free text safe to surface to clients.
    """

    provider: str
    model_hint: str
    kind: str
    detail: str


@dataclass
class ModelListResult:
    """Aggregated model-listing outcome across the whole selector chain.

    ``models`` preserves the exact shape ``list_models`` returned before this
    was introduced (provider ``ModelInfo`` dicts); ``errors`` is the additive
    channel that lets callers tell "no models" apart from "every provider
    rejected our credentials".
    """

    models: list[dict] = field(default_factory=list)
    errors: list[ProviderListError] = field(default_factory=list)


async def _list_provider_models_detailed(provider: LLMProvider) -> list:
    """Ask adapters to surface listing failures when they expose that capability."""

    list_models = provider.list_models
    try:
        supports_strict = "raise_on_error" in inspect.signature(list_models).parameters
    except (TypeError, ValueError):
        supports_strict = False
    if supports_strict:
        return await list_models(raise_on_error=True)  # type: ignore[call-arg]
    return await list_models()


class ProviderBuildError(Exception):
    """Raised when a provider cannot be instantiated."""


class ProviderNotConfiguredError(ProviderBuildError):
    """Raised when resolving a selector whose active link is not usable yet.

    A gateway can boot before the operator has supplied an API key; the
    selector then exists in an unconfigured state so that a later
    ``sync_primary`` (Web UI / RPC config edit) can bring it live in place
    without a restart. Subclasses ``ProviderBuildError`` so every existing
    resolve() error handler degrades the same way.
    """


def _unsupported_runtime_message(provider: str) -> str:
    return (
        f"Provider '{provider}' is registered but runtime support "
        "is not enabled in this wave"
    )


def _missing_base_url_message(provider: str) -> str:
    return f"Provider '{provider}' requires an explicit base_url"


def _exception_status_code(exc: Exception) -> int | None:
    """Best-effort HTTP status code from a provider list_models exception.

    Adapters raise heterogeneous errors: ``httpx.HTTPStatusError`` carries a
    ``response.status_code``; others are plain messages. When no structured
    code is present, ``classify_provider_error`` still classifies from the
    message text (e.g. "invalid api key"), so ``None`` is a safe default.
    """
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    return None


_ProviderConfigIdentity = tuple[
    str, str, str, str, str, str, bool, tuple[tuple[str, str], ...]
]
_CapacityConfigIdentity = tuple[
    str, str, str, str, str, str, tuple[tuple[str, str], ...]
]


def _provider_config_identity(cfg: ProviderConfig) -> _ProviderConfigIdentity:
    provider_routing = tuple(sorted((str(k), str(v)) for k, v in cfg.provider_routing.items()))
    return (
        cfg.provider,
        cfg.model,
        cfg.api_key,
        cfg.base_url,
        cfg.org_id,
        cfg.proxy,
        cfg.replay_provider_state,
        provider_routing,
    )


def _capacity_config_identity(cfg: ProviderConfig) -> _CapacityConfigIdentity:
    """Identity of one physical deployment for capacity-bound failover."""

    provider_routing = tuple(sorted((str(k), str(v)) for k, v in cfg.provider_routing.items()))
    return (
        cfg.provider,
        cfg.model,
        cfg.api_key,
        cfg.base_url,
        cfg.org_id,
        cfg.proxy,
        provider_routing,
    )


def _without_provider_state_replay(cfg: ProviderConfig) -> ProviderConfig:
    """Return an isolated config that cannot replay provider-private state."""

    return replace(
        cfg,
        provider_routing=dict(cfg.provider_routing),
        replay_provider_state=False,
    )


def _build_provider(cfg: ProviderConfig) -> LLMProvider:
    """Instantiate the correct provider class from a ProviderConfig."""
    provider_id = str(cfg.provider or "").strip().lower()
    credential_hint = credential_provider_hint(cfg.api_key)
    if credential_hint and credential_hint != provider_id:
        raise ProviderBuildError(
            f"Credential format belongs to provider '{credential_hint}', "
            f"not configured provider '{provider_id or '(unset)'}'"
        )
    endpoint_hint = endpoint_provider_hint(cfg.base_url)
    if credential_hint and endpoint_hint and credential_hint != endpoint_hint:
        raise ProviderBuildError(
            f"Credential format belongs to provider '{credential_hint}', "
            f"but the configured endpoint belongs to provider '{endpoint_hint}'"
        )
    try:
        spec = get_provider_spec(cfg.provider)
    except UnknownProviderError as exc:
        raise ProviderBuildError(str(exc)) from exc

    if not spec.runtime_supported:
        raise ProviderBuildError(_unsupported_runtime_message(cfg.provider))

    context = _build_context(cfg, spec)

    if not context.base_url and spec.requires_base_url():
        raise ProviderBuildError(_missing_base_url_message(cfg.provider))

    factory = _BACKEND_FACTORIES.get(spec.backend)
    if factory is None:
        raise ProviderBuildError(_unsupported_runtime_message(cfg.provider))
    return factory(context)


@dataclass(frozen=True)
class ProviderBuildContext:
    """Everything a backend factory may consume, assembled in one place.

    The superset of ``ProviderConfig`` runtime fields plus the spec-derived
    fields the adapters need. Provider constructor signatures are unchanged;
    each backend factory picks the fields its adapter accepts, so a
    capability an adapter cannot receive yet (e.g. ``num_ctx``, which
    ``ProviderConfig`` does not carry) is a visible gap here instead of an
    implicit omission scattered across call sites.
    """

    provider_id: str
    backend: str
    kind: str
    model: str
    api_key: str = ""
    base_url: str = ""  # resolved: config override or the spec default
    org_id: str = ""
    proxy: str = ""
    provider_routing: Mapping[str, str] = field(default_factory=dict)
    replay_provider_state: bool = True
    # OllamaProvider knob; never populated today because ProviderConfig has
    # no num_ctx field — kept visible so the gap is explicit.
    num_ctx: int | None = None
    # Spec-derived fields.
    auth_header_style: AuthHeaderStyle = "bearer"
    compat: OpenAICompatPolicy = field(default_factory=OpenAICompatPolicy)
    static_model_ids: tuple[str, ...] = ()


def _build_context(cfg: ProviderConfig, spec: ProviderSpec) -> ProviderBuildContext:
    return ProviderBuildContext(
        provider_id=cfg.provider,
        backend=spec.backend,
        kind=spec.provider_kind,
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url or spec.default_base_url,
        org_id=cfg.org_id,
        proxy=cfg.proxy,
        provider_routing=dict(cfg.provider_routing),
        replay_provider_state=cfg.replay_provider_state,
        auth_header_style=spec.auth_header_style,
        compat=spec.compat,
        static_model_ids=spec.static_model_ids,
    )


def _build_anthropic(ctx: ProviderBuildContext) -> LLMProvider:
    kwargs: dict = {
        "api_key": ctx.api_key,
        "model": ctx.model,
        "replay_provider_state": ctx.replay_provider_state,
        "auth_header_style": ctx.auth_header_style,
        "provider_id": ctx.provider_id,
        # Native Anthropic keeps its built-in SKU list. Compatibility
        # endpoints use an exact registry list when present, otherwise the
        # configured model only — never an unrelated Claude catalog.
        "listing_model_ids": (
            None
            if ctx.provider_id == "anthropic"
            else (ctx.static_model_ids or ((ctx.model,) if ctx.model else ()))
        ),
        "temperature_floor_model_ids": ctx.compat.temperature_floor_model_ids,
        "temperature_floor": ctx.compat.temperature_floor,
    }
    if ctx.base_url:
        kwargs["base_url"] = ctx.base_url
    if ctx.proxy:
        kwargs["proxy"] = ctx.proxy
    return AnthropicProvider(**kwargs)


def _build_openai_compat(ctx: ProviderBuildContext) -> LLMProvider:
    kwargs: dict = {
        "api_key": ctx.api_key,
        "model": ctx.model,
        "provider_kind": ctx.kind,
        "compat": ctx.compat,
        "replay_provider_state": ctx.replay_provider_state,
        "provider_id": ctx.provider_id,
    }
    if ctx.base_url:
        kwargs["base_url"] = ctx.base_url
    if ctx.org_id:
        kwargs["org_id"] = ctx.org_id
    if ctx.proxy:
        kwargs["proxy"] = ctx.proxy
    if ctx.provider_routing:
        kwargs["provider_routing"] = ctx.provider_routing
    return OpenAIProvider(**kwargs)


def _build_openai_responses(ctx: ProviderBuildContext) -> LLMProvider:
    # Responses chat is deliberately stateless (``store: false``) and converts
    # shared Messages into fresh input items, so there is no provider-private
    # state to replay.  Keep the shared replay flag out of the adapter until
    # native item continuity is introduced; cross-provider execution is
    # therefore fail-closed by construction here.
    kwargs: dict = {
        "api_key": ctx.api_key,
        "model": ctx.model,
        "provider_id": ctx.provider_id,
    }
    if ctx.base_url:
        kwargs["base_url"] = ctx.base_url
    if ctx.org_id:
        kwargs["org_id"] = ctx.org_id
    if ctx.proxy:
        kwargs["proxy"] = ctx.proxy
    return OpenAIResponsesProvider(**kwargs)


def _build_ollama(ctx: ProviderBuildContext) -> LLMProvider:
    kwargs: dict = {"model": ctx.model, "provider_id": ctx.provider_id}
    if ctx.base_url:
        kwargs["base_url"] = ctx.base_url
    if ctx.proxy:
        kwargs["proxy"] = ctx.proxy
    if ctx.api_key:
        kwargs["api_key"] = ctx.api_key
    if ctx.num_ctx is not None:
        kwargs["num_ctx"] = ctx.num_ctx
    return OllamaProvider(**kwargs)


def _build_openai_codex(ctx: ProviderBuildContext) -> LLMProvider:
    kwargs: dict = {"model": ctx.model, "provider_id": ctx.provider_id}
    if ctx.base_url:
        kwargs["base_url"] = ctx.base_url
    if ctx.proxy:
        kwargs["proxy"] = ctx.proxy
    return OpenAICodexProvider(**kwargs)


_BACKEND_FACTORIES: dict[str, Callable[[ProviderBuildContext], LLMProvider]] = {
    "anthropic": _build_anthropic,
    "openai_compat": _build_openai_compat,
    "openai_responses": _build_openai_responses,
    "ollama": _build_ollama,
    "openai_codex": _build_openai_codex,
}


class ModelSelector:
    """Resolves a provider from primary config with fallback chain support.

    Usage::

        selector = ModelSelector(SelectorConfig(
            primary=ProviderConfig("anthropic", "claude-sonnet-4-6", api_key="..."),
            fallbacks=[ProviderConfig("ollama", "llama3")],
        ))
        provider = selector.resolve()  # returns primary
        # on failure, call selector.next_fallback() to get next in chain
    """

    def __init__(
        self,
        config: SelectorConfig,
        plugin: ProviderPlugin | None = None,
    ) -> None:
        self._config = config
        self._chain: list[ProviderConfig] = [config.primary, *config.fallbacks]
        self._index = 0
        self._plugin = plugin
        # Per-selector (and therefore per-turn for runtime clones) invariant.
        # Keep it after rewriting the current chain so a plugin that supplies
        # a new failover chain later cannot re-enable provider-private replay.
        self._provider_state_replay_disabled = False
        self._capacity_bounded_fallbacks: frozenset[_CapacityConfigIdentity] | None = None

    def _apply_capacity_fallback_bound(self) -> None:
        allowed = self._capacity_bounded_fallbacks
        if allowed is None:
            return
        current = self._chain[self._index]
        tail = [
            cfg
            for cfg in self._chain[self._index + 1 :]
            if _capacity_config_identity(cfg) in allowed
        ]
        self._chain = [current, *tail]
        self._index = 0

    def _install_capacity_fallback_bound(self, entries: list[object]) -> None:
        current = self._chain[self._index]
        materialized: list[ProviderConfig] = []
        for entry in entries:
            if isinstance(entry, ProviderConfig):
                materialized.append(entry)
                continue
            if not isinstance(entry, Mapping):
                continue
            provider = str(entry.get("provider") or current.provider).strip()
            model = str(entry.get("model") or "").strip()
            if not provider or not model:
                continue
            materialized.extend(
                cfg
                for cfg in self._chain[self._index + 1 :]
                if cfg.provider == provider and cfg.model == model
            )
        self._capacity_bounded_fallbacks = frozenset(
            _capacity_config_identity(cfg) for cfg in materialized
        )
        self._apply_capacity_fallback_bound()

    @property
    def is_configured(self) -> bool:
        """True when the active chain link can serve requests.

        Mirrors the boot gate: a provider id must be set, and an API key must
        be present unless the provider spec says none is required (Ollama,
        LM Studio, …). A selector can exist unconfigured so config hot-apply
        (``sync_primary``) can bring it live without a gateway restart.
        """
        cfg = self._chain[self._index]
        if not (cfg.provider or "").strip():
            return False
        try:
            requires_key = get_provider_spec(cfg.provider).requires_api_key()
        except Exception:  # noqa: BLE001 - unknown providers still need a key
            requires_key = True
        return bool(cfg.api_key) or not requires_key

    def resolve(self) -> LLMProvider:
        """Return the current provider (primary on first call)."""
        if not self.is_configured:
            cfg = self._chain[self._index]
            raise ProviderNotConfiguredError(
                f"Provider '{cfg.provider or '(unset)'}' is not configured yet "
                "(missing API key); add one via the Web UI settings or the "
                "[llm] section of config.toml"
            )
        return _build_provider(self._chain[self._index])

    @property
    def active_provider_id(self) -> str:
        """Configured provider id of the currently-active chain link.

        This is the operator-facing identity (e.g. ``"openrouter"``,
        ``"deepseek"``) — distinct from the wire-protocol backend class that
        serves it. OpenAI-compatible providers all run through
        ``OpenAIProvider``, whose ``provider_name`` is the generic ``"openai"``;
        surfacing that would mislabel an OpenRouter deployment as OpenAI.
        """
        return self._chain[self._index].provider

    def has_fallback(self) -> bool:
        """True if there is at least one more fallback available."""
        return self._index < len(self._chain) - 1

    def remaining_chain(self) -> list[ProviderConfig]:
        """Copy of the active chain link plus untried fallbacks, in order.

        Read-only view for callers that need the candidate deployment set —
        e.g. the provider health ledger's never-strand eligibility check.
        """
        return list(self._chain[self._index :])

    def next_fallback(self) -> LLMProvider:
        """Advance to the next fallback and return it.

        Raises IndexError if no more fallbacks are available.
        """
        if not self.has_fallback():
            raise IndexError("No more provider fallbacks available")
        next_index = self._index + 1
        provider = _build_provider(self._chain[next_index])
        self._index = next_index
        return provider

    def _fallback_chain_after_failure(
        self,
        primary_failure: Exception,
    ) -> tuple[ProviderConfig, list[ProviderConfig]]:
        """Resolve and constrain a failure-specific chain without mutating state."""

        current = self._chain[self._index]
        if self._plugin is not None and hasattr(self._plugin, "failover_hook"):
            chain = resolve_failover_chain(primary_failure, self._config, self._plugin)
        else:
            chain = list(self._chain[self._index + 1 :])
        if self._provider_state_replay_disabled:
            chain = [_without_provider_state_replay(cfg) for cfg in chain]
        if self._capacity_bounded_fallbacks is not None:
            chain = [
                cfg
                for cfg in chain
                if _capacity_config_identity(cfg) in self._capacity_bounded_fallbacks
            ]
        return current, chain

    def _activate_fallback_chain(
        self,
        current: ProviderConfig,
        chain: list[ProviderConfig],
    ) -> LLMProvider:
        """Build the first candidate before atomically installing its chain."""

        if not chain:
            raise IndexError("No fallback chain available")
        provider = _build_provider(chain[0])
        self._chain = [current, *chain]
        self._index = 1
        return provider

    def next_fallback_after_failure(self, primary_failure: Exception) -> LLMProvider:
        """Advance to the next fallback, consulting ``plugin.failover_hook``.

        When a plugin is registered its ``failover_hook`` return value
        replaces the static fallback chain from ``SelectorConfig``. An
        empty chain raises ``IndexError`` exactly like ``next_fallback``.
        """

        current, chain = self._fallback_chain_after_failure(primary_failure)
        return self._activate_fallback_chain(current, chain)

    def next_fallback_after_failure_matching(
        self,
        primary_failure: Exception,
        *,
        predicate: Callable[[ProviderConfig], bool],
    ) -> LLMProvider:
        """Atomically advance to the first constrained fallback candidate."""

        current, chain = self._fallback_chain_after_failure(primary_failure)
        matching_chain = [candidate for candidate in chain if predicate(candidate)]
        return self._activate_fallback_chain(current, matching_chain)

    def override_provider_config(
        self,
        cfg: ProviderConfig,
        *,
        preserve_existing_tail: bool = True,
    ) -> None:
        """Replace the active chain head with a full per-turn provider config.

        Cross-provider tier execution: unlike ``override_model`` (which keeps
        the primary's provider and credentials), this installs a complete
        ``ProviderConfig`` — provider id, credentials, base URL — as the
        turn's primary. The previous primary is kept as the first fallback so
        pre-content failover still has somewhere to go.
        """
        if self._provider_state_replay_disabled:
            cfg = _without_provider_state_replay(cfg)
        original_primary = self._chain[0]
        deduped_fallbacks: list[ProviderConfig] = []
        seen: set[_ProviderConfigIdentity] = {_provider_config_identity(cfg)}
        candidates = (
            [original_primary, *self._chain[1:]] if preserve_existing_tail else []
        )
        for candidate in candidates:
            identity = _provider_config_identity(candidate)
            if identity in seen:
                continue
            seen.add(identity)
            deduped_fallbacks.append(candidate)
        self._chain = [cfg, *deduped_fallbacks]
        self._index = 0
        self._apply_capacity_fallback_bound()

    def override_provider_config_with_bounded_fallbacks(
        self,
        cfg: ProviderConfig,
        approved_fallbacks: list[object],
    ) -> None:
        """Install a cross-provider head while preserving only proven fallbacks."""

        self.override_provider_config(cfg)
        self._install_capacity_fallback_bound(approved_fallbacks)

    def override_model(self, model: str) -> None:
        """Update the model on the primary provider config (for runtime switching)."""
        if model and model != self._chain[0].model:
            original_primary = self._chain[0]
            overridden_primary = ProviderConfig(
                provider=self._chain[0].provider,
                model=model,
                api_key=self._chain[0].api_key,
                base_url=self._chain[0].base_url,
                org_id=self._chain[0].org_id,
                proxy=self._chain[0].proxy,
                provider_routing=self._chain[0].provider_routing,
                replay_provider_state=self._chain[0].replay_provider_state,
            )
            fallback_chain = [original_primary, *self._chain[1:]]
            deduped_fallbacks: list[ProviderConfig] = []
            seen: set[_ProviderConfigIdentity] = {
                _provider_config_identity(overridden_primary)
            }
            for cfg in fallback_chain:
                identity = _provider_config_identity(cfg)
                if identity in seen:
                    continue
                seen.add(identity)
                deduped_fallbacks.append(cfg)
            self._chain = [overridden_primary, *deduped_fallbacks]
            self._index = 0
            self._apply_capacity_fallback_bound()

    def override_original_primary_model(self, model: str) -> None:
        """Restore the turn clone's configured primary and apply a model.

        A cross-provider router override replaces only the clone's active
        chain head; ``self._config.primary`` remains the configured provider
        that an explicit per-turn model belongs to.  Restoring from that
        immutable turn baseline prevents an explicit primary model id from
        being sent to the routed provider.
        """
        original = self._config.primary
        restored = replace(
            original,
            model=model or original.model,
            provider_routing=dict(original.provider_routing),
        )
        candidates = [*self._config.fallbacks, *self._chain]
        deduped_fallbacks: list[ProviderConfig] = []
        seen: set[_ProviderConfigIdentity] = {_provider_config_identity(restored)}
        for candidate in candidates:
            identity = _provider_config_identity(candidate)
            if identity in seen:
                continue
            seen.add(identity)
            deduped_fallbacks.append(candidate)
        self._chain = [restored, *deduped_fallbacks]
        self._index = 0
        self._apply_capacity_fallback_bound()

    def disable_provider_state_replay(self) -> None:
        """Disable provider-private history replay across the whole turn chain."""

        self._provider_state_replay_disabled = True
        self._config = SelectorConfig(
            primary=_without_provider_state_replay(self._config.primary),
            fallbacks=[
                _without_provider_state_replay(cfg) for cfg in self._config.fallbacks
            ],
        )
        self._chain = [_without_provider_state_replay(cfg) for cfg in self._chain]

    def override_model_with_fallback_chain(
        self,
        model: str,
        fallback_chain: list[object],
        *,
        preserve_existing_tail: bool = True,
    ) -> None:
        """Override primary model and prefer router-provided fallback models.

        Router fallback entries are intentionally small metadata dictionaries.
        The selector can safely synthesize same-provider fallbacks by reusing
        the current provider credentials. Cross-provider entries require a
        configured fallback with matching credentials; otherwise they are
        skipped instead of guessing secrets.
        """
        self.override_model(model)
        if not fallback_chain and preserve_existing_tail:
            return

        current = self._chain[0]
        existing_tail = list(self._chain[1:])
        existing_by_provider_model = {
            (cfg.provider, cfg.model): cfg for cfg in existing_tail
        }

        router_fallbacks: list[ProviderConfig] = []
        for entry in fallback_chain:
            if not isinstance(entry, Mapping):
                continue
            candidate_model = str(entry.get("model") or "").strip()
            if not candidate_model:
                continue
            candidate_provider = (
                str(entry.get("provider") or current.provider).strip() or current.provider
            )
            existing = existing_by_provider_model.get((candidate_provider, candidate_model))
            if existing is not None:
                router_fallbacks.append(existing)
                continue
            if candidate_provider != current.provider:
                continue
            router_fallbacks.append(
                ProviderConfig(
                    provider=current.provider,
                    model=candidate_model,
                    api_key=current.api_key,
                    base_url=current.base_url,
                    org_id=current.org_id,
                    proxy=current.proxy,
                    provider_routing=current.provider_routing,
                    replay_provider_state=current.replay_provider_state,
                )
            )

        deduped_tail: list[ProviderConfig] = []
        seen: set[_ProviderConfigIdentity] = {
            _provider_config_identity(current)
        }
        candidates = (
            [*router_fallbacks, *existing_tail]
            if preserve_existing_tail
            else router_fallbacks
        )
        for cfg in candidates:
            identity = _provider_config_identity(cfg)
            if identity in seen:
                continue
            seen.add(identity)
            deduped_tail.append(cfg)
        self._chain = [current, *deduped_tail]
        self._index = 0

    def override_model_with_bounded_fallback_chain(
        self,
        model: str,
        fallback_chain: list[object],
        approved_configured_fallbacks: list[object] | None = None,
    ) -> None:
        """Install only definitely capacity-safe fallbacks for a floored turn."""

        allowed_entries = [*fallback_chain, *(approved_configured_fallbacks or [])]
        if allowed_entries and all(
            isinstance(entry, ProviderConfig) for entry in allowed_entries
        ):
            self.override_model(model)
            current = self._chain[0]
            deduped_tail: list[ProviderConfig] = []
            seen: set[_ProviderConfigIdentity] = {
                _provider_config_identity(current)
            }
            for entry in allowed_entries:
                assert isinstance(entry, ProviderConfig)
                identity = _provider_config_identity(entry)
                if identity in seen:
                    continue
                seen.add(identity)
                deduped_tail.append(entry)
            self._chain = [current, *deduped_tail]
            self._index = 0
        else:
            self.override_model_with_fallback_chain(model, fallback_chain)
        self._install_capacity_fallback_bound(allowed_entries)

    def sync_primary(self, cfg: ProviderConfig) -> None:
        """Replace the primary provider config for future resolves and clones."""
        if self._provider_state_replay_disabled:
            cfg = _without_provider_state_replay(cfg)
        self._config.primary = cfg
        self._chain[0] = cfg
        self.reset()

    def reset(self) -> None:
        """Reset to primary provider."""
        self._index = 0

    def clone(self) -> ModelSelector:
        """Return an independent copy for concurrent use.

        Deep-copies the config (primary + fallbacks, including each
        provider_routing dict) so the clone starts at index 0 with its own
        chain and is unaffected by later mutations of the original's
        config — whether a rebind (sync_primary) or an in-place edit of a
        shared ProviderConfig's provider_routing.
        """
        config_copy = SelectorConfig(
            primary=replace(
                self._config.primary,
                provider_routing=dict(self._config.primary.provider_routing),
            ),
            fallbacks=[
                replace(cfg, provider_routing=dict(cfg.provider_routing))
                for cfg in self._config.fallbacks
            ],
        )
        cloned = ModelSelector(config_copy, plugin=self._plugin)
        cloned._provider_state_replay_disabled = self._provider_state_replay_disabled
        cloned._capacity_bounded_fallbacks = self._capacity_bounded_fallbacks
        cloned._apply_capacity_fallback_bound()
        return cloned

    async def list_models(self) -> list[dict]:
        """Aggregate models from all configured providers in the chain."""
        return (await self.list_models_detailed()).models

    async def list_models_detailed(
        self,
        *,
        snapshot_resolver: Callable[[ProviderConfig], Sequence[ModelInfo] | None]
        | None = None,
    ) -> ModelListResult:
        """Aggregate models across the chain, keeping per-provider failures.

        Walks the chain exactly like :meth:`list_models`, but instead of
        swallowing a failed link it classifies the exception through
        :func:`classify_provider_error` and records a redacted
        :class:`ProviderListError`, so model pickers can distinguish
        "provider has no models" from "wrong key / URL".

        ``snapshot_resolver`` is an additive gateway boundary for providers
        whose ordinary model listing must be network-free. Returning ``None``
        keeps the provider's normal listing path; returning a sequence --
        including an empty one -- makes that sequence authoritative for the
        individual chain link. Other links continue through their adapters,
        so a cached provider neither suppresses their rows nor their errors.
        """
        result = ModelListResult()
        for cfg in self._chain:
            try:
                snapshot = snapshot_resolver(cfg) if snapshot_resolver is not None else None
                if snapshot is not None:
                    result.models.extend(model.model_dump() for model in snapshot)
                    continue
                provider = _build_provider(cfg)
                provider_models = await _list_provider_models_detailed(provider)
                result.models.extend(m.model_dump() for m in provider_models)
            except Exception as exc:
                result.errors.append(
                    ProviderListError(
                        provider=cfg.provider,
                        model_hint=cfg.model,
                        kind=classify_provider_error(
                            cfg.provider,
                            _exception_status_code(exc),
                            message=str(exc),
                        ).value,
                        # Provider error bodies can echo credentials (bad
                        # keys, signed URLs) — never repeat them verbatim.
                        detail=redact_error_text(str(exc)),
                    )
                )
        return result

    @property
    def current_config(self) -> ProviderConfig:
        return self._chain[self._index]


def build_provider(
    provider: str,
    model: str,
    api_key: str = "",
    base_url: str = "",
    org_id: str = "",
    proxy: str = "",
) -> LLMProvider:
    """Convenience factory: build a single provider directly."""
    return build_provider_from_config(
        ProviderConfig(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            org_id=org_id,
            proxy=proxy,
        )
    )


def build_provider_from_config(config: ProviderConfig) -> LLMProvider:
    """Build one provider while preserving the complete deployment config.

    Callers that already resolved a :class:`ProviderConfig` must not flatten it
    back into ``build_provider``'s legacy argument list: doing so would discard
    provider routing and continuity policy.  Copy the only mutable member so an
    adapter cannot mutate selector-owned configuration through a shared dict.
    """

    isolated = replace(
        config,
        provider_routing=dict(config.provider_routing),
    )
    return _build_provider(isolated)
