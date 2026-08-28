from __future__ import annotations

import re

ANSI_RE = re.compile(
    r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"
)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def trim_empty_edges(lines: list[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def dedupe_adjacent(lines: list[str]) -> list[str]:
    deduped: list[str] = []
    last: str | None = None
    for line in lines:
        if line != last:
            deduped.append(line)
        last = line
    return deduped


def head_tail(lines: list[str], head: int, tail: int) -> list[str]:
    if len(lines) <= head + tail:
        return lines
    omitted = len(lines) - head - tail
    return [*lines[:head], f"... omitted {omitted} lines ...", *lines[-tail:]]


def count_pattern(lines: list[str], pattern: str, flags: str = "") -> int:
    re_flags = 0
    if "i" in flags:
        re_flags |= re.IGNORECASE
    if "m" in flags:
        re_flags |= re.MULTILINE
    compiled = re.compile(pattern, re_flags)
    return sum(1 for line in lines if compiled.search(line))
