from __future__ import annotations

import json

from opensquilla.observability.decision_log import (
    DecisionEntry,
    build_intent_summary,
    compute_hashes,
    load_entries,
    write_decision_entry,
)


def test_decision_debug_mirror_stays_structured_not_raw(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENSQUILLA_DEBUG_LOG", "1")
    raw_prompt = "secret prompt that must not be mirrored"
    prompt_hash, system_hash, tools_hash = compute_hashes(
        raw_prompt,
        "system prompt",
        ["memory_search"],
    )
    entry = DecisionEntry(
        turn_id="turn-1",
        session_key="agent:main:test",
        session_id="session-1",
        prompt_hash=prompt_hash,
        system_prompt_hash=system_hash,
        tool_list_hash=tools_hash,
        tool_choice="auto",
        tokens_input=10,
        tokens_output=5,
        model="fake-model",
        provider="fake",
        latency_ms=12,
        ts="2026-05-03T20:00:00Z",
        cache_dynamic_hash="dyn",
        cache_read_input_tokens=7,
        cache_creation_input_tokens=3,
        resolved_model="fake-model",
        alias_resolution_chain=["alias", "fake-model"],
        provider_after_rewrite="fake",
        cache_legacy_hash="legacy",
        cache_shadow_final_hash="shadow",
        cache_key_collision=False,
        reasoning_hint_resolved="<think>...</think><final>...</final>",
        daily_notes_omitted=True,
        daily_notes_count_before_omit=2,
        daily_notes_policy_reason="auto_injection_disabled",
    )

    write_decision_entry(entry, log_dir=tmp_path)

    debug_files = list((tmp_path / "debug").glob("decisions-*-raw.jsonl"))
    assert len(debug_files) == 1
    mirror_text = debug_files[0].read_text(encoding="utf-8")
    mirror_payload = json.loads(mirror_text)

    assert raw_prompt not in mirror_text
    assert mirror_payload["turn_id"] == "turn-1"
    assert mirror_payload["entry"]["prompt_hash"] == prompt_hash
    assert mirror_payload["entry"]["system_prompt_hash"] == system_hash
    assert mirror_payload["entry"]["tool_list_hash"] == tools_hash
    assert mirror_payload["entry"]["cache_dynamic_hash"] == "dyn"
    assert mirror_payload["entry"]["cache_read_input_tokens"] == 7
    assert mirror_payload["entry"]["cache_creation_input_tokens"] == 3
    assert mirror_payload["entry"]["cache_legacy_hash"] == "legacy"
    assert mirror_payload["entry"]["cache_shadow_final_hash"] == "shadow"
    assert mirror_payload["entry"]["daily_notes_omitted"] is True
    assert mirror_payload["entry"]["daily_notes_count_before_omit"] == 2
    assert mirror_payload["entry"]["daily_notes_policy_reason"] == "auto_injection_disabled"


def test_intent_summary_is_redacted_and_persisted(tmp_path) -> None:
    summary = build_intent_summary(
        "Please analyze /home/alice/private/vendor.pdf with api_key=sk-"
        "1234567890abcdef1234567890abcdef and email alice@example.com",
    )

    assert summary == ""
    assert "/home/alice" not in summary
    assert "sk-1234567890abcdef1234567890abcdef" not in summary
    assert "alice@example.com" not in summary

    entry = DecisionEntry(
        turn_id="turn-2",
        session_key="agent:main:test",
        prompt_hash="a" * 16,
        system_prompt_hash="b" * 16,
        tool_list_hash="c" * 16,
        tool_choice="auto",
        tokens_input=1,
        tokens_output=2,
        model="fake-model",
        provider="fake",
        latency_ms=3,
        ts="2026-05-03T20:00:00Z",
        intent_summary=summary,
    )

    write_decision_entry(entry, log_dir=tmp_path)
    loaded = load_entries(next(tmp_path.glob("decisions-*.jsonl")))

    assert loaded[0].intent_summary is None


def test_decision_log_round_trips_daily_notes_policy(tmp_path) -> None:
    entry = DecisionEntry(
        turn_id="turn-daily",
        session_key="agent:main:test",
        prompt_hash="a" * 16,
        system_prompt_hash="b" * 16,
        tool_list_hash="c" * 16,
        tool_choice="auto",
        tokens_input=1,
        tokens_output=2,
        model="fake-model",
        provider="fake",
        latency_ms=3,
        ts="2026-05-03T20:00:00Z",
        daily_notes_omitted=True,
        daily_notes_count_before_omit=3,
        daily_notes_policy_reason="auto_injection_disabled",
    )

    write_decision_entry(entry, log_dir=tmp_path)
    loaded = load_entries(next(tmp_path.glob("decisions-*.jsonl")))

    assert loaded[0].daily_notes_omitted is True
    assert loaded[0].daily_notes_count_before_omit == 3
    assert loaded[0].daily_notes_policy_reason == "auto_injection_disabled"
