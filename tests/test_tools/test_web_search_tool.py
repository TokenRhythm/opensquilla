from __future__ import annotations

import inspect
import json

import httpx
import pytest

import opensquilla.tools.builtin.web as web_module
from opensquilla.search.types import (
    DEFAULT_SEARCH_MAX_RESULTS,
    SearchOptions,
    SearchProviderError,
    SearchProviderSpec,
    SearchResult,
)


@pytest.mark.asyncio
async def test_web_search_tool_builds_canonical_options_and_returns_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_options: list[SearchOptions] = []

    async def fake_run_canonical_web_search(
        options: SearchOptions,
        **kwargs: object,
    ) -> dict[str, object]:
        seen_options.append(options)
        assert "fetcher" in kwargs
        return {
            "ok": True,
            "query": options.query,
            "mode": options.mode,
            "provider_attempts": [{"provider": "exa", "status": "success"}],
            "diagnostics": {"selected_provider": "exa", "fetched_count": 1},
            "sources": [
                {
                    "rank": 1,
                    "title": "Python release",
                    "url": "https://www.python.org/downloads/",
                    "canonical_url": "https://www.python.org/downloads/",
                    "domain": "www.python.org",
                    "provider": "exa",
                    "fetched": True,
                }
            ],
            "results": [
                {
                    "title": "Python release",
                    "url": "https://www.python.org/downloads/",
                    "excerpt": "Python release notes",
                    "fetched": True,
                }
            ],
        }

    monkeypatch.setattr(
        web_module,
        "run_canonical_web_search",
        fake_run_canonical_web_search,
    )

    bare_web_search = inspect.unwrap(web_module.web_search)
    result = await bare_web_search(
        "python release",
        mode="technical",
        provider="exa",
        max_results=12,
        fetch_top_k=2,
        max_chars_per_source=1200,
        include_domains=["python.org"],
        exclude_domains=["docs.python.org"],
        recency="month",
    )
    payload = json.loads(result)

    assert payload["ok"] is True
    assert payload["provider_attempts"] == [{"provider": "exa", "status": "success"}]
    assert payload["diagnostics"]["fetched_count"] == 1
    assert payload["sources"][0]["url"] == "https://www.python.org/downloads/"
    assert payload["results"][0]["excerpt"] == "Python release notes"
    assert seen_options == [
        SearchOptions(
            query="python release",
            mode="technical",
            max_results=12,
            fetch_top_k=2,
            max_chars_per_source=1200,
            include_domains=("python.org",),
            exclude_domains=("docs.python.org",),
            recency="month",
            provider="exa",
        )
    ]


@pytest.mark.asyncio
async def test_web_search_tool_uses_configured_source_backed_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_options: list[SearchOptions] = []

    async def fake_run_canonical_web_search(
        options: SearchOptions,
        **kwargs: object,
    ) -> dict[str, object]:
        seen_options.append(options)
        return {"ok": True, "query": options.query, "results": []}

    monkeypatch.setattr(
        web_module,
        "run_canonical_web_search",
        fake_run_canonical_web_search,
    )
    monkeypatch.setattr(web_module, "_active_max_results", 7)

    bare_web_search = inspect.unwrap(web_module.web_search)
    payload = json.loads(await bare_web_search("python release", provider="auto"))

    assert payload["ok"] is True
    assert seen_options == [
        SearchOptions(
            query="python release",
            mode="auto",
            max_results=7,
            fetch_top_k=3,
            max_chars_per_source=1500,
            provider=None,
        )
    ]


@pytest.mark.asyncio
async def test_web_search_tool_accepts_bocha_provider_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_options: list[SearchOptions] = []

    async def fake_run_canonical_web_search(
        options: SearchOptions,
        **kwargs: object,
    ) -> dict[str, object]:
        seen_options.append(options)
        return {"ok": True, "query": options.query, "results": []}

    monkeypatch.setattr(
        web_module,
        "run_canonical_web_search",
        fake_run_canonical_web_search,
    )

    bare_web_search = inspect.unwrap(web_module.web_search)
    payload = json.loads(await bare_web_search("python release", provider="bocha"))

    assert payload["ok"] is True
    assert seen_options == [
        SearchOptions(
            query="python release",
            mode="auto",
            max_results=DEFAULT_SEARCH_MAX_RESULTS,
            provider="bocha",
        )
    ]


@pytest.mark.asyncio
async def test_web_search_tool_accepts_iqs_provider_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_options: list[SearchOptions] = []

    async def fake_run_canonical_web_search(
        options: SearchOptions,
        **kwargs: object,
    ) -> dict[str, object]:
        seen_options.append(options)
        return {"ok": True, "query": options.query, "results": []}

    monkeypatch.setattr(
        web_module,
        "run_canonical_web_search",
        fake_run_canonical_web_search,
    )

    bare_web_search = inspect.unwrap(web_module.web_search)
    payload = json.loads(await bare_web_search("python release", provider="iqs"))

    assert payload["ok"] is True
    assert seen_options == [
        SearchOptions(
            query="python release",
            mode="auto",
            max_results=DEFAULT_SEARCH_MAX_RESULTS,
            provider="iqs",
        )
    ]


@pytest.mark.asyncio
async def test_web_search_tool_rejects_sensitive_query_without_calling_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_canonical_web_search(
        options: SearchOptions,
        **kwargs: object,
    ) -> dict[str, object]:
        raise AssertionError("run_canonical_web_search should not be called")

    monkeypatch.setattr(
        web_module,
        "run_canonical_web_search",
        fake_run_canonical_web_search,
    )

    bare_web_search = inspect.unwrap(web_module.web_search)
    result = await bare_web_search("OPENAI_API_KEY=sk-secret-1234567890")
    payload = json.loads(result)

    assert payload["ok"] is False
    assert payload["query"] == "[redacted]"
    assert payload["error_kind"] == "invalid_request"
    assert payload["error_class"] == "SensitiveInput"
    assert "sk-secret" not in result


@pytest.mark.asyncio
async def test_web_search_benchmark_blocklist_blocks_query_without_calling_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_canonical_web_search(
        options: SearchOptions,
        **kwargs: object,
    ) -> dict[str, object]:
        raise AssertionError("run_canonical_web_search should not be called")

    monkeypatch.setenv("OPENSQUILLA_SEARCH_BENCHMARK_BLOCKLIST", "1")
    monkeypatch.setattr(
        web_module,
        "run_canonical_web_search",
        fake_run_canonical_web_search,
    )

    payload = await web_module.run_web_search_payload("draco benchmark")

    assert payload["ok"] is True
    assert payload["blocked_query"] is True
    assert payload["benchmark_blocklist_enabled"] is True
    assert payload["results"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"query": 123}, "query must be a non-empty string."),
        (
            {"query": "python release", "provider": "serpapi"},
            "Invalid provider. Expected one of: auto, bocha, brave, duckduckgo, exa, iqs, serpdive, tavily.",
        ),
        (
            {"query": "python release", "mode": "invalid"},
            "Invalid mode. Expected one of: auto, broad, news, technical.",
        ),
        (
            {"query": "python release", "recency": "hour"},
            "Invalid recency. Expected one of: day, month, week, year.",
        ),
        (
            {"query": "python release", "max_results": "bad"},
            "max_results must be an integer.",
        ),
        (
            {"query": "python release", "include_domains": "example.com"},
            "include_domains must be a list or tuple of strings.",
        ),
        (
            {"query": "python release", "include_domains": [123]},
            "include_domains must be a list or tuple of strings.",
        ),
        (
            {"query": "python release", "exclude_domains": [object()]},
            "exclude_domains must be a list or tuple of strings.",
        ),
    ],
)
async def test_web_search_tool_rejects_invalid_args_without_calling_core(
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, object],
    message: str,
) -> None:
    async def fake_run_canonical_web_search(options: SearchOptions) -> dict[str, object]:
        raise AssertionError("run_canonical_web_search should not be called")

    monkeypatch.setattr(
        web_module,
        "run_canonical_web_search",
        fake_run_canonical_web_search,
    )

    bare_web_search = inspect.unwrap(web_module.web_search)
    result = await bare_web_search(**kwargs)  # type: ignore[arg-type]
    payload = json.loads(result)

    assert payload == {
        "ok": False,
        "error_kind": "invalid_request",
        "error": message,
        "retry_allowed": False,
    }


@pytest.mark.asyncio
async def test_web_discover_keeps_lightweight_result_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_web_discover_payload(
        query: str,
        max_results: int | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        assert query == "python release"
        assert max_results == 3
        return {
            "ok": True,
            "query": query,
            "provider": "duckduckgo",
            "results": [
                {
                    "title": "Python release",
                    "url": "https://www.python.org/downloads/",
                    "snippet": "Release notes",
                }
            ],
        }

    monkeypatch.setattr(
        web_module,
        "run_web_discover_payload",
        fake_run_web_discover_payload,
    )

    bare_web_discover = inspect.unwrap(web_module.web_discover)
    payload = json.loads(await bare_web_discover("python release", max_results=3))

    assert payload == {
        "query": "python release",
        "provider": "duckduckgo",
        "results": [
            {
                "title": "Python release",
                "url": "https://www.python.org/downloads/",
                "snippet": "Release notes",
            }
        ],
    }


@pytest.mark.asyncio
async def test_web_discover_keeps_explicit_failure_marker_for_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_web_discover_payload(
        query: str,
        max_results: int | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        return {
            "ok": False,
            "query": query,
            "provider": "duckduckgo",
            "results": [],
            "error_kind": "blocked",
            "error": {"message": "Search was blocked."},
            "retry_allowed": False,
        }

    monkeypatch.setattr(
        web_module,
        "run_web_discover_payload",
        fake_run_web_discover_payload,
    )

    bare_web_discover = inspect.unwrap(web_module.web_discover)
    payload = json.loads(await bare_web_discover("python release"))

    assert payload["ok"] is False
    assert payload["error_kind"] == "blocked"
    assert payload["error"] == "Search was blocked."
    assert payload["retry_allowed"] is False


@pytest.mark.asyncio
async def test_web_discover_uses_ranked_runtime_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.search.registry as registry

    calls: list[tuple[str, dict[str, object]]] = []

    class FakeProvider:
        async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
            return [
                SearchResult(
                    title=f"{query} result",
                    url="https://example.com",
                    snippet=str(max_results),
                )
            ]

    def fake_get_provider(name: str, **kwargs: object) -> FakeProvider:
        calls.append((name, kwargs))
        return FakeProvider()

    monkeypatch.setenv("BOCHA_SEARCH_API_KEY", "bocha-key")
    monkeypatch.setattr(registry, "get_provider", fake_get_provider)

    try:
        web_module.configure_search("duckduckgo", max_results=4)
        payload = await web_module.run_web_discover_payload("python release", max_results=2)
    finally:
        web_module.reset_search_runtime()

    assert payload["ok"] is True
    assert payload["provider"] == "bocha"
    assert payload["results"][0]["snippet"] == "2"
    assert calls == [
        (
            "bocha",
            {
                "proxy": "",
                "use_env_proxy": False,
                "diagnostics": False,
                "api_key": "bocha-key",
            },
        )
    ]


@pytest.mark.asyncio
async def test_web_discover_benchmark_blocklist_filters_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.search.registry as registry

    class FakeProvider:
        async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
            return [
                SearchResult(
                    title="DRACO dataset",
                    url="https://huggingface.co/datasets/perplexity-ai/draco",
                    snippet="benchmark data",
                ),
                SearchResult(
                    title="Safe result",
                    url="https://example.com/safe",
                    snippet=query,
                ),
            ]

    def fake_get_provider(name: str, **kwargs: object) -> FakeProvider:
        return FakeProvider()

    monkeypatch.setenv("OPENSQUILLA_SEARCH_BENCHMARK_BLOCKLIST", "1")
    monkeypatch.setattr(registry, "get_provider", fake_get_provider)

    try:
        web_module.configure_search("duckduckgo", max_results=4)
        payload = await web_module.run_web_discover_payload("python release", max_results=2)
    finally:
        web_module.reset_search_runtime()

    assert payload["ok"] is True
    assert payload["benchmark_blocklist_enabled"] is True
    assert payload["blocked_count"] == 1
    assert [item["url"] for item in payload["results"]] == ["https://example.com/safe"]


@pytest.mark.asyncio
async def test_web_discover_preserves_network_fallback_for_custom_active_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.search.registry as registry
    from opensquilla.search.registry import register_provider

    custom_provider = "test_custom_discover_fail"
    calls: list[str] = []

    class FailingProvider:
        name = custom_provider

        async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
            raise SearchProviderError(
                provider=custom_provider,
                kind="network",
                message="network down",
                retryable=True,
            )

    class DuckProvider:
        async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
            return [
                SearchResult(
                    title="Duck fallback",
                    url="https://example.com",
                    snippet=query,
                )
            ]

    def fake_get_provider(name: str, **kwargs: object) -> FailingProvider | DuckProvider:
        calls.append(name)
        if name == custom_provider:
            return FailingProvider()
        assert name == "duckduckgo"
        return DuckProvider()

    register_provider(
        custom_provider,
        FailingProvider,
        SearchProviderSpec(provider_id=custom_provider),
    )
    monkeypatch.setattr(registry, "get_provider", fake_get_provider)

    try:
        web_module.configure_search(
            custom_provider,
            fallback_policy="network",
            diagnostics=True,
        )
        payload = await web_module.run_web_discover_payload("python release")
    finally:
        web_module.reset_search_runtime()

    assert payload["ok"] is True
    assert payload["provider"] == "duckduckgo"
    assert payload["fallbackFrom"] == custom_provider
    assert payload["attempts"] == [
        {"provider": custom_provider, "status": "error", "error_kind": "network"},
        {"provider": "duckduckgo", "status": "success"},
    ]
    assert calls == [custom_provider, "duckduckgo"]


@pytest.mark.asyncio
async def test_web_discover_preserves_raw_custom_network_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.search.registry as registry
    from opensquilla.search.registry import register_provider

    custom_provider = "test_custom_discover_raw_network"
    calls: list[str] = []

    class FailingProvider:
        name = custom_provider

        async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
            request = httpx.Request("GET", "https://custom.example/search")
            raise httpx.ConnectError("network down", request=request)

    class DuckProvider:
        name = "duckduckgo"

        async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
            return [
                SearchResult(
                    title="Duck fallback",
                    url="https://example.com",
                    snippet=query,
                )
            ]

    def fake_get_provider(name: str, **kwargs: object) -> FailingProvider | DuckProvider:
        calls.append(name)
        return FailingProvider() if name == custom_provider else DuckProvider()

    register_provider(
        custom_provider,
        FailingProvider,
        SearchProviderSpec(provider_id=custom_provider),
    )
    monkeypatch.setattr(registry, "get_provider", fake_get_provider)

    try:
        web_module.configure_search(
            custom_provider,
            fallback_policy="network",
            diagnostics=True,
        )
        payload = await web_module.run_web_discover_payload("python release")
    finally:
        web_module.reset_search_runtime()

    assert payload["ok"] is True
    assert payload["fallbackFrom"] == custom_provider
    assert payload["attempts"] == [
        {"provider": custom_provider, "status": "error", "error_kind": "network"},
        {"provider": "duckduckgo", "status": "success"},
    ]
    assert calls == [custom_provider, "duckduckgo"]


def test_web_discover_failure_projection_preserves_canonical_diagnostics() -> None:
    payload = web_module._web_discover_payload_from_canonical(
        {
            "ok": False,
            "query": "python release",
            "provider_attempts": [
                {"provider": "custom", "status": "error", "error_kind": "parse"}
            ],
            "diagnostics": {"selected_provider": "", "fallback_from": ""},
            "error_kind": "parse",
            "error_class": "ValueError",
            "error": "custom search request failed.",
            "provider_retryable": False,
            "retry_allowed": False,
        },
        display_provider="custom",
    )

    assert payload["ok"] is False
    assert payload["retry_allowed"] is False
    assert payload["error_class"] == "ValueError"
    assert payload["provider_attempts"] == [
        {"provider": "custom", "status": "error", "error_kind": "parse"}
    ]
    assert payload["diagnostics"] == {"selected_provider": "", "fallback_from": ""}
