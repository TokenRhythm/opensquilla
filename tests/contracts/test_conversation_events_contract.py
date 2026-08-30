"""Contract and compatibility tests for the dormant Conversation decoder."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from opensquilla.contracts.adapters.conversation_events import (
    ConversationEventContractError,
    canonical_event_name,
    decode_conversation_event,
    decode_conversation_event_frame,
    is_conversation_event_name,
)
from opensquilla.contracts.generated.v4.conversation_events import (
    ConversationEventFrame,
)
from opensquilla.contracts.generated.v4.conversation_events_metadata import (
    CONVERSATION_EVENTS_EVENT,
    CONVERSATION_EVENTS_EVENT_METADATA,
    CONVERSATION_EVENTS_SCHEMA_VERSION,
)
from opensquilla.contracts.generated.v4.gateway_contract_registry import (
    GATEWAY_EVENT_CONTRACTS,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "contracts/gateway/v4/conversation/fixtures"


def _cases(name: str) -> list[dict[str, Any]]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["cases"]


def test_contract_metadata_and_registry_describe_the_event_family() -> None:
    assert CONVERSATION_EVENTS_EVENT == "conversation.events"
    assert CONVERSATION_EVENTS_SCHEMA_VERSION == 1
    assert CONVERSATION_EVENTS_EVENT_METADATA["legacyUnversioned"] is True
    assert "session.event.text_delta" in CONVERSATION_EVENTS_EVENT_METADATA["wireNames"]
    descriptor = GATEWAY_EVENT_CONTRACTS[CONVERSATION_EVENTS_EVENT]
    assert descriptor.frame_model is ConversationEventFrame
    assert descriptor.schema_version == CONVERSATION_EVENTS_SCHEMA_VERSION


@pytest.mark.parametrize("case", _cases("events.json"), ids=lambda case: case["id"])
def test_valid_frames_decode_without_mutating_wire_input(case: dict[str, Any]) -> None:
    wire = case["wire"]
    original = json.loads(json.dumps(wire))
    decoded = decode_conversation_event_frame(wire)
    assert wire == original
    assert decoded.raw_payload is wire.get("payload")
    assert decoded.name.startswith(("session.event.", "session.", "task.", "chat."))


@pytest.mark.parametrize("case", _cases("errors.json"), ids=lambda case: case["id"])
def test_invalid_frames_are_rejected(case: dict[str, Any]) -> None:
    with pytest.raises(ConversationEventContractError):
        decode_conversation_event_frame(case["wire"])


def test_legacy_aliases_are_normalized_only_in_the_projection() -> None:
    payload = {
        "sessionKey": "agent:main:legacy",
        "taskId": "task-legacy",
        "streamSeq": 4,
    }
    decoded = decode_conversation_event("text_delta", payload)
    assert decoded.name == "session.event.text_delta"
    assert decoded.legacy is True
    assert decoded.session_key == "agent:main:legacy"
    assert decoded.task_id == "task-legacy"
    assert decoded.stream_seq == 4
    assert payload == {
        "sessionKey": "agent:main:legacy",
        "taskId": "task-legacy",
        "streamSeq": 4,
    }


def test_canonical_payload_marks_schema_version_and_preserves_extensions() -> None:
    decoded = decode_conversation_event(
        "session.event.text_delta",
        {
            "schema_version": 1,
            "key": "agent:main:canonical",
            "future": {"enabled": True},
        },
        {"replayed": True},
        10,
    )
    assert decoded.legacy is False
    assert decoded.schema_version == 1
    assert decoded.connection_seq == 10
    assert decoded.payload == {
        "schema_version": 1,
        "key": "agent:main:canonical",
        "future": {"enabled": True},
    }
    assert decoded.meta == {"replayed": True}


def test_unknown_additive_event_is_safe_to_observe_but_not_known() -> None:
    decoded = decode_conversation_event(
        "session.event.future_checkpoint",
        {"key": "agent:main:alpha", "schema_version": 1},
    )
    assert decoded.kind == "unknown"
    assert decoded.is_known is False
    assert decoded.session_key == "agent:main:alpha"


def test_null_or_primitive_payload_never_becomes_an_object_projection() -> None:
    decoded = decode_conversation_event("session.event.warning", None)
    assert decoded.payload is None
    assert decoded.raw_payload is None
    assert decoded.legacy is True


def test_legacy_explicit_null_fields_remain_legacy() -> None:
    decoded = decode_conversation_event_frame(
        {
            "event": "session.event.state_change",
            "payload": {
                "schema_version": None,
                "sessionKey": "agent:main:alpha",
                "streamSeq": None,
            },
        }
    )
    assert decoded.legacy is True
    assert decoded.schema_version is None
    assert decoded.session_key == "agent:main:alpha"
    assert decoded.stream_seq is None


def test_event_name_helpers_reject_unrelated_gateway_events() -> None:
    assert canonical_event_name("session.turn_committed.v1") == "session.event.turn_committed"
    assert is_conversation_event_name("task.running") is True
    assert is_conversation_event_name("presence") is False
    assert is_conversation_event_name(None) is False


def test_frame_form_and_callback_form_are_equivalent() -> None:
    frame = {
        "event": "session.event.text_delta",
        "payload": {"key": "agent:main:alpha", "stream_seq": 3},
        "meta": {"replayed": False},
        "seq": 9,
    }
    from_frame = decode_conversation_event(frame)
    from_callback = decode_conversation_event(
        frame["event"],
        frame["payload"],
        frame["meta"],
        frame["seq"],
    )
    assert from_frame == from_callback
