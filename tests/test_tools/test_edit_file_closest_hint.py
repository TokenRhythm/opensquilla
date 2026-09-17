"""Fix #1: nearest-candidate ("did you mean") hint on edit_file no-match.

When edit_file cannot find ``old_text`` (even after flexible/escape recovery),
the retry error now appends the closest source regions so the model can correct
its next edit instead of re-reading blindly. These tests pin the hint content,
the untouched exact/multi-match paths, length bounding, and the
``edit_file.no_match_hint`` telemetry event.
"""

from __future__ import annotations

import pytest

from opensquilla.tools.builtin.filesystem import (
    _apply_edit_replacements,
    _edit_file_closest_lines_hint,
    _EditReplacement,
)
from opensquilla.tools.types import (
    RetryableToolInputError,
    ToolContext,
    current_tool_context,
)

SAMPLE = "def compute(value):\n    total = value + 1\n    return total\n"


def _edit(old_text: str, new_text: str, label: str = "edits[0]") -> _EditReplacement:
    return _EditReplacement(old_text=old_text, new_text=new_text, label=label)


def test_hint_surfaces_closest_region_with_line_numbers() -> None:
    # anchor line differs by one token from the real source line
    hint = _edit_file_closest_lines_hint("    total = value + 2\n", SAMPLE)
    assert "Did you mean one of these sections?" in hint
    assert "total = value + 1" in hint
    # the matching source line is line 2 (1-based), numbered in the snippet
    assert "   2|" in hint


def test_hint_empty_when_nothing_close() -> None:
    assert _edit_file_closest_lines_hint("wholly unrelated zzzzz\n", SAMPLE) == ""
    assert _edit_file_closest_lines_hint("", SAMPLE) == ""
    assert _edit_file_closest_lines_hint("   \n", SAMPLE) == ""


def test_apply_edit_no_match_appends_hint() -> None:
    with pytest.raises(RetryableToolInputError) as exc:
        _apply_edit_replacements(
            SAMPLE,
            [_edit("    total = value + 2\n", "    total = value + 3\n")],
            path="src/a.py",
        )
    message = str(exc.value)
    assert "could not find" in message
    assert "Did you mean one of these sections?" in message
    assert "total = value + 1" in message


def test_apply_edit_exact_match_has_no_hint() -> None:
    out = _apply_edit_replacements(
        SAMPLE,
        [_edit("    total = value + 1\n", "    total = value + 9\n")],
        path="src/a.py",
    )
    assert "value + 9" in out
    assert "Did you mean" not in out


def test_apply_edit_multi_match_keeps_count_message_without_hint() -> None:
    doubled = SAMPLE + SAMPLE
    with pytest.raises(RetryableToolInputError) as exc:
        _apply_edit_replacements(
            doubled,
            [_edit("    total = value + 1\n", "    total = value + 9\n")],
            path="src/a.py",
        )
    message = str(exc.value)
    assert "matches 2 locations" in message
    assert "Did you mean" not in message


def test_hint_is_length_bounded_and_truncates_long_lines() -> None:
    long_line = "    total = " + ("x" * 600)
    content = "def compute(value):\n" + long_line + "\n    return total\n"
    hint = _edit_file_closest_lines_hint("    total = " + ("x" * 599) + "\n", content)
    assert hint  # a close match exists
    assert "…" in hint  # per-line truncation applied
    assert len(hint) < 1500  # bounded well under the raw 600-char source line


def test_no_match_emits_hint_telemetry_event() -> None:
    events: list[dict] = []
    ctx = ToolContext(on_runtime_event=events.append, session_key="agent:main:test")
    token = current_tool_context.set(ctx)
    try:
        with pytest.raises(RetryableToolInputError):
            _apply_edit_replacements(
                SAMPLE,
                [_edit("    total = value + 2\n", "    total = value + 3\n")],
                path="src/a.py",
            )
    finally:
        current_tool_context.reset(token)

    hint_events = [e for e in events if e.get("name") == "edit_file.no_match_hint"]
    assert len(hint_events) == 1
    event = hint_events[0]
    assert event["feature"] == "edit_file_recovery"
    assert event["tool"] == "edit_file"
    assert event["outcome"] == "hint"
    assert event["reason"] == "closest_lines"
    assert event["matches"] >= 1
    # the no-match rejection event is still emitted alongside the hint event
    assert any(e.get("reason") == "no_match" for e in events)
