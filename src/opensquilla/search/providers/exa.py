"""Exa Search provider — uses the Exa Search API."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from opensquilla.search.registry import register_provider
from opensquilla.search.retry_policy import is_retryable_http_status
from opensquilla.search.types import (
    Recency,
    SearchErrorKind,
    SearchProviderError,
    SearchProviderSpec,
    SearchResult,
)
from opensquilla.secrets import clean_header_secret

_API_URL = "https://api.exa.ai/search"
_RESULT_METADATA_EXCLUDE = {
    "title",
    "url",
    "score",
    "publishedDate",
    "published_at",
    "text",
    "highlights",
    "summary",
}
_RECENCY_DAYS: dict[Recency, int] = {
    "day": 1,
    "week": 7,
    "month": 31,
    "year": 365,
}


class ExaSearchProvider:
    """Search provider using Exa's search endpoint with inline contents."""

    name: str = "exa"

    def __init__(
        self,
        api_key: str = "",
        proxy: str = "",
        use_env_proxy: bool = False,
        diagnostics: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = clean_header_secret(
            api_key or os.environ.get("EXA_API_KEY", ""),
            label="Exa API key",
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
                message="Exa API key not set",
                retryable=False,
            )

        result_limit = min(max(int(max_results), 1), 20)
        body: dict[str, Any] = {
            "query": query,
            "numResults": result_limit,
            "type": "auto",
            "contents": {
                "text": {"maxCharacters": 1500},
                "highlights": {"maxCharacters": 500, "query": query},
                "summary": {"query": query},
            },
        }
        if include_domains:
            body["includeDomains"] = list(include_domains)
        if exclude_domains:
            body["excludeDomains"] = list(exclude_domains)
        if recency:
            body["startPublishedDate"] = _recency_start_date(recency)

        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                proxy=self._proxy,
                trust_env=self._trust_env,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    _API_URL,
                    json=body,
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": self._api_key,
                    },
                )
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise SearchProviderError(
                provider=self.name,
                kind="timeout",
                message=str(exc) or "Exa search request timed out.",
                retryable=True,
            ) from exc
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            kind = _classify_status(status_code)
            raise SearchProviderError(
                provider=self.name,
                kind=kind,
                message=f"Exa search failed with HTTP {status_code}.",
                retryable=_is_retryable_status(status_code, kind),
                status_code=status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise SearchProviderError(
                provider=self.name,
                kind="network",
                message=str(exc) or "Exa search network request failed.",
                retryable=True,
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise SearchProviderError(
                provider=self.name,
                kind="parse",
                message="Exa search returned malformed JSON.",
                retryable=False,
            ) from exc
        if not isinstance(data, dict):
            raise SearchProviderError(
                provider=self.name,
                kind="parse",
                message="Exa search returned an invalid response payload.",
                retryable=False,
            )
        return [
            _result_from_item(item, data)
            for item in (data.get("results") or [])[:result_limit]
        ]


def _recency_start_date(recency: Recency) -> str:
    return (datetime.now(UTC).date() - timedelta(days=_RECENCY_DAYS[recency])).isoformat()


def _classify_status(status_code: int) -> SearchErrorKind:
    if status_code in {401, 403}:
        return "auth"
    if status_code == 429:
        return "rate_limit"
    return "http"


def _is_retryable_status(status_code: int, kind: SearchErrorKind) -> bool:
    del kind
    return is_retryable_http_status(status_code)


def _result_from_item(item: dict[str, Any], response_data: dict[str, Any]) -> SearchResult:
    text = str(item.get("text") or "")
    highlights = _string_list(item.get("highlights"))
    summary = str(item.get("summary") or "")
    snippet = _first_non_empty(summary, highlights[0] if highlights else "", text)
    return SearchResult(
        title=str(item.get("title") or ""),
        url=str(item.get("url") or ""),
        snippet=snippet,
        source="exa",
        provider="exa",
        published_at=item.get("publishedDate") or item.get("published_at"),
        score=_numeric_score(item.get("score")),
        highlights=highlights,
        content=text or snippet,
        raw_metadata=_safe_metadata(item, response_data),
    )


def _first_non_empty(*values: str) -> str:
    for value in values:
        if value.strip():
            return value
    return ""


def _numeric_score(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _safe_metadata(item: dict[str, Any], response_data: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        key: response_data[key]
        for key in ("requestId", "request_id")
        if key in response_data
    }
    metadata.update(
        {
            key: value
            for key, value in item.items()
            if key not in _RESULT_METADATA_EXCLUDE
        }
    )
    return metadata


register_provider(
    "exa",
    ExaSearchProvider,
    SearchProviderSpec(
        provider_id="exa",
        requires_api_key=True,
        env_key="EXA_API_KEY",
        capabilities=frozenset({"web", "freshness", "domain_filter", "semantic", "content"}),
    ),
)
