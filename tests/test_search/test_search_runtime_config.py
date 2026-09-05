from __future__ import annotations

import sys

from opensquilla.search.providers.exa import ExaSearchProvider
from opensquilla.search.runtime_config import SearchRuntimeConfig, resolve_search_runtime
from opensquilla.search.types import SearchOptions, SearchProviderError


def _clear_search_env(monkeypatch) -> None:
    for key in (
        "BOCHA_SEARCH_API_KEY",
        "BRAVE_SEARCH_API_KEY",
        "IQS_SEARCH_API_KEY",
        "SERPDIVE_API_KEY",
        "TAVILY_API_KEY",
        "EXA_API_KEY",
        "CUSTOM_EXA_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


def test_resolver_no_key_default_uses_duckduckgo_without_keyed_attempts(monkeypatch) -> None:
    _clear_search_env(monkeypatch)

    runtime = resolve_search_runtime(SearchRuntimeConfig(provider="duckduckgo"))

    assert runtime.provider_order(SearchOptions(query="q")) == ("duckduckgo",)
    duckduckgo = runtime.provider_config("duckduckgo")
    assert duckduckgo.available is True
    assert duckduckgo.credential_source == "none"
    assert runtime.provider_config("tavily").available is False


def test_resolver_uses_configured_env_for_active_provider(monkeypatch) -> None:
    _clear_search_env(monkeypatch)
    monkeypatch.setenv("CUSTOM_EXA_KEY", "env-exa-key")

    runtime = resolve_search_runtime(
        SearchRuntimeConfig(provider="exa", api_key_env="CUSTOM_EXA_KEY")
    )

    exa = runtime.provider_config("exa")
    assert exa.available is True
    assert exa.credential_source == "configured_env"
    assert exa.provider_kwargs()["api_key"] == "env-exa-key"


def test_resolver_prefers_configured_exa_key_over_configured_env(monkeypatch) -> None:
    _clear_search_env(monkeypatch)
    monkeypatch.setenv("CUSTOM_EXA_KEY", "env-exa-key")

    runtime = resolve_search_runtime(
        SearchRuntimeConfig(
            provider="exa",
            api_key="configured-exa-key",
            api_key_env="CUSTOM_EXA_KEY",
        )
    )

    exa = runtime.provider_config("exa")
    assert exa.available is True
    assert exa.credential_source == "configured"
    assert exa.provider_kwargs()["api_key"] == "configured-exa-key"

    provider = runtime.build_provider("exa")
    assert isinstance(provider, ExaSearchProvider)
    assert provider._api_key == "configured-exa-key"


def test_resolver_partial_key_orders_configured_provider_then_duckduckgo(monkeypatch) -> None:
    _clear_search_env(monkeypatch)

    runtime = resolve_search_runtime(
        SearchRuntimeConfig(provider="brave", api_key="brave-key")
    )

    assert runtime.provider_order(SearchOptions(query="q")) == ("brave", "duckduckgo")


def test_resolver_bocha_default_env_orders_bocha_then_duckduckgo(monkeypatch) -> None:
    _clear_search_env(monkeypatch)
    monkeypatch.setenv("BOCHA_SEARCH_API_KEY", "bocha-key")

    runtime = resolve_search_runtime(SearchRuntimeConfig(provider="duckduckgo"))

    assert runtime.provider_order(SearchOptions(query="q")) == ("bocha", "duckduckgo")
    bocha = runtime.provider_config("bocha")
    assert bocha.available is True
    assert bocha.credential_source == "spec_env"


def test_resolver_iqs_default_env_orders_iqs_then_duckduckgo(monkeypatch) -> None:
    _clear_search_env(monkeypatch)
    monkeypatch.setenv("IQS_SEARCH_API_KEY", "iqs-key")

    runtime = resolve_search_runtime(SearchRuntimeConfig(provider="duckduckgo"))

    assert runtime.provider_order(SearchOptions(query="q")) == ("iqs", "duckduckgo")
    iqs = runtime.provider_config("iqs")
    assert iqs.available is True
    assert iqs.credential_source == "spec_env"


def test_resolver_all_key_mode_tie_breakers(monkeypatch) -> None:
    _clear_search_env(monkeypatch)
    monkeypatch.setenv("BOCHA_SEARCH_API_KEY", "bocha-key")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    monkeypatch.setenv("EXA_API_KEY", "exa-key")

    runtime = resolve_search_runtime(SearchRuntimeConfig(provider="duckduckgo"))

    assert runtime.provider_order(SearchOptions(query="q")) == (
        "bocha",
        "tavily",
        "brave",
        "exa",
        "duckduckgo",
    )
    assert runtime.provider_order(SearchOptions(query="q", mode="technical")) == (
        "exa",
        "bocha",
        "brave",
        "tavily",
        "duckduckgo",
    )
    assert runtime.provider_order(SearchOptions(query="q", recency="week")) == (
        "bocha",
        "tavily",
        "brave",
        "exa",
        "duckduckgo",
    )
    assert runtime.provider_order(SearchOptions(query="q", mode="news")) == (
        "bocha",
        "tavily",
        "brave",
        "exa",
        "duckduckgo",
    )


def test_resolver_all_key_mode_tie_breakers_with_iqs(monkeypatch) -> None:
    _clear_search_env(monkeypatch)
    monkeypatch.setenv("BOCHA_SEARCH_API_KEY", "bocha-key")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.setenv("IQS_SEARCH_API_KEY", "iqs-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    monkeypatch.setenv("EXA_API_KEY", "exa-key")

    runtime = resolve_search_runtime(SearchRuntimeConfig(provider="duckduckgo"))

    assert runtime.provider_order(SearchOptions(query="q")) == (
        "bocha",
        "tavily",
        "iqs",
        "brave",
        "exa",
        "duckduckgo",
    )
    assert runtime.provider_order(SearchOptions(query="q", mode="technical")) == (
        "exa",
        "bocha",
        "brave",
        "tavily",
        "iqs",
        "duckduckgo",
    )
    assert runtime.provider_order(SearchOptions(query="q", recency="week")) == (
        "bocha",
        "tavily",
        "iqs",
        "brave",
        "exa",
        "duckduckgo",
    )
    assert runtime.provider_order(
        SearchOptions(query="q", include_domains=("python.org",))
    ) == ("tavily", "iqs", "exa")


def test_execution_plan_bounds_auto_network_without_truncating_provider_order(
    monkeypatch,
) -> None:
    _clear_search_env(monkeypatch)
    monkeypatch.setenv("BOCHA_SEARCH_API_KEY", "bocha-key")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.setenv("IQS_SEARCH_API_KEY", "iqs-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    runtime = resolve_search_runtime(
        SearchRuntimeConfig(provider="duckduckgo", fallback_policy="network")
    )
    options = SearchOptions(query="q")

    assert runtime.provider_order(options) == (
        "bocha",
        "tavily",
        "iqs",
        "brave",
        "exa",
        "duckduckgo",
    )
    plan = runtime.execution_plan(options)
    assert plan.provider_names == ("bocha", "tavily")
    assert plan.planned_provider_ids == ("bocha", "tavily")
    assert plan.primary_provider == "bocha"
    assert plan.fallback_provider == "tavily"
    assert plan.selection_mode == "automatic"
    assert plan.fallback_mode == "network"


def test_execution_plan_auto_network_uses_next_capability_matched_provider(
    monkeypatch,
) -> None:
    _clear_search_env(monkeypatch)
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.setenv("IQS_SEARCH_API_KEY", "iqs-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    runtime = resolve_search_runtime(
        SearchRuntimeConfig(provider="duckduckgo", fallback_policy="network")
    )
    options = SearchOptions(query="q", include_domains=("python.org",))

    assert runtime.provider_order(options) == ("tavily", "iqs", "exa")
    assert runtime.execution_plan(options).provider_names == ("tavily", "iqs")


def test_execution_plan_auto_network_uses_duckduckgo_when_no_keyed_candidate(
    monkeypatch,
) -> None:
    _clear_search_env(monkeypatch)
    runtime = resolve_search_runtime(
        SearchRuntimeConfig(provider="duckduckgo", fallback_policy="network")
    )

    assert runtime.execution_plan(SearchOptions(query="q")).provider_names == (
        "duckduckgo",
    )


def test_execution_plan_auto_network_uses_duckduckgo_when_no_keyed_backup(
    monkeypatch,
) -> None:
    _clear_search_env(monkeypatch)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    runtime = resolve_search_runtime(
        SearchRuntimeConfig(provider="duckduckgo", fallback_policy="network")
    )
    options = SearchOptions(query="q", include_domains=("python.org",))

    assert runtime.provider_order(options) == ("tavily",)
    assert runtime.execution_plan(options).provider_names == (
        "tavily",
        "duckduckgo",
    )


def test_execution_plan_explicit_network_preserves_duckduckgo_fallback(monkeypatch) -> None:
    _clear_search_env(monkeypatch)
    runtime = resolve_search_runtime(
        SearchRuntimeConfig(
            provider="tavily",
            api_key="tavily-key",
            fallback_policy="network",
        )
    )

    plan = runtime.execution_plan(SearchOptions(query="q", provider="tavily"))
    assert plan.provider_names == ("tavily", "duckduckgo")
    assert plan.selection_mode == "explicit"
    assert plan.fallback_mode == "network"


def test_execution_plan_off_preselects_only_one_auto_auth_skip(monkeypatch) -> None:
    _clear_search_env(monkeypatch)
    monkeypatch.setenv("BOCHA_SEARCH_API_KEY", "bocha-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    runtime = resolve_search_runtime(
        SearchRuntimeConfig(provider="duckduckgo", fallback_policy="off")
    )
    options = SearchOptions(query="q")

    assert runtime.provider_order(options) == ("bocha", "tavily", "duckduckgo")
    plan = runtime.execution_plan(options)
    assert plan.provider_names == ("bocha", "tavily")
    assert plan.fallback_mode == "auth_missing"


def test_off_policy_only_allows_auto_dynamic_auth_missing_skip(monkeypatch) -> None:
    _clear_search_env(monkeypatch)
    runtime = resolve_search_runtime(
        SearchRuntimeConfig(provider="duckduckgo", fallback_policy="off")
    )
    error = SearchProviderError(
        provider="bocha",
        kind="auth",
        message="Bocha API key not set",
        retryable=False,
    )

    assert runtime.should_fallback(error, explicit_provider=False) is True
    assert runtime.should_fallback(error, explicit_provider=True) is False


def test_network_policy_requires_transient_classification_and_provider_permission(
    monkeypatch,
) -> None:
    _clear_search_env(monkeypatch)
    runtime = resolve_search_runtime(
        SearchRuntimeConfig(provider="duckduckgo", fallback_policy="network")
    )

    for error in (
        SearchProviderError("bocha", "blocked", "terminal", retryable=True),
        SearchProviderError("bocha", "parse", "terminal", retryable=True),
        SearchProviderError("bocha", "unknown", "terminal", retryable=True),
    ):
        assert runtime.should_fallback(
            error,
            explicit_provider=False,
        ) is False
    assert runtime.should_fallback(
        SearchProviderError(
            provider="bocha",
            kind="blocked",
            message="challenge surfaced as server error",
            retryable=True,
            status_code=500,
        ),
        explicit_provider=False,
    ) is False
    assert runtime.should_fallback(
        SearchProviderError(
            provider="bocha",
            kind="http",
            message="bad request",
            retryable=True,
            status_code=400,
        ),
        explicit_provider=False,
    ) is False
    assert runtime.should_fallback(
        SearchProviderError(
            provider="bocha",
            kind="rate_limit",
            message="rate limited without HTTP status",
            retryable=True,
        ),
        explicit_provider=False,
    ) is False
    assert runtime.should_fallback(
        SearchProviderError(
            provider="bocha",
            kind="rate_limit",
            message="rate limited",
            retryable=True,
            status_code=429,
        ),
        explicit_provider=False,
    ) is True
    assert runtime.should_fallback(
        SearchProviderError(
            provider="bocha",
            kind="http",
            message="server error",
            retryable=False,
            status_code=500,
        ),
        explicit_provider=False,
    ) is False
    assert runtime.should_fallback(
        SearchProviderError(
            provider="bocha",
            kind="http",
            message="server error",
            retryable=True,
            status_code=500,
        ),
        explicit_provider=False,
    ) is True


def test_resolver_domain_constrained_auto_prefers_domain_filter_providers(
    monkeypatch,
) -> None:
    _clear_search_env(monkeypatch)
    monkeypatch.setenv("BOCHA_SEARCH_API_KEY", "bocha-key")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    monkeypatch.setenv("EXA_API_KEY", "exa-key")

    runtime = resolve_search_runtime(SearchRuntimeConfig(provider="duckduckgo"))

    assert runtime.provider_order(
        SearchOptions(query="q", include_domains=("python.org",))
    ) == ("tavily", "exa")
    assert runtime.provider_order(
        SearchOptions(query="q", exclude_domains=("spam.example",))
    ) == ("tavily", "exa")


def test_resolver_domain_constrained_technical_prefers_exa_then_tavily(
    monkeypatch,
) -> None:
    _clear_search_env(monkeypatch)
    monkeypatch.setenv("BOCHA_SEARCH_API_KEY", "bocha-key")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    monkeypatch.setenv("EXA_API_KEY", "exa-key")

    runtime = resolve_search_runtime(SearchRuntimeConfig(provider="duckduckgo"))

    assert runtime.provider_order(
        SearchOptions(
            query="q",
            mode="technical",
            include_domains=("python.org",),
        )
    ) == ("exa", "tavily")


def test_resolver_domain_constrained_freshness_skips_bocha_without_domain_filter(
    monkeypatch,
) -> None:
    _clear_search_env(monkeypatch)
    monkeypatch.setenv("BOCHA_SEARCH_API_KEY", "bocha-key")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    monkeypatch.setenv("EXA_API_KEY", "exa-key")

    runtime = resolve_search_runtime(SearchRuntimeConfig(provider="duckduckgo"))

    assert runtime.provider_order(
        SearchOptions(
            query="q",
            include_domains=("python.org",),
            recency="week",
        )
    ) == ("tavily", "exa")


def test_resolver_domain_constrained_bocha_only_uses_duckduckgo_local_filter(
    monkeypatch,
) -> None:
    _clear_search_env(monkeypatch)
    monkeypatch.setenv("BOCHA_SEARCH_API_KEY", "bocha-key")

    runtime = resolve_search_runtime(SearchRuntimeConfig(provider="duckduckgo"))

    assert runtime.provider_order(
        SearchOptions(query="q", include_domains=("python.org",))
    ) == ("duckduckgo",)


def test_resolver_provider_kwargs_include_proxy_and_diagnostics(monkeypatch) -> None:
    _clear_search_env(monkeypatch)

    runtime = resolve_search_runtime(
        SearchRuntimeConfig(
            provider="duckduckgo",
            proxy="http://proxy.test",
            use_env_proxy=True,
            diagnostics=True,
        )
    )

    kwargs = runtime.provider_config("duckduckgo").provider_kwargs()
    assert kwargs == {
        "proxy": "http://proxy.test",
        "use_env_proxy": True,
        "diagnostics": True,
    }


def test_runtime_build_provider_registers_builtin_providers_in_fresh_process(
    monkeypatch,
) -> None:
    import opensquilla.search.registry as registry

    for module_name in (
        "opensquilla.search.providers.bocha",
        "opensquilla.search.providers.iqs",
        "opensquilla.search.providers.tavily",
        "opensquilla.search.providers.brave",
        "opensquilla.search.providers.exa",
        "opensquilla.search.providers.duckduckgo",
    ):
        monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.setattr(registry, "_providers", {})

    runtime = resolve_search_runtime(
        SearchRuntimeConfig(provider="bocha", api_key="bocha-key")
    )

    provider = runtime.build_provider("bocha")

    assert provider.__class__.__name__ == "BochaSearchProvider"
    assert provider.name == "bocha"
