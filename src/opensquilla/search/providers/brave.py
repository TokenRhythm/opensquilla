"""Brave Search provider — uses the Brave Web Search API."""

from __future__ import annotations

import os

import httpx

from opensquilla.search.registry import register_provider
from opensquilla.search.retry_policy import is_retryable_http_status
from opensquilla.search.types import Recency, SearchErrorKind, SearchProviderError, SearchResult
from opensquilla.secrets import clean_header_secret

_API_URL = "https://api.search.brave.com/res/v1/web/search"
_RECENCY_FRESHNESS = {
    "day": "pd",
    "week": "pw",
    "month": "pm",
    "year": "py",
}
type _QueryParamValue = str | int | float | bool | None


class BraveSearchProvider:
    """Search provider using the Brave Search API."""

    name: str = "brave"

    def __init__(
        self,
        api_key: str = "",
        proxy: str = "",
        use_env_proxy: bool = False,
        diagnostics: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = clean_header_secret(
            api_key or os.environ.get("BRAVE_SEARCH_API_KEY", ""),
            label="Brave Search API key",
        )
        self._proxy = proxy or None
        self._trust_env = bool(use_env_proxy) and not self._proxy
        self._diagnostics = bool(diagnostics)
        self._transport = transport

    async def search(
        self,
        query: str,
        max_results: int = 5,
        *,
        recency: Recency | None = None,
    ) -> list[SearchResult]:
        if not self._api_key:
            raise SearchProviderError(
                provider=self.name,
                kind="auth",
                message="Brave search API key not set",
                retryable=False,
            )

        try:
            params: dict[str, _QueryParamValue] = {"q": query, "count": min(max_results, 20)}
            if recency:
                params["freshness"] = _RECENCY_FRESHNESS[recency]

            async with httpx.AsyncClient(
                timeout=15.0,
                proxy=self._proxy,
                trust_env=self._trust_env,
                transport=self._transport,
            ) as client:
                response = await client.get(
                    _API_URL,
                    params=params,
                    headers={
                        "Accept": "application/json",
                        "X-Subscription-Token": self._api_key,
                    },
                )
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise SearchProviderError(
                provider=self.name,
                kind="timeout",
                message=str(exc) or "Brave search request timed out.",
                retryable=True,
            ) from exc
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code in {401, 403}:
                kind: SearchErrorKind = "auth"
            elif status_code == 429:
                kind = "rate_limit"
            else:
                kind = "http"
            raise SearchProviderError(
                provider=self.name,
                kind=kind,
                message=str(exc) or f"Brave search failed with HTTP {status_code}.",
                retryable=is_retryable_http_status(status_code),
                status_code=status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise SearchProviderError(
                provider=self.name,
                kind="network",
                message=str(exc) or "Brave search network request failed.",
                retryable=True,
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise SearchProviderError(
                provider=self.name,
                kind="parse",
                message="Brave search returned malformed JSON.",
                retryable=False,
            ) from exc
        if not isinstance(data, dict):
            raise SearchProviderError(
                provider=self.name,
                kind="parse",
                message="Brave search returned an invalid response payload.",
                retryable=False,
            )
        results: list[SearchResult] = []

        for item in (data.get("web", {}).get("results") or [])[:max_results]:
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("description", ""),
                    source="brave",
                    provider="brave",
                    published_at=(
                        item.get("age") or item.get("page_age") or item.get("published_at")
                    ),
                )
            )

        return results


register_provider("brave", BraveSearchProvider)
