"""Trusted search-tool outcome parsing and semantic request keys.

Only built-in web retrieval tools are allowed to promote a JSON ``ok: false``
payload into runtime failure state. Keeping that trust boundary here avoids
interpreting arbitrary third-party tool output as an OpenSquilla control
contract while keeping the top-level execution-status module package-neutral.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from opensquilla.search.normalize import canonicalize_query_key

WEB_RETRIEVAL_TOOL_NAMES: frozenset[str] = frozenset({"web_search", "web_discover"})
type WebRetrievalSemanticKey = tuple[
    str,
    str,
    str,
    tuple[str, ...],
    tuple[str, ...],
]

_ERROR_KIND_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class WebToolOutcome:
    """A structured failure emitted by a trusted web retrieval tool."""

    error_kind: str
    retry_allowed: bool | None


def parse_web_tool_outcome(tool_name: str, content: Any) -> WebToolOutcome | None:
    """Parse an explicit ``ok: false`` result from a trusted search tool.

    Missing, malformed, or non-boolean ``ok`` fields are intentionally ignored
    for compatibility with legacy payloads. The retry decision also remains
    unknown unless the tool emits an actual JSON boolean.
    """

    if tool_name not in WEB_RETRIEVAL_TOOL_NAMES:
        return None
    payload = _payload_mapping(content)
    if payload is None or payload.get("ok") is not False:
        return None
    retry_allowed = payload.get("retry_allowed")
    if not isinstance(retry_allowed, bool):
        retry_allowed = None
    return WebToolOutcome(
        error_kind=_normalize_error_kind(payload.get("error_kind")),
        retry_allowed=retry_allowed,
    )


def web_retrieval_semantic_key(
    arguments: Mapping[str, Any],
) -> WebRetrievalSemanticKey:
    """Build the cross-provider identity of a web retrieval request.

    Provider/tool choice and presentation limits do not change the underlying
    information request. Query intent, mode, recency, and domain filters do.
    """

    query = canonicalize_query_key(str(arguments.get("query") or ""))
    mode = _normalized_scalar(arguments.get("mode"), default="auto")
    recency = _normalized_scalar(arguments.get("recency"), default="")
    include_domains = _normalized_domains(arguments.get("include_domains"))
    exclude_domains = _normalized_domains(arguments.get("exclude_domains"))
    return (query, mode, recency, include_domains, exclude_domains)


def _payload_mapping(content: Any) -> Mapping[str, Any] | None:
    if isinstance(content, Mapping):
        return content
    if not isinstance(content, str):
        return None
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _normalize_error_kind(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    normalized = _ERROR_KIND_SEPARATOR_RE.sub("_", value.strip().lower()).strip("_")
    return normalized[:64] or "unknown"


def _normalized_scalar(value: Any, *, default: str) -> str:
    if not isinstance(value, str):
        return default
    return value.strip().lower() or default


def _normalized_domains(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        candidates: Sequence[Any] = (value,)
    elif isinstance(value, Sequence):
        candidates = value
    else:
        return ()
    normalized = {
        candidate.strip().lower().strip(".")
        for candidate in candidates
        if isinstance(candidate, str) and candidate.strip().strip(".")
    }
    return tuple(sorted(normalized))
