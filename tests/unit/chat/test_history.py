from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from opensquilla.chat.history import transcript_entries_to_chat_messages
from opensquilla.content_reader import MAX_DISPLAY_CONTENT_BYTES


@pytest.mark.parametrize("count", [100, 300])
def test_bounded_history_preserves_many_tool_identities_and_control_metadata(count):
    segments = [{
        "type": "tool_use", "tool_use_id": f"call-{index}-" + "a" * 100,
        "name": "diagnostic_" + "tool" * 20, "status": "completed",
        "activity_order": index + 1, "stream_seq": index + 1000,
        "tool_presentation": {"operationKey": "diagnostic.test", "argumentDisplay": "all"},
        "input": {"query": "small", "count": index, "enabled": True},
    } for index in range(count)]
    entry = SimpleNamespace(
        role="assistant", content="answer", tool_calls=segments,
        turn_usage={"input_tokens": 12345, "output_tokens": 67890, "model": "model"},
        turn_context={"turn_id": "turn", "input_mode": "normal"},
    )
    legacy = transcript_entries_to_chat_messages([entry])[0]
    bounded = transcript_entries_to_chat_messages([entry], content_mode="bounded")[0]
    assert bounded["tool_calls"] == legacy["tool_calls"]
    assert bounded["usage"] == legacy["usage"]
    assert bounded["turn_context"] == legacy["turn_context"]
    assert "historyPayloadPreview" not in bounded
    assert entry.tool_calls == segments


def test_bounded_history_previews_nested_details_without_rewriting_segment_facts():
    text = "🙂正文" * 10000
    reasoning = "🙂思考" * 10000
    segments = [
        {"type": "text", "text": text, "presentation": "intermediate", "activity_order": 2},
        {"type": "tool_use", "tool_use_id": "call", "name": "diagnostic_tool", "activity_order": 3,
         "input": {"nested": {"body": "\x00" * 200000}, "number": 123, "enabled": True}},
        {"type": "tool_result", "tool_use_id": "call", "name": "diagnostic_tool",
         "activity_order": 4,
         "content": [{"type": "text", "text": "result" * 100000}], "is_error": False},
        {"type": "text", "text": text, "presentation": "answer", "activity_order": 5},
    ]
    entry = SimpleNamespace(role="assistant", content=text + "\n\n" + text,
                            reasoning_content=reasoning, tool_calls=segments)
    legacy = transcript_entries_to_chat_messages([entry])[0]
    bounded = transcript_entries_to_chat_messages([entry], content_mode="bounded")[0]
    assert len(json.dumps(bounded, ensure_ascii=False).encode()) < 200000
    for before, after in zip(legacy["tool_calls"], bounded["tool_calls"], strict=True):
        for key in ("type", "tool_use_id", "name", "activity_order", "presentation", "is_error"):
            assert after.get(key) == before.get(key)
    assert bounded["tool_calls"][1]["input"]["number"] == 123
    assert bounded["tool_calls"][1]["input"]["enabled"] is True
    assert bounded["tool_calls"][1]["input"]["nested"]["body"]
    assert bounded["historyPayloadPreview"] == {
        "detailsTruncated": True, "reasoningUtf16Length": len(reasoning.encode("utf-16-le")) // 2,
        "textUtf16Lengths": [len(text.encode("utf-16-le")) // 2] * 2,
    }
    assert legacy["tool_calls"] == segments
    assert entry.reasoning_content == reasoning


def test_bounded_numeric_detail_containers_charge_full_scalar_wire_size():
    segments = [{"type": "tool_result", "tool_use_id": f"call-{index}", "name": "diagnostic",
                 "result": [9223372036854775807] * 256} for index in range(300)]
    entry = SimpleNamespace(role="assistant", content="Done", tool_calls=segments)
    message = transcript_entries_to_chat_messages([entry], content_mode="bounded")[0]
    assert sum(len(json.dumps(segment["result"], separators=(",", ":")).encode())
               for segment in message["tool_calls"]) <= 128 * 1024
    assert all(
        segment["result"] and all(number == 9223372036854775807 for number in segment["result"])
        for segment in message["tool_calls"]
    )
    assert message["historyPayloadPreview"]["detailsTruncated"] is True


@pytest.mark.parametrize("encoded", [False, True])
def test_bounded_history_keeps_workspace_action_envelopes_and_previews_only_large_bodies(encoded):
    result = {
        "previewStatus": "registered", "documentId": "doc_test", "resourceId": "document:doc_test",
        "open": {"resourceId": "document:doc_test"},
        "entrypoint": "C:/workspace/" + "nested/" * 35 + "index.html", "workspace": "C:/workspace",
    }
    value = json.dumps(result, separators=(",", ":")) if encoded else result
    segments = [
        {"type": "tool_result", "tool_use_id": f"call-{index}", "name": "open_workspace_preview",
         "result": value} for index in range(300)
    ]
    entry = SimpleNamespace(role="assistant", content="Done", tool_calls=segments)
    message = transcript_entries_to_chat_messages([entry], content_mode="bounded")[0]
    assert message["tool_calls"] == segments
    assert "historyPayloadPreview" not in message
    large = {**result, "stdout": "large output" * 100000}
    entry.tool_calls = [{**segments[0], "result": json.dumps(large) if encoded else large}]
    message = transcript_entries_to_chat_messages([entry], content_mode="bounded")[0]
    projected = message["tool_calls"][0]["result"]
    if encoded:
        projected = json.loads(projected)
    assert {key: value for key, value in projected.items() if key != "stdout"} == result
    assert len(projected["stdout"].encode()) <= 16384
    assert message["historyPayloadPreview"]["detailsTruncated"] is True


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


def test_bounded_history_projection_does_not_put_large_body_on_wire() -> None:
    body = "x" * (26 * 1024 * 1024 + 17)
    entry = SimpleNamespace(
        id=44,
        message_id="large-message",
        session_id="session-1",
        session_key="agent:main:webchat:bounded-history",
        role="assistant",
        content=body,
        created_at="now",
        provenance_kind=None,
        provenance_source_session_key=None,
        provenance_source_tool=None,
        turn_usage=None,
        tool_calls=None,
    )

    messages = transcript_entries_to_chat_messages([entry], content_mode="bounded")

    assert len(messages) == 1
    message = messages[0]
    assert len(message["text"].encode("utf-8")) <= 16 * 1024
    assert message["contentRef"] == {
        "version": 1,
        "sessionKey": "agent:main:webchat:bounded-history",
        "sessionId": "session-1",
        "messageId": "large-message",
        "byteLength": len(body.encode("utf-8")),
    }


def test_bounded_history_content_ref_preserves_compacted_source() -> None:
    body = "x" * (20 * 1024)
    entry = SimpleNamespace(
        id=47,
        message_id="duplicate-message",
        session_id="session-1",
        session_key="agent:main:webchat:bounded-history",
        role="user",
        content=body,
        content_byte_length=len(body.encode("utf-8")),
        content_source="compacted",
        created_at="now",
        provenance_kind=None,
        provenance_source_session_key=None,
        provenance_source_tool=None,
        turn_usage=None,
        tool_calls=None,
    )

    message = transcript_entries_to_chat_messages([entry], content_mode="bounded")[0]

    assert message["contentRef"]["source"] == "compacted"
    assert message["contentRef"]["view"] == "raw"


def test_bounded_history_complete_display_projection_needs_no_hydration() -> None:
    body = '{"text":"' + ("x" * (20 * 1024)) + '","display_text":"shown"}'
    entry = SimpleNamespace(
        id=46,
        message_id="projected-message",
        session_id="session-1",
        session_key="agent:main:webchat:bounded-history",
        role="assistant",
        content=body,
        created_at="now",
        provenance_kind=None,
        provenance_source_session_key=None,
        provenance_source_tool=None,
        turn_usage=None,
        tool_calls=None,
    )

    message = transcript_entries_to_chat_messages([entry], content_mode="bounded")[0]

    assert message["text"] == "shown"
    assert message["contentPreviewComplete"] is True
    assert "contentRef" not in message


def test_bounded_tool_result_json_projects_result_text_without_protocol_envelope() -> None:
    body = '{"type":"tool_result","tool_use_id":"call-1","content":"' + (
        "safe output " * 2_000
    ) + '"}'
    entry = SimpleNamespace(
        id=52,
        message_id="tool-json-result",
        session_id="session-1",
        session_key="agent:main:webchat:bounded-history",
        role="tool",
        content=body,
        # A complete row can also carry bounded-storage metadata. Its safe
        # projected preview must remain visible, unlike a cut JSON envelope.
        content_byte_length=len(body.encode()),
        created_at="now",
        provenance_kind=None,
        provenance_source_session_key=None,
        provenance_source_tool=None,
        turn_usage=None,
        tool_calls=None,
    )

    message = transcript_entries_to_chat_messages([entry], content_mode="bounded")[0]

    assert message["text"].startswith("safe output ")
    assert "tool_result" not in message["text"]
    assert message["contentRef"]["view"] == "display"


def test_tool_result_block_list_projects_text_blocks() -> None:
    entry = SimpleNamespace(
        id=53,
        message_id="tool-json-blocks",
        session_id="session-1",
        session_key="agent:main:webchat:bounded-history",
        role="tool",
        content=(
            '[{"type":"tool_result","content":[{"type":"text","text":"line one"},'
            '{"type":"text","text":"line two"}]}]'
        ),
        created_at="now",
        provenance_kind=None,
        provenance_source_session_key=None,
        provenance_source_tool=None,
        turn_usage=None,
        tool_calls=None,
    )

    message = transcript_entries_to_chat_messages([entry], content_mode="bounded")[0]

    assert message["text"] == "line one\nline two"


def test_truncated_tool_result_envelope_fails_closed() -> None:
    entry = SimpleNamespace(
        id=54,
        message_id="tool-json-truncated",
        session_id="session-1",
        session_key="agent:main:webchat:bounded-history",
        role="tool",
        content='{"type": "tool_result", "content": "incomplete',
        created_at="now",
        provenance_kind=None,
        provenance_source_session_key=None,
        provenance_source_tool=None,
        turn_usage=None,
        tool_calls=None,
    )

    message = transcript_entries_to_chat_messages([entry], content_mode="bounded")[0]

    assert message["text"] == ""


def test_bounded_history_does_not_leak_truncated_protocol_json_prefix() -> None:
    body = '{"text":"' + ("x" * (20 * 1024)) + '","display_text":"shown"}'
    entry = SimpleNamespace(
        id=49,
        message_id="truncated-json",
        session_id="session-1",
        session_key="agent:main:webchat:bounded-history",
        role="assistant",
        # Simulate the bounded SQL projection: only the prefix crossed the
        # Python boundary, while content_byte_length retains the full size.
        content=body[:4096],
        content_byte_length=len(body.encode("utf-8")),
        created_at="now",
        provenance_kind=None,
        provenance_source_session_key=None,
        provenance_source_tool=None,
        turn_usage=None,
        tool_calls=None,
    )

    message = transcript_entries_to_chat_messages([entry], content_mode="bounded")[0]

    assert message["text"] == ""
    assert message["contentRef"]["view"] == "display"


@pytest.mark.parametrize("role", ["assistant", "tool"])
def test_bounded_history_marks_non_user_large_rows_as_semantic_view(role: str) -> None:
    body = "visible " + ("x" * (20 * 1024))
    entry = SimpleNamespace(
        id=48,
        message_id=f"{role}-large",
        session_id="session-1",
        session_key="agent:main:webchat:bounded-history",
        role=role,
        content=body,
        created_at="now",
        provenance_kind=None,
        provenance_source_session_key=None,
        provenance_source_tool=None,
        turn_usage=None,
        tool_calls=None,
    )

    message = transcript_entries_to_chat_messages([entry], content_mode="bounded")[0]

    assert message["text"].startswith("visible ")
    assert message["contentRef"]["view"] == "display"


@pytest.mark.parametrize("role", ["assistant", "tool"])
def test_bounded_history_marks_oversized_transformed_rows_unavailable(role: str) -> None:
    # The raw JSON is deliberately larger than the semantic display reader's
    # cap.  Its display_text is safe and short, but the authoritative body
    # contains provider/tool protocol material that must not cross the wire.
    body = (
        '{"text":"'
        + ("x" * (MAX_DISPLAY_CONTENT_BYTES + 1))
        + '","display_text":"safe preview"}'
    )
    entry = SimpleNamespace(
        id=50,
        message_id=f"{role}-oversized-transformed",
        session_id="session-1",
        session_key="agent:main:webchat:bounded-history",
        role=role,
        content=body[:4096],
        content_byte_length=len(body.encode("utf-8")),
        created_at="now",
        provenance_kind=None,
        provenance_source_session_key=None,
        provenance_source_tool=None,
        turn_usage=None,
        tool_calls=None,
    )

    message = transcript_entries_to_chat_messages([entry], content_mode="bounded")[0]

    assert message["text"] == ""
    assert "contentRef" not in message
    assert message["contentUnavailableReason"] == "display_projection_too_large"
    assert "x" * 1024 not in str(message)


def test_bounded_history_marks_pending_metadata_without_unsafe_reference() -> None:
    entry = SimpleNamespace(
        id=51,
        message_id="assistant-pending-content-size",
        session_id="session-1",
        session_key="agent:main:webchat:bounded-history",
        role="assistant",
        content='{"text":"protocol prefix that is not complete',
        content_metadata_pending=True,
        content_truncated=True,
        created_at="now",
        provenance_kind=None,
        provenance_source_session_key=None,
        provenance_source_tool=None,
        turn_usage=None,
        tool_calls=None,
    )

    message = transcript_entries_to_chat_messages([entry], content_mode="bounded")[0]

    assert message["text"] == ""
    assert message["contentMetadataPending"] is True
    assert "contentRef" not in message
    assert message["contentUnavailableReason"] == "content_metadata_pending"


def test_legacy_history_projection_keeps_full_body_for_direct_callers() -> None:
    body = "legacy body"
    entry = SimpleNamespace(
        id=45,
        message_id="legacy-message",
        session_id="session-1",
        session_key="agent:main:webchat:legacy-history",
        role="assistant",
        content=body,
        created_at="now",
        provenance_kind=None,
        provenance_source_session_key=None,
        provenance_source_tool=None,
        turn_usage=None,
        tool_calls=None,
    )

    messages = transcript_entries_to_chat_messages([entry])

    assert messages[0]["text"] == body


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




def test_transcript_entries_to_chat_messages_projects_public_tool_arguments() -> None:
    presentation = {
        "category": "network_read",
        "primaryArguments": ["url"],
        "argumentDisplay": "primary",
        "lifecycleDisplay": "boundary",
    }
    raw_tool_calls = [
        {
            "type": "tool_use",
            "tool_use_id": "fetch-1",
            "name": "http_request",
            "input": {
                "url": "https://example.test/report",
                "headers": {"Authorization": "secret"},
                "body": "private request body",
            },
            "tool_presentation": presentation,
        },
        {
            "type": "tool_result",
            "tool_use_id": "fetch-1",
            "name": "http_request",
            "arguments": {
                "url": "https://example.test/report",
                "headers": {"Authorization": "secret"},
            },
            "result": "ok",
            "tool_presentation": presentation,
        },
    ]
    entry = _assistant_entry(tool_calls=raw_tool_calls)

    messages = transcript_entries_to_chat_messages([entry])

    assert messages[0]["tool_calls"][0]["input"] == {
        "url": "https://example.test/report"
    }
    assert messages[0]["tool_calls"][1]["arguments"] == {
        "url": "https://example.test/report"
    }
    assert entry.tool_calls == raw_tool_calls


def test_transcript_entries_to_chat_messages_classifies_legacy_tool_arguments() -> None:
    entry = _assistant_entry(
        tool_calls=[
            {
                "type": "tool_use",
                "tool_use_id": "read-legacy",
                "name": "read_file",
                "input": {"path": "src/app.py", "offset": 500, "limit": 1000},
            }
        ]
    )

    messages = transcript_entries_to_chat_messages([entry])

    tool_use = messages[0]["tool_calls"][0]
    assert tool_use["input"] == {"path": "src/app.py"}
    assert "tool_presentation" not in tool_use


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
