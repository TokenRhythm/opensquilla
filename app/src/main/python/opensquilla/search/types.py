"""Search types — request/result/spec dataclasses and provider protocol."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

SearchErrorKind = Literal[
    "auth",
    "rate_limit",
    "timeout",
    "network",
    "http",
    "parse",
    "blocked",
    "unknown",
]
SearchMode = Literal["auto", "news", "technical", "broad"]
Recency = Literal["day", "week", "month", "year"]
SearchSelectionMode = Literal["automatic", "explicit"]
SearchFallbackMode = Literal["none", "auth_missing", "network"]

# Single source of truth for the web-search result count.
# DEFAULT_SEARCH_MAX_RESULTS is the count used when a caller does not request an
# explicit number; MAX_SEARCH_RESULTS is the hard ceiling every surface clamps to
# (it matches the per-provider upper bound, e.g. Brave's count cap).
DEFAULT_SEARCH_MAX_RESULTS = 10
MAX_SEARCH_RESULTS = 20


@dataclass
class SearchRequest:
    """A search request with the same defaults as provider.search(...)."""

    query: str
    max_results: int = DEFAULT_SEARCH_MAX_RESULTS


@dataclass
class SearchResult:
    """A single search result entry.

    Extra metadata is optional so existing callers that construct only
    title/url/snippet remain source-compatible.
    """

    title: str
    url: str
    snippet: str
    source: str = ""
    published_at: str | None = None
    provider: str = ""
    score: float | None = None
    highlights: list[str] = field(default_factory=list)
    content: str = ""
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchOptions:
    """Normalized search request options shared by search surfaces."""

    query: str
    mode: SearchMode = "auto"
    max_results: int = DEFAULT_SEARCH_MAX_RESULTS
    fetch_top_k: int = 3
    max_chars_per_source: int = 1500
    include_domains: tuple[str, ...] = ()
    exclude_domains: tuple[str, ...] = ()
    recency: Recency | None = None
    provider: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "query", self.query.strip())
        object.__setattr__(
            self, "max_results", min(max(self.max_results, 1), MAX_SEARCH_RESULTS)
        )
        object.__setattr__(self, "fetch_top_k", min(max(self.fetch_top_k, 0), 5))
        object.__setattr__(
            self,
            "max_chars_per_source",
            min(max(self.max_chars_per_source, 200), 5000),
        )
        object.__setattr__(self, "include_domains", _normalize_domains(self.include_domains))
        object.__setattr__(self, "exclude_domains", _normalize_domains(self.exclude_domains))


@dataclass(frozen=True)
class SearchExecutionPlan:
    """Bounded provider sequence for one canonical search execution."""

    provider_names: tuple[str, ...] = ()
    selection_mode: SearchSelectionMode = "automatic"
    fallback_mode: SearchFallbackMode = "none"

    def __post_init__(self) -> None:
        if len(self.provider_names) > 2:
            raise ValueError("Search execution plans support at most two providers.")
        if len(set(self.provider_names)) != len(self.provider_names):
            raise ValueError("Search execution plans must not repeat providers.")
        if self.fallback_mode != "none" and len(self.provider_names) < 2:
            raise ValueError("A search fallback mode requires a fallback provider.")

    @property
    def primary_provider(self) -> str | None:
        return self.provider_names[0] if self.provider_names else None

    @property
    def fallback_provider(self) -> str | None:
        return self.provider_names[1] if len(self.provider_names) > 1 else None

    @property
    def planned_provider_ids(self) -> tuple[str, ...]:
        return self.provider_names


def _normalize_domains(value: str | Iterable[str]) -> tuple[str, ...]:
    values: tuple[str, ...]
    if isinstance(value, str):
        values = (value,)
    else:
        values = tuple(value)

    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        domain = _normalize_domain_item(item)
        if not domain or domain in seen:
            continue
        normalized.append(domain)
        seen.add(domain)
    return tuple(normalized)


def _normalize_domain_item(value: str) -> str:
    raw = value.strip().lower()
    if not raw:
        return ""

    parsed = urlsplit(raw)
    if parsed.netloc:
        host = parsed.hostname or ""
    elif "/" in raw:
        host = urlsplit(f"//{raw}").hostname or ""
    else:
        host = raw

    return host.strip(".")


@dataclass
class SearchHit:
    """Canonical search hit shape after provider normalization."""

    title: str
    url: str
    canonical_url: str
    domain: str
    provider: str
    snippet: str
    rank: int | None = None
    score: float | None = None
    published_at: str | None = None
    fetched: bool = False
    fetch_status: str = "not_requested"
    excerpt: str = ""
    extractor: str = ""
    content_truncated: bool = False
    highlights: list[str] = field(default_factory=list)
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchDiagnostics:
    """Diagnostics collected during a normalized search request."""

    query: str
    mode: SearchMode
    selected_provider: str = ""
    provider_attempts: list[dict[str, Any]] = field(default_factory=list)
    fallback_from: str = ""
    fetched_count: int = 0
    fetch_failed_count: int = 0
    duplicate_count: int = 0
    domain_limited_count: int = 0
    returned_chars: int = 0
    budget_clamped: bool = False
    recency_supported: bool = True
    recency_degraded: bool = False
    cache_status: str = "disabled"
    empty_reason: str = ""
    loop_guard: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchProviderSpec:
    """Static metadata for a web search provider."""

    provider_id: str
    runtime_supported: bool = True
    metadata_supported: bool = True
    requires_api_key: bool = False
    env_key: str = ""
    capabilities: frozenset[str] = field(default_factory=lambda: frozenset({"web"}))


@dataclass
class SearchProviderError(RuntimeError):
    """Structured search provider failure for diagnostics and fallback policy."""

    provider: str
    kind: SearchErrorKind
    message: str
    retryable: bool = False
    status_code: int | None = None

    def __str__(self) -> str:
        return self.message


@runtime_checkable
class SearchProvider(Protocol):
    """Protocol for search backends."""

    name: str

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]: ...
