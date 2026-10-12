from __future__ import annotations

import os
import re
from typing import Any

from .formatters import count_pattern, dedupe_adjacent, head_tail, strip_ansi, trim_empty_edges
from .types import Rule

# Opt-in failure-window lever (off by default). When enabled, error output
# keeps at least the success summarize window, so a rule whose failure window
# is smaller than its summarize window never truncates failures harder than
# successes.
_FAILURE_PRESERVE_ENV = "OPENSQUILLA_TOOLCOMP_FAILURE_PRESERVE"
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})


def _failure_preserve_enabled() -> bool:
    raw = os.environ.get(_FAILURE_PRESERVE_ENV, "").strip().lower()
    return raw in _TRUE_ENV_VALUES


def _compile_flags(flags: str = "") -> int:
    compiled = 0
    if "i" in flags:
        compiled |= re.IGNORECASE
    if "m" in flags:
        compiled |= re.MULTILINE
    return compiled


def _patterns(values: Any) -> list[re.Pattern[str]]:
    if not isinstance(values, list):
        return []
    patterns: list[re.Pattern[str]] = []
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            patterns.append(re.compile(value))
        except re.error:
            continue
    return patterns


def _apply_output_matches(rule: Rule, text: str) -> str | None:
    for entry in rule.output_matches:
        pattern = entry.get("pattern")
        message = entry.get("message")
        if not isinstance(pattern, str) or not isinstance(message, str):
            continue
        try:
            if re.search(pattern, text, re.MULTILINE):
                return message
        except re.error:
            continue
    return None


def _summarize_window(rule: Rule, *, exit_code: int) -> tuple[int, int]:
    if exit_code != 0 and rule.failure.get("preserveOnFailure"):
        head = int(rule.failure.get("head") or 12)
        tail = int(rule.failure.get("tail") or 12)
        if _failure_preserve_enabled():
            head = max(head, int(rule.summarize.get("head") or 8))
            tail = max(tail, int(rule.summarize.get("tail") or 8))
        return head, tail
    return int(rule.summarize.get("head") or 8), int(rule.summarize.get("tail") or 8)


def reduce_with_rule(rule: Rule, raw_text: str, *, exit_code: int) -> tuple[str, dict[str, int]]:
    text = strip_ansi(raw_text) if rule.transforms.get("stripAnsi") else raw_text
    output_match = _apply_output_matches(rule, text)
    if output_match is not None:
        return output_match, {}

    lines = text.splitlines()
    if rule.transforms.get("trimEmptyEdges"):
        lines = trim_empty_edges(lines)
    if rule.transforms.get("dedupeAdjacent"):
        lines = dedupe_adjacent(lines)

    counter_lines = list(lines)
    skip_patterns = _patterns(rule.filters.get("skipPatterns"))
    if skip_patterns:
        lines = [
            line
            for line in lines
            if not any(pattern.search(line) for pattern in skip_patterns)
        ]

    keep_patterns = _patterns(rule.filters.get("keepPatterns"))
    if keep_patterns:
        kept = [line for line in lines if any(pattern.search(line) for pattern in keep_patterns)]
        if kept:
            lines = kept

    if not lines and rule.on_empty:
        return rule.on_empty, {}

    fact_source = counter_lines if rule.counter_source == "preKeep" else lines
    facts: dict[str, int] = {}
    for counter in rule.counters:
        name = counter.get("name")
        pattern = counter.get("pattern")
        if not isinstance(name, str) or not isinstance(pattern, str):
            continue
        facts[name] = count_pattern(fact_source, pattern, str(counter.get("flags") or ""))

    head, tail = _summarize_window(rule, exit_code=exit_code)
    compacted = head_tail(lines, head, tail)
    return "\n".join(compacted).strip(), facts
