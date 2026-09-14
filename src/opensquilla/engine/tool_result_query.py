"""Bounded character pages and line queries for streamed execution logs."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from itertools import chain

from opensquilla.engine.tool_result_store import ToolResultStore

# Keep the semantics of str.splitlines(), including CRLF split across chunks.
_LINE_BREAK_RE = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")
_REGEX_META_RE = re.compile(r"[.\^$*+?{}\[\]\\|()]")
MAX_REGEX_LINE_CHARS = 1024 * 1024


def character_page(chunks: Iterable[str], offset: int, limit: int) -> str:
    parts: list[str] = []
    remaining = limit
    for chunk in chunks:
        if offset >= len(chunk):
            offset -= len(chunk)
            continue
        part = chunk[offset:offset + remaining]
        parts.append(part)
        remaining -= len(part)
        offset = 0
        if not remaining:
            break
    return "".join(parts)


@dataclass(frozen=True)
class LogLine:
    number: int
    offset: int
    text: str
    chars: int
    matched: bool | None
    match_offset: int | None = None


def iter_log_lines(
    chunks: Iterable[str], *, preview_chars: int, pattern: str | None = None,
) -> Iterator[LogLine]:
    literal = pattern if pattern and not _REGEX_META_RE.search(pattern) else None
    regex = re.compile(pattern) if pattern is not None and literal is None else None
    number = 1
    offset = 0
    line_start = 0
    line_chars = 0
    preview: list[str] = []
    preview_size = 0
    regex_parts: list[str] = []
    literal_tail = ""
    literal_matched = False
    match_offset = None
    pending_cr = False
    for chunk in chunks:
        if pending_cr and chunk:
            if chunk.startswith("\n"):
                chunk = chunk[1:]
                offset += 1
                line_start += 1
            pending_cr = False
        cursor = 0
        for boundary in chain(_LINE_BREAK_RE.finditer(chunk), (None,)):
            end = boundary.start() if boundary is not None else len(chunk)
            fragment = chunk[cursor:end]
            line_chars += len(fragment)
            if preview_size < preview_chars:
                kept = fragment[:preview_chars - preview_size]
                preview.append(kept)
                preview_size += len(kept)
            if literal is not None and not literal_matched:
                scanned = literal_tail + fragment
                match_at = scanned.find(literal)
                literal_matched = match_at >= 0
                if literal_matched:
                    match_offset = offset - len(literal_tail) + match_at
                literal_tail = scanned[-(len(literal) - 1):] if len(literal) > 1 else ""
            elif regex is not None and literal is None:
                if line_chars <= MAX_REGEX_LINE_CHARS:
                    regex_parts.append(fragment)
                else:
                    regex_parts.clear()
            offset += len(fragment)
            if boundary is None:
                break
            matched = None
            if literal is not None:
                matched = literal_matched
            elif regex is not None and line_chars <= MAX_REGEX_LINE_CHARS:
                match = regex.search("".join(regex_parts))
                matched = match is not None
                match_offset = line_start + match.start() if match is not None else None
            yield LogLine(number, line_start, "".join(preview), line_chars, matched, match_offset)
            separator = boundary.group()
            pending_cr = separator == "\r" and boundary.end() == len(chunk)
            offset += len(separator)
            line_start = offset
            cursor = boundary.end()
            number += 1
            line_chars = 0
            preview = []
            preview_size = 0
            regex_parts = []
            literal_tail = ""
            literal_matched = False
            match_offset = None
    if line_chars:
        matched = None
        if literal is not None:
            matched = literal_matched
        elif regex is not None and line_chars <= MAX_REGEX_LINE_CHARS:
            match = regex.search("".join(regex_parts))
            matched = match is not None
            match_offset = line_start + match.start() if match is not None else None
        yield LogLine(number, line_start, "".join(preview), line_chars, matched, match_offset)


class _QueryText:
    """Keep only enough response text to apply the existing response budget."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.parts: list[str] = []
        self.size = 0
        self.truncated = False

    def append(self, text: str) -> None:
        available = max(0, self.limit - self.size)
        self.parts.append(text[:available])
        self.size += min(len(text), available)
        if len(text) > available:
            self.truncated = True

    def line(self, line: LogLine, width: int) -> None:
        self.append(f"{line.number:>{width}}| {line.text}\n")
        if line.chars > len(line.text):
            self.truncated = True

    def result(self) -> str:
        return "".join(self.parts).rstrip("\n")


def log_ranges(
    store: ToolResultStore, session_id: str, handle: str,
    ranges: list[tuple[int, int]], *, max_chars: int, labels: bool = False,
    omission_notice: bool = False,
) -> tuple[str, bool]:
    output = _QueryText(max_chars + 1)
    if not ranges:
        return "", False
    active = 0
    started = False
    for line in iter_log_lines(
        store.iter_text_chunks(handle, session_id=session_id), preview_chars=max_chars + 1,
    ):
        while active < len(ranges) and line.number > ranges[active][1]:
            active += 1
            started = False
        if active == len(ranges):
            break
        start, end = ranges[active]
        if line.number < start:
            continue
        if not started and labels:
            output.append(f"[lines {start}-{end}]\n")
        elif not started and omission_notice and active:
            omitted = start - ranges[active - 1][1] - 1
            output.append(f"[... omitted {omitted} lines ...]\n")
        started = True
        output.line(line, len(str(end)))
        if output.truncated:
            break
    return output.result(), output.truncated


def log_grep(
    store: ToolResultStore, session_id: str, handle: str, *, pattern: str,
    context_lines: int, max_chars: int, line_count: int,
) -> tuple[str, bool]:
    re.compile(pattern)
    before: deque[LogLine] = deque(maxlen=context_lines)
    ranges: list[tuple[int, int]] = []
    # Find only enough matching ranges to fill a response, then render in a
    # second streaming pass. Do not collect every matching line in the file.
    response_size = 0
    last_included = 0
    incomplete = False
    warning = ""
    for line in iter_log_lines(
        store.iter_text_chunks(handle, session_id=session_id),
        preview_chars=max_chars + 1, pattern=pattern,
    ):
        if line.matched is None:
            warning = (
                f"Search incomplete: regex inspection of line {line.number} exceeds "
                f"{MAX_REGEX_LINE_CHARS} characters. The stored log is unchanged. "
                f"Use raw_slice with offset={line.offset} to inspect this line."
            )
            incomplete = True
            break
        if line.matched:
            if line.match_offset is not None and line.chars > max_chars:
                warning = (
                    f"Matching line {line.number} exceeds this response's character budget. "
                    f"The first match is at character offset {line.match_offset}; "
                    f"use raw_slice with offset={max(0, line.match_offset - 100)} "
                    "to read the match and surrounding text."
                )
            start = max(1, line.number - context_lines)
            end = min(line_count, line.number + context_lines)
            if ranges and start <= ranges[-1][1] + 1:
                ranges[-1] = (ranges[-1][0], max(end, ranges[-1][1]))
            else:
                ranges.append((start, end))
            for entry in (*before, line):
                if entry.number > last_included:
                    response_size += entry.chars + len(str(entry.number)) + 3
            last_included = line.number
        elif ranges and line.number <= ranges[-1][1] and line.number > last_included:
            response_size += line.chars + len(str(line.number)) + 3
            last_included = line.number
        before.append(line)
        if response_size > max_chars:
            incomplete = True
            break
    body, truncated = log_ranges(
        store, session_id, handle, ranges, max_chars=max_chars, labels=True,
    )
    if warning:
        body = warning + ("\n" + body if body else "")
    elif not ranges:
        body = f"No matches for pattern: {pattern}"
    return body, truncated or incomplete
