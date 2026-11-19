from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from opensquilla.search.providers.keenable import KeenableSearchProvider
from opensquilla.search.registry import get_provider_spec
from opensquilla.search.types import SearchProviderError

_PUBLIC_URL = "https://api.keenable.ai/v1/search/public"
_KEYED_URL = "https://api.keenable.ai/v1/search"
# Real responses leave ``description`` empty and put page text in ``snippet``.
_PAGE_TEXT = "Release notes\n\n" + "OpenSquilla adds a new search provider. " * 30


@pytest.fixture(autouse=True)
def _no_keenable_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEENABLE_API_KEY", raising=False)


def _empty_response(requests: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_keenable_search_is_keyless_and_maps_snippet_text() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Keenable result",
                        "url": "https://example.com/a",
                        "description": "",
                        "snippet": _PAGE_TEXT,
                        "published_at": "2026-10-01T00:00:00Z",
                        "acquired_at": "2026-10-09T23:28:53Z",
                    }
                ]
            },
        )

    provider = KeenableSearchProvider(transport=httpx.MockTransport(handler))

    results = await provider.search("OpenSquilla", max_results=3)

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == _PUBLIC_URL
    assert requests[0].headers["X-Keenable-Title"] == "OpenSquilla"
    assert requests[0].headers["Content-Type"] == "application/json"
    assert "X-API-Key" not in requests[0].headers
    assert json.loads(requests[0].content) == {"query": "OpenSquilla", "max_results": 3}

    result = results[0]
    assert result.provider == "keenable"
    assert result.source == "keenable"
    assert result.title == "Keenable result"
    assert result.url == "https://example.com/a"
    assert result.content == _PAGE_TEXT.strip()
    assert result.snippet.startswith("Release notes OpenSquilla adds")
    assert "\n" not in result.snippet
    assert len(result.snippet) <= 500
    assert result.published_at == "2026-10-01T00:00:00Z"
    assert result.raw_metadata == {"acquired_at": "2026-10-09T23:28:53Z"}


@pytest.mark.asyncio
async def test_keenable_search_falls_back_to_description_when_snippet_is_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Short",
                        "url": "https://example.com/b",
                        "description": "Description text",
                        "snippet": "",
                    }
                ]
            },
        )

    provider = KeenableSearchProvider(transport=httpx.MockTransport(handler))

    results = await provider.search("OpenSquilla")

    assert results[0].snippet == "Description text"
    assert results[0].content == "Description text"
    assert results[0].published_at is None


@pytest.mark.asyncio
async def test_keenable_search_uses_keyed_endpoint_when_key_is_set() -> None:
    requests: list[httpx.Request] = []
    provider = KeenableSearchProvider(
        api_key="dummy-keenable-key",
        transport=_empty_response(requests),
    )

    await provider.search("OpenSquilla")

    assert str(requests[0].url) == _KEYED_URL
    assert requests[0].headers["X-API-Key"] == "dummy-keenable-key"
    assert requests[0].headers["X-Keenable-Title"] == "OpenSquilla"


@pytest.mark.asyncio
async def test_keenable_search_reads_key_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEENABLE_API_KEY", "env-keenable-key")
    requests: list[httpx.Request] = []
    provider = KeenableSearchProvider(transport=_empty_response(requests))

    await provider.search("OpenSquilla")

    assert str(requests[0].url) == _KEYED_URL
    assert requests[0].headers["X-API-Key"] == "env-keenable-key"


@pytest.mark.asyncio
async def test_keenable_search_sends_single_domain_as_site() -> None:
    requests: list[httpx.Request] = []
    provider = KeenableSearchProvider(transport=_empty_response(requests))

    await provider.search("asyncio task groups", include_domains=("docs.python.org",))

    assert json.loads(requests[0].content) == {
        "query": "asyncio task groups",
        "max_results": 5,
        "site": "docs.python.org",
    }


@pytest.mark.asyncio
async def test_keenable_search_expresses_several_domains_as_query_operators() -> None:
    requests: list[httpx.Request] = []
    provider = KeenableSearchProvider(transport=_empty_response(requests))

    await provider.search(
        "asyncio task groups",
        include_domains=("docs.python.org", "stackoverflow.com"),
        exclude_domains=("medium.com",),
    )

    body = json.loads(requests[0].content)
    assert body == {
        "query": (
            "asyncio task groups (site:docs.python.org OR site:stackoverflow.com) "
            "-site:medium.com"
        ),
        "max_results": 5,
    }


@pytest.mark.asyncio
async def test_keenable_search_maps_recency_to_published_after() -> None:
    requests: list[httpx.Request] = []
    provider = KeenableSearchProvider(transport=_empty_response(requests))

    await provider.search("python release", recency="week")

    expected = (datetime.now(UTC).date() - timedelta(days=7)).isoformat()
    assert json.loads(requests[0].content)["published_after"] == expected


@pytest.mark.asyncio
async def test_keenable_search_clamps_max_results_to_shared_ceiling() -> None:
    requests: list[httpx.Request] = []
    provider = KeenableSearchProvider(transport=_empty_response(requests))

    await provider.search("OpenSquilla", max_results=40)

    assert json.loads(requests[0].content)["max_results"] == 20


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body", "kind", "retryable"),
    [
        (
            429,
            {"error": "Rate limit exceeded", "message": "Public API hourly limit reached."},
            "rate_limit",
            True,
        ),
        (401, {"error": "Unauthorized"}, "auth", False),
        (400, {"error": "Invalid parameter"}, "http", False),
    ],
)
async def test_keenable_search_classifies_http_errors(
    status_code: int,
    body: dict[str, str],
    kind: str,
    retryable: bool,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    provider = KeenableSearchProvider(transport=httpx.MockTransport(handler))

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("OpenSquilla")

    error = exc_info.value
    assert error.provider == "keenable"
    assert error.kind == kind
    assert error.retryable is retryable
    assert error.status_code == status_code
    assert str(status_code) in error.message


@pytest.mark.asyncio
async def test_keenable_search_reports_malformed_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    provider = KeenableSearchProvider(transport=httpx.MockTransport(handler))

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("OpenSquilla")

    assert exc_info.value.kind == "parse"


def test_keenable_spec_is_keyless_opt_in() -> None:
    spec = get_provider_spec("keenable")

    assert spec.runtime_supported is True
    assert spec.requires_api_key is False
    assert spec.opt_in is True
    assert spec.env_key == "KEENABLE_API_KEY"
    assert spec.capabilities == frozenset({"web", "freshness", "domain_filter", "content"})
