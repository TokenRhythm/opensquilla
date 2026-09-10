"""Synthetic accepted messages survive durable replay without turn aggregation."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from opensquilla.engine.history import (
    AssistantReplayError,
    HistoryReplayEntryProjection,
    decode_assistant_replay,
    limit_turns,
    project_history_replay,
    project_history_replay_capacity,
    reconstruct_messages_from_entry,
    repair_tool_pairing,
    strip_historical_tool_pairs,
)
from opensquilla.provider.types import (
    ContentBlockText,
    ContentBlockThinking,
    Message,
    ProviderReplayState,
)


def _envelope() -> dict:
    records = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "call-1", "name": "lookup", "input": {}}],
            "reasoning_content": "first reasoning",
            "provider_replay": {
                "protocol": "openai_chat_completions",
                "source": "synthetic-route",
                "model": "synthetic-model",
                "reasoning_details": [
                    {"type": "reasoning.text", "text": "first reasoning", "index": 0},
                    {"type": "reasoning.encrypted", "data": "dummy-signature", "index": 1},
                ],
            },
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "42"}],
        },
        {"role": "user", "content": "Use the confirmed result."},
        {"role": "assistant", "content": "42", "reasoning_content": "second reasoning"},
    ]
    return {"version": 1, "messages": [Message.model_validate(m).model_dump() for m in records]}


def test_replay_restores_per_call_native_state_instead_of_display_aggregates():
    envelope = _envelope()
    messages = reconstruct_messages_from_entry(
        "assistant",
        "aggregate display",
        [{"type": "text", "text": "flattened"}],
        "combined reasoning",
        assistant_replay=envelope,
    )
    assert [m.model_dump() for m in messages] == envelope["messages"]
    assert [m.reasoning_content for m in messages] == [
        "first reasoning",
        None,
        None,
        "second reasoning",
    ]
    assert repair_tool_pairing(messages) == messages
    messages[0].provider_replay.reasoning_details[1]["data"] = "changed"
    assert envelope == _envelope()


@pytest.mark.parametrize(
    "value",
    [
        {"version": 2, "messages": []},
        {"version": True, "messages": []},
        {"version": 1, "messages": {}},
        {"version": 1, "messages": [{"role": "system", "content": "secret-dummy"}]},
        {"version": 1, "messages": [{"role": "user", "content": "secret-dummy"}]},
        {"version": 1, "messages": [{"role": "assistant", "content": 42}]},
        {"version": 1, "messages": [{"role": "assistant", "content": "", "future": {}}]},
    ],
)
def test_invalid_replay_never_falls_back_to_aggregate_or_logs_payload(value):
    with pytest.raises(AssistantReplayError) as exc:
        reconstruct_messages_from_entry("assistant", "aggregate", None, assistant_replay=value)
    assert "secret-dummy" not in str(exc.value)


def test_history_capacity_counts_native_reasoning_state():
    envelope = _envelope()
    larger = deepcopy(envelope)
    larger["messages"][0]["provider_replay"]["reasoning_details"][1]["data"] = "opaque " * 4000

    def capacity(value):
        projection = project_history_replay(
            [HistoryReplayEntryProjection(role="assistant", content="42", assistant_replay=value)],
            entry_projector=lambda entry, _index: entry,
        )
        assert [m.model_dump() for m in projection.messages] == value["messages"]
        return project_history_replay_capacity(projection)

    small = capacity(envelope)
    large = capacity(larger)
    assert large.estimated_tokens > small.estimated_tokens


def test_empty_replay_is_distinct_from_legacy_unknown():
    assert decode_assistant_replay({"version": 1, "messages": []}) == []
    legacy = reconstruct_messages_from_entry("assistant", "old answer", None, "old aggregate")
    assert legacy[0].reasoning_content == "old aggregate"


@pytest.mark.parametrize("target", ["envelope", "native_state", "content_block"])
def test_unknown_replay_fields_cannot_be_silently_discarded(target):
    envelope = _envelope()
    value = {
        "envelope": envelope,
        "native_state": envelope["messages"][0]["provider_replay"],
        "content_block": envelope["messages"][0]["content"][0],
    }[target]
    value["future_required_field"] = "private-dummy"
    with pytest.raises(AssistantReplayError) as exc:
        decode_assistant_replay(envelope)
    assert "private-dummy" not in str(exc.value)


def test_emergency_compaction_preserves_replay_and_native_budget():
    from opensquilla.engine.runtime import TurnRunner
    from opensquilla.session.compaction import estimate_entry_model_replay_tokens

    envelope = _envelope()
    entry = SimpleNamespace(role="assistant", content="display", assistant_replay=envelope)
    payload = TurnRunner._entry_for_emergency_compaction(entry)
    restored = TurnRunner._emergency_replay_entry(payload)
    assert restored.assistant_replay == envelope
    assert estimate_entry_model_replay_tokens(restored) == estimate_entry_model_replay_tokens(entry)
    messages = reconstruct_messages_from_entry(
        restored.role,
        restored.content,
        restored.tool_calls,
        assistant_replay=restored.assistant_replay,
    )
    assert [message.model_dump() for message in messages] == envelope["messages"]
    payload["assistant_replay"]["messages"].clear()
    assert restored.assistant_replay == envelope


@pytest.mark.parametrize("unpacked", [False, True])
def test_artifact_history_supplements_native_replay_without_changing_accepted_messages(unpacked):
    from opensquilla.session.compaction import (
        estimate_entry_model_replay_chars,
        estimate_entry_model_replay_tokens,
    )

    envelope = _envelope()
    marker = "[generated artifact omitted: synthetic.txt (text/plain)]"
    content = (
        "display aggregate\n" + marker
        if unpacked
        else json.dumps(
            {
                "text": "display aggregate",
                "artifacts": [
                    {"id": "synthetic-artifact", "name": "synthetic.txt", "mime": "text/plain"}
                ],
            }
        )
    )
    messages = reconstruct_messages_from_entry(
        "assistant",
        content,
        None,
        "display reasoning",
        assistant_replay=envelope,
    )
    assert [message.model_dump() for message in messages[:-1]] == envelope["messages"]
    notice = messages[-1]
    assert notice.role == "user" and marker in notice.content
    assert "display aggregate" not in notice.content
    assert notice.reasoning_content is None and notice.provider_replay is None
    assert decode_assistant_replay(envelope) == messages[:-1]
    # This application context belongs to the preceding turn; it cannot take
    # that turn's place when the history window keeps only one user request.
    original_request = Message(role="user", content="synthetic request")
    simple_turn = [original_request, messages[0], messages[1], messages[3], notice]
    assert limit_turns(simple_turn, 1) == simple_turn

    entry = SimpleNamespace(role="assistant", content=content, assistant_replay=envelope)
    without_artifact = SimpleNamespace(
        role="assistant", content="display", assistant_replay=envelope
    )
    assert estimate_entry_model_replay_tokens(entry) > estimate_entry_model_replay_tokens(
        without_artifact
    )
    assert estimate_entry_model_replay_chars(entry) > estimate_entry_model_replay_chars(
        without_artifact
    )


@pytest.mark.parametrize("native_kind", ["unsigned", "provider_replay", "thinking_signature"])
@pytest.mark.parametrize("has_turn_context", [False, True])
def test_silent_replay_projection_only_changes_unsigned_history(native_kind, has_turn_context):
    content = [ContentBlockText(text="NO_"), ContentBlockText(text="REPLY\nVisible status.")]
    if native_kind == "thinking_signature":
        content.insert(0, ContentBlockThinking(
            thinking="captured reasoning", signature="dummy-sign",
        ))
    message = Message(
        role="assistant", content=content, reasoning_content="captured reasoning",
        provider_replay=(
            ProviderReplayState(
                protocol="openai_chat_completions", source="synthetic-silent-origin",
                model="test/model", reasoning_details=[
                    {"type": "reasoning.encrypted", "data": "dummy-state"}
                ],
            )
            if native_kind == "provider_replay" else None
        ),
    )
    envelope = {"version": 1, "messages": [message.model_dump(mode="json")]}
    before = deepcopy(envelope)
    replay = reconstruct_messages_from_entry(
        "assistant", "Visible status.", None, assistant_replay=envelope,
        turn_context=(
            {"run_kind": "goal", "input_mode": "system_event"} if has_turn_context else None
        ),
    )
    assert envelope == before
    assert replay[0].reasoning_content == "captured reasoning"
    if native_kind == "unsigned":
        assert "".join(block.text for block in replay[0].content) == "Visible status."
    else:
        # Native content may be covered by opaque state/signatures. Keep the
        # accepted call exact instead of applying presentation cleanup to it.
        assert replay == [message]


@pytest.mark.parametrize("display", ["NO_REPLY\nHuman-visible literal text.", "Different display."])
def test_unsigned_replay_does_not_guess_suppression_without_canonical_match(display):
    message = Message(role="assistant", content="NO_REPLY\nHuman-visible literal text.")
    envelope = {"version": 1, "messages": [message.model_dump(mode="json")]}
    assert reconstruct_messages_from_entry(
        "assistant", display, None, assistant_replay=envelope,
    ) == [message]


@pytest.mark.parametrize("visible_text", [False, True])
def test_restricted_tool_history_cannot_reintroduce_anthropic_native_blocks(visible_text):
    from opensquilla.provider.anthropic import AnthropicProvider
    from opensquilla.provider.types import ChatConfig

    provider = AnthropicProvider(
        api_key="synthetic-key", model="claude-sonnet-4-6",
        base_url="https://synthetic-anthropic.invalid",
    )
    native = [
        {
            "type": "thinking", "thinking": "synthetic private tool reasoning",
            "signature": "synthetic-private-signature",
        },
        {"type": "redacted_thinking", "data": "synthetic-private-opaque"},
        {
            "type": "tool_use", "id": "synthetic-lookup", "name": "lookup",
            "input": {"path": "/synthetic/private/file.txt"},
        },
    ]
    if visible_text:
        native.insert(2, {"type": "text", "text": "Synthetic public answer."})
    message = Message.model_validate({
        "role": "assistant", "content": native,
        "reasoning_content": "synthetic private tool reasoning",
        "provider_replay": {
            "protocol": "anthropic_messages", "source": provider._replay_source,
            "model": provider.model, "native_content": native,
        },
    })
    result = Message.model_validate({
        "role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "synthetic-lookup",
            "content": "synthetic private tool result",
        }],
    })
    envelope = {"version": 1, "messages": [message.model_dump(), result.model_dump()]}
    original = deepcopy(envelope)
    restored = decode_assistant_replay(envelope)
    control, _ = provider._build_payload(restored, None, ChatConfig(), record_diagnostics=False)
    assert control["messages"][0]["content"] == native
    projected, stats = strip_historical_tool_pairs(restored)
    assert stats.tool_uses_removed == stats.tool_results_removed == 1
    assert envelope == original
    assert [entry.model_dump() for entry in restored] == original["messages"]
    if visible_text:
        assert projected == [Message(
            role="assistant", content=[ContentBlockText(text="Synthetic public answer.")],
        )]
    else:
        assert projected == []
    payload, _ = provider._build_payload(projected, None, ChatConfig(), record_diagnostics=False)
    wire = json.dumps(payload)
    assert "synthetic-private" not in wire
    assert "synthetic private" not in wire
    assert "/synthetic/private" not in wire
    assert "thinking" not in wire


@pytest.mark.parametrize("captured_value,accepted_value,mutation,expected", [
    (3, 3, None, True),
    (3.0, 3.0, None, True),
    (True, True, None, True),
    (3, 3.0, None, False),
    (1, True, None, False),
    (3, 3, "remove_tool", False),
    (3, 3, "change_text", False),
])
def test_native_assistant_content_requires_unchanged_accepted_response(
    captured_value, accepted_value, mutation, expected,
):
    from opensquilla.engine.agent import _native_assistant_content
    from opensquilla.engine.types import ToolCall

    native = [
        {
            "type": "thinking", "thinking": "Synthetic thought.",
            "signature": "synthetic-signature",
        },
        {"type": "text", "text": "Synthetic answer."},
        {
            "type": "tool_use", "id": "synthetic-lookup", "name": "lookup",
            "input": {"nested": {"value": captured_value}},
        },
    ]
    state = ProviderReplayState(
        protocol="anthropic_messages", source="synthetic-source", model="synthetic-model",
        native_content=deepcopy(native),
    )
    original = state.model_dump_json()
    calls = [] if mutation == "remove_tool" else [ToolCall(
        tool_use_id="synthetic-lookup", tool_name="lookup",
        arguments={"nested": {"value": accepted_value}},
    )]
    content = _native_assistant_content(
        state,
        response_text="Changed answer." if mutation == "change_text" else "Synthetic answer.",
        tool_calls=calls,
    )
    if expected:
        assert content is not None
        assert json.dumps([block.model_dump() for block in content]) == json.dumps(native)
    else:
        assert content is None
    assert state.model_dump_json() == original
