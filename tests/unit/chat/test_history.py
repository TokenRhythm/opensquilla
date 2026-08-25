from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.chat.history import transcript_entries_to_chat_messages


def test_transcript_entries_to_chat_messages_preserves_usage_and_artifacts() -> None:
    entry = SimpleNamespace(
        id=42,
        message_id="m1",
        role="assistant",
        content=(
            '{"text": "raw", "display_text": "shown", '
            '"artifacts": [{"id": "art-a1"}]}'
        ),
        created_at="now",
        provenance_kind=None,
        provenance_source_session_key=None,
        provenance_source_tool=None,
        turn_usage={"input_tokens": 1, "output_tokens": 2, "model": "openai/test"},
        tool_calls=None,
    )

    messages = transcript_entries_to_chat_messages([entry])

    assert messages[0]["id"] == "m1"
    assert messages[0]["text"] == "shown"
    assert messages[0]["transcript_id"] == 42
    assert messages[0]["artifacts"][0]["id"] == "art-a1"
    assert messages[0]["input_tokens"] == 1
    assert messages[0]["output_tokens"] == 2
    assert messages[0]["model"] == "openai/test"
    assert "reasoning_content" not in messages[0]


def test_transcript_entries_to_chat_messages_rebuilds_artifact_thumbnail_url() -> None:
    # A persisted assistant turn stores the public artifact payload, which carries
    # the reconstructed thumbnail_url but not the internal has_thumbnail boolean.
    entry = SimpleNamespace(
        id=43,
        message_id="m3",
        role="assistant",
        content=(
            '{"text": "here is the chart", "artifacts": [{'
            '"id": "art-bmYMIceM2Ddx3rkFM4BOmZ7A", "kind": "artifact_ref", '
            '"name": "chart.png", "mime": "image/png", "size": 954199, '
            '"session_id": "session-1", "source": "publish_artifact", '
            '"created_at": "2026-06-13T00:00:00Z", "store": "artifacts", '
            '"download_url": "/api/v1/artifacts/art-bmYMIceM2Ddx3rkFM4BOmZ7A", '
            '"thumbnail_url": "/api/v1/artifacts/art-bmYMIceM2Ddx3rkFM4BOmZ7A?variant=thumb"'
            '}]}'
        ),
        created_at="now",
        provenance_kind=None,
        provenance_source_session_key=None,
        provenance_source_tool=None,
        turn_usage=None,
        tool_calls=None,
    )

    messages = transcript_entries_to_chat_messages([entry])

    artifact = messages[0]["artifacts"][0]
    assert artifact["id"] == "art-bmYMIceM2Ddx3rkFM4BOmZ7A"
    assert artifact["thumbnail_url"] == (
        "/api/v1/artifacts/art-bmYMIceM2Ddx3rkFM4BOmZ7A?variant=thumb"
    )


def test_transcript_entries_to_chat_messages_sanitizes_legacy_preflight_confirmation() -> None:
    entry = SimpleNamespace(
        id=43,
        message_id="m2",
        role="user",
        content=(
            "请帮我判断这份供应商续费材料：这个合同要不要签、拒绝还是谈判，并给我一份决策表。\n\n"
            "合同摘录：\n"
            "- 服务期：2026-07-01 到 2027-06-30\n\n"
            "Confirmed request fields:\n"
            "- audience: decision owner\n"
            "- decision_question: 签不签合同\n\n"
            "<!-- opensquilla:meta_preflight_confirmed=1 -->\n"
            "<!-- opensquilla:meta_preflight_run_id=01KTCSELFVALID123 -->"
        ),
        created_at="now",
        provenance_kind=None,
        provenance_source_session_key=None,
        provenance_source_tool=None,
        turn_usage=None,
        tool_calls=None,
    )

    messages = transcript_entries_to_chat_messages([entry])

    assert messages[0]["text"] == (
        "请帮我判断这份供应商续费材料：这个合同要不要签、拒绝还是谈判，并给我一份决策表。\n\n"
        "合同摘录：\n"
        "- 服务期：2026-07-01 到 2027-06-30"
    )
    assert "Confirmed request fields" not in messages[0]["text"]
    assert "opensquilla:meta_preflight" not in messages[0]["text"]


def test_transcript_entries_to_chat_messages_hides_marker_only_preflight_confirmation() -> None:
    entry = SimpleNamespace(
        id=45,
        message_id="m4",
        role="user",
        content=(
            "<!-- opensquilla:meta_preflight_confirmed=1 -->\n"
            "<!-- opensquilla:meta_preflight_run_id=01KTCMARKERONLY -->"
        ),
        created_at="now",
        provenance_kind=None,
        provenance_source_session_key=None,
        provenance_source_tool=None,
        turn_usage=None,
        tool_calls=None,
    )

    messages = transcript_entries_to_chat_messages([entry])

    assert messages[0]["text"] == ""


def test_transcript_entries_to_chat_messages_hides_legacy_generated_plan_control() -> None:
    entry = SimpleNamespace(
        id=46,
        message_id="m-plan",
        role="user",
        content=(
            "[2026-07-27T20:14+08:00 Mon Asia/Shanghai]\n"
            "Implement the approved plan “Site refresh”. "
            "Work through its ordered steps and record truthful checkpoints."
        ),
        created_at="now",
        provenance_kind=None,
        provenance_source_session_key=None,
        provenance_source_tool=None,
        turn_context={"plan_run_id": "run-1"},
        turn_usage=None,
        tool_calls=None,
    )

    messages = transcript_entries_to_chat_messages([entry])

    assert messages[0]["text"] == ""
    assert messages[0]["turn_context"]["plan_run_id"] == "run-1"


def test_transcript_entries_to_chat_messages_keeps_explicit_plan_implementation_text() -> None:
    entry = SimpleNamespace(
        id=47,
        message_id="m-plan-custom",
        role="user",
        content="Implement only the first two approved steps, then stop.",
        created_at="now",
        provenance_kind=None,
        provenance_source_session_key=None,
        provenance_source_tool=None,
        turn_context={"plan_run_id": "run-2"},
        turn_usage=None,
        tool_calls=None,
    )

    messages = transcript_entries_to_chat_messages([entry])

    assert messages[0]["text"] == "Implement only the first two approved steps, then stop."


def _assistant_entry(**overrides: object) -> SimpleNamespace:
    entry = SimpleNamespace(
        id=7,
        message_id="m2",
        role="assistant",
        content="final answer",
        created_at="now",
        provenance_kind=None,
        provenance_source_session_key=None,
        provenance_source_tool=None,
        turn_usage=None,
        tool_calls=None,
    )
    for key, value in overrides.items():
        setattr(entry, key, value)
    return entry


def test_transcript_entries_to_chat_messages_carries_assistant_reasoning() -> None:
    entry = _assistant_entry(reasoning_content="Weighing both options first.")

    messages = transcript_entries_to_chat_messages([entry])

    assert messages[0]["reasoning_content"] == "Weighing both options first."


def test_transcript_entries_to_chat_messages_omits_blank_reasoning() -> None:
    entry = _assistant_entry(reasoning_content="   ")

    messages = transcript_entries_to_chat_messages([entry])

    assert "reasoning_content" not in messages[0]


def test_transcript_entries_to_chat_messages_sanitizes_tool_call_preflight_payloads() -> None:
    entry = SimpleNamespace(
        id=44,
        message_id="m3",
        role="assistant",
        content="done",
        created_at="now",
        provenance_kind=None,
        provenance_source_session_key=None,
        provenance_source_tool=None,
        turn_usage=None,
        tool_calls=[
            {
                "name": "meta_user_input",
                "input": {
                    "clarify_skip_summary": {
                        "trigger_message": (
                            "请帮我判断这份供应商续费材料。\n\n"
                            "合同摘录：\n"
                            "- 价格：每月 $4,800\n\n"
                            "Confirmed request fields:\n"
                            "- audience: decision owner\n"
                            "- decision_question: 签不签合同\n\n"
                            "<!-- opensquilla:meta_preflight_confirmed=1 -->"
                        )
                    }
                },
            }
        ],
    )

    messages = transcript_entries_to_chat_messages([entry])

    trigger = messages[0]["tool_calls"][0]["input"]["clarify_skip_summary"][
        "trigger_message"
    ]
    assert trigger == (
        "请帮我判断这份供应商续费材料。\n\n"
        "合同摘录：\n"
        "- 价格：每月 $4,800"
    )
    assert "Confirmed request fields" not in trigger


def test_transcript_entries_to_chat_messages_keeps_plain_confirmed_fields_text() -> None:
    entry = SimpleNamespace(
        id=46,
        message_id="m5",
        role="assistant",
        content="done",
        created_at="now",
        provenance_kind=None,
        provenance_source_session_key=None,
        provenance_source_tool=None,
        turn_usage=None,
        tool_calls=[{"text": "Confirmed request fields:\n- this is a visible note"}],
    )

    messages = transcript_entries_to_chat_messages([entry])

    assert messages[0]["tool_calls"][0]["text"] == (
        "Confirmed request fields:\n- this is a visible note"
    )


def test_transcript_entries_to_chat_messages_cleans_goal_sentinels_without_mutation() -> None:
    raw_content = (
        '{"text": "HEARTBEAT_OK\\nraw status", '
        '"display_text": "NO_REPLY\\nvisible status\\nHEARTBEAT_OK", '
        '"artifacts": [{"id": "art-status"}]}'
    )
    raw_tool_calls = [
        {"type": "text", "text": "NO_REPLY\nchecking the external state"},
        {
            "type": "tool_use",
            "tool_use_id": "call-status",
            "name": "read_status",
            "input": {},
        },
        {"type": "text", "text": "HEARTBEAT_OK"},
    ]
    entry = _assistant_entry(
        content=raw_content,
        tool_calls=raw_tool_calls,
        turn_context={"intent": "goal_continuation"},
    )

    messages = transcript_entries_to_chat_messages([entry])

    assert messages[0]["text"] == "visible status"
    assert messages[0]["artifacts"][0]["id"] == "art-status"
    assert messages[0]["tool_calls"] == [
        {"type": "text", "text": "checking the external state"},
        {
            "type": "tool_use",
            "tool_use_id": "call-status",
            "name": "read_status",
            "input": {},
        },
    ]
    assert entry.content == raw_content
    assert entry.tool_calls == raw_tool_calls


def test_transcript_entries_to_chat_messages_keeps_unattributed_mixed_sentinel_text() -> None:
    entry = _assistant_entry(
        content="NO_REPLY\nThis is quoted historical prose.",
        tool_calls=[
            {
                "type": "text",
                "text": "HEARTBEAT_OK\nThis segment has no system-event provenance.",
            }
        ],
    )

    messages = transcript_entries_to_chat_messages([entry])

    assert messages[0]["text"] == "NO_REPLY\nThis is quoted historical prose."
    assert messages[0]["tool_calls"][0]["text"] == (
        "HEARTBEAT_OK\nThis segment has no system-event provenance."
    )


def test_transcript_entries_to_chat_messages_hides_exact_assistant_sentinel() -> None:
    entry = _assistant_entry(content="  NO_REPLY\n", tool_calls=None)

    assert transcript_entries_to_chat_messages([entry]) == []


def test_transcript_entries_to_chat_messages_keeps_isolated_tool_markers_as_text() -> None:
    # Exact syntax without a confirmed adjacent result is still ordinary text.
    # This avoids reinterpreting user-requested examples as internal activity.
    entry = _assistant_entry(
        message_id="m-flat-narration",
        content=(
            "继续补齐上下文: 章节重新生成接口、前端 API client 与测试结构。\n"
            "[Used tool: read_file]\n"
            "[Used tool: read_file]\n"
            "[Used tool: list_dir]"
        ),
    )

    messages = transcript_entries_to_chat_messages([entry])

    assert messages[0]["text"] == (
        "继续补齐上下文: 章节重新生成接口、前端 API client 与测试结构。\n"
        "[Used tool: read_file]\n"
        "[Used tool: read_file]\n"
        "[Used tool: list_dir]"
    )
    assert "tool_calls" not in messages[0]


def test_transcript_entries_to_chat_messages_folds_flattened_tool_result_dump() -> None:
    # The raw wrapper is hidden, while the tool identity and payload remain
    # available through the existing expandable tool timeline.
    entries = [
        _assistant_entry(
            message_id="m-flat-tooluse",
            content="[Used tool: read_file]",
        ),
        _assistant_entry(
            message_id="m-flat-toolresult",
            role="user",
            content=(
                '[Tool result (call_00_TUIq7hPsIGaww7lcUiuc8669): 1  """Proposal '
                'generation and management API routes."""\n2  \n3  import json]'
            ),
        ),
    ]

    messages = transcript_entries_to_chat_messages(entries)

    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert messages[0]["text"] == ""
    assert messages[0]["tool_calls"] == [
        {
            "type": "tool_use",
            "tool_use_id": "call_00_TUIq7hPsIGaww7lcUiuc8669",
            "name": "read_file",
            "input": {},
            "legacy_projection": True,
        },
        {
            "type": "tool_result",
            "tool_use_id": "call_00_TUIq7hPsIGaww7lcUiuc8669",
            "name": "read_file",
            "result": (
                '1  """Proposal generation and management API routes."""\n2  \n3  import json'
            ),
            "legacy_projection": True,
        },
    ]


def test_transcript_entries_to_chat_messages_projects_ordered_parallel_activity() -> None:
    entries = [
        _assistant_entry(
            id=20,
            message_id="m-parallel-tools",
            content=(
                "Inspect the source.\n"
                "[Used tool: read_file]\n"
                "Compare the directory.\n"
                "[Used tool: list_dir]"
            ),
        ),
        _assistant_entry(
            id=21,
            message_id="m-parallel-results",
            role="user",
            content=(
                "[Tool result (call-read): source payload]\n"
                "[Tool result (call-list): directory payload]"
            ),
        ),
    ]

    messages = transcript_entries_to_chat_messages(entries)

    assert len(messages) == 1
    assert messages[0]["message_id"] == "m-parallel-tools"
    assert messages[0]["text"] == ""
    assert messages[0]["tool_calls"] == [
        {"type": "text", "text": "Inspect the source."},
        {
            "type": "tool_use",
            "tool_use_id": "call-read",
            "name": "read_file",
            "input": {},
            "legacy_projection": True,
        },
        {"type": "text", "text": "Compare the directory."},
        {
            "type": "tool_use",
            "tool_use_id": "call-list",
            "name": "list_dir",
            "input": {},
            "legacy_projection": True,
        },
        {
            "type": "tool_result",
            "tool_use_id": "call-read",
            "name": "read_file",
            "result": "source payload",
            "legacy_projection": True,
        },
        {
            "type": "tool_result",
            "tool_use_id": "call-list",
            "name": "list_dir",
            "result": "directory payload",
            "legacy_projection": True,
        },
    ]


def test_transcript_entries_to_chat_messages_keeps_untrusted_mismatch_as_text() -> None:
    entries = [
        _assistant_entry(
            message_id="m-mismatch-tools",
            content="[Used tool: read_file]",
        ),
        _assistant_entry(
            message_id="m-mismatch-results",
            role="user",
            content=(
                "[Tool result (call-read): source payload]\n"
                "[Tool result (call-extra): unmatched payload]"
            ),
        ),
    ]

    messages = transcript_entries_to_chat_messages(entries)

    assert [message["text"] for message in messages] == [
        "[Used tool: read_file]",
        (
            "[Tool result (call-read): source payload]\n"
            "[Tool result (call-extra): unmatched payload]"
        ),
    ]
    assert all("tool_calls" not in message for message in messages)


def test_transcript_entries_to_chat_messages_keeps_duplicate_result_ids_as_text() -> None:
    entries = [
        _assistant_entry(
            content="[Used tool: read_file]\n[Used tool: list_dir]",
        ),
        _assistant_entry(
            role="user",
            content=(
                "[Tool result (call-duplicate): source payload]\n"
                "[Tool result (call-duplicate): directory payload]"
            ),
        ),
    ]

    messages = transcript_entries_to_chat_messages(entries)

    assert len(messages) == 2
    assert all("tool_calls" not in message for message in messages)
    assert "[Used tool:" in messages[0]["text"]
    assert "[Tool result" in messages[1]["text"]


def test_transcript_entries_to_chat_messages_keeps_unattributed_tool_result_text() -> None:
    entry = _assistant_entry(
        message_id="m-user-toolresult-doc",
        role="user",
        content="[Tool result (example): this is documentation, not a tool event]",
    )

    messages = transcript_entries_to_chat_messages([entry])

    assert messages[0]["text"] == entry.content


def test_transcript_entries_to_chat_messages_preserves_ambiguous_result_suffix() -> None:
    entry = _assistant_entry(
        message_id="m-toolresult-with-request",
        role="user",
        tool_call_id="call-1",
        content="[Tool result (call-1): ok]\nPlease also update README.md",
    )

    messages = transcript_entries_to_chat_messages([entry])

    assert messages[0]["text"] == entry.content


@pytest.mark.parametrize(
    "payload",
    [
        '["a"]\nsecond payload line',
        '{"items": []}\nJSON diagnostic detail',
        "values = [1, 2]\nprint(values)",
        "ordinary output ]\ncontinuation from the tool",
    ],
)
def test_transcript_entries_to_chat_messages_keeps_ambiguous_multiline_remainder_in_result(
    payload: str,
) -> None:
    entries = [
        _assistant_entry(content="[Used tool: read_file]"),
        _assistant_entry(
            role="user",
            content=f"[Tool result (call-1): {payload}]",
        ),
    ]

    messages = transcript_entries_to_chat_messages(entries)

    assert len(messages) == 1
    assert messages[0]["tool_calls"][1]["result"] == payload
    assert all(segment.get("type") != "text" for segment in messages[0]["tool_calls"])


def test_transcript_entries_to_chat_messages_keeps_trusted_call_narration_ordered() -> None:
    entries = [
        _assistant_entry(
            content=(
                "Before the tool.\n"
                "[Used tool: read_file]\n"
                "After the tool was requested."
            ),
        ),
        _assistant_entry(
            role="user",
            content="[Tool result (call-1): source payload]",
        ),
    ]

    messages = transcript_entries_to_chat_messages(entries)

    assert [segment.get("text") for segment in messages[0]["tool_calls"]] == [
        "Before the tool.",
        None,
        "After the tool was requested.",
        None,
    ]


def test_transcript_entries_to_chat_messages_keeps_isolated_tool_only_turn() -> None:
    entry = _assistant_entry(
        message_id="m-flat-toolonly",
        content="[Used tool: read_file]\n[Used tool: list_dir]",
    )

    messages = transcript_entries_to_chat_messages([entry])

    assert len(messages) == 1
    assert messages[0]["text"] == "[Used tool: read_file]\n[Used tool: list_dir]"
    assert "tool_calls" not in messages[0]


def test_transcript_entries_to_chat_messages_recognizes_legacy_pair_across_page() -> None:
    previous = _assistant_entry(
        id=10,
        message_id="m-boundary-tool",
        content="[Used tool: read_file]",
    )
    result = _assistant_entry(
        id=11,
        message_id="m-boundary-result",
        role="user",
        content="[Tool result (call-boundary): private payload]",
    )

    messages = transcript_entries_to_chat_messages(
        [result],
        previous_entry=previous,
    )

    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert messages[0]["text"] == ""
    assert messages[0]["tool_calls"][1] == {
        "type": "tool_result",
        "tool_use_id": "call-boundary",
        "name": "read_file",
        "result": "private payload",
        "legacy_projection": True,
    }


def test_transcript_entries_to_chat_messages_uses_lookahead_without_duplicate_fold() -> None:
    tool = _assistant_entry(
        id=12,
        message_id="m-boundary-tool",
        content="[Used tool: read_file]",
    )
    following = _assistant_entry(
        id=13,
        message_id="m-boundary-result",
        role="user",
        content="[Tool result (call-boundary): private payload]",
    )

    assert transcript_entries_to_chat_messages([tool], next_entry=following) == []


def test_transcript_entries_to_chat_messages_keeps_ordinary_bracketed_text() -> None:
    # Regression guard: bracketed prose that is not a tool marker is untouched.
    entry = _assistant_entry(
        message_id="m-brackets",
        content="Here is the plan.\n[step 1] read the config\n[step 2] apply it",
    )

    messages = transcript_entries_to_chat_messages([entry])

    assert messages[0]["text"] == (
        "Here is the plan.\n[step 1] read the config\n[step 2] apply it"
    )


def test_transcript_entries_to_chat_messages_keeps_narration_when_segments_present() -> None:
    # When structured tool segments exist, the folded timeline renders them, so
    # the turn is kept even after its "[Used tool: ...]" narration marker is
    # stripped from the display text.
    entry = _assistant_entry(
        message_id="m-flat-with-segments",
        content="Reading the files now.\n[Used tool: read_file]",
        tool_calls=[
            {
                "type": "tool_use",
                "tool_use_id": "call-1",
                "name": "read_file",
                "input": {},
            }
        ],
    )

    messages = transcript_entries_to_chat_messages([entry])

    assert messages[0]["text"] == "Reading the files now."
    assert messages[0]["tool_calls"] == [
        {
            "type": "tool_use",
            "tool_use_id": "call-1",
            "name": "read_file",
            "input": {},
        }
    ]
