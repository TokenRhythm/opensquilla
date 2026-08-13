"""Recognition helpers for legacy flattened tool transcript projections."""

from __future__ import annotations

import re
from dataclasses import dataclass

_USED_TOOL_LINE = re.compile(r"^\[Used tool: [^\]\r\n]*\]$")
_TOOL_RESULT_PREFIX = re.compile(r"^\[Tool result \([^\)\r\n]+\): ")
_TOOL_RESULT_START = re.compile(
    r"(?m)^\[Tool result \((?P<tool_use_id>[^\)\r\n]+)\): "
)


@dataclass(frozen=True, slots=True)
class FlattenedToolResult:
    """Structured display projection recovered from one legacy result marker."""

    tool_use_id: str
    content: str


@dataclass(frozen=True, slots=True)
class FlattenedToolResults:
    """Ordered results recovered from one confirmed legacy transcript row."""

    results: tuple[FlattenedToolResult, ...]


def has_flattened_used_tool_line(content: str) -> bool:
    """Return whether content carries an exact flattened tool-use line."""

    return any(_USED_TOOL_LINE.fullmatch(line.strip()) for line in content.splitlines())


def flattened_used_tool_names(content: str) -> list[str]:
    """Return tool names from exact flattened tool-use lines, in source order."""

    names: list[str] = []
    for line in content.splitlines():
        visible = line.strip()
        if _USED_TOOL_LINE.fullmatch(visible) is None:
            continue
        name = visible[len("[Used tool: ") : -1].strip()
        if name:
            names.append(name)
    return names


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

    return parse_flattened_tool_result_dumps(content) is not None


def parse_flattened_tool_result_dumps(
    content: str,
) -> FlattenedToolResults | None:
    """Recover every ordered result marker from one legacy flattened row.

    The historical serializer joined content blocks with newlines and did not
    escape newlines inside a result payload. Marker starts are therefore the
    only reliable internal boundary. In particular, a closing bracket on the
    first payload line cannot distinguish a single-line result followed by
    narration from a multiline payload whose first line happens to end in
    ``]``. The final marker therefore owns the complete remainder through its
    last closing bracket as auditable tool output. If the complete row has no
    final closer, parsing fails and callers preserve the original text.
    """

    visible = content.lstrip()
    if not _TOOL_RESULT_PREFIX.match(visible):
        return None
    matches = list(_TOOL_RESULT_START.finditer(visible))
    if not matches or matches[0].start() != 0:
        return None

    results: list[FlattenedToolResult] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(visible)
        payload_with_closer = visible[match.end() : end].rstrip()
        if not payload_with_closer.endswith("]"):
            return None
        tool_use_id = match.group("tool_use_id").strip()
        if not tool_use_id:
            return None
        results.append(
            FlattenedToolResult(
                tool_use_id=tool_use_id,
                content=payload_with_closer[:-1],
            )
        )
    return FlattenedToolResults(tuple(results))


def parse_flattened_tool_result_dump(content: str) -> FlattenedToolResult | None:
    """Recover the id and payload from one confirmed complete legacy projection.

    This parser is intentionally not a classifier. Callers must first establish
    structured identity or adjacency to an exact ``[Used tool: ...]`` line so a
    user-authored example is never reinterpreted as internal activity.
    """

    parsed = parse_flattened_tool_result_dumps(content)
    if parsed is None or len(parsed.results) != 1:
        return None
    return parsed.results[0]


def strip_confirmed_flattened_tool_result(content: str) -> str:
    """Hide a complete confirmed legacy result projection.

    The historical serializer did not escape newlines or brackets inside result
    snippets, so a multiline projection cannot be split safely from arbitrary
    suffix prose. A parsed row is hidden only when its complete remainder is
    retained as tool output; otherwise the original row remains verbatim.
    """

    leading = len(content) - len(content.lstrip())
    visible = content[leading:]
    parsed = parse_flattened_tool_result_dumps(visible)
    if parsed is not None:
        return ""
    return content
