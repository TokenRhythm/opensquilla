from __future__ import annotations

import json

import httpx
import pytest

from opensquilla.search.providers.bocha import BochaSearchProvider
from opensquilla.search.registry import get_provider_spec
from opensquilla.search.types import SearchProviderError


@pytest.mark.asyncio
async def test_bocha_search_posts_summary_request_and_maps_results() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "code": 200,
                "msg": "success",
                "log_id": "log-bocha-1",
                "data": {
                    "webPages": {
                        "value": [
                            {
                                "id": "https://example.cn/a",
                                "name": "Bocha result",
                                "url": "https://example.cn/a",
                                "snippet": "Snippet text",
                                "summary": "Summary text",
                                "siteName": "Example CN",
                                "displayUrl": "example.cn/a",
                                "datePublished": "2026-03-10T23:21:00+08:00",
                                "dateLastCrawled": "2026-03-11T08:00:00+08:00",
                            }
                        ]
                    }
                },
            },
        )

    provider = BochaSearchProvider(
        api_key="dummy-bocha-key",
        transport=httpx.MockTransport(handler),
    )

    results = await provider.search("OpenSquilla", max_results=3, recency="year")

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "https://api.bochaai.com/v1/web-search"
    assert requests[0].headers["Authorization"] == "Bearer dummy-bocha-key"
    assert requests[0].headers["Content-Type"] == "application/json"
    body = json.loads(requests[0].content)
    assert body == {
        "query": "OpenSquilla",
        "count": 3,
        "summary": True,
        "freshness": "oneYear",
    }

    result = results[0]
    assert result.provider == "bocha"
    assert result.source == "bocha"
    assert result.title == "Bocha result"
    assert result.url == "https://example.cn/a"
    assert result.snippet == "Snippet text"
    assert result.content == "Summary text"
    assert result.published_at == "2026-03-10T23:21:00+08:00"
    assert result.raw_metadata["log_id"] == "log-bocha-1"
    assert result.raw_metadata["siteName"] == "Example CN"
    assert result.raw_metadata["displayUrl"] == "example.cn/a"


@pytest.mark.asyncio
async def test_bocha_search_clamps_count_to_provider_limit() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 200, "data": {"webPages": {"value": []}}})

    provider = BochaSearchProvider(
        api_key="dummy-bocha-key",
        transport=httpx.MockTransport(handler),
    )

    await provider.search("OpenSquilla", max_results=40)

    body = json.loads(requests[0].content)
    assert body["count"] == 20


@pytest.mark.asyncio
async def test_bocha_missing_api_key_raises_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOCHA_SEARCH_API_KEY", raising=False)
    provider = BochaSearchProvider()

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("OpenSquilla")

    assert exc_info.value.provider == "bocha"
    assert exc_info.value.kind == "auth"
    assert exc_info.value.retryable is False
    assert str(exc_info.value) == "Bocha API key not set"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "kind", "retryable"),
    [
        (400, "http", False),
        (401, "auth", False),
        (403, "auth", False),
        (408, "http", True),
        (429, "rate_limit", True),
        (500, "http", True),
    ],
)
async def test_bocha_http_errors_are_classified(
    status_code: int,
    kind: str,
    retryable: bool,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json={"error": "nope"})

    provider = BochaSearchProvider(
        api_key="dummy-bocha-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("OpenSquilla")

    assert exc_info.value.provider == "bocha"
    assert exc_info.value.kind == kind
    assert exc_info.value.retryable is retryable
    assert exc_info.value.status_code == status_code
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_code", "kind", "retryable", "status_code"),
    [
        ("400", "http", False, 400),
        ("401", "auth", False, 401),
        ("408", "http", True, 408),
        ("429", "rate_limit", True, 429),
        ("500", "http", True, 500),
    ],
)
async def test_bocha_api_error_codes_are_classified(
    api_code: str,
    kind: str,
    retryable: bool,
    status_code: int,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": api_code, "msg": "bocha error"})

    provider = BochaSearchProvider(
        api_key="dummy-bocha-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("OpenSquilla")

    assert exc_info.value.provider == "bocha"
    assert exc_info.value.kind == kind
    assert exc_info.value.retryable is retryable
    assert exc_info.value.status_code == status_code
    assert str(exc_info.value) == "bocha error"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_bocha_nonnumeric_api_error_is_terminal() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"code": "INVALID_PARAMETER", "msg": "invalid request"},
        )

    provider = BochaSearchProvider(
        api_key="dummy-bocha-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("OpenSquilla")

    assert exc_info.value.kind == "http"
    assert exc_info.value.retryable is False
    assert exc_info.value.status_code is None
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "kind"),
    [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "network")],
)
async def test_bocha_transport_errors_are_retryable_once(
    error_type: type[httpx.HTTPError],
    kind: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise error_type("transient failure", request=request)

    provider = BochaSearchProvider(
        api_key="dummy-bocha-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("OpenSquilla")

    assert exc_info.value.kind == kind
    assert exc_info.value.retryable is True
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_bocha_malformed_json_is_terminal_parse_error() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="not json")

    provider = BochaSearchProvider(
        api_key="dummy-bocha-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("OpenSquilla")

    assert exc_info.value.kind == "parse"
    assert exc_info.value.retryable is False
    assert len(requests) == 1


def test_bocha_provider_spec_is_runtime_supported_after_import() -> None:
    import opensquilla.search.providers.bocha  # noqa: F401

    spec = get_provider_spec("bocha")

    assert spec.runtime_supported is True
    assert spec.requires_api_key is True
    assert spec.env_key == "BOCHA_SEARCH_API_KEY"
    assert spec.capabilities == frozenset({"web", "freshness", "content"})
