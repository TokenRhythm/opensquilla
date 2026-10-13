"""Window recovery must retain live state in supported tool-result text blocks."""

import json

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.request_window import compact_request_window_tools
from opensquilla.provider import (
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    OpenAIProvider,
)
from opensquilla.provider.anthropic import AnthropicProvider
from opensquilla.provider.ollama import OllamaProvider
from opensquilla.provider.protocol import project_provider_final_request
from opensquilla.provider.request_proof import (
    _tool_result_content_is_unresolved,
    protected_tool_result_indexes,
)
from opensquilla.provider.types import ChatConfig
from opensquilla.session.compaction import _api_round_requires_raw, _tool_result_payload_is_live


@pytest.mark.parametrize("status", ["running", "pending", "completed", "error"])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("form", ["string", "dict_text", "native_text"])
def test_live_tool_payload_survives_core_and_agent_window_recovery(status, nested, form):
    payload = {"status": status}
    if nested:
        payload = {"execution_status": payload}
    text = json.dumps(payload)
    content = text
    if form == "dict_text":
        content = [{"type": "text", "text": text}]
    elif form == "native_text":
        content = [ContentBlockText(text=text)]
    messages = [
        Message(role="user", content="current exact request"),
        Message(role="assistant", content=[ContentBlockToolUse(
            id="call-a", name="lookup", input={},
        )]),
        Message(role="user", content=[ContentBlockToolResult(
            tool_use_id="call-a", content=content,
        )]),
    ]
    before = [message.model_copy(deep=True) for message in messages]
    live = status in {"running", "pending"}
    agent = Agent(
        provider=OpenAIProvider(api_key="unused-synthetic", model="synthetic"),
        config=AgentConfig(model_id="synthetic", context_window_tokens=16000, max_tokens=1024),
    )
    agent._current_turn_message = "current exact request"

    entries = agent._message_count_compaction_entries(messages[1:])
    assert _api_round_requires_raw(entries) is live
    assert _tool_result_payload_is_live(content) is live
    assert _tool_result_content_is_unresolved(content) is live
    assert agent._live_turn_compaction_boundary(messages, protected_turn_start_index=0) == (
        None if live else (0, len(messages))
    )
    outcome = agent._recover_local_request_window(
        messages, protected_turn_start_index=0,
        request_context_insert_index=0, runtime_context_insert_index=0,
    )
    if live:
        assert outcome is None
    else:
        assert outcome is not None
        assert outcome.removed_count == 2
        assert outcome.messages[0] is messages[0]
        assert "Tool execution receipts" in str(outcome.messages[1].content)
    assert messages == before


@pytest.mark.parametrize("content", [
    'The tool documentation contains {"status":"running"}.',
    '{"metadata":{"status":"running"}}',
    [{"type": "text", "text": '{"metadata":{"status":"pending"}}'}],
    [{"type": "text", "text": 'Example status: {"status":"running"}'}],
    [{"type": "image", "text": '{"status":"running"}'}],
    [{"text": '{"status":"running"}'}],
    [{"metadata": {"type": "text", "text": '{"status":"running"}'}}],
    ' [{"type":"text","text":"{\\"status\\":\\"running\\"}"}]',
])
def test_live_tool_payload_does_not_search_arbitrary_nested_metadata(content):
    assert not _tool_result_payload_is_live(content)
    assert not _tool_result_content_is_unresolved(content)


@pytest.mark.parametrize("form", ["dict_text", "native_text"])
@pytest.mark.parametrize("status", ["pending", "running"])
@pytest.mark.parametrize("provider_kind", ["openai", "anthropic", "ollama"])
def test_live_text_tool_body_stays_raw_through_pruning_and_serialization(
    monkeypatch, form, status, provider_kind,
):
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_UNRESOLVED_RESULTS", "1")
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_RECENT_RESULTS", "0")
    live_output = "LIVE-" + "x" * 3000
    live_text = json.dumps({"execution_status": {"status": status}, "output": live_output})
    content = (
        [{"type": "text", "text": live_text}]
        if form == "dict_text" else [ContentBlockText(text=live_text)]
    )
    messages = [
        Message(role="user", content="current exact request"),
        Message(role="assistant", content=[ContentBlockToolUse(
            id=f"call-{i}", name="lookup", input={},
        ) for i in range(4)]),
        Message(role="user", content=[ContentBlockToolResult(
            tool_use_id="call-0", content=content,
        )]),
        *[Message(role="user", content=[ContentBlockToolResult(
            tool_use_id=f"call-{i}", content="DONE-" + str(i) * 3000,
        )]) for i in range(1, 4)],
    ]
    before = [message.model_copy(deep=True) for message in messages]
    assert protected_tool_result_indexes(messages) == frozenset({0})
    pruned = compact_request_window_tools(messages)
    assert pruned[2] is messages[2]
    assert pruned[3] != messages[3]

    provider = {
        "openai": OpenAIProvider(api_key="unused-synthetic", model="synthetic"),
        "anthropic": AnthropicProvider(api_key="unused-synthetic", model="synthetic"),
        "ollama": OllamaProvider(model="synthetic"),
    }[provider_kind]
    # Nested tool-content blocks use their JSON representation at the adapter
    # boundary. Round-trip both forms exactly as persisted message replay does.
    replayed = [Message.model_validate(message.model_dump(mode="json")) for message in pruned]
    projection = project_provider_final_request(
        provider, replayed, config=ChatConfig(provider_request_max_chars=8500),
    )
    assert projection is not None
    assert projection.fits
    assert projection.proof["protected_tool_result_count"] == 1
    serialized = json.dumps(projection.payload)
    assert live_output in serialized
    assert "DONE-" + "1" * 3000 not in serialized
    assert messages == before
