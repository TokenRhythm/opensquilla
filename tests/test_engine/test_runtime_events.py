from __future__ import annotations

import json

from opensquilla.engine.runtime_events import append_runtime_event
from opensquilla.engine.turn_runner.agent_bootstrap_stage import (
    _post_tool_empty_recovery_mode_from_env,
    _reasoning_prefill_recovery_mode_from_env,
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


def test_provider_response_recovery_modes_from_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_POST_TOOL_EMPTY_RECOVERY_MODE", "warn_model")
    monkeypatch.setenv("OPENSQUILLA_REASONING_PREFILL_RECOVERY_MODE", "recover")

    assert _post_tool_empty_recovery_mode_from_env() == "warn_model"
    assert _reasoning_prefill_recovery_mode_from_env() == "recover"

    monkeypatch.setenv("OPENSQUILLA_POST_TOOL_EMPTY_RECOVERY_MODE", "invalid")
    monkeypatch.setenv("OPENSQUILLA_REASONING_PREFILL_RECOVERY_MODE", "invalid")

    assert _post_tool_empty_recovery_mode_from_env() == "log"
    assert _reasoning_prefill_recovery_mode_from_env() == "log"
