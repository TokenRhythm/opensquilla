"""Unit coverage for the shared silent-reply protocol."""

from __future__ import annotations

import json

import pytest

from opensquilla.engine.silent_reply import (
    HEARTBEAT_ACK_TOKEN,
    NO_REPLY_TOKEN,
    is_silent_reply_prefix,
    normalize_silent_reply,
    sanitize_historical_silent_reply,
    sanitize_silent_reply_segments,
)


@pytest.mark.parametrize(
    ("token", "reason"),
    [
        (NO_REPLY_TOKEN, "no_reply"),
        (HEARTBEAT_ACK_TOKEN, "heartbeat_ack"),
    ],
)
def test_exact_sentinel_is_suppressed_for_compatibility(token: str, reason: str) -> None:
    result = normalize_silent_reply(
        f"  {token}\n",
        run_kind="default",
        input_mode="user",
    )

    assert result.text == ""
    assert result.changed is True
    assert result.suppressed is True
    assert result.sentinel == token
    assert result.delivery == "suppressed"
    assert result.suppression_reason == reason


@pytest.mark.parametrize(
    "payload",
    [
        "NO_REPLY\nStill working.",
        "HEARTBEAT_OK\nStill working.",
        "Still working.\nNO_REPLY",
        "NO_REPLY\nStill working.\nHEARTBEAT_OK",
    ],
)
def test_internal_mixed_sentinel_lines_keep_substantive_text(payload: str) -> None:
    result = normalize_silent_reply(
        payload,
        run_kind="goal",
        input_mode="system_event",
    )

    assert result.text == "Still working."
    assert result.changed is True
    assert result.suppressed is False
    assert result.delivery == "visible"
    assert result.suppression_reason is None


@pytest.mark.parametrize(
    "wrapped",
    [
        "`NO_REPLY`",
        "**NO_REPLY**",
        "__NO_REPLY__",
        "*NO_REPLY*",
        "_NO_REPLY_",
        "~~NO_REPLY~~",
        "**`NO_REPLY`**",
    ],
)
def test_internal_markdown_wrapped_sentinel_line_is_recognized(wrapped: str) -> None:
    result = normalize_silent_reply(
        f"{wrapped}\nStill working.",
        run_kind="goal",
        input_mode="system_event",
    )

    assert result.text == "Still working."
    assert result.changed is True
    assert result.sentinel == NO_REPLY_TOKEN


@pytest.mark.parametrize(
    "payload",
    [
        "```\nNO_REPLY\n```",
        "Example:\n```text\nNO_REPLY",
        "Example:\n~~~text\nHEARTBEAT_OK",
        "> NO_REPLY\nStill working.",
        "Example:\n    NO_REPLY",
        "Example:\n\tHEARTBEAT_OK",
        "Example:\n   \tNO_REPLY",
        "Example:\n \tHEARTBEAT_OK",
        "Use NO_REPLY in documentation.",
        "NO_REPLY: this is an example",
        "NO_REPLY.md",
        "HEARTBEAT_OKAY",
        "The literal `NO_REPLY` stays in this sentence.",
    ],
)
def test_code_quotes_and_prose_are_not_control_tokens(payload: str) -> None:
    result = normalize_silent_reply(
        payload,
        run_kind="goal",
        input_mode="system_event",
    )

    assert result.text == payload
    assert result.changed is False
    assert result.sentinel is None


def test_only_unfenced_edge_marker_is_removed() -> None:
    payload = "NO_REPLY\n```text\nHEARTBEAT_OK"

    result = normalize_silent_reply(
        payload,
        run_kind="goal",
        input_mode="system_event",
    )

    assert result.text == "```text\nHEARTBEAT_OK"
    assert result.changed is True
    assert result.sentinel == NO_REPLY_TOKEN


def test_marker_after_closed_fence_remains_a_control_line() -> None:
    payload = "```text\nNO_REPLY\n```\nHEARTBEAT_OK"

    result = normalize_silent_reply(
        payload,
        run_kind="goal",
        input_mode="system_event",
    )

    assert result.text == "```text\nNO_REPLY\n```"
    assert result.changed is True
    assert result.sentinel == HEARTBEAT_ACK_TOKEN


@pytest.mark.parametrize("indent", ["    ", "\t"])
def test_internal_marker_removal_preserves_substantive_indentation(indent: str) -> None:
    result = normalize_silent_reply(
        f"NO_REPLY\n{indent}indented body\nHEARTBEAT_OK",
        run_kind="goal",
        input_mode="system_event",
    )

    assert result.text == f"{indent}indented body"
    assert result.changed is True


def test_mixed_sentinel_text_from_user_remains_visible() -> None:
    payload = "NO_REPLY\nThis is quoted user text."
    result = normalize_silent_reply(
        payload,
        run_kind="default",
        input_mode="user",
    )

    assert result.text == payload
    assert result.changed is False
    assert result.delivery == "visible"


def test_heartbeat_reasoning_is_removed_before_ack_suppression() -> None:
    result = normalize_silent_reply(
        "<think>private reasoning</think>\n<final>HEARTBEAT_OK</final>",
        run_kind="heartbeat",
    )

    assert result.text == ""
    assert result.suppressed is True
    assert result.suppression_reason == "heartbeat_ack"


def test_heartbeat_reasoning_is_removed_from_real_alert() -> None:
    result = normalize_silent_reply(
        "<think>private reasoning</think>\nDisk usage reached 95%.",
        run_kind="heartbeat",
    )

    assert result.text == "Disk usage reached 95%."
    assert result.changed is True
    assert result.suppressed is False


def test_heartbeat_short_ack_compatibility_is_preserved() -> None:
    result = normalize_silent_reply(
        "HEARTBEAT_OK routine poll detail",
        run_kind="heartbeat",
        heartbeat_ack_max_chars=64,
    )

    assert result.text == ""
    assert result.suppressed is True
    assert result.suppression_reason == "heartbeat_ack"


def test_heartbeat_long_alert_after_ack_is_not_suppressed() -> None:
    detail = "x" * 65
    payload = f"HEARTBEAT_OK {detail}"
    result = normalize_silent_reply(
        payload,
        run_kind="heartbeat",
        heartbeat_ack_max_chars=64,
    )

    assert result.text == payload
    assert result.suppressed is False


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("NO", True),
        ("NO_", True),
        ("NO_REP", True),
        ("NO_REPLY", True),
        ("HEARTBEAT_", True),
        ("HEARTBEAT_O", True),
        ("HEARTBEAT_OK", True),
        ("N", False),
        ("no_", False),
        ("HEART", False),
        ("Here is a reply", False),
    ],
)
def test_distinctive_partial_sentinel_detection(candidate: str, expected: bool) -> None:
    assert is_silent_reply_prefix(candidate) is expected


def test_segment_sanitizer_is_copy_on_write_and_preserves_tool_records() -> None:
    segments = [
        {"type": "text", "text": "**NO_REPLY**"},
        {"type": "text", "text": "  "},
        {"type": "tool_use", "tool_use_id": "call-1", "name": "status"},
        {
            "type": "tool_result",
            "tool_use_id": "call-1",
            "name": "status",
            "result": "waiting",
        },
    ]

    result = sanitize_silent_reply_segments(
        segments,
        run_kind="goal",
        input_mode="system_event",
    )

    assert result.segments == segments[2:]
    assert result.changed is True
    assert result.suppressed is True
    assert result.delivery == "suppressed"
    assert result.suppression_reason == "no_reply"
    assert segments[0]["text"] == "**NO_REPLY**"
    assert result.segments[0] is not segments[2]


def test_segment_sanitizer_preserves_text_tool_chronology() -> None:
    segments = [
        {"type": "text", "text": "NO_REPLY\nPreparing."},
        {"type": "tool_use", "tool_use_id": "call-1", "name": "status"},
        {
            "type": "tool_result",
            "tool_use_id": "call-1",
            "name": "status",
            "result": "ready",
        },
        {"type": "text", "text": "Finished."},
    ]

    result = sanitize_silent_reply_segments(
        segments,
        run_kind="goal",
        input_mode="system_event",
    )

    assert result.segments == [
        {"type": "text", "text": "Preparing."},
        segments[1],
        segments[2],
        {"type": "text", "text": "Finished."},
    ]
    assert "".join(
        str(segment.get("text") or "")
        for segment in result.segments
        if segment.get("type") == "text"
    ) == "Preparing.Finished."


def test_segment_sanitizer_projects_split_marker_around_tool_boundary() -> None:
    segments = [
        {"type": "text", "text": "NO_"},
        {"type": "tool_use", "tool_use_id": "call-1", "name": "status"},
        {"type": "text", "text": "REPLY\nVisible body."},
    ]

    result = sanitize_silent_reply_segments(
        segments,
        run_kind="goal",
        input_mode="system_event",
    )

    assert result.segments == [
        segments[1],
        {"type": "text", "text": "Visible body."},
    ]
    assert "NO_" not in str(result.segments)
    assert "REPLY" not in str(result.segments)


def test_segment_sanitizer_keeps_middle_literal_marker() -> None:
    segments = [
        {"type": "text", "text": "Before\n"},
        {"type": "text", "text": "NO_REPLY\n"},
        {"type": "text", "text": "After"},
    ]

    result = sanitize_silent_reply_segments(
        segments,
        run_kind="goal",
        input_mode="system_event",
    )

    assert result.segments == segments
    assert result.changed is False


def test_history_sanitizer_preserves_envelope_metadata_and_source_objects() -> None:
    payload = {
        "text": "NO_REPLY\nStill checking.",
        "display_text": "**NO_REPLY**\nStill checking.",
        "artifacts": [{"id": "artifact-1"}],
        "future_field": {"keep": True},
    }
    content = json.dumps(payload, ensure_ascii=False)
    segments = [{"type": "text", "text": "NO_REPLY\nStill checking."}]

    result = sanitize_historical_silent_reply(
        content,
        segments,
        role="assistant",
        turn_context={"intent": "goal_continuation"},
    )

    projected = json.loads(result.content)
    assert projected["text"] == "Still checking."
    assert projected["display_text"] == "Still checking."
    assert projected["artifacts"] == payload["artifacts"]
    assert projected["future_field"] == payload["future_field"]
    assert result.segments == [{"type": "text", "text": "Still checking."}]
    assert result.changed is True
    assert result.suppressed is False
    assert json.loads(content) == payload
    assert segments == [{"type": "text", "text": "NO_REPLY\nStill checking."}]


def test_history_sanitizer_does_not_interpret_non_assistant_rows() -> None:
    segments = [{"type": "text", "text": "NO_REPLY"}]

    result = sanitize_historical_silent_reply(
        "NO_REPLY",
        segments,
        role="user",
        turn_context={"intent": "goal_continuation"},
    )

    assert result.content == "NO_REPLY"
    assert result.segments == segments
    assert result.changed is False
    assert result.delivery == "visible"


def test_normalization_and_segment_projection_are_idempotent() -> None:
    first = normalize_silent_reply(
        "NO_REPLY\r\nSynthetic result body.\r\nHEARTBEAT_OK",
        run_kind="goal",
        input_mode="system_event",
    )
    second = normalize_silent_reply(
        first.text,
        run_kind="goal",
        input_mode="system_event",
    )

    assert first.text == "Synthetic result body."
    assert second.text == first.text
    assert second.changed is False

    first_segments = sanitize_silent_reply_segments(
        [{"type": "text", "text": "NO_REPLY\nSynthetic result body."}],
        run_kind="goal",
        input_mode="system_event",
    )
    second_segments = sanitize_silent_reply_segments(
        first_segments.segments,
        run_kind="goal",
        input_mode="system_event",
    )
    assert second_segments.segments == first_segments.segments
    assert second_segments.changed is False
