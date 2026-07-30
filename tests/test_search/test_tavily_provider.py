from __future__ import annotations

import json

import httpx
import pytest

from opensquilla.search.providers.tavily import TavilySearchProvider
from opensquilla.search.types import SearchProviderError


@pytest.mark.asyncio
async def test_tavily_search_posts_low_cost_request_and_maps_results() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "request_id": "req-123",
                "response_time": 0.42,
                "usage": {"searches": 1},
                "results": [
                    {
                        "title": "Python release",
                        "url": "https://python.org/releases",
                        "content": "Python release notes",
                        "score": 0.91,
                        "published_date": "2026-06-19",
                        "highlights": ["Python", "release"],
                        "category": "docs",
                    }
                ],
            },
        )

    provider = TavilySearchProvider(
        api_key="dummy-tavily-key",
        transport=httpx.MockTransport(handler),
    )

    results = await provider.search("python release", max_results=3)

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "https://api.tavily.com/search"
    body = json.loads(requests[0].content)
    assert body == {
        "api_key": "dummy-tavily-key",
        "query": "python release",
        "max_results": 3,
        "search_depth": "basic",
        "include_raw_content": False,
        "include_answer": False,
        "include_images": False,
    }

    result = results[0]
    assert result.provider == "tavily"
    assert result.source == "tavily"
    assert result.title == "Python release"
    assert result.url == "https://python.org/releases"
    assert result.snippet == "Python release notes"
    assert result.content == "Python release notes"
    assert result.score == 0.91
    assert result.published_at == "2026-06-19"
    assert result.highlights == ["Python", "release"]
    assert result.raw_metadata["request_id"] == "req-123"
    assert result.raw_metadata["response_time"] == 0.42
    assert result.raw_metadata["usage"] == {"searches": 1}
    assert result.raw_metadata["category"] == "docs"
    assert "api_key" not in result.raw_metadata


@pytest.mark.asyncio
async def test_tavily_search_uses_raw_content_when_present() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Result",
                        "url": "https://example.com",
                        "content": "Short snippet",
                        "raw_content": "Full page text",
                        "published_at": "2026-06-18",
                    }
                ]
            },
        )

    provider = TavilySearchProvider(
        api_key="dummy-tavily-key",
        transport=httpx.MockTransport(handler),
    )

    result = (await provider.search("example"))[0]

    assert result.snippet == "Short snippet"
    assert result.content == "Full page text"
    assert result.published_at == "2026-06-18"


@pytest.mark.asyncio
async def test_tavily_search_posts_supported_filter_options() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    provider = TavilySearchProvider(
        api_key="dummy-tavily-key",
        transport=httpx.MockTransport(handler),
    )

    await provider.search(
        "python",
        max_results=4,
        recency="week",
        include_domains=("python.org", "docs.python.org"),
        exclude_domains=("notpython.org",),
    )

    body = json.loads(requests[0].content)
    assert body["time_range"] == "week"
    assert body["include_domains"] == ["python.org", "docs.python.org"]
    assert body["exclude_domains"] == ["notpython.org"]


@pytest.mark.asyncio
async def test_tavily_missing_api_key_raises_auth_error(monkeypatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    provider = TavilySearchProvider()

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("python")

    assert exc_info.value.provider == "tavily"
    assert exc_info.value.kind == "auth"
    assert exc_info.value.retryable is False
    assert str(exc_info.value) == "Tavily API key not set"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "kind", "retryable"),
    [
        (400, "http", False),
        (401, "auth", False),
        (403, "auth", False),
        (408, "http", True),
        (429, "rate_limit", True),
        (432, "rate_limit", False),
        (433, "rate_limit", False),
        (500, "http", True),
    ],
)
async def test_tavily_http_errors_are_classified(
    status_code: int,
    kind: str,
    retryable: bool,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json={"error": "nope"})

    provider = TavilySearchProvider(
        api_key="dummy-tavily-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("python")

    assert exc_info.value.provider == "tavily"
    assert exc_info.value.kind == kind
    assert exc_info.value.retryable is retryable
    assert exc_info.value.status_code == status_code
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_tavily_timeout_is_retryable_timeout() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("timed out", request=request)

    provider = TavilySearchProvider(
        api_key="dummy-tavily-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("python")

    assert exc_info.value.kind == "timeout"
    assert exc_info.value.retryable is True
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_tavily_network_error_is_retryable_network() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("network down", request=request)

    provider = TavilySearchProvider(
        api_key="dummy-tavily-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("python")

    assert exc_info.value.kind == "network"
    assert exc_info.value.retryable is True
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_tavily_malformed_json_is_terminal_parse_error() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="not json")

    provider = TavilySearchProvider(
        api_key="dummy-tavily-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("python")

    assert exc_info.value.kind == "parse"
    assert exc_info.value.retryable is False
    assert len(requests) == 1
