from __future__ import annotations

import json

import httpx
import pytest

from opensquilla.search.registry import get_provider_spec
from opensquilla.search.types import SearchProviderError


@pytest.mark.asyncio
async def test_specstream_search_posts_request_and_maps_results() -> None:
    from opensquilla.search.providers.specstream import SpecStreamSearchProvider

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "request_id": "req-ss-1",
                "sources": [
                    {
                        "title": "Python release notes",
                        "url": "https://docs.python.org/3/whatsnew/3.13.html",
                        "snippet": "Python 3.13 changed runtime features.",
                        "excerpt": "Python 3.13 release notes full text",
                        "published": "2026-06-18",
                    }
                ],
            },
        )

    provider = SpecStreamSearchProvider(
        api_key="dummy-specstream-key",
        transport=httpx.MockTransport(handler),
    )

    results = await provider.search("Python 3.13 release notes", max_results=4)

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "https://api.specstream.ai/v1/web-search"
    assert requests[0].headers["authorization"] == "Bearer dummy-specstream-key"
    body = json.loads(requests[0].content)
    assert body == {
        "q": "Python 3.13 release notes",
        "num": 4,
        "passages": True,
    }

    assert len(results) == 1
    result = results[0]
    assert result.provider == "specstream"
    assert result.source == "specstream"
    assert result.title == "Python release notes"
    assert result.url == "https://docs.python.org/3/whatsnew/3.13.html"
    assert result.snippet == "Python 3.13 changed runtime features."
    assert result.content == "Python 3.13 release notes full text"
    assert result.published_at == "2026-06-18"
    assert result.raw_metadata["request_id"] == "req-ss-1"


@pytest.mark.asyncio
async def test_specstream_search_domain_and_recency_filters() -> None:
    from opensquilla.search.providers.specstream import SpecStreamSearchProvider

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"sources": []})

    provider = SpecStreamSearchProvider(
        api_key="dummy-specstream-key",
        transport=httpx.MockTransport(handler),
    )

    await provider.search(
        "release notes",
        max_results=5,
        recency="week",
        include_domains=("docs.python.org",),
        exclude_domains=("example.org",),
    )

    body = json.loads(requests[0].content)
    assert body["q"] == "release notes site:docs.python.org -site:example.org"
    assert body["news"] is True


@pytest.mark.asyncio
async def test_specstream_search_errors_and_spec() -> None:
    from opensquilla.search.providers.specstream import SpecStreamSearchProvider

    spec = get_provider_spec("specstream")
    assert spec.env_key == "SPECSTREAM_API_KEY"
    assert spec.requires_api_key is True

    provider = SpecStreamSearchProvider(
        api_key="",
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("query")
    assert exc_info.value.kind == "auth"

    provider_429 = SpecStreamSearchProvider(
        api_key="dummy",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(429, json={"detail": "rate limited"})
        ),
    )
    with pytest.raises(SearchProviderError) as exc_429:
        await provider_429.search("query")
    assert exc_429.value.kind == "rate_limit"
    assert exc_429.value.retryable is True
