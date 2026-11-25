"""Search provider registry and factory."""

from __future__ import annotations

from opensquilla.search.types import SearchProvider, SearchProviderSpec

_providers: dict[str, type[SearchProvider]] = {}
_provider_specs: dict[str, SearchProviderSpec] = {
    "bocha": SearchProviderSpec(
        provider_id="bocha",
        requires_api_key=True,
        env_key="BOCHA_SEARCH_API_KEY",
        capabilities=frozenset({"web", "freshness", "content"}),
    ),
    "brave": SearchProviderSpec(
        provider_id="brave",
        requires_api_key=True,
        env_key="BRAVE_SEARCH_API_KEY",
        capabilities=frozenset({"web", "freshness"}),
    ),
    "duckduckgo": SearchProviderSpec(
        provider_id="duckduckgo",
        capabilities=frozenset({"web", "no_key"}),
    ),
    "iqs": SearchProviderSpec(
        provider_id="iqs",
        requires_api_key=True,
        env_key="IQS_SEARCH_API_KEY",
        capabilities=frozenset({"web", "freshness", "domain_filter", "content"}),
    ),
    "serpdive": SearchProviderSpec(
        provider_id="serpdive",
        requires_api_key=True,
        env_key="SERPDIVE_API_KEY",
        capabilities=frozenset({"web", "content"}),
    ),
    "tavily": SearchProviderSpec(
        provider_id="tavily",
        requires_api_key=True,
        env_key="TAVILY_API_KEY",
        capabilities=frozenset({"web", "freshness", "domain_filter"}),
    ),
    "exa": SearchProviderSpec(
        provider_id="exa",
        requires_api_key=True,
        env_key="EXA_API_KEY",
        capabilities=frozenset({"web", "freshness", "domain_filter", "semantic", "content"}),
    ),
    "perplexity": SearchProviderSpec(
        provider_id="perplexity",
        runtime_supported=False,
        requires_api_key=True,
        env_key="PERPLEXITY_API_KEY",
    ),
}


def register_provider(
    name: str,
    cls: type[SearchProvider],
    spec: SearchProviderSpec | None = None,
) -> None:
    """Register a runtime provider class.

    ``spec`` is optional to keep the historical ``register_provider(name, cls)``
    call shape working for tests and third-party callers.
    """

    _providers[name] = cls
    if spec is not None:
        _provider_specs[name] = spec
    elif name not in _provider_specs:
        _provider_specs[name] = SearchProviderSpec(provider_id=name)


def get_provider(name: str, **kwargs) -> SearchProvider:
    if name not in _providers:
        available = ", ".join(_providers.keys()) if _providers else "none"
        raise ValueError(f"Unknown search provider '{name}'. Available: {available}")
    return _providers[name](**kwargs)


def list_providers() -> list[str]:
    return sorted(_providers.keys())


def list_provider_specs() -> tuple[SearchProviderSpec, ...]:
    return tuple(_provider_specs[name] for name in sorted(_provider_specs))


def get_provider_spec(name: str) -> SearchProviderSpec:
    try:
        return _provider_specs[name]
    except KeyError as exc:
        available = ", ".join(sorted(_provider_specs))
        raise ValueError(f"Unknown search provider '{name}'. Available: {available}") from exc
