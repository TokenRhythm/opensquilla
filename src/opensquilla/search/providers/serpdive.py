"""SERPdive search provider — uses the SERPdive Search API."""

from __future__ import annotations

import os
from typing import Any

import httpx

from opensquilla.search.registry import register_provider
from opensquilla.search.types import SearchErrorKind, SearchProviderError, SearchResult
from opensquilla.secrets import clean_header_secret

_API_URL = "https://api.serpdive.com/v1/search"
_MAX_RESULTS_CAP = 10
_RESULT_METADATA_EXCLUDE = {
    "title",
    "url",
    "content",
}


class SerpdiveSearchProvider:
    """Search provider using the SERPdive Search API."""

    name: str = "serpdive"

    def __init__(
        self,
        api_key: str = "",
        proxy: str = "",
        use_env_proxy: bool = False,
        diagnostics: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = clean_header_secret(
            api_key or os.environ.get("SERPDIVE_API_KEY", ""),
            label="SERPdive API key",
        )
        self._proxy = proxy or None
        self._trust_env = bool(use_env_proxy) and not self._proxy
        self._diagnostics = bool(diagnostics)
        self._transport = transport

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        if not self._api_key:
            raise SearchProviderError(
                provider=self.name,
                kind="auth",
                message="SERPdive API key not set",
                retryable=False,
            )

        result_limit = min(max(int(max_results), 1), _MAX_RESULTS_CAP)
        body = {
            "query": query,
            "max_results": result_limit,
        }

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
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise SearchProviderError(
                provider=self.name,
                kind="timeout",
                message=str(exc) or "SERPdive search request timed out.",
                retryable=True,
            ) from exc
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            kind = _classify_status(status_code)
            raise SearchProviderError(
                provider=self.name,
                kind=kind,
                message=f"SERPdive search failed with HTTP {status_code}.",
                retryable=_is_retryable_status(status_code, kind),
                status_code=status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise SearchProviderError(
                provider=self.name,
                kind="network",
                message=str(exc) or "SERPdive search network request failed.",
                retryable=True,
            ) from exc

        data = response.json()
        return [
            _result_from_item(item, data) for item in (data.get("results") or [])[:result_limit]
        ]


def _classify_status(status_code: int) -> SearchErrorKind:
    if status_code in {401, 403}:
        return "auth"
    if status_code == 429:
        return "rate_limit"
    return "http"


def _is_retryable_status(status_code: int, kind: SearchErrorKind) -> bool:
    if status_code == 429:
        return True
    if kind == "http":
        return True
    return False


def _result_from_item(item: dict[str, Any], response_data: dict[str, Any]) -> SearchResult:
    content = str(item.get("content") or "")
    return SearchResult(
        title=str(item.get("title", "")),
        url=str(item.get("url", "")),
        snippet=content,
        source="serpdive",
        provider="serpdive",
        published_at=item.get("date"),
        content=content,
        raw_metadata=_safe_metadata(item, response_data),
    )


def _safe_metadata(item: dict[str, Any], response_data: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        key: response_data[key]
        for key in ("model", "response_time_ms")
        if key in response_data
    }
    metadata.update(
        {key: value for key, value in item.items() if key not in _RESULT_METADATA_EXCLUDE}
    )
    return metadata


register_provider("serpdive", SerpdiveSearchProvider)
