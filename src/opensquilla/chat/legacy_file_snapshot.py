"""Small display locators for verifiable, pre-snapshot file writes.

This module reads only the already projected tool segments. Provider replay is
never needed to render history; the private payload is read on explicit demand.
"""

from __future__ import annotations

import re
from typing import Any

# A recovery reads one bounded replay envelope, never all of a session's turns.
MAX_LEGACY_REPLAY_BYTES = 8 * 1024 * 1024
_MARKER = re.compile(
    r"\A\[historical_tool_argument_omitted\]\n"
    r"tool: write_file\ntool_use_id: ([^\r\n]{1,256})\nfield: content\n"
    r"original_chars: ([1-9][0-9]{0,9})\nsha256: ([a-f0-9]{64})\n"
    r"path: ([^\r\n]{1,4096})\nhead:\n"
)


def legacy_write_succeeded(result: dict[str, Any]) -> bool:
    status = result.get("execution_status")
    return result.get("is_error") is False and not (
        isinstance(status, dict) and status.get("status") in {"error", "timeout", "cancelled"}
    )


def legacy_write_locators(segments: object, message_id: object) -> dict[str, dict[str, Any]]:
    """Return locators only for unambiguous, successful matching write calls."""

    if not isinstance(segments, list) or not isinstance(message_id, str) or not message_id:
        return {}
    uses: dict[str, list[dict[str, Any]]] = {}
    results: dict[str, list[dict[str, Any]]] = {}
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        tool_id = segment.get("tool_use_id")
        if not isinstance(tool_id, str) or not tool_id:
            continue
        if segment.get("type") == "tool_use":
            uses.setdefault(tool_id, []).append(segment)
        elif segment.get("type") == "tool_result":
            results.setdefault(tool_id, []).append(segment)

    locators: dict[str, dict[str, Any]] = {}
    for tool_id, calls in uses.items():
        matches = results.get(tool_id, [])
        if len(calls) != 1 or len(matches) != 1:
            continue
        call, result = calls[0], matches[0]
        if (
            call.get("name") != "write_file"
            or result.get("name") != "write_file"
            or not legacy_write_succeeded(result)
            or result.get("file_snapshot") is not None
        ):
            continue
        arguments = call.get("input")
        if not isinstance(arguments, dict):
            continue
        marker = arguments.get("content")
        if not isinstance(marker, str) or len(marker) > 16 * 1024:
            continue
        matched = _MARKER.match(marker)
        if matched is None:
            continue
        marker_id, chars, sha, path = matched.groups()
        original_chars = int(chars)
        if (
            marker_id != tool_id
            or path != arguments.get("path")
            or original_chars > MAX_LEGACY_REPLAY_BYTES
        ):
            continue
        locators[tool_id] = {
            "kind": "write", "path": path, "content": "",
            "legacy_ref": {
                "message_id": message_id, "tool_use_id": tool_id,
                "sha256": sha, "original_chars": original_chars,
            },
        }
    return locators


def project_legacy_write_snapshots(segments: list[Any], message_id: object) -> list[Any]:
    """Attach only compact references; do not mutate persisted transcript rows."""

    locators = legacy_write_locators(segments, message_id)
    if not locators:
        return segments
    return [
        {**segment, "file_snapshot": locators[segment["tool_use_id"]]}
        if (
            isinstance(segment, dict) and segment.get("type") == "tool_result"
            and segment.get("tool_use_id") in locators
        ) else segment
        for segment in segments
    ]
