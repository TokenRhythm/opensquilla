"""Feishu interactive-card parsing of Approve/Deny actions."""

from __future__ import annotations

from opensquilla.channels.approval_prompt import parse_approval_action
from opensquilla.channels.feishu import FeishuChannel, FeishuChannelConfig


def _channel() -> FeishuChannel:
    return FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="websocket")
    )


def _card_event(decision: str, code: str) -> dict:
    return {
        "header": {"event_id": "evt-1"},
        "event": {
            "operator": {"open_id": "owner-open-id"},
            "open_chat_id": "chat-1",
            "action": {
                "value": {
                    "opensquilla_action": "approval_resolve",
                    "code": code,
                    "decision": decision,
                }
            },
        },
    }


def test_parse_approval_card_action_yields_inbound_message() -> None:
    channel = _channel()
    msg = channel._parse_approval_card_action(_card_event("approve", "AB12"))

    assert msg is not None
    assert msg.sender_id == "owner-open-id"
    assert msg.channel_id == "chat-1"
    assert msg.content == "/approve AB12"
    assert msg.metadata["approval_action"]["code"] == "AB12"
    # The shared parser recognises the carried action.
    assert parse_approval_action(msg) == ("AB12", "approve")


def test_parse_ignores_clarify_and_unknown_actions() -> None:
    channel = _channel()
    clarify = {
        "event": {"action": {"value": {"opensquilla_action": "clarify_submit"}}},
    }
    assert channel._parse_approval_card_action(clarify) is None
    bad_decision = _card_event("maybe", "AB12")
    assert channel._parse_approval_card_action(bad_decision) is None
    missing_code = {
        "event": {
            "action": {"value": {"opensquilla_action": "approval_resolve", "decision": "approve"}}
        }
    }
    assert channel._parse_approval_card_action(missing_code) is None


def test_parse_always_decision_yields_universal_text_spelling() -> None:
    channel = _channel()
    msg = channel._parse_approval_card_action(_card_event("always", "AB12"))

    assert msg is not None
    assert msg.content == "/approve AB12 always"
    # The shared parser maps the tap to the durable same-type decision.
    assert parse_approval_action(msg) == ("AB12", "always")


def test_group_card_action_rebuilds_group_session_key() -> None:
    # A group-origin approval card tap must land in the SAME session the
    # originating group message created, or the dispatch resolver rejects it.
    from opensquilla.channels.manager import ChannelManager

    channel = _channel()
    group_message_event = {
        "header": {"event_id": "evt-msg"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_owner"}},
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_group",
                "chat_type": "group",
                "message_type": "text",
                "content": '{"text": "@_user_1 run the deploy"}',
            },
        },
    }
    origin_msg = channel.parse_event(group_message_event)
    origin_key = ChannelManager._build_session_key("feishu", origin_msg)
    assert ":group:" in origin_key

    card_event = {
        "header": {"event_id": "evt-card"},
        "event": {
            "operator": {"open_id": "ou_owner"},
            "open_chat_id": "oc_group",
            "action": {
                "value": {
                    "opensquilla_action": "approval_resolve",
                    "code": "AB12",
                    "decision": "approve",
                    "channel_id": "oc_group",
                    "is_group": True,
                    "chat_type": "group",
                }
            },
        },
    }
    card_msg = channel._parse_approval_card_action(card_event)
    assert card_msg is not None
    assert card_msg.metadata["is_group"] is True
    card_key = ChannelManager._build_session_key("feishu", card_msg)
    assert card_key == origin_key


def test_legacy_card_without_context_stays_direct() -> None:
    # Cards rendered before the chat-context contract carry no is_group /
    # chat_type; their session key must keep the previous DM shape.
    from opensquilla.channels.manager import ChannelManager

    channel = _channel()
    msg = channel._parse_approval_card_action(_card_event("approve", "AB12"))
    assert msg is not None
    assert "is_group" not in msg.metadata
    key = ChannelManager._build_session_key("feishu", msg)
    assert ":direct:" in key
