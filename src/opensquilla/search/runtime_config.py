"""Effective runtime configuration for search providers."""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from typing import Literal

from opensquilla.search.retry_policy import is_retryable_http_status
from opensquilla.search.types import (
    SearchExecutionPlan,
    SearchFallbackMode,
    SearchOptions,
    SearchProvider,
    SearchProviderError,
)

CredentialSource = Literal["configured", "configured_env", "spec_env", "none"]

_GENERAL_TIE_BREAKER = ("bocha", "tavily", "iqs", "brave", "exa", "serpdive", "duckduckgo")
_TECHNICAL_TIE_BREAKER = ("exa", "bocha", "brave", "tavily", "iqs", "serpdive", "duckduckgo")
_FRESHNESS_TIE_BREAKER = ("bocha", "tavily", "iqs", "brave", "exa")


@dataclass(frozen=True)
class SearchRuntimeConfig:
    """Process search settings before provider-specific resolution."""

    provider: str = "duckduckgo"
    max_results: int = 5
    api_key: str = ""
    api_key_env: str = ""
    proxy: str = ""
    use_env_proxy: bool = False
    fallback_policy: str = "off"
    diagnostics: bool = False


@dataclass(frozen=True)
class SearchProviderRuntimeConfig:
    """Effective provider settings with secrets kept out of diagnostics."""

    provider_id: str
    active_provider: str
    runtime_supported: bool
    requires_api_key: bool
    env_key: str
    capabilities: frozenset[str]
    credential_source: CredentialSource
    api_key: str = ""
    proxy: str = ""
    use_env_proxy: bool = False
    diagnostics: bool = False
    fallback_policy: str = "off"

    @property
    def credential_configured(self) -> bool:
        return bool(self.api_key)

    @property
    def available(self) -> bool:
        if not self.runtime_supported:
            return False
        return (not self.requires_api_key) or self.credential_configured

    @property
    def skipped_reason(self) -> str:
        if not self.runtime_supported:
            return "runtime_unsupported"
        if self.requires_api_key and not self.credential_configured:
            return "missing_api_key"
        return ""

    def provider_kwargs(self) -> dict[str, object]:
        kwargs: dict[str, object] = {
            "proxy": self.proxy,
            "use_env_proxy": self.use_env_proxy,
            "diagnostics": self.diagnostics,
        }
        if self.requires_api_key and self.api_key:
            kwargs["api_key"] = self.api_key
        return kwargs

    def public_status(self) -> dict[str, object]:
        return {
            "provider": self.provider_id,
            "available": self.available,
            "credentialSource": self.credential_source,
            "credentialConfigured": self.credential_configured,
            "skippedReason": self.skipped_reason,
            "capabilities": sorted(self.capabilities),
        }


@dataclass(frozen=True)
class ResolvedSearchRuntime:
    """Resolved runtime state used by search tools and orchestrators."""

    config: SearchRuntimeConfig
    providers: dict[str, SearchProviderRuntimeConfig]

    @property
    def active_provider(self) -> str:
        return self.config.provider

    @property
    def fallback_policy(self) -> str:
        return self.config.fallback_policy

    def provider_config(self, provider_id: str) -> SearchProviderRuntimeConfig:
        try:
            return self.providers[provider_id]
        except KeyError as exc:
            available = ", ".join(sorted(self.providers))
            raise ValueError(
                f"Unknown search provider '{provider_id}'. Available: {available}"
            ) from exc

    def provider_kwargs(self, provider_id: str) -> dict[str, object]:
        return self.provider_config(provider_id).provider_kwargs()

    def build_provider(self, provider_id: str) -> SearchProvider:
        from opensquilla.search.registry import get_provider

        _ensure_builtin_search_providers()
        return get_provider(provider_id, **self.provider_kwargs(provider_id))

    def provider_order(self, options: SearchOptions) -> tuple[str, ...]:
        if options.provider:
            return self._explicit_provider_order(options.provider, recency=options.recency)
        requires_domain_filter = bool(options.include_domains or options.exclude_domains)
        if options.recency is not None or options.mode == "news":
            return self._freshness_provider_order(
                requires_domain_filter=requires_domain_filter
            )
        if options.mode == "technical":
            return self._ranked_available(
                _TECHNICAL_TIE_BREAKER,
                required_capabilities=_required_auto_capabilities(
                    requires_domain_filter=requires_domain_filter
                ),
            )
        return self._ranked_available(
            _GENERAL_TIE_BREAKER,
            required_capabilities=_required_auto_capabilities(
                requires_domain_filter=requires_domain_filter
            ),
        )

    def execution_plan(self, options: SearchOptions) -> SearchExecutionPlan:
        """Build the bounded, duplicate-free provider sequence for one request."""

        ranked_names = tuple(dict.fromkeys(self.provider_order(options)))
        # Automatic mode preselects one backup even with fallback disabled so a
        # provider that discovers a missing key locally can be skipped without
        # turning that skip into a second network attempt. All other failures
        # still stop immediately under the ``off`` policy.
        max_attempts = (
            2
            if self.fallback_policy == "network" or options.provider is None
            else 1
        )
        provider_names = list(ranked_names[:max_attempts])
        if (
            (self.fallback_policy == "network" or options.provider is None)
            and len(provider_names) == 1
            and provider_names[0] != "duckduckgo"
        ):
            duckduckgo = self.providers.get("duckduckgo")
            if duckduckgo is not None and duckduckgo.available:
                provider_names.append("duckduckgo")
        planned_provider_names = tuple(provider_names)
        fallback_mode: SearchFallbackMode = "none"
        if len(planned_provider_names) > 1:
            fallback_mode = (
                "network" if self.fallback_policy == "network" else "auth_missing"
            )
        return SearchExecutionPlan(
            provider_names=planned_provider_names,
            selection_mode="explicit" if options.provider is not None else "automatic",
            fallback_mode=fallback_mode,
        )

    def should_fallback(
        self,
        error: SearchProviderError,
        *,
        explicit_provider: bool,
    ) -> bool:
        if error.provider == "duckduckgo":
            return False
        if error.kind == "auth":
            return (not explicit_provider) and _is_missing_key_error(error)
        if self.fallback_policy != "network":
            return False
        if not error.retryable:
            return False
        if error.kind not in {"timeout", "network", "rate_limit", "http"}:
            return False
        if error.kind in {"timeout", "network"}:
            return True
        if error.status_code is not None:
            return is_retryable_http_status(error.status_code)
        return False

    def _explicit_provider_order(self, provider_id: str, *, recency: str | None) -> tuple[str, ...]:
        if (
            self.fallback_policy == "network"
            and provider_id != "duckduckgo"
        ):
            return (provider_id, "duckduckgo")
        return (provider_id,)

    def _freshness_provider_order(
        self,
        *,
        requires_domain_filter: bool = False,
    ) -> tuple[str, ...]:
        ranked = list(
            self._ranked_available(
                _FRESHNESS_TIE_BREAKER,
                required_capabilities=_required_auto_capabilities(
                    requires_domain_filter=requires_domain_filter,
                    requires_freshness=True,
                ),
            )
        )
        duckduckgo = self.providers.get("duckduckgo")
        if (
            duckduckgo is not None
            and duckduckgo.available
            and "duckduckgo" not in ranked
            and (not ranked or not requires_domain_filter)
        ):
            ranked.append("duckduckgo")
        return tuple(ranked)

    def _ranked_available(
        self,
        tie_breaker: tuple[str, ...],
        *,
        required_capabilities: tuple[str, ...] = (),
        include_unavailable_if_empty: bool = False,
    ) -> tuple[str, ...]:
        ranked: list[str] = []
        for provider_id in tie_breaker:
            provider = self.providers.get(provider_id)
            if provider is None:
                continue
            if not _has_required_capabilities(provider.capabilities, required_capabilities):
                continue
            if provider.available:
                ranked.append(provider_id)
        if ranked:
            return tuple(ranked)
        if include_unavailable_if_empty:
            return tuple(
                provider_id
                for provider_id in tie_breaker
                if provider_id in self.providers
                and (
                    not required_capabilities
                    or _has_required_capabilities(
                        self.providers[provider_id].capabilities,
                        required_capabilities,
                    )
                )
            )
        duckduckgo = self.providers.get("duckduckgo")
        if duckduckgo is not None and duckduckgo.available:
            return ("duckduckgo",)
        return ()


_runtime_config = SearchRuntimeConfig()


def configure_search_runtime(
    provider: str,
    max_results: int = 5,
    *,
    api_key: str = "",
    api_key_env: str = "",
    proxy: str = "",
    use_env_proxy: bool = False,
    fallback_policy: str = "off",
    diagnostics: bool = False,
) -> None:
    """Persist process-wide search settings used by all search surfaces."""

    global _runtime_config
    _runtime_config = SearchRuntimeConfig(
        provider=provider,
        max_results=max_results,
        api_key=api_key.strip(),
        api_key_env=api_key_env.strip(),
        proxy=proxy.strip(),
        use_env_proxy=bool(use_env_proxy),
        fallback_policy=fallback_policy if fallback_policy in {"off", "network"} else "off",
        diagnostics=bool(diagnostics),
    )


def get_search_runtime_config() -> SearchRuntimeConfig:
    return _runtime_config


def resolve_search_runtime(
    config: SearchRuntimeConfig | None = None,
) -> ResolvedSearchRuntime:
    """Resolve all provider credentials and availability from one config."""

    from opensquilla.search.registry import list_provider_specs

    cfg = config or _runtime_config
    providers: dict[str, SearchProviderRuntimeConfig] = {}
    for spec in list_provider_specs():
        if not spec.runtime_supported:
            continue
        api_key, source = _resolve_api_key(cfg, spec.provider_id, spec.env_key)
        providers[spec.provider_id] = SearchProviderRuntimeConfig(
            provider_id=spec.provider_id,
            active_provider=cfg.provider,
            runtime_supported=spec.runtime_supported,
            requires_api_key=spec.requires_api_key,
            env_key=spec.env_key,
            capabilities=spec.capabilities,
            credential_source=source,
            api_key=api_key,
            proxy=cfg.proxy,
            use_env_proxy=cfg.use_env_proxy,
            diagnostics=cfg.diagnostics,
            fallback_policy=cfg.fallback_policy,
        )
    return ResolvedSearchRuntime(config=cfg, providers=providers)


def get_resolved_search_runtime() -> ResolvedSearchRuntime:
    return resolve_search_runtime(_runtime_config)


def _ensure_builtin_search_providers() -> None:
    for module_name in (
        "opensquilla.search.providers.bocha",
        "opensquilla.search.providers.iqs",
        "opensquilla.search.providers.serpdive",
        "opensquilla.search.providers.tavily",
        "opensquilla.search.providers.brave",
        "opensquilla.search.providers.exa",
        "opensquilla.search.providers.duckduckgo",
    ):
        importlib.import_module(module_name)


def _resolve_api_key(
    config: SearchRuntimeConfig,
    provider_id: str,
    spec_env_key: str,
) -> tuple[str, CredentialSource]:
    if provider_id == config.provider and config.api_key:
        return config.api_key, "configured"
    if provider_id == config.provider and config.api_key_env:
        configured_env_value = os.environ.get(config.api_key_env, "").strip()
        if configured_env_value:
            return configured_env_value, "configured_env"
    if spec_env_key:
        spec_env_value = os.environ.get(spec_env_key, "").strip()
        if spec_env_value:
            return spec_env_value, "spec_env"
    return "", "none"


def _required_auto_capabilities(
    *,
    requires_domain_filter: bool = False,
    requires_freshness: bool = False,
) -> tuple[str, ...]:
    capabilities: list[str] = []
    if requires_freshness:
        capabilities.append("freshness")
    if requires_domain_filter:
        capabilities.append("domain_filter")
    return tuple(capabilities)


def _has_required_capabilities(
    provider_capabilities: frozenset[str],
    required_capabilities: tuple[str, ...],
) -> bool:
    return all(capability in provider_capabilities for capability in required_capabilities)


def _is_missing_key_error(error: SearchProviderError) -> bool:
    if error.kind != "auth" or error.status_code is not None:
        return False
    message = error.message.lower()
    return any(
        marker in message
        for marker in (
            "api key not set",
            "key not set",
            "not configured",
            "not set",
            "missing",
            "unset",
        )
    )
