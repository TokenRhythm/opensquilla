from __future__ import annotations

import httpx
import pytest

from opensquilla.gateway.config import GatewayConfig
from opensquilla.search.providers.brave import BraveSearchProvider
from opensquilla.search.providers.duckduckgo import DuckDuckGoProvider
from opensquilla.search.providers.exa import ExaSearchProvider
from opensquilla.search.types import SearchProviderError, SearchResult
from opensquilla.tools.builtin import web


def test_gateway_config_accepts_search_api_key() -> None:
    config = GatewayConfig(search_api_key="brave-test-key")

    assert config.search_api_key == "brave-test-key"


def test_brave_provider_prefers_explicit_api_key(monkeypatch) -> None:
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)

    provider = BraveSearchProvider(api_key="brave-test-key")

    assert provider._api_key == "brave-test-key"


def test_brave_provider_strips_trailing_paste_punctuation(monkeypatch) -> None:
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)

    provider = BraveSearchProvider(api_key="brave-test-key、")

    assert provider._api_key == "brave-test-key"


def test_web_search_kwargs_pass_brave_api_key() -> None:
    web.configure_search("brave", api_key="brave-test-key")

    assert web._search_provider_kwargs("brave")["api_key"] == "brave-test-key"


def test_web_search_kwargs_pass_tavily_api_key() -> None:
    web.configure_search("tavily", api_key="tavily-test-key")

    assert web._search_provider_kwargs("tavily")["api_key"] == "tavily-test-key"


def test_web_search_kwargs_pass_exa_api_key() -> None:
    web.configure_search("exa", api_key="exa-test-key")

    assert web._search_provider_kwargs("exa")["api_key"] == "exa-test-key"


def test_exa_provider_prefers_explicit_api_key(monkeypatch) -> None:
    monkeypatch.delenv("EXA_API_KEY", raising=False)

    provider = ExaSearchProvider(api_key="exa-test-key")

    assert provider._api_key == "exa-test-key"


@pytest.mark.asyncio
async def test_brave_provider_maps_provider_source_and_published_at() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Brave title",
                            "url": "https://example.com/brave",
                            "description": "Brave snippet",
                            "age": "2026-06-19",
                        }
                    ]
                }
            },
        )

    provider = BraveSearchProvider(
        api_key="brave-test-key",
        transport=httpx.MockTransport(handler),
    )

    result = (await provider.search("brave"))[0]

    assert result.provider == "brave"
    assert result.source == "brave"
    assert result.published_at == "2026-06-19"


@pytest.mark.asyncio
async def test_brave_provider_passes_recency_as_freshness() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"web": {"results": []}})

    provider = BraveSearchProvider(
        api_key="brave-test-key",
        transport=httpx.MockTransport(handler),
    )

    await provider.search("brave", recency="week")

    assert requests[0].url.params["freshness"] == "pw"


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
async def test_brave_http_errors_are_classified_once(
    status_code: int,
    kind: str,
    retryable: bool,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json={"error": "nope"})

    provider = BraveSearchProvider(
        api_key="brave-test-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("brave")

    assert exc_info.value.kind == kind
    assert exc_info.value.retryable is retryable
    assert exc_info.value.status_code == status_code
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "kind"),
    [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "network")],
)
async def test_brave_transport_errors_are_retryable_once(
    error_type: type[httpx.HTTPError],
    kind: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise error_type("transient failure", request=request)

    provider = BraveSearchProvider(
        api_key="brave-test-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("brave")

    assert exc_info.value.kind == kind
    assert exc_info.value.retryable is True
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_brave_malformed_json_is_terminal_parse_error() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="not json")

    provider = BraveSearchProvider(
        api_key="brave-test-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("brave")

    assert exc_info.value.kind == "parse"
    assert exc_info.value.retryable is False
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_duckduckgo_provider_maps_provider_and_source() -> None:
    html = """
    <html>
      <body>
        <div class="result">
          <h2 class="result__title"><a href="https://example.com/ddg">DDG title</a></h2>
          <a class="result__snippet">DDG snippet</a>
        </div>
      </body>
    </html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    provider = DuckDuckGoProvider(transport=httpx.MockTransport(handler))

    result = (await provider.search("duck"))[0]

    assert result.provider == "duckduckgo"
    assert result.source == "duckduckgo"


@pytest.mark.asyncio
async def test_duckduckgo_provider_treats_anomaly_challenge_as_blocked() -> None:
    html = """
    <html>
      <body>
        <form id="challenge-form" action="//duckduckgo.com/anomaly.js">
          <div class="anomaly-modal">Unfortunately, bots use DuckDuckGo too.</div>
        </form>
      </body>
    </html>
    """

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202, text=html)

    provider = DuckDuckGoProvider(transport=httpx.MockTransport(handler))

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("duck")

    assert exc_info.value.provider == "duckduckgo"
    assert exc_info.value.kind == "blocked"
    assert exc_info.value.retryable is False
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_duckduckgo_provider_allows_real_no_results() -> None:
    html = """
    <html>
      <body>
        <div class="no-results">No results found for improbable query.</div>
      </body>
    </html>
    """

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=html)

    provider = DuckDuckGoProvider(transport=httpx.MockTransport(handler))

    assert await provider.search("improbable query") == []
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_duckduckgo_provider_treats_unparseable_empty_page_as_parse_error() -> None:
    html = "<html><body><main>DuckDuckGo</main></body></html>"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=html)

    provider = DuckDuckGoProvider(transport=httpx.MockTransport(handler))

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("duck")

    assert exc_info.value.provider == "duckduckgo"
    assert exc_info.value.kind == "parse"
    assert exc_info.value.retryable is False
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [(400, False), (401, False), (403, False), (408, True), (429, True), (500, True)],
)
async def test_duckduckgo_http_errors_are_classified_once(
    status_code: int,
    retryable: bool,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, text="upstream error")

    provider = DuckDuckGoProvider(transport=httpx.MockTransport(handler))

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("duck")

    assert exc_info.value.provider == "duckduckgo"
    assert exc_info.value.kind == "http"
    assert exc_info.value.retryable is retryable
    assert exc_info.value.status_code == status_code
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "kind"),
    [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "network")],
)
async def test_duckduckgo_transport_errors_are_retryable_once(
    error_type: type[httpx.HTTPError],
    kind: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise error_type("transient failure", request=request)

    provider = DuckDuckGoProvider(transport=httpx.MockTransport(handler))

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("duck")

    assert exc_info.value.provider == "duckduckgo"
    assert exc_info.value.kind == kind
    assert exc_info.value.retryable is True
    assert len(requests) == 1


def test_search_payload_keeps_lightweight_metadata() -> None:
    payload = web._search_payload(
        "python",
        "tavily",
        [
            SearchResult(
                title="Title",
                url="https://Docs.Python.org/3/?utm_source=x#intro",
                snippet="Snippet",
                provider="tavily",
                source="tavily",
                published_at="2026-06-19",
                score=0.9,
                content="Full content must stay out",
                raw_metadata={"debug": "must stay out"},
            )
        ],
    )

    result = payload["results"][0]
    assert result == {
        "title": "Title",
        "url": "https://Docs.Python.org/3/?utm_source=x#intro",
        "snippet": "Snippet",
        "provider": "tavily",
        "published_at": "2026-06-19",
        "score": 0.9,
        "domain": "docs.python.org",
        "canonical_url": "https://docs.python.org/3",
    }
