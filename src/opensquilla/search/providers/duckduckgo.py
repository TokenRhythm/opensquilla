"""DuckDuckGo search provider — HTML scraper via httpx."""

from __future__ import annotations

import urllib.parse

import httpx
from bs4 import BeautifulSoup

from opensquilla.search.registry import register_provider
from opensquilla.search.retry_policy import is_retryable_http_status
from opensquilla.search.types import SearchErrorKind, SearchProviderError, SearchResult

_DDHTML_URL = "https://html.duckduckgo.com/html"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}
_BLOCKED_MARKERS = (
    "anomaly.js",
    "challenge-form",
    "anomaly-modal",
    "bots use duckduckgo",
    "please complete the following challenge",
)
_NO_RESULTS_MARKERS = (
    "no results found",
    "no results.",
    "not many results contain",
)


class DuckDuckGoProvider:
    """Search provider using DuckDuckGo HTML endpoint."""

    name: str = "duckduckgo"

    def __init__(
        self,
        proxy: str = "",
        use_env_proxy: bool = False,
        diagnostics: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._proxy = proxy or None
        self._trust_env = bool(use_env_proxy) and not self._proxy
        self._diagnostics = bool(diagnostics)
        self._transport = transport

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                proxy=self._proxy,
                trust_env=self._trust_env,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    _DDHTML_URL,
                    data={"q": query, "b": "", "kl": ""},
                    headers=_HEADERS,
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            if isinstance(exc, httpx.TimeoutException):
                kind: SearchErrorKind = "timeout"
                retryable = True
            elif isinstance(exc, httpx.HTTPStatusError):
                kind = "http"
                retryable = is_retryable_http_status(exc.response.status_code)
            else:
                kind = "network"
                retryable = True
            raise SearchProviderError(
                provider=self.name,
                kind=kind,
                message=str(exc) or "DuckDuckGo search request failed.",
                retryable=retryable,
                status_code=(
                    exc.response.status_code
                    if isinstance(exc, httpx.HTTPStatusError)
                    else None
                ),
            ) from exc

        if response.status_code == 202 or _is_blocked_response(response.text):
            raise SearchProviderError(
                provider=self.name,
                kind="blocked",
                message="DuckDuckGo returned an anti-bot challenge.",
                retryable=False,
                status_code=response.status_code,
            )

        soup = BeautifulSoup(response.text, "html.parser")
        results: list[SearchResult] = []

        for elem in soup.select(".result"):
            title_a = elem.select_one(".result__title a")
            if not title_a:
                continue

            title = title_a.get_text(strip=True)
            href_value = title_a.get("href", "")
            href = href_value if isinstance(href_value, str) else ""

            # Skip ads
            if "y.js" in href:
                continue

            # Clean DDG redirect URLs
            if "//duckduckgo.com/l/?uddg=" in href:
                href = urllib.parse.unquote(href.split("uddg=")[1].split("&")[0])

            snippet_elem = elem.select_one(".result__snippet")
            snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""

            results.append(
                SearchResult(
                    title=title,
                    url=href,
                    snippet=snippet,
                    source="duckduckgo",
                    provider="duckduckgo",
                )
            )
            if len(results) >= max_results:
                break

        if not results and not _is_no_results_response(soup):
            raise SearchProviderError(
                provider=self.name,
                kind="parse",
                message="DuckDuckGo search response did not contain parseable results.",
                retryable=False,
                status_code=response.status_code,
            )

        return results


def _is_blocked_response(html: str) -> bool:
    lowered = html.lower()
    return any(marker in lowered for marker in _BLOCKED_MARKERS)


def _is_no_results_response(soup: BeautifulSoup) -> bool:
    if soup.select_one("#no-results, .no-results, .no-results__message, .result--no-result"):
        return True
    lowered = " ".join(soup.get_text(" ", strip=True).lower().split())
    return any(marker in lowered for marker in _NO_RESULTS_MARKERS)


register_provider("duckduckgo", DuckDuckGoProvider)
