"""Tokenjuice bridge for OpenSquilla tool-result projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opensquilla.plugins.tokenjuice import reduce_tool_result as _reduce_tool_result_backend


@dataclass(frozen=True)
class TokenjuiceReduction:
    inline_text: str
    raw_chars: int
    reduced_chars: int
    ratio: float
    reducer: str | None = None


def _string_arg(args: dict[str, Any] | None, *names: str) -> str | None:
    if not args:
        return None
    for name in names:
        value = args.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return None


def reduce_tool_result_with_tokenjuice(
    *,
    tool_name: str,
    content: str,
    is_error: bool,
    tool_use_id: str,
    arguments: dict[str, Any] | None = None,
    command: str | None = None,
    cwd: str | None = None,
    max_inline_chars: int | None = None,
    timeout_seconds: float = 5.0,
) -> TokenjuiceReduction | None:
    """Run the built-in tokenjuice projection backend for a tool result.

    Returns ``None`` when tokenjuice fails or does not reduce the result.
    Projection is best-effort because tool output must never fail an agent turn.
    """

    del timeout_seconds
    command = command or _string_arg(arguments, "command")
    cwd = cwd or _string_arg(arguments, "workdir", "cwd")
    try:
        reduction = _reduce_tool_result_backend(
            tool_name=tool_name,
            content=content,
            is_error=is_error,
            tool_use_id=tool_use_id,
            arguments=arguments,
            command=command,
            cwd=cwd,
            max_inline_chars=max_inline_chars,
        )
    except Exception:
        return None
    if reduction is None:
        return None
    if reduction.inline_text.splitlines() == content.splitlines():
        return None
    if len(reduction.inline_text) >= len(content):
        return None
    return TokenjuiceReduction(
        inline_text=reduction.inline_text,
        raw_chars=reduction.raw_chars,
        reduced_chars=reduction.reduced_chars,
        ratio=reduction.ratio,
        reducer=reduction.reducer,
    )
