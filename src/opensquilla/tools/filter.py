"""Authoritative allow/deny filtering for effective tool catalogs."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol


class NamedTool(Protocol):
    name: str


def filter_tools[TNamedTool: NamedTool](
    tools: Iterable[TNamedTool],
    *,
    allow: set[str] | frozenset[str] | None,
    deny: set[str] | frozenset[str],
) -> list[TNamedTool]:
    """Return allowed tools with deny taking unconditional precedence."""

    denied = frozenset(deny)
    allowed = None if allow is None else frozenset(allow)
    return [
        tool
        for tool in tools
        if tool.name not in denied and (allowed is None or tool.name in allowed)
    ]
