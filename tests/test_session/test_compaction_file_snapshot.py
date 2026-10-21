"""Durable file previews must not become model context or compaction pressure."""

import json
from copy import deepcopy

import pytest

from opensquilla.session.compaction import (
    _attachment_safe_obligation_entries,
    _format_chunk_for_llm,
    estimate_entries_model_replay_chars,
    estimate_entry_model_replay_tokens,
    estimate_entry_replay_tokens,
)
from opensquilla.session.compaction_state import extract_compaction_obligations


def _entry() -> dict:
    return {
        "role": "assistant",
        "content": "Updated the page.",
        "tool_calls": [
            {
                "type": "tool_use",
                "tool_use_id": "call-write",
                "name": "write_file",
                "input": {"path": "page.html", "content": "omitted from history"},
            },
            {
                "type": "tool_result",
                "tool_use_id": "call-write",
                "result": "Written page.html",
                "is_error": False,
            },
        ],
    }


def _measure(entry: dict) -> tuple[int, int, int, str]:
    return (
        estimate_entry_model_replay_tokens(entry),
        estimate_entries_model_replay_chars([entry]),
        estimate_entry_replay_tokens(entry),
        _format_chunk_for_llm([entry]),
    )


@pytest.mark.parametrize("kind", ["write", "edit"])
@pytest.mark.parametrize(
    "content", ["", "UI_ONLY /private/snapshot.html\n" * 10_000], ids=["empty", "large"]
)
def test_file_snapshot_does_not_inflate_compaction_admission(kind: str, content: str) -> None:
    baseline = _entry()
    entry = deepcopy(baseline)
    entry["tool_calls"][1]["file_snapshot"] = {
        "kind": kind,
        "path": "/private/snapshot.html",
        "content": content,
    }
    before = deepcopy(entry)

    assert _measure(entry) == _measure(baseline)
    assert entry == before


def test_only_segment_metadata_is_excluded_not_same_named_tool_data() -> None:
    baseline = _entry()
    entry = deepcopy(baseline)
    user_data = {"type": "tool_result", "file_snapshot": "ACTUAL TOOL DATA " * 500}
    entry["tool_calls"][0]["input"]["file_snapshot"] = user_data
    entry["tool_calls"][1]["result"] = {"file_snapshot": user_data}
    before = deepcopy(entry)
    expected = _measure(entry)
    assert expected[0] > _measure(baseline)[0]
    assert expected[1] > _measure(baseline)[1]
    assert "ACTUAL TOOL DATA" in expected[3]

    entry["tool_calls"][1]["file_snapshot"] = {
        "kind": "write",
        "path": "page.html",
        "content": "UI ONLY " * 10_000,
    }

    assert _measure(entry) == expected
    safe = _attachment_safe_obligation_entries([entry])[0]
    assert safe["tool_calls"][0]["input"]["file_snapshot"] == user_data
    assert safe["tool_calls"][1]["result"]["file_snapshot"] == user_data
    assert entry["tool_calls"][0] == before["tool_calls"][0]
    assert entry["tool_calls"][1]["result"] == before["tool_calls"][1]["result"]


def test_file_snapshot_does_not_enter_compaction_obligations() -> None:
    baseline = _entry()
    entry = deepcopy(baseline)
    entry["tool_calls"][1]["file_snapshot"] = {
        "kind": "write",
        "path": "/private/ui-only.html",
        "content": "Run python /private/ui-only.py to fix the error UI_ONLY.",
    }
    before = deepcopy(entry)

    projected = _attachment_safe_obligation_entries([entry])
    assert projected == _attachment_safe_obligation_entries([baseline])
    obligations = extract_compaction_obligations(projected)
    assert obligations == extract_compaction_obligations([baseline])
    assert "UI_ONLY" not in json.dumps(projected)
    assert entry == before
