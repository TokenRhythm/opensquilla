"""Keenable search provider — uses the Keenable search API.

Keenable works without an API key. The provider is opt-in: it is only
available once ``search_provider = "keenable"`` is selected or
``KEENABLE_API_KEY`` is set, and the key only raises rate limits.
"""

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
from opensquilla.usage_request import accounted_request

_BASE_URL = "https://api.keenable.ai"
_API_URL = f"{_BASE_URL}/v1/search"
_PUBLIC_API_URL = f"{_BASE_URL}/v1/search/public"
# Keyless requests must name the calling application.
_APP_TITLE = "OpenSquilla"
# ``snippet`` carries several thousand characters of page text; keep the
# result snippet short and pass the full text through ``content``.
_SNIPPET_MAX_CHARS = 500
_RECENCY_DAYS: dict[Recency, int] = {
    "day": 1,
    "week": 7,
    "month": 31,
    "year": 365,
}
_RESULT_METADATA_EXCLUDE = {"title", "url", "snippet", "description", "published_at"}


class KeenableSearchProvider:
    """Search provider using the Keenable search endpoint."""

    name: str = "keenable"

    def __init__(
        self,
        api_key: str = "",
        proxy: str = "",
        use_env_proxy: bool = False,
        diagnostics: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = clean_header_secret(
            api_key or os.environ.get("KEENABLE_API_KEY", ""),
            label="Keenable API key",
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
        result_limit = min(max(int(max_results), 1), 20)
        body: dict[str, Any] = {
            "query": _query_with_domain_filters(query, include_domains, exclude_domains),
            "max_results": result_limit,
        }
        if len(include_domains) == 1:
            body["site"] = include_domains[0]
        if recency:
            body["published_after"] = _recency_start_date(recency)

        headers = {
            "Content-Type": "application/json",
            "X-Keenable-Title": _APP_TITLE,
        }
        url = _PUBLIC_API_URL
        if self._api_key:
            url = _API_URL
            headers["X-API-Key"] = self._api_key

        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                proxy=self._proxy,
                trust_env=self._trust_env,
                transport=self._transport,
            ) as client:
                response = await accounted_request(
                    client,
                    "POST",
                    url,
                    provider=self.name,
                    operation="web_search",
                    base_url=_BASE_URL,
                    json=body,
                    headers=headers,
                )
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise SearchProviderError(
                provider=self.name,
                kind="timeout",
                message=str(exc) or "Keenable search request timed out.",
                retryable=True,
            ) from exc
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            kind = _classify_status(status_code)
            detail = _error_detail(exc.response)
            message = f"Keenable search failed with HTTP {status_code}."
            if detail:
                message = f"Keenable search failed with HTTP {status_code} ({detail})."
            raise SearchProviderError(
                provider=self.name,
                kind=kind,
                message=message,
                retryable=is_retryable_http_status(status_code),
                status_code=status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise SearchProviderError(
                provider=self.name,
                kind="network",
                message=str(exc) or "Keenable search network request failed.",
                retryable=True,
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise SearchProviderError(
                provider=self.name,
                kind="parse",
                message="Keenable search returned malformed JSON.",
                retryable=False,
            ) from exc

        items = data.get("results") if isinstance(data, dict) else None
        if not isinstance(items, list):
            items = []
        return [
            _result_from_item(item)
            for item in items[:result_limit]
            if isinstance(item, dict)
        ]


def _query_with_domain_filters(
    query: str,
    include_domains: tuple[str, ...],
    exclude_domains: tuple[str, ...],
) -> str:
    # The API's ``site`` field takes a single domain. Several included domains
    # and any exclusions are expressed as search operators in the query.
    parts = [query]
    if len(include_domains) > 1:
        parts.append("(" + " OR ".join(f"site:{domain}" for domain in include_domains) + ")")
    parts.extend(f"-site:{domain}" for domain in exclude_domains)
    return " ".join(parts)


def _recency_start_date(recency: Recency) -> str:
    return (datetime.now(UTC).date() - timedelta(days=_RECENCY_DAYS[recency])).isoformat()


def _classify_status(status_code: int) -> SearchErrorKind:
    if status_code in {401, 403}:
        return "auth"
    if status_code == 429:
        return "rate_limit"
    return "http"


def _error_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text.strip()[:200]
    if not isinstance(data, dict):
        return ""
    return str(data.get("message") or data.get("error") or "")[:200]


def _result_from_item(item: dict[str, Any]) -> SearchResult:
    # ``description`` is usually empty; the page text is in ``snippet``.
    text = str(item.get("snippet") or "").strip() or str(item.get("description") or "").strip()
    return SearchResult(
        title=str(item.get("title") or ""),
        url=str(item.get("url") or ""),
        snippet=_compact_snippet(text),
        source="keenable",
        provider="keenable",
        published_at=item.get("published_at") or None,
        content=text,
        raw_metadata={
            key: value
            for key, value in item.items()
            if key not in _RESULT_METADATA_EXCLUDE
        },
    )


def _compact_snippet(text: str) -> str:
    compact = " ".join(text.split())
    if len(compact) <= _SNIPPET_MAX_CHARS:
        return compact
    return compact[:_SNIPPET_MAX_CHARS].rstrip()


register_provider(
    "keenable",
    KeenableSearchProvider,
    SearchProviderSpec(
        provider_id="keenable",
        opt_in=True,
        env_key="KEENABLE_API_KEY",
        capabilities=frozenset({"web", "freshness", "domain_filter", "content"}),
    ),
)
