"""Recognition helpers for legacy flattened tool transcript projections."""

from __future__ import annotations

import re

_USED_TOOL_LINE = re.compile(r"^\[Used tool: [^\]\r\n]*\]$")
_TOOL_RESULT_PREFIX = re.compile(r"^\[Tool result \([^\)\r\n]+\): ")
_SINGLE_LINE_TOOL_RESULT = re.compile(
    r"^\[Tool result \([^\)\r\n]+\): [^\r\n]*\](?:\r?\n|$)"
)


def has_flattened_used_tool_line(content: str) -> bool:
    """Return whether content carries an exact flattened tool-use line."""

    return any(_USED_TOOL_LINE.fullmatch(line.strip()) for line in content.splitlines())


def strip_flattened_used_tool_lines(content: str) -> str:
    """Remove exact tool-use marker lines while preserving surrounding prose."""

    kept = [
        line
        for line in content.split("\n")
        if _USED_TOOL_LINE.fullmatch(line.strip()) is None
    ]
    return "\n".join(kept).strip()


def is_flattened_tool_result_dump(content: str) -> bool:
    """Recognize a complete legacy ``[Tool result (...): ...]`` projection."""

    visible = content.lstrip()
    return bool(_TOOL_RESULT_PREFIX.match(visible)) and visible.rstrip().endswith("]")


def strip_confirmed_flattened_tool_result(content: str) -> str:
    """Hide a confirmed result projection without discarding visible suffix text.

    The historical serializer did not escape newlines or brackets inside result
    snippets, so a multiline projection cannot be split safely from arbitrary
    suffix prose. Pure result dumps are removable as a whole; the unambiguous
    single-line form may also be removed while retaining the following text.
    """

    leading = len(content) - len(content.lstrip())
    visible = content[leading:]
    single_line = _SINGLE_LINE_TOOL_RESULT.match(visible)
    if single_line is not None:
        suffix = visible[single_line.end() :]
        return suffix.strip()
    if is_flattened_tool_result_dump(visible):
        return ""
    return content
