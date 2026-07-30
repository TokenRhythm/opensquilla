from __future__ import annotations

import sys
import types
from typing import Any

import pytest

import opensquilla.search.canonical as canonical_module
from opensquilla.search.canonical import run_canonical_web_search
from opensquilla.search.runtime_config import SearchRuntimeConfig, resolve_search_runtime
from opensquilla.search.types import SearchOptions, SearchProviderError, SearchResult


class FakeProvider:
    name = "tavily"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        return [
            SearchResult(
                title="Python release",
                url="https://www.python.org/downloads/release/python-3135/?utm_source=x",
                snippet="Python release announcement",
                provider="tavily",
                source="tavily",
                published_at="2026-06-11",
                score=0.9,
                content="Python release announcement with enough content for an excerpt.",
            ),
            SearchResult(
                title="Duplicate",
                url="https://www.python.org/downloads/release/python-3135/#notes",
                snippet="Duplicate announcement",
                provider="tavily",
                source="tavily",
            ),
        ][:max_results]


class AuthFailProvider:
    name = "tavily"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        raise SearchProviderError("tavily", "auth", "Tavily auth failed", retryable=False)


class MissingKeyAuthProvider:
    name = "tavily"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        raise SearchProviderError(
            "tavily",
            "auth",
            "Tavily API key not set",
            retryable=False,
            status_code=None,
        )


class ConfiguredBadKeyAuthProvider:
    name = "tavily"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        raise SearchProviderError(
            "tavily",
            "auth",
            "raw secret sk-test leaked",
            retryable=False,
            status_code=401,
        )


class SensitiveErrorProvider:
    name = "tavily"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        raise SearchProviderError(
            "tavily",
            "http",
            "secret token sk-test url https://example.com?api_key=abc raw body",
            retryable=False,
        )


class ShortContentProvider:
    name = "tavily"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        return [
            SearchResult(
                title="Fetched source",
                url="https://example.com/article",
                snippet="Short provider snippet.",
                provider="tavily",
                source="tavily",
                content="Tiny.",
            )
        ][:max_results]


class SnippetProvider:
    name = "tavily"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        return [
            SearchResult(
                title="Fallback source",
                url="https://example.com/fallback",
                snippet="Provider snippet remains available.",
                provider="tavily",
                source="tavily",
            )
        ][:max_results]


class NetworkFailProvider:
    name = "tavily"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        raise SearchProviderError("tavily", "network", "Network failed", retryable=True)


class NamedNetworkFailProvider:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self._calls = calls

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        self._calls.append(self.name)
        raise SearchProviderError(
            self.name,
            "network",
            f"{self.name} network failed",
            retryable=True,
        )


class NamedSuccessProvider:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self._calls = calls

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        self._calls.append(self.name)
        return [
            SearchResult(
                title=f"{self.name} result",
                url=f"https://{self.name}.example/result",
                snippet=f"{self.name} snippet",
                provider=self.name,
                source=self.name,
            )
        ][:max_results]


class NonRetryableHttpProvider:
    name = "tavily"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        raise SearchProviderError(
            self.name,
            "http",
            "Bad request",
            retryable=False,
            status_code=400,
        )


class BlockedProvider:
    def __init__(self, name: str = "duckduckgo") -> None:
        self.name = name

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        raise SearchProviderError(
            self.name,
            "blocked",
            "Provider returned an anti-bot challenge",
            retryable=True,
        )


class EmptyProvider:
    name = "duckduckgo"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        return []


class FallbackProvider:
    name = "duckduckgo"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        return [
            SearchResult(
                title="Fallback result",
                url="https://example.org/result",
                snippet="Fallback snippet",
                provider="duckduckgo",
                source="duckduckgo",
            )
        ][:max_results]


class QueryCaptureProvider:
    name = "duckduckgo"

    def __init__(self, calls: list[tuple[str, str, int]]) -> None:
        self._calls = calls

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        self._calls.append((self.name, query, max_results))
        return [
            SearchResult(
                title="Fresh-ish fallback result",
                url="https://example.org/result",
                snippet="Fallback snippet",
                provider="duckduckgo",
                source="duckduckgo",
            )
        ][:max_results]


class UsefulTopResultsProvider:
    name = "tavily"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        useful_content = "Useful provider content. " * 12
        return [
            SearchResult(
                title="Useful first",
                url="https://example.com/first",
                snippet="First snippet",
                provider="tavily",
                source="tavily",
                content=useful_content,
            ),
            SearchResult(
                title="Useful second",
                url="https://example.com/second",
                snippet="Second snippet",
                provider="tavily",
                source="tavily",
                content=useful_content,
            ),
            SearchResult(
                title="Short third",
                url="https://example.com/third",
                snippet="Third snippet",
                provider="tavily",
                source="tavily",
                content="Short.",
            ),
        ][:max_results]


class DomainFilteringProvider:
    name = "tavily"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        return [
            SearchResult(
                title="Allowed exact",
                url="https://python.org/about",
                snippet="Allowed exact domain",
                provider="tavily",
                source="tavily",
                content="Short.",
            ),
            SearchResult(
                title="Allowed subdomain",
                url="https://www.python.org/downloads",
                snippet="Allowed subdomain",
                provider="tavily",
                source="tavily",
                content="Short.",
            ),
            SearchResult(
                title="Blocked suffix lookalike",
                url="https://notpython.org/article",
                snippet="Must not match python.org",
                provider="tavily",
                source="tavily",
                content="Short.",
            ),
            SearchResult(
                title="Excluded docs",
                url="https://docs.python.org/3/",
                snippet="Explicitly excluded subdomain",
                provider="tavily",
                source="tavily",
                content="Short.",
            ),
        ][:max_results]


class RecencyAwareProvider:
    name = "tavily"

    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self._calls = calls

    async def search(
        self,
        query: str,
        max_results: int = 5,
        *,
        recency: str | None = None,
    ) -> list[SearchResult]:
        self._calls.append((query, {"max_results": max_results, "recency": recency}))
        return [
            SearchResult(
                title="Fresh result",
                url="https://example.com/fresh",
                snippet="Fresh snippet",
                provider="tavily",
                source="tavily",
            )
        ]


class RootDomainSpamProvider:
    name = "tavily"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        return [
            SearchResult(
                title=f"Example result {index}",
                url=url,
                snippet="Short.",
                provider="tavily",
                source="tavily",
            )
            for index, url in enumerate(
                (
                    "https://www.example.com/a",
                    "https://docs.example.com/b",
                    "https://blog.example.com/c",
                    "https://news.example.com/d",
                    "https://python.org/e",
                ),
                start=1,
            )
        ][:max_results]


class CountingShortProvider:
    name = "tavily"

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        self._calls.append(query)
        return [
            SearchResult(
                title="Cached result",
                url="https://example.com/cached",
                snippet="Short.",
                provider="tavily",
                source="tavily",
            )
        ]


@pytest.mark.asyncio
async def test_canonical_web_search_dedupes_and_uses_provider_content_without_fetch() -> None:
    payload = await run_canonical_web_search(
        SearchOptions(query="python release", max_results=5, fetch_top_k=0),
        provider_factory=lambda name: FakeProvider(),
    )

    assert payload["ok"] is True
    assert payload["query"] == "python release"
    assert payload["results"][0]["provider"] == "tavily"
    assert payload["results"][0]["domain"] == "www.python.org"
    assert (
        payload["results"][0]["canonical_url"]
        == "https://www.python.org/downloads/release/python-3135/"
    )
    assert payload["results"][0]["published_at"] == "2026-06-11"
    assert payload["results"][0]["rank"] == 1
    assert payload["diagnostics"]["duplicate_count"] == 1
    assert payload["results"][0]["excerpt"].startswith("Python release announcement")
    assert payload["results"][0]["fetched"] is False
    assert "raw_metadata" not in payload["results"][0]
    assert payload["sources"] == [
        {
            "rank": 1,
            "title": "Python release",
            "url": "https://www.python.org/downloads/release/python-3135/?utm_source=x",
            "canonical_url": "https://www.python.org/downloads/release/python-3135/",
            "domain": "www.python.org",
            "provider": "tavily",
            "fetched": False,
            "fetch_status": "not_requested",
        }
    ]


@pytest.mark.asyncio
async def test_canonical_web_search_primary_auth_failure_does_not_silent_fallback() -> None:
    payload = await run_canonical_web_search(
        SearchOptions(query="python release", provider="tavily"),
        provider_factory=lambda name: AuthFailProvider(),
    )

    assert payload["ok"] is False
    assert payload["error_kind"] == "auth"
    assert payload["provider_retryable"] is False
    assert payload["retry_allowed"] is False
    assert payload["provider_attempts"] == [{"provider": "tavily", "status": "auth_failed"}]


@pytest.mark.asyncio
async def test_canonical_web_search_no_key_auto_skips_known_missing_key_providers(
    monkeypatch,
) -> None:
    for key in (
        "BRAVE_SEARCH_API_KEY",
        "TAVILY_API_KEY",
        "EXA_API_KEY",
        "BOCHA_SEARCH_API_KEY",
        "IQS_SEARCH_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    def provider_factory(name: str) -> MissingKeyAuthProvider | FallbackProvider:
        if name in {"tavily", "brave"}:
            return MissingKeyAuthProvider()
        return FallbackProvider()

    payload = await run_canonical_web_search(
        SearchOptions(query="q", fetch_top_k=0),
        runtime=resolve_search_runtime(SearchRuntimeConfig(provider="duckduckgo")),
        provider_factory=provider_factory,
    )

    assert payload["ok"] is True
    assert payload["provider_attempts"] == [
        {"provider": "duckduckgo", "status": "success"},
    ]


@pytest.mark.asyncio
async def test_canonical_web_search_no_key_default_uses_duckduckgo_directly(monkeypatch) -> None:
    for key in (
        "BRAVE_SEARCH_API_KEY",
        "TAVILY_API_KEY",
        "EXA_API_KEY",
        "BOCHA_SEARCH_API_KEY",
        "IQS_SEARCH_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    attempted: list[str] = []

    def provider_factory(name: str) -> FallbackProvider:
        attempted.append(name)
        return FallbackProvider()

    payload = await run_canonical_web_search(
        SearchOptions(query="q", fetch_top_k=0),
        runtime=resolve_search_runtime(SearchRuntimeConfig(provider="duckduckgo")),
        provider_factory=provider_factory,
    )

    assert payload["ok"] is True
    assert attempted == ["duckduckgo"]
    assert payload["provider_attempts"] == [{"provider": "duckduckgo", "status": "success"}]


@pytest.mark.asyncio
async def test_canonical_web_search_configured_auth_failure_does_not_fallback_or_leak() -> None:
    def provider_factory(name: str) -> ConfiguredBadKeyAuthProvider | FallbackProvider:
        if name == "tavily":
            return ConfiguredBadKeyAuthProvider()
        return FallbackProvider()

    payload = await run_canonical_web_search(
        SearchOptions(query="q", provider="tavily", fetch_top_k=0),
        runtime=resolve_search_runtime(
            SearchRuntimeConfig(provider="tavily", api_key="configured-bad-key")
        ),
        provider_factory=provider_factory,
    )

    assert payload["ok"] is False
    assert payload["error_kind"] == "auth"
    assert payload["provider_attempts"] == [{"provider": "tavily", "status": "auth_failed"}]
    assert "sk-test" not in payload["error"]
    assert "raw secret sk-test leaked" not in payload["error"]


@pytest.mark.asyncio
async def test_canonical_web_search_public_error_message_is_sanitized() -> None:
    payload = await run_canonical_web_search(
        SearchOptions(query="q", provider="tavily", fetch_top_k=0),
        provider_factory=lambda name: SensitiveErrorProvider(),
    )

    assert payload["ok"] is False
    assert payload["error_kind"] == "http"
    assert len(payload["error"]) < 120
    assert "sk-test" not in payload["error"]
    assert "api_key=abc" not in payload["error"]
    assert "raw body" not in payload["error"]


@pytest.mark.asyncio
async def test_canonical_web_search_fetches_compact_excerpt_for_short_provider_content() -> None:
    async def fetcher(url: str, max_chars: int) -> dict[str, Any]:
        return {
            "text": (
                '<external-content source="https://example.com">'
                "Fetched body text"
                "</external-content>"
            ),
            "extractor": "readability",
            "truncated": False,
            "status": 200,
        }

    payload = await run_canonical_web_search(
        SearchOptions(query="q", fetch_top_k=1, max_chars_per_source=500),
        provider_factory=lambda name: ShortContentProvider(),
        fetcher=fetcher,
    )

    assert payload["ok"] is True
    assert payload["results"][0]["fetched"] is True
    assert payload["results"][0]["fetch_status"] == "ok"
    assert payload["results"][0]["extractor"] == "readability"
    assert "Fetched body text" in payload["results"][0]["excerpt"]
    assert payload["diagnostics"]["fetched_count"] == 1


@pytest.mark.asyncio
async def test_canonical_web_search_keeps_provider_excerpt_when_fetch_fails() -> None:
    async def fetcher(url: str, max_chars: int) -> dict[str, Any]:
        return {"error": "blocked", "status": 403, "extractor": "none", "text": ""}

    payload = await run_canonical_web_search(
        SearchOptions(query="q", fetch_top_k=1),
        provider_factory=lambda name: SnippetProvider(),
        fetcher=fetcher,
    )

    assert payload["ok"] is True
    assert payload["results"][0]["excerpt"] == "Provider snippet remains available."
    assert payload["results"][0]["fetch_status"] != "ok"
    assert payload["diagnostics"]["fetch_failed_count"] == 1


@pytest.mark.asyncio
async def test_canonical_web_search_default_fetcher_fetches_compact_excerpt(
    monkeypatch,
) -> None:
    fetch_calls: list[tuple[str, int]] = []

    async def fake_run_web_fetch_payload(url: str, max_chars: int) -> dict[str, Any]:
        fetch_calls.append((url, max_chars))
        return {
            "text": (
                '<external-content source="https://example.com/article">'
                "Fetched by default fetcher"
                "</external-content>"
            ),
            "extractor": "web_fetch",
            "truncated": False,
            "status": 200,
        }

    fake_web_fetch = types.SimpleNamespace(
        run_web_fetch_payload=fake_run_web_fetch_payload,
    )
    monkeypatch.setitem(sys.modules, "opensquilla.tools.builtin.web_fetch", fake_web_fetch)

    payload = await run_canonical_web_search(
        SearchOptions(query="q", fetch_top_k=1, max_chars_per_source=500),
        provider_factory=lambda name: ShortContentProvider(),
        use_cache=False,
    )

    assert payload["ok"] is True
    assert fetch_calls == [("https://example.com/article", 500)]
    assert payload["results"][0]["fetched"] is True
    assert payload["results"][0]["extractor"] == "web_fetch"
    assert "Fetched by default fetcher" in payload["results"][0]["excerpt"]
    assert payload["diagnostics"]["fetched_count"] == 1


@pytest.mark.asyncio
async def test_canonical_web_search_treats_malformed_fetch_payload_as_fetch_failure() -> None:
    async def fetcher(url: str, max_chars: int) -> None:
        return None

    payload = await run_canonical_web_search(
        SearchOptions(query="q", fetch_top_k=1),
        provider_factory=lambda name: SnippetProvider(),
        fetcher=fetcher,
    )

    assert payload["ok"] is True
    assert payload["results"][0]["excerpt"] == "Provider snippet remains available."
    assert payload["results"][0]["fetch_status"] == "malformed_payload"
    assert payload["diagnostics"]["fetch_failed_count"] == 1


@pytest.mark.asyncio
async def test_canonical_web_search_falls_back_on_retryable_network_error() -> None:
    def provider_factory(name: str) -> NetworkFailProvider | FallbackProvider:
        if name == "tavily":
            return NetworkFailProvider()
        return FallbackProvider()

    payload = await run_canonical_web_search(
        SearchOptions(query="q", provider="tavily", fetch_top_k=0),
        runtime=resolve_search_runtime(
            SearchRuntimeConfig(provider="tavily", api_key="tavily-key", fallback_policy="network")
        ),
        provider_factory=provider_factory,
    )

    assert payload["ok"] is True
    assert payload["provider_attempts"] == [
        {"provider": "tavily", "status": "error", "error_kind": "network"},
        {"provider": "duckduckgo", "status": "success"},
    ]
    assert payload["diagnostics"]["fallback_from"] == "tavily"
    assert payload["results"][0]["provider"] == "duckduckgo"
    assert "retry_allowed" not in payload
    assert "provider_retryable" not in payload


@pytest.mark.asyncio
async def test_canonical_web_search_auto_network_attempts_at_most_two_ranked_providers(
    monkeypatch,
) -> None:
    for key in (
        "BOCHA_SEARCH_API_KEY",
        "TAVILY_API_KEY",
        "BRAVE_SEARCH_API_KEY",
        "EXA_API_KEY",
        "IQS_SEARCH_API_KEY",
    ):
        monkeypatch.setenv(key, f"{key.lower()}-value")
    calls: list[str] = []
    runtime = resolve_search_runtime(
        SearchRuntimeConfig(provider="duckduckgo", fallback_policy="network")
    )

    payload = await run_canonical_web_search(
        SearchOptions(query="bounded fallback", fetch_top_k=0),
        runtime=runtime,
        provider_factory=lambda name: NamedNetworkFailProvider(name, calls),
    )

    assert payload["ok"] is False
    assert payload["retry_allowed"] is False
    assert payload["provider_retryable"] is True
    assert calls == ["bocha", "tavily"]
    assert payload["provider_attempts"] == [
        {"provider": "bocha", "status": "error", "error_kind": "network"},
        {"provider": "tavily", "status": "error", "error_kind": "network"},
    ]


@pytest.mark.asyncio
async def test_canonical_web_search_auto_network_skips_runtime_missing_key_to_backup(
    monkeypatch,
) -> None:
    monkeypatch.setenv("EXA_API_KEY", "stale-exa-key")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.delenv("BOCHA_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("IQS_SEARCH_API_KEY", raising=False)
    attempted: list[str] = []

    def provider_factory(name: str) -> MissingKeyAuthProvider | NamedSuccessProvider:
        attempted.append(name)
        if name == "exa":
            return MissingKeyAuthProvider()
        return NamedSuccessProvider(name, [])

    payload = await run_canonical_web_search(
        SearchOptions(query="python sqlite api docs", mode="technical", fetch_top_k=0),
        runtime=resolve_search_runtime(
            SearchRuntimeConfig(provider="duckduckgo", fallback_policy="network")
        ),
        provider_factory=provider_factory,
    )

    assert payload["ok"] is True
    assert attempted == ["exa", "brave"]
    assert payload["provider_attempts"] == [
        {"provider": "exa", "status": "auth_missing"},
        {"provider": "brave", "status": "success"},
    ]


@pytest.mark.asyncio
async def test_canonical_web_search_auto_off_skips_local_auth_missing_once(
    monkeypatch,
) -> None:
    monkeypatch.setenv("EXA_API_KEY", "stale-exa-key")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.delenv("BOCHA_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("IQS_SEARCH_API_KEY", raising=False)
    attempted: list[str] = []

    def provider_factory(name: str) -> MissingKeyAuthProvider | NamedSuccessProvider:
        attempted.append(name)
        if name == "exa":
            return MissingKeyAuthProvider()
        return NamedSuccessProvider(name, [])

    payload = await run_canonical_web_search(
        SearchOptions(query="python sqlite api docs", mode="technical", fetch_top_k=0),
        runtime=resolve_search_runtime(
            SearchRuntimeConfig(provider="duckduckgo", fallback_policy="off")
        ),
        provider_factory=provider_factory,
    )

    assert payload["ok"] is True
    assert attempted == ["exa", "brave"]
    assert payload["provider_attempts"] == [
        {"provider": "exa", "status": "auth_missing"},
        {"provider": "brave", "status": "success"},
    ]


@pytest.mark.asyncio
async def test_canonical_web_search_auto_off_does_not_network_fallback(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BOCHA_SEARCH_API_KEY", "bocha-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("IQS_SEARCH_API_KEY", raising=False)
    calls: list[str] = []

    payload = await run_canonical_web_search(
        SearchOptions(query="bounded off", fetch_top_k=0),
        runtime=resolve_search_runtime(
            SearchRuntimeConfig(provider="duckduckgo", fallback_policy="off")
        ),
        provider_factory=lambda name: NamedNetworkFailProvider(name, calls),
    )

    assert payload["ok"] is False
    assert calls == ["bocha"]
    assert payload["provider_attempts"] == [
        {"provider": "bocha", "status": "error", "error_kind": "network"},
    ]


@pytest.mark.asyncio
async def test_canonical_web_search_network_policy_respects_non_retryable_error() -> None:
    attempted: list[str] = []

    def provider_factory(name: str) -> NonRetryableHttpProvider | FallbackProvider:
        attempted.append(name)
        if name == "tavily":
            return NonRetryableHttpProvider()
        return FallbackProvider()

    payload = await run_canonical_web_search(
        SearchOptions(query="q", provider="tavily", fetch_top_k=0),
        runtime=resolve_search_runtime(
            SearchRuntimeConfig(
                provider="tavily",
                api_key="tavily-key",
                fallback_policy="network",
            )
        ),
        provider_factory=provider_factory,
    )

    assert payload["ok"] is False
    assert payload["error_kind"] == "http"
    assert payload["provider_retryable"] is False
    assert payload["retry_allowed"] is False
    assert attempted == ["tavily"]


@pytest.mark.asyncio
async def test_canonical_web_search_explicit_provider_does_not_fallback_when_policy_off() -> None:
    def provider_factory(name: str) -> NetworkFailProvider | FallbackProvider:
        if name == "tavily":
            return NetworkFailProvider()
        return FallbackProvider()

    payload = await run_canonical_web_search(
        SearchOptions(query="q", provider="tavily", fetch_top_k=0),
        runtime=resolve_search_runtime(
            SearchRuntimeConfig(provider="tavily", api_key="tavily-key", fallback_policy="off")
        ),
        provider_factory=provider_factory,
    )

    assert payload["ok"] is False
    assert payload["error_kind"] == "network"
    assert payload["provider_attempts"] == [
        {"provider": "tavily", "status": "error", "error_kind": "network"}
    ]


@pytest.mark.asyncio
async def test_canonical_web_search_surfaces_blocked_provider_failure() -> None:
    payload = await run_canonical_web_search(
        SearchOptions(query="q", provider="duckduckgo", fetch_top_k=0),
        provider_factory=lambda name: BlockedProvider(name),
    )

    assert payload["ok"] is False
    assert payload["error_kind"] == "blocked"
    assert payload["provider_retryable"] is True
    assert payload["retry_allowed"] is False
    assert payload["provider_attempts"] == [
        {"provider": "duckduckgo", "status": "error", "error_kind": "blocked"}
    ]
    assert payload["diagnostics"]["empty_reason"] == ""


@pytest.mark.asyncio
async def test_canonical_web_search_does_not_fallback_on_retryable_blocked_error() -> None:
    calls: list[str] = []

    def provider_factory(name: str) -> BlockedProvider | FallbackProvider:
        calls.append(name)
        if name == "tavily":
            return BlockedProvider(name)
        return FallbackProvider()

    payload = await run_canonical_web_search(
        SearchOptions(query="q", provider="tavily", fetch_top_k=0),
        runtime=resolve_search_runtime(
            SearchRuntimeConfig(provider="tavily", api_key="tavily-key", fallback_policy="network")
        ),
        provider_factory=provider_factory,
    )

    assert payload["ok"] is False
    assert payload["retry_allowed"] is False
    assert payload["provider_attempts"] == [
        {"provider": "tavily", "status": "error", "error_kind": "blocked"},
    ]
    assert calls == ["tavily"]


@pytest.mark.asyncio
async def test_canonical_web_search_marks_true_empty_results() -> None:
    payload = await run_canonical_web_search(
        SearchOptions(query="q", provider="duckduckgo", fetch_top_k=0),
        provider_factory=lambda name: EmptyProvider(),
    )

    assert payload["ok"] is True
    assert payload["results"] == []
    assert payload["sources"] == []
    assert payload["diagnostics"]["empty_reason"] == "no_results"


@pytest.mark.asyncio
async def test_canonical_web_search_fetch_top_k_only_considers_top_ranked_slice() -> None:
    fetch_calls: list[str] = []

    async def fetcher(url: str, max_chars: int) -> dict[str, Any]:
        fetch_calls.append(url)
        return {
            "text": (
                '<external-content source="https://example.com">'
                "Fetched body text"
                "</external-content>"
            ),
            "extractor": "readability",
            "truncated": False,
            "status": 200,
        }

    payload = await run_canonical_web_search(
        SearchOptions(query="q", fetch_top_k=2),
        provider_factory=lambda name: UsefulTopResultsProvider(),
        fetcher=fetcher,
    )

    assert fetch_calls == []
    assert payload["results"][2]["rank"] == 3
    assert payload["results"][2]["fetched"] is False
    assert payload["diagnostics"]["fetched_count"] == 0


@pytest.mark.asyncio
async def test_canonical_web_search_filters_include_and_exclude_domains_before_fetch() -> None:
    fetch_calls: list[str] = []

    async def fetcher(url: str, max_chars: int) -> dict[str, Any]:
        fetch_calls.append(url)
        return {
            "text": (
                '<external-content source="https://example.com">'
                "Fetched body text"
                "</external-content>"
            ),
            "extractor": "readability",
            "truncated": False,
            "status": 200,
        }

    payload = await run_canonical_web_search(
        SearchOptions(
            query="python",
            include_domains=("https://PYTHON.org/docs",),
            exclude_domains=("docs.python.org",),
            fetch_top_k=5,
        ),
        provider_factory=lambda name: DomainFilteringProvider(),
        fetcher=fetcher,
    )

    assert payload["ok"] is True
    assert [result["title"] for result in payload["results"]] == [
        "Allowed exact",
        "Allowed subdomain",
    ]
    assert [result["rank"] for result in payload["results"]] == [1, 2]
    assert fetch_calls == [
        "https://python.org/about",
        "https://www.python.org/downloads",
    ]


@pytest.mark.asyncio
async def test_canonical_web_search_soft_degrades_explicit_duckduckgo_recency() -> None:
    calls: list[tuple[str, str, int]] = []

    payload = await run_canonical_web_search(
        SearchOptions(query="q", provider="duckduckgo", recency="week", fetch_top_k=0),
        provider_factory=lambda name: QueryCaptureProvider(calls),
    )

    assert payload["ok"] is True
    assert payload["query"] == "q"
    assert calls == [("duckduckgo", "q past week", 10)]
    assert payload["provider_attempts"] == [{"provider": "duckduckgo", "status": "success"}]
    assert payload["diagnostics"]["recency_supported"] is False
    assert payload["diagnostics"]["recency_degraded"] is True


@pytest.mark.asyncio
async def test_canonical_web_search_no_key_auto_recency_uses_duckduckgo_soft_degrade(
    monkeypatch,
) -> None:
    for key in (
        "BRAVE_SEARCH_API_KEY",
        "TAVILY_API_KEY",
        "EXA_API_KEY",
        "BOCHA_SEARCH_API_KEY",
        "IQS_SEARCH_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    calls: list[tuple[str, str, int]] = []

    def provider_factory(name: str) -> QueryCaptureProvider:
        assert name == "duckduckgo"
        return QueryCaptureProvider(calls)

    payload = await run_canonical_web_search(
        SearchOptions(query="q", recency="week", fetch_top_k=0),
        runtime=resolve_search_runtime(SearchRuntimeConfig(provider="duckduckgo")),
        provider_factory=provider_factory,
    )

    assert payload["ok"] is True
    assert calls == [("duckduckgo", "q past week", 10)]
    assert payload["provider_attempts"] == [{"provider": "duckduckgo", "status": "success"}]
    assert payload["diagnostics"]["recency_supported"] is False
    assert payload["diagnostics"]["recency_degraded"] is True


@pytest.mark.asyncio
async def test_canonical_web_search_technical_mode_off_uses_top_ranked_provider_once(
    monkeypatch,
) -> None:
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    # A real Bocha key in the developer's environment would outrank brave in
    # the technical-mode fallback order; keep the ordering assertion hermetic.
    monkeypatch.delenv("BOCHA_SEARCH_API_KEY", raising=False)
    attempted: list[str] = []

    def provider_factory(name: str) -> NamedSuccessProvider:
        attempted.append(name)
        return NamedSuccessProvider(name, [])

    payload = await run_canonical_web_search(
        SearchOptions(query="python sqlite api docs", mode="technical", fetch_top_k=0),
        provider_factory=provider_factory,
    )

    assert payload["ok"] is True
    assert attempted == ["exa"]
    assert payload["provider_attempts"] == [
        {"provider": "exa", "status": "success"},
    ]


@pytest.mark.asyncio
async def test_canonical_web_search_passes_supported_recency_kwarg_only(monkeypatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    calls: list[tuple[str, dict[str, Any]]] = []

    payload = await run_canonical_web_search(
        SearchOptions(query="q", recency="week", fetch_top_k=0),
        runtime=resolve_search_runtime(SearchRuntimeConfig(provider="duckduckgo")),
        provider_factory=lambda name: RecencyAwareProvider(calls),
    )

    assert payload["ok"] is True
    assert calls == [("q", {"max_results": 10, "recency": "week"})]


@pytest.mark.asyncio
async def test_canonical_web_search_rejects_empty_query_without_calling_provider() -> None:
    def provider_factory(name: str) -> FakeProvider:
        raise AssertionError("provider_factory should not be called")

    payload = await run_canonical_web_search(
        SearchOptions(query="   "),
        provider_factory=provider_factory,
    )

    assert payload["ok"] is False
    assert payload["error_kind"] == "invalid_request"
    assert payload["provider_attempts"] == []


@pytest.mark.asyncio
async def test_canonical_web_search_limits_root_domain_spam_without_include_filter() -> None:
    canonical_module.clear_canonical_web_search_cache_for_tests()

    payload = await run_canonical_web_search(
        SearchOptions(query="root domain spam", max_results=5, fetch_top_k=0),
        provider_factory=lambda name: RootDomainSpamProvider(),
    )

    assert payload["ok"] is True
    assert [result["domain"] for result in payload["results"]] == [
        "www.example.com",
        "docs.example.com",
        "blog.example.com",
        "python.org",
    ]
    assert payload["diagnostics"]["domain_limited_count"] == 1
    assert [result["rank"] for result in payload["results"]] == [1, 2, 3, 4]


class MultiLabelSuffixProvider:
    name = "tavily"

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        sites = (
            ("Site One", "https://www.one.co.uk/news/1"),
            ("Site Two", "https://www.two.co.uk/politics/2"),
            ("Site Three", "https://www.three.co.uk/product/3"),
            ("Site Four", "https://www.four.co.uk/content/4"),
            ("Site Five", "https://news.five.co.uk/story/5"),
        )
        return [
            SearchResult(
                title=title,
                url=url,
                snippet=f"{title} coverage of the topic.",
                provider="tavily",
                source="tavily",
                content=f"{title} full article body long enough to be an excerpt.",
            )
            for title, url in sites
        ][:max_results]


def test_root_domain_returns_registrable_domain_not_public_suffix() -> None:
    roots = {
        canonical_module._root_domain("www.one.co.uk"),
        canonical_module._root_domain("www.two.co.uk"),
        canonical_module._root_domain("www.three.co.uk"),
    }
    assert "co.uk" not in roots
    assert len(roots) == 3


@pytest.mark.asyncio
async def test_canonical_web_search_spam_limit_keeps_distinct_multi_label_suffix_sites() -> None:
    payload = await run_canonical_web_search(
        SearchOptions(query="uk politics news", max_results=5, fetch_top_k=0),
        provider_factory=lambda name: MultiLabelSuffixProvider(),
    )

    assert payload["ok"] is True
    assert len(payload["results"]) == 5
    assert payload["diagnostics"]["domain_limited_count"] == 0


@pytest.mark.asyncio
async def test_canonical_web_search_preserves_include_domain_depth_without_spam_limit() -> None:
    canonical_module.clear_canonical_web_search_cache_for_tests()

    payload = await run_canonical_web_search(
        SearchOptions(
            query="root domain spam include",
            max_results=5,
            fetch_top_k=0,
            include_domains=("example.com",),
        ),
        provider_factory=lambda name: RootDomainSpamProvider(),
    )

    assert payload["ok"] is True
    assert [result["domain"] for result in payload["results"]] == [
        "www.example.com",
        "docs.example.com",
        "blog.example.com",
        "news.example.com",
    ]
    assert payload["diagnostics"]["domain_limited_count"] == 0


@pytest.mark.asyncio
async def test_canonical_web_search_caches_complete_payload_for_repeated_request() -> None:
    canonical_module.clear_canonical_web_search_cache_for_tests()
    provider_calls: list[str] = []
    fetch_calls: list[str] = []

    async def fetcher(url: str, max_chars: int) -> dict[str, Any]:
        fetch_calls.append(url)
        return {
            "text": (
                '<external-content source="https://example.com">'
                "Fetched cache body"
                "</external-content>"
            ),
            "extractor": "readability",
            "truncated": False,
            "status": 200,
        }

    options = SearchOptions(
        query="cache me",
        provider="tavily",
        max_results=2,
        fetch_top_k=1,
        max_chars_per_source=500,
    )
    first = await run_canonical_web_search(
        options,
        provider_factory=lambda name: CountingShortProvider(provider_calls),
        fetcher=fetcher,
        use_cache=True,
    )
    second = await run_canonical_web_search(
        options,
        provider_factory=lambda name: CountingShortProvider(provider_calls),
        fetcher=fetcher,
        use_cache=True,
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert provider_calls == ["cache me"]
    assert fetch_calls == ["https://example.com/cached"]
    assert first["diagnostics"]["cache_status"] == "miss"
    assert second["diagnostics"]["cache_status"] == "hit"
    assert second["results"][0]["excerpt"] == "Fetched cache body"


@pytest.mark.asyncio
async def test_canonical_web_search_cache_key_includes_execution_plan(monkeypatch) -> None:
    canonical_module.clear_canonical_web_search_cache_for_tests()
    for key in (
        "BOCHA_SEARCH_API_KEY",
        "TAVILY_API_KEY",
        "BRAVE_SEARCH_API_KEY",
        "EXA_API_KEY",
        "IQS_SEARCH_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BOCHA_SEARCH_API_KEY", "bocha-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    calls: list[str] = []
    options = SearchOptions(query="plan cache", fetch_top_k=0)

    first = await run_canonical_web_search(
        options,
        runtime=resolve_search_runtime(
            SearchRuntimeConfig(provider="duckduckgo", fallback_policy="network")
        ),
        provider_factory=lambda name: NamedSuccessProvider(name, calls),
        use_cache=True,
    )
    second = await run_canonical_web_search(
        options,
        runtime=resolve_search_runtime(
            SearchRuntimeConfig(provider="duckduckgo", fallback_policy="off")
        ),
        provider_factory=lambda name: NamedSuccessProvider(name, calls),
        use_cache=True,
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert calls == ["bocha", "bocha"]
    assert first["diagnostics"]["cache_status"] == "miss"
    assert second["diagnostics"]["cache_status"] == "miss"
