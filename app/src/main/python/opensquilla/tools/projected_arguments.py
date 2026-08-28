"""Helpers for refusing provider-only projected tool arguments."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

TOOL_ARGUMENT_PROJECTION_PREFIX = "[tool_use_argument_projection]\n"
HISTORICAL_TOOL_ARGUMENT_PROJECTION_PREFIX = "[historical_tool_argument_omitted]\n"
INVALID_PROVIDER_CONTEXT_PROJECTION_PREFIX = "[invalid_provider_context_projection:"
PROVIDER_REQUEST_TOOL_INPUT_COMPACTED_PREFIX = "[provider_request_tool_input_compacted:"
INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY = "_invalid_provider_context_arguments"
COMPACTED_TOOL_ARGUMENT_MARKERS = frozenset(
    {
        "_opensquilla_compacted_tool_arguments",
        "_opensquilla_compacted_tool_input",
    }
)
# Matches instantiated provider-request compaction markers anywhere inside a
# string argument, not only at char 0. Requires the numeric fields the marker
# producers always fill in ("<n> chars", "original_chars=<n>", ":<n>:<hash>")
# so template literals in this codebase (braces, no digits) and prose that
# merely names a marker prefix do not match.
_COMPACTED_MARKER_SUBSTRING_RE = re.compile(
    r"\[provider_request_[a-z0-9_]*(?:compacted|omitted):"
    r"[^\]\n]*(?:\d+ chars|original_chars=\d+)"
    r"|\[opensquilla_compacted:[A-Za-z0-9_.-]+:\d+:[0-9a-f]{8,64}\]"
)


@dataclass(frozen=True)
class ProjectedToolArgumentMatch:
    kind: str
    path: str


def is_provider_context_marker_value(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return False


def _projection_string_kind(value: str) -> str | None:
    stripped = value.lstrip()
    if stripped.startswith(PROVIDER_REQUEST_TOOL_INPUT_COMPACTED_PREFIX):
        return "provider_request_projection_string"
    if stripped.startswith(
        (
            TOOL_ARGUMENT_PROJECTION_PREFIX,
            HISTORICAL_TOOL_ARGUMENT_PROJECTION_PREFIX,
            INVALID_PROVIDER_CONTEXT_PROJECTION_PREFIX,
        )
    ):
        return "projection_string"
    if _COMPACTED_MARKER_SUBSTRING_RE.search(value):
        return "compacted_marker_substring"
    return None


def find_projected_tool_argument(
    value: Any,
    *,
    path: str = "",
) -> ProjectedToolArgumentMatch | None:
    if isinstance(value, str):
        kind = _projection_string_kind(value)
        if kind is not None:
            return ProjectedToolArgumentMatch(kind=kind, path=path)
        return None

    if isinstance(value, dict):
        marker_keys = COMPACTED_TOOL_ARGUMENT_MARKERS | {INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY}
        for key in marker_keys:
            if is_provider_context_marker_value(value.get(key)):
                return ProjectedToolArgumentMatch(
                    kind="provider_context_argument_marker",
                    path=f"{path}.{key}" if path else str(key),
                )
        for key, nested in value.items():
            nested_path = f"{path}.{key}" if path else str(key)
            match = find_projected_tool_argument(nested, path=nested_path)
            if match is not None:
                return match
        return None

    if isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            nested_path = f"{path}[{index}]" if path else f"[{index}]"
            match = find_projected_tool_argument(nested, path=nested_path)
            if match is not None:
                return match

    return None
