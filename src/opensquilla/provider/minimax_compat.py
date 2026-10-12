"""Compatibility parser for MiniMax native XML tool-call text."""

from __future__ import annotations

import re
from dataclasses import dataclass

_WRAPPER_MARKER = re.compile(r"<\s*minimax:tool_call\s*>", re.IGNORECASE)
_INVOKE_RE = re.compile(
    r'<\s*invoke\s+name\s*=\s*"([^"]+)"\s*>(.*?)<\s*/\s*invoke\s*>',
    re.DOTALL | re.IGNORECASE,
)
_PARAM_RE = re.compile(
    r'<\s*parameter\s+name\s*=\s*"([^"]+)"\s*>(.*?)<\s*/\s*parameter\s*>',
    re.DOTALL | re.IGNORECASE,
)


@dataclass(frozen=True)
class MinimaxToolCall:
    name: str
    arguments: dict[str, str]


def contains_minimax_protocol(text: str) -> bool:
    """Return True when text contains the distinctive MiniMax tool wrapper."""
    return bool(_WRAPPER_MARKER.search(text))


def parse_minimax_tool_calls(text: str) -> list[MinimaxToolCall]:
    """Extract MiniMax native tool invocations from assistant text."""
    if not contains_minimax_protocol(text):
        return []

    calls: list[MinimaxToolCall] = []
    for invoke_match in _INVOKE_RE.finditer(text):
        name = invoke_match.group(1).strip()
        body = invoke_match.group(2)
        arguments: dict[str, str] = {}
        for param_match in _PARAM_RE.finditer(body):
            key = param_match.group(1).strip()
            value = param_match.group(2)
            if value.startswith("\n"):
                value = value[1:]
            if value.endswith("\n"):
                value = value[:-1]
            arguments[key] = value
        if name:
            calls.append(MinimaxToolCall(name=name, arguments=arguments))
    return calls
