"""Alibaba Cloud IQS provider — uses the IQS unified search API."""

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

_API_URL = "https://cloud-iqs.aliyuncs.com/search/unified"
# The unified endpoint rejects queries longer than 500 characters outright.
_QUERY_MAX_CHARS = 500
# LiteAdvanced is the only engine that honors numResults and site filters,
# and it bills at the flat low-cost tier.
_ENGINE_TYPE = "LiteAdvanced"
_RECENCY_TIME_RANGE: dict[Recency, str] = {
    "day": "OneDay",
    "week": "OneWeek",
    "month": "OneMonth",
    "year": "OneYear",
}
_RESULT_METADATA_EXCLUDE = {
    "title",
    "link",
    "snippet",
    "summary",
    "mainText",
    "markdownText",
    "richMainBody",
    "publishedTime",
    "published_at",
    "rerankScore",
}


class IqsSearchProvider:
    """Search provider using the Alibaba Cloud IQS unified search endpoint."""

    name: str = "iqs"

    def __init__(
        self,
        api_key: str = "",
        proxy: str = "",
        use_env_proxy: bool = False,
        diagnostics: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = clean_header_secret(
            api_key or os.environ.get("IQS_SEARCH_API_KEY", ""),
            label="IQS API key",
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
                message="IQS API key not set",
                retryable=False,
            )

        result_limit = min(max(int(max_results), 1), 20)
        advanced_params: dict[str, Any] = {"numResults": result_limit}
        if include_domains:
            advanced_params["includeSites"] = ",".join(include_domains)
        if exclude_domains:
            advanced_params["excludeSites"] = ",".join(exclude_domains)
        body: dict[str, Any] = {
            "query": query[:_QUERY_MAX_CHARS],
            "engineType": _ENGINE_TYPE,
            # contents.summary stays off: it is a separately billed add-on,
            # while mainText is free and feeds SearchResult.content.
            "contents": {"mainText": True, "rerankScore": True},
            "advancedParams": advanced_params,
        }
        if recency:
            body["timeRange"] = _RECENCY_TIME_RANGE[recency]

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
                message=str(exc) or "IQS search request timed out.",
                retryable=True,
            ) from exc
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            kind = _classify_status(status_code)
            detail = _error_detail(exc.response)
            message = f"IQS search failed with HTTP {status_code}."
            if detail:
                message = f"IQS search failed with HTTP {status_code} ({detail})."
            raise SearchProviderError(
                provider=self.name,
                kind=kind,
                message=message,
                retryable=_is_retryable_status(status_code, kind),
                status_code=status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise SearchProviderError(
                provider=self.name,
                kind="network",
                message=str(exc) or "IQS search network request failed.",
                retryable=True,
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise SearchProviderError(
                provider=self.name,
                kind="parse",
                message="IQS search returned malformed JSON.",
                retryable=False,
            ) from exc

        items = data.get("pageItems") if isinstance(data, dict) else None
        if not isinstance(items, list):
            items = []
        return [
            _result_from_item(item, data)
            for item in items[:result_limit]
            if isinstance(item, dict)
        ]


def _classify_status(status_code: int) -> SearchErrorKind:
    # IQS reports bad credentials as 403 (Retrieval.InvalidAPIKey) and, on the
    # SDK-compatible path, as 404 (InvalidAccessKeyId.NotFound); neither is
    # worth retrying against the same key.
    if status_code in {401, 403, 404}:
        return "auth"
    if status_code == 429:
        return "rate_limit"
    return "http"


def _is_retryable_status(status_code: int, kind: SearchErrorKind) -> bool:
    del kind
    return is_retryable_http_status(status_code)


def _error_detail(response: httpx.Response) -> str:
    # Error bodies are usually {"errorCode", "errorMessage"} JSON, but some
    # 400s return a bare server stack trace as text.
    try:
        data = response.json()
    except ValueError:
        return response.text.strip()[:200]
    if not isinstance(data, dict):
        return ""
    code = data.get("errorCode") or data.get("code")
    message = data.get("errorMessage") or data.get("message")
    parts = [str(part) for part in (code, message) if part]
    return ": ".join(parts)[:200]


def _result_from_item(item: dict[str, Any], response_data: dict[str, Any]) -> SearchResult:
    snippet = str(item.get("snippet") or "")
    main_text = str(item.get("mainText") or "")
    return SearchResult(
        title=str(item.get("title") or ""),
        url=str(item.get("link") or ""),
        snippet=snippet,
        source="iqs",
        provider="iqs",
        published_at=item.get("publishedTime") or item.get("published_at"),
        score=_numeric_score(item.get("rerankScore")),
        content=main_text or snippet,
        raw_metadata=_safe_metadata(item, response_data),
    )


def _numeric_score(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def _safe_metadata(item: dict[str, Any], response_data: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        key: response_data[key]
        for key in ("requestId",)
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
    "iqs",
    IqsSearchProvider,
    SearchProviderSpec(
        provider_id="iqs",
        requires_api_key=True,
        env_key="IQS_SEARCH_API_KEY",
        capabilities=frozenset({"web", "freshness", "domain_filter", "content"}),
    ),
)
