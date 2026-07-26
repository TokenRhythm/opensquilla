"""Tests for the shared channel approval-prompt contract."""

from __future__ import annotations

import pytest

from opensquilla.channels.approval_prompt import (
    ApprovalPromptRequest,
    bind_short_code,
    parse_approval_action,
    release_short_code,
    render_approval_prompt,
    reset_short_codes,
    resolve_short_code,
)
from opensquilla.channels.contract import ChannelCapabilityProfile
from opensquilla.channels.types import IncomingMessage


@pytest.fixture(autouse=True)
def _reset_codes():
    reset_short_codes()
    yield
    reset_short_codes()


def _request(short_code: str = "AB12") -> ApprovalPromptRequest:
    return ApprovalPromptRequest(
        approval_id="exec-1",
        namespace="exec",
        session_key="agent:main:chat",
        command_or_tool="rm target.txt",
        agent="main",
        short_code=short_code,
    )


def test_render_picks_interactive_card_when_adapter_supports_it() -> None:
    profile = ChannelCapabilityProfile(channel_type="feishu", interactive_cards=True)
    rendered = render_approval_prompt(profile, _request())
    assert "card" in rendered
    assert "text" in rendered
    # The card action carries the short code, never the raw approval id.
    actions = rendered["card"]["elements"][1]["actions"]
    values = [a["value"] for a in actions]
    assert {v["decision"] for v in values} == {"approve", "deny"}
    assert all(v["code"] == "AB12" for v in values)
    assert all(v["opensquilla_action"] == "approval_resolve" for v in values)
    assert "exec-1" not in str(rendered["card"])


def test_render_falls_back_to_text_without_interactive_cards() -> None:
    profile = ChannelCapabilityProfile(channel_type="slack", interactive_cards=False)
    rendered = render_approval_prompt(profile, _request())
    assert "card" not in rendered
    assert "text" in rendered
    assert "/approve AB12" in rendered["text"]
    assert "/deny AB12" in rendered["text"]


def test_render_with_none_profile_is_text_only() -> None:
    rendered = render_approval_prompt(None, _request())
    assert set(rendered) == {"text"}


def test_parse_text_commands_are_case_insensitive() -> None:
    assert parse_approval_action("/approve AB12") == ("AB12", "approve")
    assert parse_approval_action("/deny ab12") == ("AB12", "deny")
    assert parse_approval_action("  /APPROVE xy9z  ") == ("XY9Z", "approve")
    assert parse_approval_action("/approve AB12 always") == ("AB12", "always")
    assert parse_approval_action("/approve ab12 ALWAYS") == ("AB12", "always")
    # "always" on a deny is meaningless and parses as a plain deny.
    assert parse_approval_action("/deny AB12 always") == ("AB12", "deny")


def test_parse_rejects_bare_word_and_missing_code() -> None:
    assert parse_approval_action("approve the budget") is None
    assert parse_approval_action("/approve") is None
    assert parse_approval_action("please /deny this") is None
    assert parse_approval_action("/approve AB12 extra") is None


def test_parse_incoming_message_text() -> None:
    msg = IncomingMessage(sender_id="u1", channel_id="c1", content="/deny AB12")
    assert parse_approval_action(msg) == ("AB12", "deny")


def test_parse_card_action_from_metadata() -> None:
    msg = IncomingMessage(
        sender_id="u1",
        channel_id="c1",
        content="/approve AB12",
        metadata={
            "approval_action": {
                "opensquilla_action": "approval_resolve",
                "code": "ab12",
                "decision": "approve",
            }
        },
    )
    assert parse_approval_action(msg) == ("AB12", "approve")


def test_parse_card_action_requires_discriminator() -> None:
    assert (
        parse_approval_action({"value": {"opensquilla_action": "clarify_submit", "code": "AB12"}})
        is None
    )


def test_short_code_binding_round_trip_and_idempotency() -> None:
    code = bind_short_code(
        "exec-1", namespace="exec", session_key="s", owner_sender_id="owner-1"
    )
    assert len(code) == 4
    # Re-binding the same approval reuses the existing code.
    assert (
        bind_short_code("exec-1", namespace="exec", session_key="s", owner_sender_id="owner-1")
        == code
    )
    binding = resolve_short_code(code.lower())  # case-insensitive lookup
    assert binding is not None
    assert binding.approval_id == "exec-1"
    assert binding.owner_sender_id == "owner-1"


def test_unknown_code_resolves_to_none() -> None:
    assert resolve_short_code("ZZZZ") is None


def test_release_short_code_drops_binding() -> None:
    code = bind_short_code("exec-1", namespace="exec", session_key="s", owner_sender_id="o")
    release_short_code("exec-1")
    assert resolve_short_code(code) is None
    # Idempotent.
    release_short_code("exec-1")


def test_parse_strips_leading_bot_mention() -> None:
    # Mention-gated groups REQUIRE addressing the bot, and Slack/Discord keep
    # the raw mention markup in content — the command must still parse.
    assert parse_approval_action("<@U123ABC> /approve AB12") == ("AB12", "approve")
    assert parse_approval_action("<@!987654> /deny ab12") == ("AB12", "deny")
    assert parse_approval_action("<@U08AAA|bot> /approve AB12 always") == ("AB12", "always")
    assert parse_approval_action("  <@U123> /approve AB12") == ("AB12", "approve")
    # A mention alone, or one that is not leading, is not an approval action.
    assert parse_approval_action("<@U123ABC>") is None
    assert parse_approval_action("please <@U123> /deny AB12") is None


def test_prompt_renders_summary_label() -> None:
    request = ApprovalPromptRequest(
        approval_id="exec-1",
        namespace="exec",
        session_key="agent:main:chat",
        command_or_tool="pypi.org",
        agent="main",
        short_code="AB12",
        summary_label="Network host",
    )
    rendered = render_approval_prompt(
        ChannelCapabilityProfile(channel_type="feishu", interactive_cards=True), request
    )
    assert "Network host: pypi.org" in rendered["text"]
    assert "(unknown command)" not in rendered["text"]
    card_body = rendered["card"]["elements"][0]["text"]["content"]
    assert "Network host" in card_body
    assert "pypi.org" in card_body


def test_card_action_value_carries_origin_context() -> None:
    request = ApprovalPromptRequest(
        approval_id="exec-1",
        namespace="exec",
        session_key="agent:main:feishu:group:oc_1:sender:ou_1",
        command_or_tool="rm target.txt",
        agent="main",
        short_code="AB12",
        origin_channel_id="oc_1",
        origin_is_group=True,
        origin_chat_type="group",
        origin_thread_id="t-9",
    )
    rendered = render_approval_prompt(
        ChannelCapabilityProfile(channel_type="feishu", interactive_cards=True), request
    )
    for action in rendered["card"]["elements"][1]["actions"]:
        value = action["value"]
        assert value["channel_id"] == "oc_1"
        assert value["is_group"] is True
        assert value["chat_type"] == "group"
        assert value["thread_id"] == "t-9"


def test_card_action_value_omits_absent_origin_context() -> None:
    rendered = render_approval_prompt(
        ChannelCapabilityProfile(channel_type="feishu", interactive_cards=True),
        _request(),
    )
    for action in rendered["card"]["elements"][1]["actions"]:
        value = action["value"]
        assert "channel_id" not in value
        assert "is_group" not in value
        assert "chat_type" not in value
        assert "thread_id" not in value
