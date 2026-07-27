from __future__ import annotations

import json

import httpx
import pytest

from opensquilla.search.registry import get_provider_spec
from opensquilla.search.types import SearchProviderError


@pytest.mark.asyncio
async def test_exa_search_posts_content_request_and_maps_results() -> None:
    from opensquilla.search.providers.exa import ExaSearchProvider

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "requestId": "req-exa-1",
                "results": [
                    {
                        "title": "Python release notes",
                        "url": "https://docs.python.org/3/whatsnew/3.13.html",
                        "score": 0.87,
                        "publishedDate": "2026-06-18",
                        "text": "Python 3.13 release notes full text",
                        "highlights": ["Python 3.13", "release notes"],
                        "summary": "Python 3.13 changed runtime features.",
                        "author": "Python Docs",
                    }
                ],
            },
        )

    provider = ExaSearchProvider(
        api_key="dummy-exa-key",
        transport=httpx.MockTransport(handler),
    )

    results = await provider.search("Python 3.13 release notes", max_results=4)

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "https://api.exa.ai/search"
    assert requests[0].headers["x-api-key"] == "dummy-exa-key"
    body = json.loads(requests[0].content)
    assert body == {
        "query": "Python 3.13 release notes",
        "numResults": 4,
        "type": "auto",
        "contents": {
            "text": {"maxCharacters": 1500},
            "highlights": {
                "maxCharacters": 500,
                "query": "Python 3.13 release notes",
            },
            "summary": {"query": "Python 3.13 release notes"},
        },
    }

    result = results[0]
    assert result.provider == "exa"
    assert result.source == "exa"
    assert result.title == "Python release notes"
    assert result.url == "https://docs.python.org/3/whatsnew/3.13.html"
    assert result.snippet == "Python 3.13 changed runtime features."
    assert result.content == "Python 3.13 release notes full text"
    assert result.score == 0.87
    assert result.published_at == "2026-06-18"
    assert result.highlights == ["Python 3.13", "release notes"]
    assert result.raw_metadata["requestId"] == "req-exa-1"
    assert result.raw_metadata["author"] == "Python Docs"


@pytest.mark.asyncio
async def test_exa_search_posts_supported_filters() -> None:
    from opensquilla.search.providers.exa import ExaSearchProvider

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    provider = ExaSearchProvider(
        api_key="dummy-exa-key",
        transport=httpx.MockTransport(handler),
    )

    await provider.search(
        "python",
        max_results=3,
        recency="month",
        include_domains=("python.org", "docs.python.org"),
        exclude_domains=("notpython.org",),
    )

    body = json.loads(requests[0].content)
    assert body["includeDomains"] == ["python.org", "docs.python.org"]
    assert body["excludeDomains"] == ["notpython.org"]
    assert "startPublishedDate" in body


@pytest.mark.asyncio
async def test_exa_missing_api_key_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from opensquilla.search.providers.exa import ExaSearchProvider

    monkeypatch.delenv("EXA_API_KEY", raising=False)
    provider = ExaSearchProvider()

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("python")

    assert exc_info.value.provider == "exa"
    assert exc_info.value.kind == "auth"
    assert exc_info.value.retryable is False
    assert str(exc_info.value) == "Exa API key not set"


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
async def test_exa_http_errors_are_classified(
    status_code: int,
    kind: str,
    retryable: bool,
) -> None:
    from opensquilla.search.providers.exa import ExaSearchProvider

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json={"error": "nope"})

    provider = ExaSearchProvider(
        api_key="dummy-exa-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("python")

    assert exc_info.value.provider == "exa"
    assert exc_info.value.kind == kind
    assert exc_info.value.retryable is retryable
    assert exc_info.value.status_code == status_code
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "kind"),
    [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "network")],
)
async def test_exa_transport_errors_are_retryable_once(
    error_type: type[httpx.HTTPError],
    kind: str,
) -> None:
    from opensquilla.search.providers.exa import ExaSearchProvider

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise error_type("transient failure", request=request)

    provider = ExaSearchProvider(
        api_key="dummy-exa-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("python")

    assert exc_info.value.kind == kind
    assert exc_info.value.retryable is True
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_exa_malformed_json_is_terminal_parse_error() -> None:
    from opensquilla.search.providers.exa import ExaSearchProvider

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="not json")

    provider = ExaSearchProvider(
        api_key="dummy-exa-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("python")

    assert exc_info.value.kind == "parse"
    assert exc_info.value.retryable is False
    assert len(requests) == 1


def test_exa_provider_spec_is_runtime_supported_after_import() -> None:
    import opensquilla.search.providers.exa  # noqa: F401

    spec = get_provider_spec("exa")

    assert spec.runtime_supported is True
    assert spec.requires_api_key is True
    assert spec.env_key == "EXA_API_KEY"
