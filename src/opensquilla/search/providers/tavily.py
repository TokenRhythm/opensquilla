"""Tavily Search provider — uses the Tavily Search API."""

from __future__ import annotations

import os
from typing import Any

import httpx

from opensquilla.search.registry import register_provider
from opensquilla.search.retry_policy import is_retryable_http_status
from opensquilla.search.types import Recency, SearchErrorKind, SearchProviderError, SearchResult
from opensquilla.secrets import clean_header_secret

_API_URL = "https://api.tavily.com/search"
_RESULT_METADATA_EXCLUDE = {
    "title",
    "url",
    "link",
    "content",
    "snippet",
    "raw_content",
    "highlights",
}


class TavilySearchProvider:
    """Search provider using the Tavily Search API."""

    name: str = "tavily"

    def __init__(
        self,
        api_key: str = "",
        proxy: str = "",
        use_env_proxy: bool = False,
        diagnostics: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = clean_header_secret(
            api_key or os.environ.get("TAVILY_API_KEY", ""),
            label="Tavily API key",
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
        include_domains: tuple[str, ...] = (),
        exclude_domains: tuple[str, ...] = (),
    ) -> list[SearchResult]:
        if not self._api_key:
            raise SearchProviderError(
                provider=self.name,
                kind="auth",
                message="Tavily API key not set",
                retryable=False,
            )

        result_limit = min(max(int(max_results), 1), 20)
        body = {
            "api_key": self._api_key,
            "query": query,
            "max_results": result_limit,
            "search_depth": "basic",
            "include_raw_content": False,
            "include_answer": False,
            "include_images": False,
        }
        if recency:
            body["time_range"] = recency
        if include_domains:
            body["include_domains"] = list(include_domains)
        if exclude_domains:
            body["exclude_domains"] = list(exclude_domains)

        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                proxy=self._proxy,
                trust_env=self._trust_env,
                transport=self._transport,
            ) as client:
                response = await client.post(_API_URL, json=body)
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise SearchProviderError(
                provider=self.name,
                kind="timeout",
                message=str(exc) or "Tavily search request timed out.",
                retryable=True,
            ) from exc
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            kind = _classify_status(status_code)
            raise SearchProviderError(
                provider=self.name,
                kind=kind,
                message=f"Tavily search failed with HTTP {status_code}.",
                retryable=_is_retryable_status(status_code, kind),
                status_code=status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise SearchProviderError(
                provider=self.name,
                kind="network",
                message=str(exc) or "Tavily search network request failed.",
                retryable=True,
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise SearchProviderError(
                provider=self.name,
                kind="parse",
                message="Tavily search returned malformed JSON.",
                retryable=False,
            ) from exc
        if not isinstance(data, dict):
            raise SearchProviderError(
                provider=self.name,
                kind="parse",
                message="Tavily search returned an invalid response payload.",
                retryable=False,
            )
        return [
            _result_from_item(item, data) for item in (data.get("results") or [])[:result_limit]
        ]


def _classify_status(status_code: int) -> SearchErrorKind:
    if status_code in {401, 403}:
        return "auth"
    if status_code in {429, 432, 433}:
        return "rate_limit"
    return "http"


def _is_retryable_status(status_code: int, kind: SearchErrorKind) -> bool:
    del kind
    return is_retryable_http_status(status_code)


def _result_from_item(item: dict[str, Any], response_data: dict[str, Any]) -> SearchResult:
    snippet = str(item.get("content") or item.get("snippet") or "")
    raw_content = item.get("raw_content")
    highlights = _string_list(item.get("highlights"))
    return SearchResult(
        title=str(item.get("title", "")),
        url=str(item.get("url") or item.get("link") or ""),
        snippet=snippet,
        source="tavily",
        provider="tavily",
        published_at=item.get("published_date") or item.get("published_at"),
        score=_numeric_score(item.get("score")),
        highlights=highlights,
        content=str(raw_content) if raw_content else snippet,
        raw_metadata=_safe_metadata(item, response_data),
    )


def _numeric_score(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    if not all(isinstance(item, str) for item in value):
        return []
    return value


def _safe_metadata(item: dict[str, Any], response_data: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        key: response_data[key]
        for key in ("request_id", "response_time", "usage")
        if key in response_data
    }
    metadata.update(
        {
            key: value
            for key, value in item.items()
            if key not in _RESULT_METADATA_EXCLUDE and key != "api_key"
        }
    )
    return metadata


register_provider("tavily", TavilySearchProvider)
