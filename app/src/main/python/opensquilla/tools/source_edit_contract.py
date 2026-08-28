"""Revision-based source read and line edit helpers."""

from __future__ import annotations

import difflib
import hashlib
from pathlib import Path
from typing import Any


class SourceEditContractError(ValueError):
    """Raised when a source edit contract input cannot be applied."""


DEFAULT_SOURCE_READ_LINES = 200


def source_revision_for_path(path: Path) -> str:
    """Return a stable short revision token for the current file bytes."""

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"file_{digest[:16]}"


def _line_count(text: str) -> int:
    if text == "":
        return 0
    return len(text.splitlines())


def _validate_line_range(*, start_line: Any, end_line: Any, line_count: int) -> tuple[int, int]:
    if (
        not isinstance(start_line, int)
        or isinstance(start_line, bool)
        or not isinstance(end_line, int)
        or isinstance(end_line, bool)
    ):
        raise SourceEditContractError("line ranges must use integer start_line and end_line")
    if start_line < 1 or end_line < 1:
        raise SourceEditContractError("line ranges must be positive")
    if start_line > end_line:
        raise SourceEditContractError("start_line must be less than or equal to end_line")
    if end_line > line_count:
        raise SourceEditContractError(
            f"line range {start_line}-{end_line} exceeds file length {line_count}"
        )
    return start_line, end_line


def build_line_receipt(
    path: Path,
    *,
    start_line: int,
    end_line: int | None,
    display_path: str,
) -> dict[str, Any]:
    """Build a model-facing read receipt with plain source lines."""

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    total_lines = len(lines)
    effective_end_line = (
        min(total_lines, start_line + DEFAULT_SOURCE_READ_LINES - 1)
        if end_line is None
        else end_line
    )
    start, end = _validate_line_range(
        start_line=start_line,
        end_line=effective_end_line,
        line_count=total_lines,
    )
    return {
        "status": "success",
        "path": display_path,
        "revision": source_revision_for_path(path),
        "range": [start, end],
        "total_lines": total_lines,
        "lines": [
            {"line": line_number, "text": lines[line_number - 1]}
            for line_number in range(start, end + 1)
        ],
    }


def _replacement_lines(replacement: Any, *, index: int) -> list[str]:
    if not isinstance(replacement, str):
        raise SourceEditContractError(f"edits[{index}].replacement must be a string")
    if replacement == "":
        return []
    return replacement.splitlines(keepends=True)


def _normalized_edits(
    original: str,
    edits: list[dict[str, object]],
) -> list[tuple[int, int, list[str]]]:
    if not isinstance(edits, list) or not edits:
        raise SourceEditContractError("edits must be a non-empty array")

    line_count = _line_count(original)
    normalized: list[tuple[int, int, list[str]]] = []
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise SourceEditContractError(f"edits[{index}] must be an object")
        start, end = _validate_line_range(
            start_line=edit.get("start_line"),
            end_line=edit.get("end_line"),
            line_count=line_count,
        )
        normalized.append(
            (
                start,
                end,
                _replacement_lines(edit.get("replacement"), index=index),
            )
        )

    previous_end = 0
    for start, end, _replacement in sorted(normalized, key=lambda item: item[0]):
        if start <= previous_end:
            raise SourceEditContractError("edits must not overlap")
        previous_end = end
    return normalized


def apply_line_edits(original: str, edits: list[dict[str, object]]) -> str:
    """Apply inclusive 1-based line edits to source text."""

    normalized = _normalized_edits(original, edits)
    lines = original.splitlines(keepends=True)
    for start, end, replacement in sorted(normalized, key=lambda item: item[0], reverse=True):
        lines[start - 1 : end] = replacement
    return "".join(lines)


def build_diff_summary(
    before: str,
    after: str,
    *,
    path: str,
    max_chars: int = 4000,
) -> str:
    """Return a bounded unified diff summary for a source edit."""

    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="\n",
        )
    )
    if len(diff) <= max_chars:
        return diff
    omitted = len(diff) - max_chars
    return f"{diff[:max_chars]}\n[diff_summary_truncated: omitted_chars={omitted}]"
