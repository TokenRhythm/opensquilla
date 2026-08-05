"""Compaction safety levers: tiny guard plus default-on assistant protection.

Covers the OPENSQUILLA_PROVIDER_COMPACTION_TINY_GUARD_CHARS and
OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_RECENT_ASSISTANT env levers
with explicit rollback coverage.
"""

from __future__ import annotations

import json

import pytest

from opensquilla.provider.request_proof import (
    _compact_argument_string,
    _compact_recent_tail_payload_once,
    _emergency_compact_current_turn_payload_once,
    _final_hard_cap_payload_once,
    _hard_compact_string,
    prove_or_compact_provider_payload,
)

TINY_GUARD_ENV = "OPENSQUILLA_PROVIDER_COMPACTION_TINY_GUARD_CHARS"
PROTECT_RECENT_ENV = "OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_RECENT_ASSISTANT"
NEVER_WORSE_ENV = "OPENSQUILLA_PROVIDER_COMPACTION_NEVER_WORSE"


@pytest.fixture(autouse=True)
def _clean_relevant_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (TINY_GUARD_ENV, PROTECT_RECENT_ENV, NEVER_WORSE_ENV):
        monkeypatch.delenv(name, raising=False)


def _aggregate_args_payload() -> dict[str, object]:
    """Payload whose assistant tail triggers tier-2 aggregate argument mode."""
    big = "x" * 2000
    tool_calls = [
        {
            "id": f"call-{index}",
            "type": "function",
            "function": {
                "name": "exec_command",
                "arguments": json.dumps({"command": big, "workdir": "/w", "session": "s1"}),
            },
        }
        for index in range(3)
    ]
    return {
        "messages": [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "", "tool_calls": tool_calls},
        ]
    }


def test_tiny_guard_defaults_off_replaces_tiny_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NEVER_WORSE_ENV, "0")
    compacted = _compact_argument_string("s1", preview=False)
    assert compacted.startswith("[provider_request_tool_input_compacted:")
    assert len(compacted) > len("s1")


def test_tiny_guard_keeps_strings_shorter_than_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TINY_GUARD_ENV, "120")
    monkeypatch.setenv(NEVER_WORSE_ENV, "0")
    assert _compact_argument_string("s1", preview=False) == "s1"
    assert _compact_argument_string("y" * 120, preview=False) == "y" * 120
    long_value = "z" * 121
    assert _compact_argument_string(long_value, preview=False) != long_value


def test_tiny_guard_applies_to_hard_compact(monkeypatch: pytest.MonkeyPatch) -> None:
    value = "h" * 110
    assert _hard_compact_string(value, label="t").startswith("[opensquilla_compacted:")
    monkeypatch.setenv(TINY_GUARD_ENV, "120")
    assert _hard_compact_string(value, label="t") == value


def test_tiny_guard_invalid_env_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TINY_GUARD_ENV, "not-a-number")
    monkeypatch.setenv(NEVER_WORSE_ENV, "0")
    compacted = _compact_argument_string("s1", preview=False)
    assert compacted.startswith("[provider_request_tool_input_compacted:")


def test_aggregate_mode_preserves_tiny_arguments_with_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TINY_GUARD_ENV, "120")
    monkeypatch.setenv(PROTECT_RECENT_ENV, "0")
    monkeypatch.setenv(NEVER_WORSE_ENV, "0")
    compacted, metadata = _compact_recent_tail_payload_once(_aggregate_args_payload())
    assert metadata["aggregate_tool_arguments_compacted"] is True
    for message in compacted["messages"]:
        for tool_call in message.get("tool_calls") or []:
            arguments = json.loads(tool_call["function"]["arguments"])
            # Tiny fields survive verbatim; only the oversized command is compacted.
            assert arguments["workdir"] == "/w"
            assert arguments["session"] == "s1"
            assert arguments["command"].startswith(
                "[provider_request_tool_input_compacted:"
            )


def test_protect_recent_assistant_on_by_default() -> None:
    payload = _aggregate_args_payload()
    compacted, _ = _compact_recent_tail_payload_once(payload)
    last = compacted["messages"][-1]
    assert (
        last["tool_calls"][0]["function"]["arguments"]
        == payload["messages"][-1]["tool_calls"][0]["function"]["arguments"]
    )


def test_protect_recent_assistant_can_be_rolled_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PROTECT_RECENT_ENV, "0")
    payload = _aggregate_args_payload()
    compacted, _ = _compact_recent_tail_payload_once(payload)
    arguments = json.loads(
        compacted["messages"][-1]["tool_calls"][0]["function"]["arguments"]
    )
    assert arguments["command"].startswith("[provider_request_tool_input_compacted:")


def test_protect_recent_assistant_exempts_last_turn_tier2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PROTECT_RECENT_ENV, "1")
    payload = _aggregate_args_payload()
    # Add an older assistant turn that must still be compacted.
    payload["messages"].insert(1, deep_assistant_turn())
    compacted, _ = _compact_recent_tail_payload_once(payload)
    older = compacted["messages"][1]
    older_args = older["tool_calls"][0]["function"]["arguments"]
    assert "[provider_request_" in older_args
    last = compacted["messages"][-1]
    last_args = last["tool_calls"][0]["function"]["arguments"]
    assert last_args == payload["messages"][-1]["tool_calls"][0]["function"]["arguments"]
    assert "[provider_request_" not in last_args


def deep_assistant_turn() -> dict[str, object]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "old-call",
                "type": "function",
                "function": {
                    "name": "apply_patch",
                    "arguments": json.dumps({"patch": "p" * 3000}),
                },
            }
        ],
    }


def test_protect_recent_assistant_exempts_last_turn_tier3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PROTECT_RECENT_ENV, "1")
    fresh_patch = "diff --git a/f b/f\n" + "+" + "p" * 2000
    payload = {
        "messages": [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "old " + "o" * 2000},
            {"role": "user", "content": "go on"},
            {"role": "assistant", "content": fresh_patch},
        ]
    }
    compacted = _emergency_compact_current_turn_payload_once(payload)
    assert "emergency_compacted" in compacted["messages"][1]["content"]
    assert compacted["messages"][3]["content"] == fresh_patch


def test_protect_recent_assistant_remains_raw_at_hard_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PROTECT_RECENT_ENV, "1")
    fresh_patch = "d" * 5000
    payload = {
        "messages": [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "old " + "o" * 2000},
            {"role": "assistant", "content": fresh_patch},
        ]
    }
    compacted = _final_hard_cap_payload_once(payload)
    assert compacted["messages"][1]["content"].startswith("[opensquilla_compacted:")
    # Protected turn stays byte-identical even at the final request-only tier.
    protected = compacted["messages"][2]["content"]
    assert protected == fresh_patch


def test_hard_cap_keeps_user_prompt_before_anthropic_tool_result() -> None:
    active_prompt = "ACTIVE_USER_REQUEST " + ("u" * 5000)
    payload = {
        "messages": [
            {"role": "user", "content": active_prompt},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "lookup",
                        "input": {"query": "x" * 2000},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "content": "result " + ("r" * 5000),
                    }
                ],
            },
        ]
    }

    compacted = _final_hard_cap_payload_once(payload)

    assert compacted["messages"][0]["content"] == active_prompt
    assert compacted["messages"][2]["content"] == payload["messages"][2]["content"]


def test_proof_reports_tier_and_lever_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TINY_GUARD_ENV, "120")
    monkeypatch.setenv(PROTECT_RECENT_ENV, "on")
    payload = {
        "messages": [
            {"role": "user", "content": "task"},
            {"role": "tool", "tool_call_id": "c1", "content": "r" * 4000},
            {"role": "tool", "tool_call_id": "c2", "content": "fresh-1"},
            {"role": "tool", "tool_call_id": "c3", "content": "fresh-2"},
        ]
    }
    compacted, proof = prove_or_compact_provider_payload(
        payload,
        projection_adapter="openai",
        proof_budget=3000,
    )
    assert proof is not None
    assert proof["compaction_tier"] == proof["retry_count"]
    assert proof["compaction_tier"] >= 1
    assert proof["compaction_tiny_guard_chars"] == 120
    assert proof["compaction_protect_recent_assistant"] is True


def test_proof_tier_zero_when_fits() -> None:
    _, proof = prove_or_compact_provider_payload(
        {"messages": [{"role": "user", "content": "small"}]},
        projection_adapter="openai",
        proof_budget=10_000,
    )
    assert proof is not None
    assert proof["compaction_tier"] == 0
    assert proof["compaction_tiny_guard_chars"] == 0
    assert proof["compaction_protect_recent_assistant"] is True
    assert proof["compaction_protect_recent_results"] == 2
    assert proof["compaction_protect_error_results"] is True
    assert proof["compaction_protect_unresolved_results"] is True
    assert proof["compaction_skip_projected"] is True
    assert proof["compaction_never_worse"] is True
