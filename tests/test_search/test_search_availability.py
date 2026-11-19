from __future__ import annotations

from collections.abc import Iterator

import pytest

import opensquilla.search.canonical as canonical
import opensquilla.search.registry as registry
import opensquilla.search.runtime_config as runtime_config
from opensquilla.search.types import SearchOptions, SearchProviderError, SearchResult
from opensquilla.tools.builtin.web import run_web_discover_payload, run_web_search_payload

KEYED_PROVIDERS = {
    "bocha": "BOCHA_SEARCH_API_KEY",
    "brave": "BRAVE_SEARCH_API_KEY",
    "exa": "EXA_API_KEY",
    "iqs": "IQS_SEARCH_API_KEY",
    "tavily": "TAVILY_API_KEY",
}


@pytest.fixture(autouse=True)
def isolated_search_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for env_key in KEYED_PROVIDERS.values():
        monkeypatch.delenv(env_key, raising=False)
    monkeypatch.setattr(runtime_config, "_runtime_config", runtime_config.SearchRuntimeConfig())
    canonical.clear_canonical_web_search_cache_for_tests()
    yield
    canonical.clear_canonical_web_search_cache_for_tests()


@pytest.mark.parametrize("provider", KEYED_PROVIDERS)
@pytest.mark.parametrize("fallback_policy", ["off", "network"])
async def test_explicit_missing_key_never_constructs_or_calls_provider(
    provider: str,
    fallback_policy: str,
) -> None:
    runtime = runtime_config.resolve_search_runtime(
        runtime_config.SearchRuntimeConfig(fallback_policy=fallback_policy)
    )

    def unexpected_factory(name: str) -> None:
        pytest.fail(f"Unavailable provider was constructed: {name}")

    payload = await canonical.run_canonical_web_search(
        SearchOptions(query="synthetic release information", provider=provider),
        runtime=runtime,
        provider_factory=unexpected_factory,
    )

    assert payload["ok"] is False
    assert payload["error_kind"] == "auth"
    assert payload["provider_attempts"] == [{"provider": provider, "status": "auth_missing"}]
    assert payload["retry_allowed"] is False
    assert "API key is not configured" in payload["error"]
    assert "Do not retry" in payload["error"]


async def test_explicit_keenable_without_opt_in_never_constructs_provider() -> None:
    runtime = runtime_config.resolve_search_runtime(runtime_config.SearchRuntimeConfig())

    def unexpected_factory(name: str) -> None:
        pytest.fail(f"Opt-in provider was constructed without opt-in: {name}")

    payload = await canonical.run_canonical_web_search(
        SearchOptions(query="synthetic release information", provider="keenable"),
        runtime=runtime,
        provider_factory=unexpected_factory,
    )

    assert payload["ok"] is False
    assert payload["provider_attempts"] == [
        {"provider": "keenable", "status": "error", "error_kind": "unknown"}
    ]
    with pytest.raises(SearchProviderError, match="opt-in"):
        runtime.require_available("keenable")


@pytest.mark.parametrize("provider", KEYED_PROVIDERS)
async def test_runtime_builder_rejects_missing_key_before_factory(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    def unexpected_factory(*args: object, **kwargs: object) -> None:
        pytest.fail("Provider factory must not run without credentials")

    monkeypatch.setattr(registry, "get_provider", unexpected_factory)
    runtime = runtime_config.get_resolved_search_runtime()
    with pytest.raises(SearchProviderError) as exc_info:
        runtime.build_provider(provider)
    assert exc_info.value.kind == "auth"
    assert exc_info.value.retryable is False


@pytest.mark.parametrize("surface", ["search", "discover"])
@pytest.mark.parametrize("provider", KEYED_PROVIDERS)
async def test_web_tool_entrypoints_reject_missing_key_without_factory(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    provider: str,
) -> None:
    def unexpected_factory(*args: object, **kwargs: object) -> None:
        pytest.fail("Web tool must reject missing credentials before constructing providers")

    monkeypatch.setattr(registry, "get_provider", unexpected_factory)
    if surface == "search":
        payload = await run_web_search_payload("synthetic changelog", provider=provider)
    else:
        payload = await run_web_discover_payload("synthetic changelog", provider_name=provider)
    assert payload["ok"] is False
    assert payload["error_kind"] == "auth"
    assert payload["provider_attempts"] == [{"provider": provider, "status": "auth_missing"}]
    assert payload["retry_allowed"] is False


async def test_explicit_cached_search_rechecks_removed_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "synthetic-search-key")

    class FakeProvider:
        async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
            return [SearchResult(title="Release", url="https://example.com/release", snippet="New")]

    options = SearchOptions(query="synthetic cache credentials", provider="brave")
    first = await canonical.run_canonical_web_search(
        options,
        provider_factory=lambda name: FakeProvider(),
        use_cache=True,
    )
    assert first["ok"] is True
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY")

    def unexpected_factory(name: str) -> None:
        pytest.fail("Removed credentials must be rejected before constructing the provider")

    second = await canonical.run_canonical_web_search(
        options,
        provider_factory=unexpected_factory,
        use_cache=True,
    )
    assert second["ok"] is False
    assert second["error_kind"] == "auth"
    assert second["provider_attempts"] == [{"provider": "brave", "status": "auth_missing"}]


async def test_unknown_provider_still_returns_structured_failure() -> None:
    payload = await canonical.run_canonical_web_search(
        SearchOptions(query="synthetic unknown provider", provider="unknown-provider")
    )
    assert payload["ok"] is False
    assert payload["error_kind"] == "unknown"
    assert payload["retry_allowed"] is False
