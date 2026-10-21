from copy import deepcopy

import pytest

from opensquilla.engine.request_window import (
    compact_entry_tool_results,
    compact_request_user_text,
    compact_request_window_tools,
    iter_request_window_candidates,
)
from opensquilla.execution_status import normalize_execution_status
from opensquilla.provider import ContentBlockToolResult, ContentBlockToolUse, Message


def test_tool_projection_preserves_errors_recent_results_and_canonical_rows() -> None:
    entries = [
        {
            "role": "assistant",
            "content": "summary and user prose stay intact",
            "tool_calls": [{
                "type": "tool_result", "tool_use_id": f"call-{index}",
                "result": "synthetic detail " * 3000,
                "is_error": index == 1,
            }],
        }
        for index in range(6)
    ]
    before = deepcopy(entries)

    projected = compact_entry_tool_results(entries, protected_start_index=3)

    assert entries == before
    assert len(projected[0]["tool_calls"][0]["result"]) < len(entries[0]["tool_calls"][0]["result"])
    assert projected[1] == entries[1]
    assert projected[3:] == entries[3:]
    assert all(item["content"] == "summary and user prose stay intact" for item in projected)


def test_tool_projection_keeps_native_messages_and_critical_status() -> None:
    messages = [
        Message(role="user", content=[ContentBlockToolResult(
            tool_use_id=f"call-{index}", content="synthetic output " * 3000,
            execution_status=(
                normalize_execution_status({"status": "unknown", "source": "runtime"})
                if index == 1 else None
            ),
        )])
        for index in range(5)
    ]
    before = [message.model_copy(deep=True) for message in messages]
    projected = compact_request_window_tools(messages)

    assert messages == before
    assert projected[0] is not messages[0]
    assert projected[1] is messages[1]
    assert projected[-2:] == messages[-2:]


@pytest.mark.parametrize("window", [32_768, 131_072])
def test_three_message_user_projection_is_an_unchanged_index_request_view(window):
    original = "First task. " + "long historical content " * window + " Owner is Mira."
    messages = [
        Message(role="user", content=original),
        Message(role="assistant", content="Recorded."),
        Message(role="user", content="Continue the same task."),
    ]
    before = deepcopy(messages)
    candidates = list(iter_request_window_candidates(
        messages, protected_start_index=2, active_user_index=2,
        retain_user_messages=True, user_text_max_chars=window - 4096,
    ))
    projected = next(candidate for candidate in candidates if candidate.omitted_count == 0)
    assert projected.kept_indices == (0, 1, 2)
    assert [projected.map_index(index) for index in (0, 2, len(messages))] == [0, 2, 3]
    assert len(projected.messages) == 3
    assert projected.messages[0].content.endswith(" Owner is Mira.")
    assert "not a summary" in projected.messages[0].content
    assert projected.messages[1] is messages[1] and projected.messages[2] is messages[2]
    assert messages == before


def test_historical_user_excerpt_keeps_source_marker_and_admits_its_lossy_boundary():
    import hashlib
    import json

    original = "start " + "historical " * 5000 + "MIDDLE_FACT" + "context " * 5000 + " end"
    projected = compact_request_user_text(original, max_chars=32_768)
    assert projected.startswith("start ") and projected.endswith(" end")
    assert "MIDDLE_FACT" not in projected
    assert hashlib.sha256(original.encode()).hexdigest() in projected
    assert "omitted" in projected and "not a summary" in projected
    envelope = json.dumps({"text": original, "attachments": [{"mime": "image/png"}]})
    assert compact_request_user_text(envelope, max_chars=32_768) == envelope


def test_no_historical_real_user_does_not_fail_recovery_planning():
    messages = [
        Message(role="user", content="Current request."),
        Message(role="assistant", content="Earlier completed live round."),
        Message(role="assistant", content="Most recent completed live round."),
    ]
    candidates = list(iter_request_window_candidates(
        messages, protected_start_index=0, active_user_index=0,
        retain_user_messages=True, user_text_max_chars=32_768,
    ))
    assert candidates and all(messages[0] in candidate.messages for candidate in candidates)


def test_tool_row_projection_uses_same_protection_ordinals_for_list_content() -> None:
    entries = [
        {"role": "tool", "content": [{"type": "text", "text": "old result " * 3_000}]},
        *[{
            "role": "assistant", "content": "",
            "tool_calls": [{
                "type": "tool_result", "tool_use_id": f"call-{index}",
                "result": "protected result " * 3_000,
            }],
        } for index in range(4)],
    ]
    before = deepcopy(entries)

    projected = compact_entry_tool_results(entries, protected_start_index=1)

    assert len(projected[0]["content"][0]["text"]) < len(entries[0]["content"][0]["text"])
    assert projected[1:] == entries[1:]
    assert entries == before


def test_window_keeps_complete_tool_round_and_exact_active_user() -> None:
    messages = [
        Message(role="user", content="old question"),
        Message(role="assistant", content="old answer"),
        Message(role="user", content="current user"),
        Message(role="assistant", content=[ContentBlockToolUse(
            id="call", name="check", input={"item": "synthetic"},
        )]),
        Message(role="user", content=[ContentBlockToolResult(
            tool_use_id="call", content="result", is_error=True,
        )]),
    ]

    candidates = list(iter_request_window_candidates(
        messages, protected_start_index=2, protected_indexes={3, 4},
    ))

    assert len(candidates) == 1
    assert candidates[0].kept_indices == (2, 3, 4)
    assert all(a is b for a, b in zip(candidates[0].messages[1:], messages[2:], strict=True))
    assert candidates[0].map_index(2) == 1
    assert "not available" in candidates[0].messages[0].content


@pytest.mark.parametrize("window", [32_768, 131_072])
def test_native_recovery_keeps_original_user_and_later_correction_on_wire(window):
    import json

    from opensquilla.engine import Agent, AgentConfig
    from opensquilla.provider import OpenAIProvider
    from opensquilla.provider.protocol import project_provider_final_request

    current = "Continue the corrected task and report current status."
    first = "Original request. " + "historical context " * 60 + " Owner is Mira; deploy to east."
    correction = "Cancel east deployment. " + "review notes " * 60 + " Deploy only to west."
    messages = [
        Message(role="user", content=first),
        Message(role="assistant", content="historical output " * (window // 2)),
        Message(role="user", content=correction),
        Message(role="assistant", content="historical output " * (window // 2)),
        Message(role="user", content="Report the corrected status."),
        Message(role="assistant", content="Verification already completed; do not repeat it."),
        Message(role="user", content=current),
    ]
    before = deepcopy(messages)
    agent = Agent(
        provider=OpenAIProvider(api_key="synthetic-unused", model="synthetic-qwen"),
        config=AgentConfig(
            context_window_tokens=window, context_window_tokens_global_override=window,
            context_window_known=True, max_tokens=2048,
        ),
    )
    agent._current_turn_message = current
    config = agent._provider_admission_chat_config(current, context_window_tokens=window)
    outcome = agent._recover_local_request_window(
        messages, protected_turn_start_index=6,
        request_context_insert_index=6, runtime_context_insert_index=6,
        config=config, input_budget_tokens=window - 2048,
    )
    assert outcome is not None and outcome.ephemeral_only
    rebuilt = agent._provider_request_messages_for_count_projection(
        outcome.messages, request_context_message=None,
        request_context_insert_index=outcome.request_context_insert_index,
        runtime_context_message=agent._freeze_preflight_runtime_context_message(),
        runtime_context_insert_index=outcome.runtime_context_insert_index,
    )
    projection = project_provider_final_request(agent.provider, rebuilt, config=config)
    assert projection is not None and projection.fits
    wire = json.dumps(projection.payload, ensure_ascii=False)
    assert first in wire and correction in wire and current in wire
    assert wire.index(first) < wire.index(correction) < wire.index(current)
    assert "Verification already completed; do not repeat it." in wire
    assert messages == before
