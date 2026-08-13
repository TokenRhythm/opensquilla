"""Recognition helpers for legacy flattened tool transcript projections."""

from __future__ import annotations

import re
from dataclasses import dataclass

_USED_TOOL_LINE = re.compile(r"^\[Used tool: [^\]\r\n]*\]$")
_TOOL_RESULT_PREFIX = re.compile(r"^\[Tool result \([^\)\r\n]+\): ")
_TOOL_RESULT_START = re.compile(
    r"(?m)^\[Tool result \((?P<tool_use_id>[^\)\r\n]+)\): "
)
_SINGLE_LINE_TOOL_RESULT = re.compile(
    r"^\[Tool result \([^\)\r\n]+\): [^\r\n]*\](?:\r?\n|$)"
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
    trailing_text: str = ""


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

    parsed = parse_flattened_tool_result_dumps(content)
    return parsed is not None and not parsed.trailing_text


def parse_flattened_tool_result_dumps(
    content: str,
) -> FlattenedToolResults | None:
    """Recover every ordered result marker from one legacy flattened row.

    The historical serializer joined content blocks with newlines and did not
    escape newlines inside a result payload. Marker starts are therefore the
    only reliable internal boundary. A final single-line marker may have
    ordinary narration after it; that suffix is returned verbatim for the
    caller to keep in the activity timeline.
    """

    visible = content.lstrip()
    if not _TOOL_RESULT_PREFIX.match(visible):
        return None
    matches = list(_TOOL_RESULT_START.finditer(visible))
    if not matches or matches[0].start() != 0:
        return None

    results: list[FlattenedToolResult] = []
    trailing_text = ""
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(visible)
        payload_with_closer = visible[match.end() : end]
        if index + 1 == len(matches):
            first_line, separator, suffix = payload_with_closer.partition("\n")
            if first_line.rstrip("\r").rstrip().endswith("]"):
                payload_with_closer = first_line.rstrip("\r").rstrip()
                trailing_text = suffix.strip() if separator else ""
            else:
                payload_with_closer = payload_with_closer.rstrip()
        else:
            payload_with_closer = payload_with_closer.rstrip()
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
    return FlattenedToolResults(tuple(results), trailing_text)


def parse_flattened_tool_result_dump(content: str) -> FlattenedToolResult | None:
    """Recover the id and payload from one confirmed complete legacy projection.

    This parser is intentionally not a classifier. Callers must first establish
    structured identity or adjacency to an exact ``[Used tool: ...]`` line so a
    user-authored example is never reinterpreted as internal activity.
    """

    parsed = parse_flattened_tool_result_dumps(content)
    if parsed is None or len(parsed.results) != 1 or parsed.trailing_text:
        return None
    return parsed.results[0]


def strip_confirmed_flattened_tool_result(content: str) -> str:
    """Hide a confirmed result projection without discarding visible suffix text.

    The historical serializer did not escape newlines or brackets inside result
    snippets, so a multiline projection cannot be split safely from arbitrary
    suffix prose. Pure result dumps are removable as a whole; the unambiguous
    single-line form may also be removed while retaining the following text.
    """

    leading = len(content) - len(content.lstrip())
    visible = content[leading:]
    parsed = parse_flattened_tool_result_dumps(visible)
    if parsed is not None:
        return parsed.trailing_text
    single_line = _SINGLE_LINE_TOOL_RESULT.match(visible)
    if single_line is not None:
        suffix = visible[single_line.end() :]
        return suffix.strip()
    if is_flattened_tool_result_dump(visible):
        return ""
    return content
