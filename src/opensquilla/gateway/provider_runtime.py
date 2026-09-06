"""Shared live provider-selector reconciliation for config mutations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opensquilla.provider.selector import ProviderConfig


def resolve_provider_selector_config(config: Any) -> ProviderConfig | None:
    llm_config = getattr(config, "llm", None)
    if llm_config is None:
        return None

    from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config
    from opensquilla.provider.selector import ProviderConfig

    runtime = resolve_llm_runtime_config(config)
    return ProviderConfig(
        provider=runtime.provider,
        model=runtime.model,
        api_key=runtime.api_key,
        base_url=runtime.base_url,
        proxy=runtime.proxy,
        provider_routing=runtime.provider_routing,
    )


def sync_resolved_provider_selector(holder: Any, provider_config: Any | None) -> None:
    if provider_config is None:
        return
    selector = getattr(holder, "provider_selector", None)
    if selector is None or not hasattr(selector, "sync_primary"):
        return
    selector.sync_primary(provider_config)


def sync_provider_selector(holder: Any, config: Any) -> None:
    sync_resolved_provider_selector(holder, resolve_provider_selector_config(config))


__all__ = [
    "resolve_provider_selector_config",
    "sync_provider_selector",
    "sync_resolved_provider_selector",
]
