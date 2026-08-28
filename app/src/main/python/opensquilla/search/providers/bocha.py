"""Bocha Search provider - uses the Bocha Web Search API."""

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

_API_URL = "https://api.bochaai.com/v1/web-search"
_RECENCY_FRESHNESS: dict[Recency, str] = {
    "day": "oneDay",
    "week": "oneWeek",
    "month": "oneMonth",
    "year": "oneYear",
}
_RESULT_METADATA_EXCLUDE = {
    "name",
    "title",
    "url",
    "snippet",
    "summary",
    "datePublished",
    "published_at",
}


class BochaSearchProvider:
    """Search provider using Bocha's web search endpoint with inline summaries."""

    name: str = "bocha"

    def __init__(
        self,
        api_key: str = "",
        proxy: str = "",
        use_env_proxy: bool = False,
        diagnostics: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = clean_header_secret(
            api_key or os.environ.get("BOCHA_SEARCH_API_KEY", ""),
            label="Bocha API key",
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
                message="Bocha API key not set",
                retryable=False,
            )

        result_limit = min(max(int(max_results), 1), 20)
        body: dict[str, Any] = {
            "query": query,
            "count": result_limit,
            "summary": True,
        }
        if recency:
            body["freshness"] = _RECENCY_FRESHNESS[recency]

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
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                )
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise SearchProviderError(
                provider=self.name,
                kind="timeout",
                message=str(exc) or "Bocha search request timed out.",
                retryable=True,
            ) from exc
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            kind = _classify_status(status_code)
            raise SearchProviderError(
                provider=self.name,
                kind=kind,
                message=f"Bocha search failed with HTTP {status_code}.",
                retryable=_is_retryable_status(status_code, kind),
                status_code=status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise SearchProviderError(
                provider=self.name,
                kind="network",
                message=str(exc) or "Bocha search network request failed.",
                retryable=True,
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise SearchProviderError(
                provider=self.name,
                kind="parse",
                message="Bocha search returned malformed JSON.",
                retryable=False,
            ) from exc
        if not isinstance(data, dict):
            raise SearchProviderError(
                provider=self.name,
                kind="parse",
                message="Bocha search returned an invalid response payload.",
                retryable=False,
            )

        _raise_for_api_error(data)
        return [
            _result_from_item(item, data)
            for item in _result_items(data)[:result_limit]
        ]


def _classify_status(status_code: int) -> SearchErrorKind:
    if status_code in {401, 403}:
        return "auth"
    if status_code == 429:
        return "rate_limit"
    return "http"


def _is_retryable_status(status_code: int, kind: SearchErrorKind) -> bool:
    del kind
    return is_retryable_http_status(status_code)


def _raise_for_api_error(data: dict[str, Any]) -> None:
    code = data.get("code")
    status_code = _api_status_code(code)
    if code is None or status_code == 200:
        return
    kind: SearchErrorKind = (
        _classify_status(status_code) if status_code is not None else "http"
    )
    message = str(data.get("msg") or data.get("message") or "Bocha search request failed.")
    raise SearchProviderError(
        provider="bocha",
        kind=kind,
        message=message,
        retryable=(
            _is_retryable_status(status_code, kind) if status_code is not None else False
        ),
        status_code=status_code,
    )


def _api_status_code(code: object) -> int | None:
    if isinstance(code, bool):
        return None
    if isinstance(code, int):
        return code
    if isinstance(code, str):
        normalized = code.strip()
        if normalized.isdecimal():
            return int(normalized)
    return None


def _result_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    payload = data.get("data")
    if not isinstance(payload, dict):
        return []

    web_pages = payload.get("webPages")
    if isinstance(web_pages, dict):
        value = web_pages.get("value")
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    results = payload.get("results")
    if isinstance(results, list):
        return [item for item in results if isinstance(item, dict)]

    value = payload.get("value")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]

    return []


def _result_from_item(item: dict[str, Any], response_data: dict[str, Any]) -> SearchResult:
    summary = str(item.get("summary") or "")
    snippet = str(item.get("snippet") or item.get("description") or "")
    return SearchResult(
        title=str(item.get("name") or item.get("title") or ""),
        url=str(item.get("url") or item.get("id") or ""),
        snippet=snippet,
        source="bocha",
        provider="bocha",
        published_at=item.get("datePublished") or item.get("published_at"),
        content=summary or snippet,
        raw_metadata=_safe_metadata(item, response_data),
    )


def _safe_metadata(item: dict[str, Any], response_data: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        key: response_data[key]
        for key in ("log_id", "msg")
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
    "bocha",
    BochaSearchProvider,
    SearchProviderSpec(
        provider_id="bocha",
        requires_api_key=True,
        env_key="BOCHA_SEARCH_API_KEY",
        capabilities=frozenset({"web", "freshness", "content"}),
    ),
)
