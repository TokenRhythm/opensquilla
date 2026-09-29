"""Tests for DecisionEntry.skills_invoked."""

from __future__ import annotations

import json
from pathlib import Path

from opensquilla.observability.decision_log import (
    SCHEMA_VERSION,
    DecisionEntry,
    load_entries,
    write_decision_entry,
)


def _make_entry(**overrides) -> DecisionEntry:
    defaults = dict(
        turn_id="t1",
        session_key="s1",
        prompt_hash="a" * 16,
        system_prompt_hash="b" * 16,
        tool_list_hash="c" * 16,
        tool_choice="auto",
        tokens_input=10,
        tokens_output=20,
        model="claude",
        provider="anthropic",
        latency_ms=100,
        ts="2026-05-20T00:00:00Z",
    )
    defaults.update(overrides)
    return DecisionEntry(**defaults)


def test_schema_version_keeps_skills_invoked_or_newer_schema() -> None:
    assert SCHEMA_VERSION >= 10


def test_skills_invoked_field_defaults_empty() -> None:
    entry = _make_entry()
    assert entry.skills_invoked == []


def test_skills_invoked_field_persists(tmp_path: Path) -> None:
    entry = _make_entry(skills_invoked=["pdf-toolkit", "summarize", "memory"])
    write_decision_entry(entry, log_dir=tmp_path)
    loaded = load_entries(next(tmp_path.glob("decisions-*.jsonl")))
    assert len(loaded) == 1
    assert loaded[0].skills_invoked == ["pdf-toolkit", "summarize", "memory"]


def test_old_schema_v9_row_reads_with_empty_skills_invoked(tmp_path: Path) -> None:
    """Backward-tolerant read: a v9 row (no skills_invoked field) must hydrate
    cleanly with an empty list."""
    v9_payload = {
        "turn_id": "old", "session_key": "s", "prompt_hash": "a" * 16,
        "system_prompt_hash": "b" * 16, "tool_list_hash": "c" * 16,
        "tool_choice": "auto", "tokens_input": 1, "tokens_output": 2,
        "model": "x", "provider": "y", "latency_ms": 3, "ts": "2026-01-01T00:00:00Z",
        "schema_version": 9,
    }
    path = tmp_path / "decisions-20260101.jsonl"
    path.write_text(json.dumps(v9_payload) + "\n", encoding="utf-8")
    loaded = load_entries(path)
    assert len(loaded) == 1
    assert loaded[0].skills_invoked == []


def test_collect_invoked_skills_filters_non_skill_view() -> None:
    """collect_invoked_skills returns only skill_view-resolved names, in order."""
    from opensquilla.engine.runtime import collect_invoked_skills
    invoked = collect_invoked_skills([
        {"name": "skill_view", "input": {"name": "pdf-toolkit"}},
        {"name": "skill_view", "input": {"name": "summarize"}},
        {"name": "other_tool", "input": {}},
    ])
    assert invoked == ["pdf-toolkit", "summarize"]


def test_collect_invoked_skills_dedups_repeated_calls() -> None:
    """A skill invoked multiple times in one turn appears once, at first position."""
    from opensquilla.engine.runtime import collect_invoked_skills
    invoked = collect_invoked_skills([
        {"name": "skill_view", "input": {"name": "pdf-toolkit"}},
        {"name": "skill_view", "input": {"name": "summarize"}},
        {"name": "skill_view", "input": {"name": "pdf-toolkit"}},  # dup
        {"name": "skill_view", "input": {"name": "memory"}},
    ])
    assert invoked == ["pdf-toolkit", "summarize", "memory"]








def test_collect_invoked_skills_extra_first_none_is_noop() -> None:
    """N16: extra_first=None preserves original behaviour."""
    from opensquilla.engine.runtime import collect_invoked_skills
    invoked = collect_invoked_skills(
        [{"name": "skill_view", "input": {"name": "pdf-toolkit"}}],
        extra_first=None,
    )
    assert invoked == ["pdf-toolkit"]
