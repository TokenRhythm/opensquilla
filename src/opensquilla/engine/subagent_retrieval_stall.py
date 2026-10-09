"""Stop subagent retrieval loops that no longer discover sources."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from opensquilla.engine.subagent_failure_fallback import (
    build_subagent_failure_json,
    web_fetch_payload_failure,
)
from opensquilla.search.normalize import canonicalize_url

_SEARCH_TOOLS = frozenset({"web_search", "web_discover"})
_FETCH_TOOLS = frozenset({"web_fetch"})
_RETRIEVAL_TOOLS = _SEARCH_TOOLS | _FETCH_TOOLS
_MAX_REPORTED_SOURCES = 16


@dataclass(frozen=True)
class RetrievalStallObservation:
    terminal: str | None = None


def _mapping(content: Any) -> Mapping[str, Any] | None:
    if isinstance(content, Mapping):
        return content
    if not isinstance(content, str):
        return None
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


def _source_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    canonical = canonicalize_url(value.strip())
    return canonical or None


def _search_urls(payload: Mapping[str, Any]) -> list[str]:
    urls: list[str] = []
    for field_name in ("results", "sources"):
        entries = payload.get(field_name)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            url = _source_url(entry.get("canonical_url") or entry.get("url"))
            if url and url not in urls:
                urls.append(url)
    return urls


def _fetch_url(payload: Mapping[str, Any]) -> str | None:
    return _source_url(payload.get("final_url") or payload.get("url"))


@dataclass
class SubagentRetrievalStallGuard:
    """Track successful retrieval attempts that add no new source."""

    hard_attempts: int = 4
    consecutive_stalled_attempts: int = 0
    _discovered_sources: dict[str, None] = field(default_factory=dict)
    _fetched_sources: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.hard_attempts < 1:
            raise ValueError("hard_attempts must be positive")

    def observe(self, results: list[Any]) -> RetrievalStallObservation:
        successful_retrieval_seen = False

        for result in results:
            tool = str(getattr(result, "tool_name", "") or "")
            if tool not in _RETRIEVAL_TOOLS or bool(getattr(result, "is_error", False)):
                continue
            payload = _mapping(getattr(result, "content", None))
            if payload is None or payload.get("ok") is False:
                continue
            if tool in _FETCH_TOOLS and web_fetch_payload_failure(payload) is not None:
                continue
            successful_retrieval_seen = True
            made_progress = False

            if tool in _SEARCH_TOOLS:
                for url in _search_urls(payload):
                    if url not in self._discovered_sources:
                        self._discovered_sources[url] = None
                        made_progress = True
            else:
                fetched_url = _fetch_url(payload)
                if fetched_url and fetched_url not in self._fetched_sources:
                    self._fetched_sources.add(fetched_url)
                    self._discovered_sources.setdefault(fetched_url, None)
                    made_progress = True

            if made_progress:
                self.consecutive_stalled_attempts = 0
            else:
                self.consecutive_stalled_attempts += 1

        if not successful_retrieval_seen:
            return RetrievalStallObservation()
        if self.consecutive_stalled_attempts >= self.hard_attempts:
            count = {
                1: "One",
                2: "Two",
                3: "Three",
                4: "Four",
            }.get(
                self.consecutive_stalled_attempts,
                str(self.consecutive_stalled_attempts),
            )
            return RetrievalStallObservation(
                terminal=build_subagent_failure_json(
                    reason_code="retrieval_stalled",
                    reason=(
                        f"{count} consecutive retrieval attempts added no new sources."
                    ),
                    partial_result={
                        "sources_found": list(self._discovered_sources)[:_MAX_REPORTED_SOURCES]
                    },
                    next_step=(
                        "Use the collected sources or retry with a narrower evidence gap."
                    ),
                )
            )
        return RetrievalStallObservation()


__all__ = [
    "RetrievalStallObservation",
    "SubagentRetrievalStallGuard",
]
