"""Legacy helpers for text-encoded tool calls.

Canonical provider events own protocol parsing. The generic protocol-filter
symbols remain as pass-through shims for upgrade compatibility; shared engine
code must not infer machine payloads from ordinary assistant text.
"""

from __future__ import annotations

import json
import re

_PLAIN_JSON_TOOL_CALL_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_.:-]*)\s*(\{.*\})\s*$",
    re.DOTALL,
)
_PLAIN_JSON_TOOL_PREFIX_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_.:-]*)\s*(?=\{)",
)


def _find_trailing_tool_call_start(text: str, tool_name: str) -> int | None:
    decoder = json.JSONDecoder()
    for match in reversed(list(_PLAIN_JSON_TOOL_PREFIX_RE.finditer(text))):
        if match.group(1) != tool_name:
            continue
        try:
            arguments, end = decoder.raw_decode(text, match.end())
        except json.JSONDecodeError:
            continue
        if text[end:].strip():
            continue
        if not isinstance(arguments, dict):
            continue
        return match.start()
    return None


def strip_synthetic_tool_call_text(text: str, tool_name: str) -> str:
    """Legacy exact-tool helper retained for explicit compatibility callers."""

    if not text:
        return text

    if "<minimax:tool_call>" in text:
        return ""

    lines = text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip():
            candidate = lines[index]
            break
    else:
        return text

    match = _PLAIN_JSON_TOOL_CALL_RE.match(candidate)
    if match is None or match.group(1) != tool_name:
        start = _find_trailing_tool_call_start(text, tool_name)
        if start is None:
            return text
        return text[:start].rstrip()

    prefix = "\n".join(lines[:index]).rstrip()
    return prefix


def strip_protocol_text_leak(text: str) -> str:
    """Deprecated pass-through shim; protocol parsing belongs to providers."""

    return text


class ProtocolTextLeakGuard:
    """Deprecated stateless pass-through shim retained for old integrations."""

    def push(self, text: str) -> str:
        return text

    def flush(self) -> str:
        return ""

    def flush_before_tool_use(self) -> str:
        return ""


def strip_synthetic_tool_call_suffix(text: str, tool_names: list[str]) -> str:
    """Legacy exact-tool helper retained for explicit compatibility callers."""

    cleaned = text
    for tool_name in tool_names:
        cleaned = strip_synthetic_tool_call_text(cleaned, tool_name)
    return cleaned
