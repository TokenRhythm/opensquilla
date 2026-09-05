"""Canonical source-backed web search orchestration."""

from __future__ import annotations

import importlib
import inspect
import re
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import asdict, replace
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import httpx

from opensquilla.search.normalize import (
    canonicalize_query_key,
    canonicalize_url,
    dedupe_hits_by_canonical_url,
    extract_domain,
)
from opensquilla.search.runtime_config import (
    ResolvedSearchRuntime,
    get_resolved_search_runtime,
)
from opensquilla.search.types import (
    SearchDiagnostics,
    SearchExecutionPlan,
    SearchHit,
    SearchOptions,
    SearchProvider,
    SearchProviderError,
    SearchResult,
)

ProviderFactory = Callable[[str], SearchProvider]
Fetcher = Callable[[str, int], Awaitable[Any]]

_FETCH_MIN_USEFUL_CHARS = 240
_ROOT_DOMAIN_RESULT_LIMIT = 3
_SEARCH_CACHE_TTL_SECONDS = 900
_EXTERNAL_CONTENT_RE = re.compile(
    r"<external-content\b[^>]*>(?P<content>.*?)</external-content>",
    re.DOTALL | re.IGNORECASE,
)
_SEARCH_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}


async def run_canonical_web_search(
    options: SearchOptions,
    *,
    runtime: ResolvedSearchRuntime | None = None,
    provider_factory: ProviderFactory | None = None,
    fetcher: Fetcher | None = None,
    loop_guard: dict[str, Any] | None = None,
    use_cache: bool | None = None,
) -> dict[str, Any]:
    """Search, normalize, dedupe, optionally fetch excerpts, and return JSON-safe payload."""

    diagnostics = SearchDiagnostics(
        query=options.query,
        mode=options.mode,
        loop_guard=dict(loop_guard or {}),
    )

    if not options.query:
        return _failure_payload(
            options,
            diagnostics,
            error_kind="invalid_request",
            error="Search query must not be empty.",
            provider_retryable=False,
            error_class="ValueError",
        )

    resolved_runtime = runtime or get_resolved_search_runtime()
    execution_plan = resolved_runtime.execution_plan(options)
    provider_names = execution_plan.provider_names

    cache_enabled = (
        runtime is None and provider_factory is None
        if use_cache is None
        else use_cache
    )
    cache_key = _cache_key(options, execution_plan)
    if cache_enabled:
        diagnostics.cache_status = "miss"
        cached_payload = _get_cached_payload(cache_key)
        if cached_payload is not None:
            cached_payload["diagnostics"]["cache_status"] = "hit"
            cached_payload["diagnostics"]["loop_guard"] = dict(loop_guard or {})
            return cached_payload

    factory = provider_factory or resolved_runtime.build_provider
    selected_provider = ""
    raw_results: list[SearchResult] = []
    explicit_provider = options.provider is not None

    for attempt_index, provider_name in enumerate(provider_names):
        try:
            provider = factory(provider_name)
            search_options, recency_supported, recency_degraded = (
                _effective_options_for_provider(
                    options,
                    resolved_runtime.provider_config(provider_name),
                )
            )
            raw_results = await _search_provider(provider, search_options)
        except Exception as exc:  # noqa: BLE001 - orchestrator converts provider failures to payloads
            search_error = _coerce_search_error(provider_name, exc)
            diagnostics.provider_attempts.append(
                _provider_error_attempt(provider_name, search_error)
            )

            has_next_provider = attempt_index + 1 < len(provider_names)
            if has_next_provider and resolved_runtime.should_fallback(
                search_error,
                explicit_provider=explicit_provider,
            ):
                diagnostics.fallback_from = diagnostics.fallback_from or provider_name
                continue
            return _failure_payload(
                options,
                diagnostics,
                error_kind=search_error.kind,
                error=_public_error_message(provider_name, search_error.kind),
                provider_retryable=search_error.retryable,
                error_class=type(exc).__name__,
            )

        selected_provider = provider_name
        diagnostics.recency_supported = recency_supported
        diagnostics.recency_degraded = recency_degraded
        diagnostics.provider_attempts.append({"provider": provider_name, "status": "success"})
        break

    if not selected_provider:
        search_error = _coerce_search_error(
            "unknown",
            RuntimeError("No search provider is available."),
        )
        return _failure_payload(
            options,
            diagnostics,
            error_kind=search_error.kind,
            error=_public_error_message(search_error.provider, search_error.kind),
            provider_retryable=False,
            error_class="RuntimeError",
        )

    diagnostics.selected_provider = selected_provider
    hits = [_search_result_to_hit(result, selected_provider, options) for result in raw_results]
    hits = _filter_hits_by_domain_options(hits, options)
    hits, diagnostics.duplicate_count = dedupe_hits_by_canonical_url(hits)
    hits, diagnostics.domain_limited_count = _limit_root_domain_spam(hits, options)
    for rank, hit in enumerate(hits, start=1):
        hit.rank = rank

    if options.fetch_top_k > 0:
        await _fetch_compact_excerpts(
            hits,
            options=options,
            diagnostics=diagnostics,
            fetcher=fetcher or _default_fetcher,
        )

    diagnostics.returned_chars = sum(len(hit.excerpt) for hit in hits)
    diagnostics.budget_clamped = any(hit.content_truncated for hit in hits)
    if not hits:
        diagnostics.empty_reason = "no_results" if not raw_results else "filtered"

    payload = {
        "ok": True,
        "query": options.query,
        "mode": options.mode,
        "provider_attempts": diagnostics.provider_attempts,
        "diagnostics": _diagnostics_payload(diagnostics),
        "sources": [_public_source_payload(hit) for hit in hits],
        "results": [_public_hit_payload(hit) for hit in hits],
    }
    if cache_enabled:
        _set_cached_payload(cache_key, payload)
    return payload


def clear_canonical_web_search_cache_for_tests() -> None:
    """Clear the in-process search cache for deterministic tests."""

    _SEARCH_CACHE.clear()


def _effective_options_for_provider(
    options: SearchOptions,
    provider_config: Any,
) -> tuple[SearchOptions, bool, bool]:
    if options.recency is None:
        return options, True, False
    if "freshness" in provider_config.capabilities:
        return options, True, False
    return (
        replace(
            options,
            query=_query_with_recency_hint(options.query, options.recency),
            recency=None,
        ),
        False,
        True,
    )


def _query_with_recency_hint(query: str, recency: str) -> str:
    hints = {
        "day": "past 24 hours",
        "week": "past week",
        "month": "past month",
        "year": "past year",
    }
    hint = hints.get(recency, "")
    if not hint:
        return query
    return f"{query} {hint}"


def _ensure_builtin_search_providers() -> None:
    for module_name in (
        "opensquilla.search.providers.bocha",
        "opensquilla.search.providers.iqs",
        "opensquilla.search.providers.serpdive",
        "opensquilla.search.providers.tavily",
        "opensquilla.search.providers.brave",
        "opensquilla.search.providers.exa",
        "opensquilla.search.providers.duckduckgo",
    ):
        importlib.import_module(module_name)


async def _search_provider(provider: SearchProvider, options: SearchOptions) -> list[SearchResult]:
    kwargs: dict[str, Any] = {
        "max_results": options.max_results,
        "recency": options.recency,
        "include_domains": options.include_domains,
        "exclude_domains": options.exclude_domains,
    }
    supported_kwargs = _supported_search_kwargs(provider, kwargs)
    return await provider.search(options.query, **supported_kwargs)


def _supported_search_kwargs(
    provider: SearchProvider,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    signature = inspect.signature(provider.search)
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        return {key: value for key, value in kwargs.items() if value not in (None, ())}

    return {
        key: value
        for key, value in kwargs.items()
        if key in signature.parameters and value not in (None, ())
    }


async def _default_fetcher(url: str, max_chars: int) -> dict[str, Any]:
    web_fetch = importlib.import_module("opensquilla.tools.builtin.web_fetch")
    return cast(
        dict[str, Any],
        await web_fetch.run_web_fetch_payload(url, max_chars=max_chars),
    )


def _cache_key(
    options: SearchOptions,
    execution_plan: SearchExecutionPlan,
) -> tuple[Any, ...]:
    return (
        canonicalize_query_key(options.query),
        options.provider or "auto",
        options.mode,
        options.recency or "",
        execution_plan.provider_names,
        execution_plan.selection_mode,
        execution_plan.fallback_mode,
        options.max_results,
        options.fetch_top_k,
        options.max_chars_per_source,
        options.include_domains,
        options.exclude_domains,
    )


def _get_cached_payload(cache_key: tuple[Any, ...]) -> dict[str, Any] | None:
    entry = _SEARCH_CACHE.get(cache_key)
    if entry is None:
        return None
    expires_at, payload = entry
    if expires_at <= time.monotonic():
        _SEARCH_CACHE.pop(cache_key, None)
        return None
    return deepcopy(payload)


def _set_cached_payload(cache_key: tuple[Any, ...], payload: dict[str, Any]) -> None:
    _SEARCH_CACHE[cache_key] = (
        time.monotonic() + _SEARCH_CACHE_TTL_SECONDS,
        deepcopy(payload),
    )


def _provider_order(options: SearchOptions) -> tuple[str, ...]:
    return get_resolved_search_runtime().provider_order(options)


def _coerce_search_error(provider_name: str, exc: Exception) -> SearchProviderError:
    if isinstance(exc, SearchProviderError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return SearchProviderError(
            provider=provider_name,
            kind="timeout",
            message=str(exc) or "Search request timed out.",
            retryable=True,
        )
    if isinstance(exc, httpx.NetworkError):
        return SearchProviderError(
            provider=provider_name,
            kind="network",
            message=str(exc) or "Search network request failed.",
            retryable=True,
        )
    return SearchProviderError(
        provider=provider_name,
        kind="unknown",
        message=str(exc) or exc.__class__.__name__,
        retryable=False,
    )


def _provider_error_attempt(provider_name: str, error: SearchProviderError) -> dict[str, Any]:
    if error.kind == "auth":
        if _is_missing_key_error(error):
            return {"provider": provider_name, "status": "auth_missing"}
        return {"provider": provider_name, "status": "auth_failed"}
    return {"provider": provider_name, "status": "error", "error_kind": error.kind}


def _is_missing_key_error(error: SearchProviderError) -> bool:
    if error.kind != "auth" or error.status_code is not None:
        return False
    message = error.message.lower()
    return any(
        marker in message
        for marker in (
            "api key not set",
            "key not set",
            "not configured",
            "not set",
            "missing",
            "unset",
        )
    )


def _search_result_to_hit(
    result: SearchResult,
    selected_provider: str,
    options: SearchOptions,
) -> SearchHit:
    excerpt_source = _first_non_empty(result.content, *result.highlights, result.snippet)
    excerpt, truncated = _truncate(excerpt_source, options.max_chars_per_source)
    return SearchHit(
        title=result.title,
        url=result.url,
        canonical_url=_search_canonical_url(result.url),
        domain=extract_domain(result.url),
        provider=result.provider or result.source or selected_provider,
        snippet=result.snippet,
        score=result.score,
        published_at=result.published_at,
        excerpt=excerpt,
        content_truncated=truncated,
        highlights=list(result.highlights),
        raw_metadata=dict(result.raw_metadata),
    )


def _filter_hits_by_domain_options(
    hits: list[SearchHit],
    options: SearchOptions,
) -> list[SearchHit]:
    if not options.include_domains and not options.exclude_domains:
        return hits

    return [
        hit
        for hit in hits
        if _domain_allowed(
            hit.domain,
            include_domains=options.include_domains,
            exclude_domains=options.exclude_domains,
        )
    ]


def _limit_root_domain_spam(
    hits: list[SearchHit],
    options: SearchOptions,
) -> tuple[list[SearchHit], int]:
    if options.include_domains:
        return hits, 0

    root_counts: dict[str, int] = {}
    limited: list[SearchHit] = []
    limited_count = 0
    for hit in hits:
        root_domain = _root_domain(hit.domain)
        count = root_counts.get(root_domain, 0)
        if root_domain and count >= _ROOT_DOMAIN_RESULT_LIMIT:
            limited_count += 1
            continue
        root_counts[root_domain] = count + 1
        limited.append(hit)
    return limited, limited_count


# Generic second-level labels used under two-letter ccTLDs (e.g. co.uk,
# com.au, gov.uk). When a host ends in one of these under a 2-letter TLD, the
# registrable domain is the last THREE labels, not two — otherwise distinct
# organizations (bbc.co.uk vs theguardian.co.uk) collapse into one root.
_SECOND_LEVEL_SUFFIXES = frozenset(
    {
        "co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go",
        "mil", "sch", "ltd", "plc", "nhs", "police", "me",
    }
)


def _root_domain(domain: str) -> str:
    normalized = domain.lower().strip(".")
    if not normalized:
        return ""
    labels = [label for label in normalized.split(".") if label]
    if len(labels) <= 2:
        return normalized
    tld = labels[-1]
    second = labels[-2]
    # Public-suffix-aware: bbc.co.uk / gov.uk stay distinct roots.
    if len(tld) == 2 and second in _SECOND_LEVEL_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _domain_allowed(
    domain: str,
    *,
    include_domains: tuple[str, ...],
    exclude_domains: tuple[str, ...],
) -> bool:
    normalized_domain = domain.lower().strip(".")
    if not normalized_domain:
        return False
    if include_domains and not any(
        _domain_matches_rule(normalized_domain, rule) for rule in include_domains
    ):
        return False
    return not any(_domain_matches_rule(normalized_domain, rule) for rule in exclude_domains)


def _domain_matches_rule(domain: str, rule: str) -> bool:
    normalized_rule = rule.lower().strip(".")
    return domain == normalized_rule or domain.endswith(f".{normalized_rule}")


async def _fetch_compact_excerpts(
    hits: list[SearchHit],
    *,
    options: SearchOptions,
    diagnostics: SearchDiagnostics,
    fetcher: Fetcher,
) -> None:
    for hit in hits[: options.fetch_top_k]:
        if _has_useful_provider_content(hit):
            continue

        try:
            payload = await fetcher(hit.url, options.max_chars_per_source)
        except Exception as exc:  # noqa: BLE001 - fetch failure should not fail search
            hit.fetch_status = exc.__class__.__name__
            diagnostics.fetch_failed_count += 1
            continue

        if not isinstance(payload, dict):
            hit.fetch_status = "malformed_payload"
            diagnostics.fetch_failed_count += 1
            continue

        text = _extract_external_content_text(str(payload.get("text") or ""))
        if not text.strip():
            hit.fetch_status = _fetch_failure_status(payload)
            hit.extractor = str(payload.get("extractor") or "")
            diagnostics.fetch_failed_count += 1
            continue

        excerpt, truncated = _truncate(text, options.max_chars_per_source)
        hit.excerpt = excerpt
        hit.fetched = True
        hit.fetch_status = "ok"
        hit.extractor = str(payload.get("extractor") or "")
        hit.content_truncated = bool(payload.get("truncated")) or truncated
        diagnostics.fetched_count += 1


def _has_useful_provider_content(hit: SearchHit) -> bool:
    return len(hit.excerpt.strip()) >= _FETCH_MIN_USEFUL_CHARS


def _search_canonical_url(url: str) -> str:
    canonical_url = canonicalize_url(url)
    original_path = urlsplit(url).path
    canonical_parts = urlsplit(canonical_url)
    if original_path.endswith("/") and not canonical_parts.path.endswith("/"):
        path = f"{canonical_parts.path}/"
        return urlunsplit(
            (
                canonical_parts.scheme,
                canonical_parts.netloc,
                path,
                canonical_parts.query,
                canonical_parts.fragment,
            )
        )
    return canonical_url


def _extract_external_content_text(text: str) -> str:
    match = _EXTERNAL_CONTENT_RE.search(text)
    if match is None:
        return text.strip()
    return match.group("content").strip()


def _fetch_failure_status(payload: dict[str, Any]) -> str:
    status = payload.get("status")
    if isinstance(status, int) and status >= 400:
        return "http_error"
    if payload.get("error"):
        return "error"
    return "error"


def _first_non_empty(*values: str) -> str:
    for value in values:
        if value.strip():
            return value
    return ""


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _failure_payload(
    options: SearchOptions,
    diagnostics: SearchDiagnostics,
    *,
    error_kind: str,
    error: str,
    provider_retryable: bool,
    error_class: str,
) -> dict[str, Any]:
    diagnostics.returned_chars = 0
    return {
        "ok": False,
        "query": options.query,
        "mode": options.mode,
        "provider_attempts": diagnostics.provider_attempts,
        "diagnostics": _diagnostics_payload(diagnostics),
        "sources": [],
        "results": [],
        "error_kind": error_kind,
        "error_class": error_class,
        "error": error,
        "provider_retryable": provider_retryable,
        "retry_allowed": False,
    }


def _public_error_message(provider: str, kind: str) -> str:
    provider_name = provider or "search provider"
    if kind == "auth":
        return f"{provider_name} search authentication failed. Check provider credentials."
    if kind == "network":
        return f"{provider_name} search network request failed."
    if kind == "timeout":
        return f"{provider_name} search request timed out."
    if kind == "rate_limit":
        return f"{provider_name} search rate limit was reached."
    if kind == "blocked":
        return f"{provider_name} search was blocked by an upstream challenge."
    if kind == "http":
        return f"{provider_name} search request failed."
    return f"{provider_name} search request failed."


def _diagnostics_payload(diagnostics: SearchDiagnostics) -> dict[str, Any]:
    return asdict(diagnostics)


def _public_hit_payload(hit: SearchHit) -> dict[str, Any]:
    payload = asdict(hit)
    payload.pop("raw_metadata", None)
    return payload


def _public_source_payload(hit: SearchHit) -> dict[str, Any]:
    return {
        "rank": hit.rank,
        "title": hit.title,
        "url": hit.url,
        "canonical_url": hit.canonical_url,
        "domain": hit.domain,
        "provider": hit.provider,
        "fetched": hit.fetched,
        "fetch_status": hit.fetch_status,
    }
