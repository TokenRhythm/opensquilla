"""Web search abstraction layer."""

from opensquilla.search.canonical import run_canonical_web_search
from opensquilla.search.registry import get_provider, register_provider
from opensquilla.search.types import (
    SearchDiagnostics,
    SearchHit,
    SearchOptions,
    SearchProvider,
    SearchProviderError,
    SearchProviderSpec,
    SearchRequest,
    SearchResult,
)

__all__ = [
    "SearchDiagnostics",
    "SearchHit",
    "SearchOptions",
    "SearchResult",
    "SearchRequest",
    "SearchProviderSpec",
    "SearchProviderError",
    "SearchProvider",
    "get_provider",
    "register_provider",
    "run_canonical_web_search",
]
