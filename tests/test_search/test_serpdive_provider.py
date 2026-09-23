from __future__ import annotations

import json

import httpx
import pytest

from opensquilla.search.providers.serpdive import SerpdiveSearchProvider
from opensquilla.search.types import SearchProviderError


@pytest.mark.asyncio
async def test_serpdive_search_posts_request_and_maps_results() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "query": "python release",
                "model": "mako",
                "response_time_ms": 1420,
                "results": [
                    {
                        "title": "Python release",
                        "url": "https://python.org/releases",
                        "content": "Python 3.14 was released with these changes.",
                        "date": "2026-06-19",
                    }
                ],
            },
        )

    provider = SerpdiveSearchProvider(
        api_key="dummy-serpdive-key",
        transport=httpx.MockTransport(handler),
    )

    results = await provider.search("python release", max_results=3)

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "https://api.serpdive.com/v1/search"
    assert requests[0].headers["Authorization"] == "Bearer dummy-serpdive-key"
    body = json.loads(requests[0].content)
    assert body == {
        "query": "python release",
        "max_results": 3,
    }

    result = results[0]
    assert result.provider == "serpdive"
    assert result.source == "serpdive"
    assert result.title == "Python release"
    assert result.url == "https://python.org/releases"
    assert result.snippet == "Python 3.14 was released with these changes."
    assert result.content == "Python 3.14 was released with these changes."
    assert result.published_at == "2026-06-19"
    assert result.raw_metadata["model"] == "mako"
    assert result.raw_metadata["response_time_ms"] == 1420


@pytest.mark.asyncio
async def test_serpdive_search_clamps_max_results_to_api_cap() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    provider = SerpdiveSearchProvider(
        api_key="dummy-serpdive-key",
        transport=httpx.MockTransport(handler),
    )

    await provider.search("python", max_results=20)

    body = json.loads(requests[0].content)
    assert body["max_results"] == 10


@pytest.mark.asyncio
async def test_serpdive_result_without_date_has_no_published_at() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Result",
                        "url": "https://example.com",
                        "content": "Some extracted sentences.",
                    }
                ]
            },
        )

    provider = SerpdiveSearchProvider(
        api_key="dummy-serpdive-key",
        transport=httpx.MockTransport(handler),
    )

    result = (await provider.search("example"))[0]

    assert result.snippet == "Some extracted sentences."
    assert result.published_at is None


@pytest.mark.asyncio
async def test_serpdive_missing_api_key_raises_auth_error(monkeypatch) -> None:
    monkeypatch.delenv("SERPDIVE_API_KEY", raising=False)
    provider = SerpdiveSearchProvider()

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("python")

    assert exc_info.value.provider == "serpdive"
    assert exc_info.value.kind == "auth"
    assert exc_info.value.retryable is False
    assert str(exc_info.value) == "SERPdive API key not set"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "kind", "retryable"),
    [
        (401, "auth", False),
        (403, "auth", False),
        (429, "rate_limit", True),
        (500, "http", True),
    ],
)
async def test_serpdive_http_errors_are_classified(
    status_code: int,
    kind: str,
    retryable: bool,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "nope"})

    provider = SerpdiveSearchProvider(
        api_key="dummy-serpdive-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("python")

    assert exc_info.value.provider == "serpdive"
    assert exc_info.value.kind == kind
    assert exc_info.value.retryable is retryable
    assert exc_info.value.status_code == status_code


@pytest.mark.asyncio
async def test_serpdive_timeout_is_retryable_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    provider = SerpdiveSearchProvider(
        api_key="dummy-serpdive-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("python")

    assert exc_info.value.kind == "timeout"
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_serpdive_network_error_is_retryable_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down", request=request)

    provider = SerpdiveSearchProvider(
        api_key="dummy-serpdive-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("python")

    assert exc_info.value.kind == "network"
    assert exc_info.value.retryable is True
