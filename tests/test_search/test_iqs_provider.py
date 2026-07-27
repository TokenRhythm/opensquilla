from __future__ import annotations

import json

import httpx
import pytest

from opensquilla.search.providers.iqs import IqsSearchProvider
from opensquilla.search.registry import get_provider_spec
from opensquilla.search.types import SearchProviderError

_UNIFIED_URL = "https://cloud-iqs.aliyuncs.com/search/unified"


@pytest.mark.asyncio
async def test_iqs_search_posts_unified_request_and_maps_results() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "requestId": "req-iqs-1",
                "pageItems": [
                    {
                        "title": "IQS result",
                        "link": "https://example.cn/a",
                        "snippet": "Snippet text",
                        "mainText": "Main text body",
                        "publishedTime": "2026-03-10T23:21:00+08:00",
                        "rerankScore": 0.875,
                        "hostname": "Example CN",
                        "images": [],
                    }
                ],
                "sceneItems": [],
                "searchInformation": {"searchTime": 412},
                "costCredits": {"search": {"liteAdvancedTextSearch": 1}},
            },
        )

    provider = IqsSearchProvider(
        api_key="dummy-iqs-key",
        transport=httpx.MockTransport(handler),
    )

    results = await provider.search("OpenSquilla", max_results=3, recency="year")

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == _UNIFIED_URL
    assert requests[0].headers["Authorization"] == "Bearer dummy-iqs-key"
    assert requests[0].headers["Content-Type"] == "application/json"
    body = json.loads(requests[0].content)
    assert body == {
        "query": "OpenSquilla",
        "engineType": "LiteAdvanced",
        "contents": {"mainText": True, "rerankScore": True},
        "advancedParams": {"numResults": 3},
        "timeRange": "OneYear",
    }

    result = results[0]
    assert result.provider == "iqs"
    assert result.source == "iqs"
    assert result.title == "IQS result"
    assert result.url == "https://example.cn/a"
    assert result.snippet == "Snippet text"
    assert result.content == "Main text body"
    assert result.published_at == "2026-03-10T23:21:00+08:00"
    assert result.score == 0.875
    assert result.raw_metadata["requestId"] == "req-iqs-1"
    assert result.raw_metadata["hostname"] == "Example CN"
    assert "mainText" not in result.raw_metadata


@pytest.mark.asyncio
async def test_iqs_search_sends_domain_filters() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"requestId": "req-iqs-2", "pageItems": []})

    provider = IqsSearchProvider(
        api_key="dummy-iqs-key",
        transport=httpx.MockTransport(handler),
    )

    await provider.search(
        "OpenSquilla",
        max_results=5,
        include_domains=("example.com", "example.org"),
        exclude_domains=("spam.example",),
    )

    body = json.loads(requests[0].content)
    assert body["advancedParams"] == {
        "numResults": 5,
        "includeSites": "example.com,example.org",
        "excludeSites": "spam.example",
    }


@pytest.mark.asyncio
async def test_iqs_search_clamps_num_results_to_provider_limit() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"requestId": "req-iqs-3", "pageItems": []})

    provider = IqsSearchProvider(
        api_key="dummy-iqs-key",
        transport=httpx.MockTransport(handler),
    )

    await provider.search("OpenSquilla", max_results=40)

    body = json.loads(requests[0].content)
    assert body["advancedParams"]["numResults"] == 20


@pytest.mark.asyncio
async def test_iqs_search_truncates_overlong_query() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"requestId": "req-iqs-4", "pageItems": []})

    provider = IqsSearchProvider(
        api_key="dummy-iqs-key",
        transport=httpx.MockTransport(handler),
    )

    await provider.search("q" * 600)

    body = json.loads(requests[0].content)
    assert body["query"] == "q" * 500


@pytest.mark.asyncio
async def test_iqs_missing_api_key_raises_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("IQS_SEARCH_API_KEY", raising=False)
    provider = IqsSearchProvider()

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("OpenSquilla")

    assert exc_info.value.provider == "iqs"
    assert exc_info.value.kind == "auth"
    assert exc_info.value.retryable is False
    assert str(exc_info.value) == "IQS API key not set"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "kind", "retryable"),
    [
        (400, "http", False),
        (401, "auth", False),
        (403, "auth", False),
        (404, "auth", False),
        (408, "http", True),
        (429, "rate_limit", True),
        (500, "http", True),
    ],
)
async def test_iqs_http_errors_are_classified(
    status_code: int,
    kind: str,
    retryable: bool,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json={"error": "nope"})

    provider = IqsSearchProvider(
        api_key="dummy-iqs-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("OpenSquilla")

    assert exc_info.value.provider == "iqs"
    assert exc_info.value.kind == kind
    assert exc_info.value.retryable is retryable
    assert exc_info.value.status_code == status_code
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_iqs_http_error_message_includes_error_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "errorCode": "Retrieval.InvalidAPIKey",
                "errorMessage": "Incorrect APIKey provided.",
            },
        )

    provider = IqsSearchProvider(
        api_key="dummy-iqs-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("OpenSquilla")

    assert exc_info.value.kind == "auth"
    assert "Retrieval.InvalidAPIKey" in str(exc_info.value)
    assert "Incorrect APIKey provided." in str(exc_info.value)


@pytest.mark.asyncio
async def test_iqs_http_error_with_non_json_body_is_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="EngineType deserialization failed")

    provider = IqsSearchProvider(
        api_key="dummy-iqs-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("OpenSquilla")

    assert exc_info.value.kind == "http"
    assert exc_info.value.retryable is False
    assert exc_info.value.status_code == 400
    assert "EngineType deserialization failed" in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "kind"),
    [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "network")],
)
async def test_iqs_transport_errors_are_retryable_once(
    error_type: type[httpx.HTTPError],
    kind: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise error_type("transient failure", request=request)

    provider = IqsSearchProvider(
        api_key="dummy-iqs-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("OpenSquilla")

    assert exc_info.value.kind == kind
    assert exc_info.value.retryable is True
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_iqs_malformed_json_raises_parse_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    provider = IqsSearchProvider(
        api_key="dummy-iqs-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("OpenSquilla")

    assert exc_info.value.kind == "parse"
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_iqs_missing_page_items_returns_empty_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"requestId": "req-iqs-5"})

    provider = IqsSearchProvider(
        api_key="dummy-iqs-key",
        transport=httpx.MockTransport(handler),
    )

    results = await provider.search("OpenSquilla")

    assert results == []


def test_iqs_provider_spec_is_runtime_supported_after_import() -> None:
    import opensquilla.search.providers.iqs  # noqa: F401

    spec = get_provider_spec("iqs")

    assert spec.runtime_supported is True
    assert spec.requires_api_key is True
    assert spec.env_key == "IQS_SEARCH_API_KEY"
    assert spec.capabilities == frozenset({"web", "freshness", "domain_filter", "content"})
