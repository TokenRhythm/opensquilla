from __future__ import annotations

import json

from opensquilla.engine.runtime_events import append_runtime_event
from opensquilla.engine.turn_runner.agent_bootstrap_stage import (
    _final_diff_contract_mode_from_env,
    _positive_int_from_env,
    _post_tool_empty_recovery_mode_from_env,
    _reasoning_prefill_recovery_mode_from_env,
    _runtime_recovery_mode_from_env,
    _source_diff_candidate_mode_from_env,
    _source_diff_preservation_mode_from_env,
)


def test_append_runtime_event_writes_jsonl(tmp_path) -> None:
    path = tmp_path / "nested" / "runtime_events.jsonl"

    append_runtime_event(
        str(path),
        {
            "feature": "tool_loop_observer",
            "reason": "reasoning_only",
            "details": {"iteration": 3},
        },
    )

    event = json.loads(path.read_text(encoding="utf-8"))
    assert event["feature"] == "tool_loop_observer"
    assert event["reason"] == "reasoning_only"
    assert event["details"] == {"iteration": 3}
    assert isinstance(event["created_at"], str)
    assert isinstance(event["timestamp"], str)


def test_append_runtime_event_ignores_missing_path(tmp_path) -> None:
    append_runtime_event(None, {"feature": "tool_loop_observer"})

    assert list(tmp_path.iterdir()) == []


def test_runtime_recovery_modes_from_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_RUNTIME_RECOVERY_MODE", "warn_model")
    monkeypatch.setenv("OPENSQUILLA_FINAL_DIFF_CONTRACT_MODE", "warn_model")
    monkeypatch.setenv("OPENSQUILLA_POST_TOOL_EMPTY_RECOVERY_MODE", "warn_model")
    monkeypatch.setenv("OPENSQUILLA_REASONING_PREFILL_RECOVERY_MODE", "recover")

    assert _runtime_recovery_mode_from_env() == "warn_model"
    assert _final_diff_contract_mode_from_env() == "warn_model"
    assert _post_tool_empty_recovery_mode_from_env() == "warn_model"
    assert _reasoning_prefill_recovery_mode_from_env() == "recover"

    monkeypatch.setenv("OPENSQUILLA_RUNTIME_RECOVERY_MODE", "invalid")
    monkeypatch.setenv("OPENSQUILLA_FINAL_DIFF_CONTRACT_MODE", "invalid")
    monkeypatch.setenv("OPENSQUILLA_POST_TOOL_EMPTY_RECOVERY_MODE", "invalid")
    monkeypatch.setenv("OPENSQUILLA_REASONING_PREFILL_RECOVERY_MODE", "invalid")

    assert _runtime_recovery_mode_from_env() == "log"
    assert _final_diff_contract_mode_from_env() == "log"
    assert _post_tool_empty_recovery_mode_from_env() == "log"
    assert _reasoning_prefill_recovery_mode_from_env() == "log"


def test_source_diff_preservation_mode_from_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_SOURCE_DIFF_PRESERVATION_MODE", "block")
    assert _source_diff_preservation_mode_from_env() == "block"

    monkeypatch.setenv("OPENSQUILLA_SOURCE_DIFF_PRESERVATION_MODE", "off")
    assert _source_diff_preservation_mode_from_env() == "off"

    monkeypatch.setenv("OPENSQUILLA_SOURCE_DIFF_PRESERVATION_MODE", "invalid")
    assert _source_diff_preservation_mode_from_env() == "log"

    monkeypatch.delenv("OPENSQUILLA_SOURCE_DIFF_PRESERVATION_MODE")
    assert _source_diff_preservation_mode_from_env() == "log"
    assert _source_diff_preservation_mode_from_env("block") == "block"
    assert _source_diff_preservation_mode_from_env("off") == "off"
    assert _source_diff_preservation_mode_from_env("invalid") == "log"

    monkeypatch.setenv("OPENSQUILLA_SOURCE_DIFF_PRESERVATION_MODE", "off")
    assert _source_diff_preservation_mode_from_env("block") == "off"


def test_source_diff_candidate_mode_from_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_SOURCE_DIFF_CANDIDATE_MODE", "warn_model")
    assert _source_diff_candidate_mode_from_env() == "warn_model"

    monkeypatch.setenv("OPENSQUILLA_SOURCE_DIFF_CANDIDATE_MODE", "off")
    assert _source_diff_candidate_mode_from_env() == "off"

    monkeypatch.setenv("OPENSQUILLA_SOURCE_DIFF_CANDIDATE_MODE", "invalid")
    assert _source_diff_candidate_mode_from_env() == "log"

    monkeypatch.delenv("OPENSQUILLA_SOURCE_DIFF_CANDIDATE_MODE")
    assert _source_diff_candidate_mode_from_env() == "log"
    assert _source_diff_candidate_mode_from_env("warn_model") == "warn_model"
    assert _source_diff_candidate_mode_from_env("off") == "off"
    assert _source_diff_candidate_mode_from_env("invalid") == "log"

    monkeypatch.setenv("OPENSQUILLA_SOURCE_DIFF_CANDIDATE_MODE", "off")
    assert _source_diff_candidate_mode_from_env("warn_model") == "off"


def test_runtime_recovery_source_loop_max_nudges_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_RUNTIME_RECOVERY_SOURCE_LOOP_MAX_NUDGES", "3")
    assert _positive_int_from_env("OPENSQUILLA_RUNTIME_RECOVERY_SOURCE_LOOP_MAX_NUDGES", 1) == 3

    monkeypatch.setenv("OPENSQUILLA_RUNTIME_RECOVERY_SOURCE_LOOP_MAX_NUDGES", "invalid")
    assert _positive_int_from_env("OPENSQUILLA_RUNTIME_RECOVERY_SOURCE_LOOP_MAX_NUDGES", 1) == 1
