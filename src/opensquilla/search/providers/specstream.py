"""SpecStream Search provider — uses the SpecStream Web Search API."""

from __future__ import annotations

import os
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

_API_URL = "https://api.specstream.ai/v1/web-search"
_RESULT_METADATA_EXCLUDE = {
    "title",
    "url",
    "snippet",
    "excerpt",
    "published",
    "source",
}


class SpecStreamSearchProvider:
    """Search provider using SpecStream's live web search endpoint."""

    name: str = "specstream"

    def __init__(
        self,
        api_key: str = "",
        proxy: str = "",
        use_env_proxy: bool = False,
        diagnostics: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = clean_header_secret(
            api_key or os.environ.get("SPECSTREAM_API_KEY", ""),
            label="SpecStream API key",
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
                message="SpecStream API key not set",
                retryable=False,
            )

        result_limit = min(max(int(max_results), 1), 20)
        effective_query = _apply_domain_filters(query, include_domains, exclude_domains)
        body: dict[str, Any] = {
            "q": effective_query,
            "num": result_limit,
            "passages": True,
        }
        if recency in {"day", "week"}:
            body["news"] = True

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
                    _API_URL,
                    provider=self.name,
                    operation="web_search",
                    base_url="https://api.specstream.ai",
                    json=body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self._api_key}",
                    },
                )
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise SearchProviderError(
                provider=self.name,
                kind="timeout",
                message=str(exc) or "SpecStream search request timed out.",
                retryable=True,
            ) from exc
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            kind = _classify_status(status_code)
            raise SearchProviderError(
                provider=self.name,
                kind=kind,
                message=f"SpecStream search failed with HTTP {status_code}.",
                retryable=_is_retryable_status(status_code, kind),
                status_code=status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise SearchProviderError(
                provider=self.name,
                kind="network",
                message=str(exc) or "SpecStream search network request failed.",
                retryable=True,
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise SearchProviderError(
                provider=self.name,
                kind="parse",
                message="SpecStream search returned malformed JSON.",
                retryable=False,
            ) from exc
        if not isinstance(data, dict):
            raise SearchProviderError(
                provider=self.name,
                kind="parse",
                message="SpecStream search returned an invalid response payload.",
                retryable=False,
            )
        sources = data.get("sources") or []
        if not isinstance(sources, list):
            return []
        return [
            _result_from_item(item, data)
            for item in sources[:result_limit]
            if isinstance(item, dict)
        ]


def _apply_domain_filters(
    query: str,
    include_domains: tuple[str, ...],
    exclude_domains: tuple[str, ...],
) -> str:
    parts = [query.strip()]
    if include_domains:
        cleaned = [d.strip() for d in include_domains if d.strip()]
        if len(cleaned) == 1:
            parts.append(f"site:{cleaned[0]}")
        elif cleaned:
            parts.append("(" + " OR ".join(f"site:{d}" for d in cleaned) + ")")
    for domain in exclude_domains:
        d = domain.strip()
        if d:
            parts.append(f"-site:{d}")
    return " ".join(p for p in parts if p)


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
    snippet = str(item.get("snippet") or "")
    excerpt = str(item.get("excerpt") or "")
    best_snippet = snippet or excerpt
    return SearchResult(
        title=str(item.get("title") or ""),
        url=str(item.get("url") or ""),
        snippet=best_snippet,
        source="specstream",
        provider="specstream",
        published_at=item.get("published"),
        content=excerpt or best_snippet,
        raw_metadata=_safe_metadata(item, response_data),
    )


def _safe_metadata(item: dict[str, Any], response_data: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        key: response_data[key]
        for key in ("request_id", "tier")
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
    "specstream",
    SpecStreamSearchProvider,
    SearchProviderSpec(
        provider_id="specstream",
        requires_api_key=True,
        env_key="SPECSTREAM_API_KEY",
        capabilities=frozenset({"web", "freshness", "domain_filter", "content"}),
    ),
)
