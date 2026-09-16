from copy import deepcopy

from opensquilla.engine.request_window import (
    compact_entry_tool_results,
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
