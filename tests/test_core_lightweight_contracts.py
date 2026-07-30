from __future__ import annotations

import json

from opensquilla.engine.session_sanitize import sanitize_session_messages
from opensquilla.engine.tool_text_compat import (
    ProtocolTextLeakGuard,
    strip_protocol_text_leak,
    strip_synthetic_tool_call_suffix,
)
from opensquilla.provider.openai import _build_openai_messages, _usage_fields
from opensquilla.provider.openrouter_attribution import openrouter_app_headers
from opensquilla.provider.types import ContentBlockText, ContentBlockToolResult, Message
from opensquilla.session.keys import (
    allows_private_memory_prompt_injection,
    build_subagent_session_key,
    build_webchat_key,
    canonicalize_session_key,
    is_subagent_key,
    normalize_account_id,
    normalize_agent_id,
)


def test_session_keys_keep_default_agent_canonical() -> None:
    assert normalize_agent_id(None) == "main"
    assert normalize_agent_id("default") == "main"
    assert build_webchat_key("default") == "agent:main:webchat:default"
    assert canonicalize_session_key("webchat:default") == "agent:main:webchat:default"


def test_session_keys_defend_account_id_segments() -> None:
    assert normalize_account_id("__proto__") == "default"
    assert normalize_account_id(" Team Alpha / Prod ") == "team-alpha---prod"


def test_subagent_keys_are_canonical_and_detected() -> None:
    key = build_subagent_session_key("Ops Agent", "Run 123!!")

    assert key == "agent:ops-agent:subagent:run-123"
    assert is_subagent_key(key)
    assert is_subagent_key("subagent:agent:main:webchat:default")


def test_private_memory_prompt_injection_is_denied_for_shared_or_runtime_keys() -> None:
    assert allows_private_memory_prompt_injection("agent:main:webchat:default") is True
    assert allows_private_memory_prompt_injection("agent:main:slack:group:g1") is False
    assert allows_private_memory_prompt_injection("agent:main:subagent:run-1") is False
    assert allows_private_memory_prompt_injection("cron:dream:run:1") is False


def test_text_encoded_tool_call_suffix_is_removed_without_losing_prose() -> None:
    text = 'Here is the answer.\n\nweb_search{"query": "opensquilla"}'

    assert strip_synthetic_tool_call_suffix(text, ["web_search"]) == "Here is the answer."


def test_minimax_tool_call_text_is_removed_as_machine_payload() -> None:
    text = "<minimax:tool_call>{}</minimax:tool_call>"

    assert strip_synthetic_tool_call_suffix(text, ["web_search"]) == ""


def test_deprecated_protocol_strip_preserves_protocol_like_html() -> None:
    text = (
        "Let me write the dashboard now.\n\n"
        '<tvoe_calls><invoke name="write_file">'
        '<parameter name="path">index.html</parameter>'
        '<parameter name="content"><!DOCTYPE html><html><body>app</body></html>'
        "</parameter></invoke></tvoe_calls>"
    )

    assert strip_protocol_text_leak(text) == text


def test_deprecated_protocol_strip_preserves_dsml_text() -> None:
    text = (
        "Let me create the printable daily record sheet as well:\n\n"
        '<｜DSML｜tool_calls><｜DSML｜invoke name="create_xlsx">'
        '<｜DSML｜parameter name="name" string="true">'
        "bean-sprout-daily-record-sheet.xlsx"
        "</｜DSML｜parameter>"
        '<｜DSML｜parameter name="sheets" string="false">'
        '[{"name":"Record Sheet","rows":[["Day","Height"]]}]'
        "</｜DSML｜parameter></｜DSML｜invoke></｜DSML｜tool_calls>"
    )

    assert strip_protocol_text_leak(text) == text


def test_deprecated_protocol_strip_preserves_details_text() -> None:
    text = (
        "Let me read the specific problematic areas to fix them.\n\n"
        "<details>"
        "<summary>View areas around line 10393, 14751, and nearby</summary>\n\n"
        "<parameter>\n\n"
        "I see two real HTML issues."
    )

    assert strip_protocol_text_leak(text) == text


def test_deprecated_streaming_guard_passes_split_tool_protocol_without_buffering() -> None:
    guard = ProtocolTextLeakGuard()

    first = "Let me write the dashboard now.\n\n<tvoe"
    second = (
        '_calls><invoke name="write_file">'
        '<parameter name="content"><!DOCTYPE html><html></html>'
    )
    assert guard.push(first) == first
    assert guard.push(second) == second
    assert guard.flush() == ""


def test_deprecated_streaming_guard_passes_split_dsml_without_buffering() -> None:
    guard = ProtocolTextLeakGuard()

    first = "Let me make the sheet.\n\n<｜DS"
    second = (
        'ML｜tool_calls><｜DSML｜invoke name="create_xlsx">'
        '<｜DSML｜parameter name="sheets">[]</｜DSML｜parameter>'
    )
    assert guard.push(first) == first
    assert guard.push(second) == second
    assert guard.flush() == ""


def test_deprecated_streaming_guard_passes_partial_marker_immediately() -> None:
    guard = ProtocolTextLeakGuard()

    assert guard.push("Explain literal <invoke") == "Explain literal <invoke"
    assert guard.flush() == ""


def test_deprecated_streaming_guard_does_not_change_behavior_at_tool_boundary() -> None:
    guard = ProtocolTextLeakGuard()

    first = "Let me read the specific problematic areas.\n\n<details>"
    second = (
        "<summary>View areas around line 10393, 14751, and nearby</summary>"
    )
    assert guard.push(first) == first
    assert guard.push(second) == second
    assert guard.flush_before_tool_use() == ""
    assert guard.push("Fixed the issues.") == "Fixed the issues."


def test_deprecated_streaming_guard_passes_regular_details_without_buffering() -> None:
    guard = ProtocolTextLeakGuard()

    first = "Here is a collapsible note.\n\n<details>"
    second = "<summary>More</summary>Visible note.</details>"
    assert guard.push(first) == first
    assert guard.push(second) == second
    assert guard.flush() == ""


def test_session_sanitize_removes_block_metadata_without_mutating_original_content() -> None:
    message = Message.model_construct(
        role="assistant",
        content=[
            {
                "type": "text",
                "text": "visible answer",
                "debug": {"provider": "test"},
            }
        ],
        reasoning_content=None,
    )

    sanitized, result = sanitize_session_messages([message])

    assert result.metadata_keys_removed == 1
    assert isinstance(sanitized[0].content[0], ContentBlockText)
    assert sanitized[0].content[0].text == "visible answer"
    assert "debug" in message.content[0]


def test_openai_payload_splits_tool_results_into_provider_tool_messages() -> None:
    message = Message(
        role="user",
        content=[
            ContentBlockToolResult(
                tool_use_id="call_1",
                content=json.dumps({"ok": True}),
                is_error=False,
            )
        ],
    )

    assert _build_openai_messages(message) == [
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": '{"ok": true}',
        }
    ]


def test_usage_fields_treat_explicit_canonical_zero_as_real_value() -> None:
    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "cache_creation_input_tokens": 0,
        "prompt_cache_miss_tokens": 999,
    }

    *_, cache_write_tokens, _ = _usage_fields(usage)

    assert cache_write_tokens == 0


def test_openrouter_attribution_headers_are_scoped_to_openrouter_hosts() -> None:
    assert openrouter_app_headers("https://api.openrouter.ai/v1")[
        "X-Title"
    ] == "OpenSquilla"
    assert openrouter_app_headers("https://api.openai.com/v1") == {}
