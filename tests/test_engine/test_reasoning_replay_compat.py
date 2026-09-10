"""Legacy records become quoted facts only when the target requires continuity."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.replay_compat import rebase_incomplete_reasoning_history
from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.engine.types import DoneEvent, ThinkingLevel
from opensquilla.provider import (
    ChatConfig,
    ContentBlockText,
    ContentBlockThinking,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    ModelCapabilities,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import ErrorEvent as ProviderError
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolStart
from opensquilla.provider.types import ContentBlockDocument, ContentBlockImage, ProviderReplayState


def _state() -> ProviderReplayState:
    return ProviderReplayState(
        protocol="synthetic",
        source="current-route",
        model="model",
        reasoning_details=[{"type": "reasoning.encrypted", "data": "opaque"}],
    )


def _compatible(message: Message) -> bool:
    return message.provider_replay is not None and message.provider_replay.source == "current-route"


def test_complete_chain_is_returned_without_projection_or_mutation():
    history = [
        Message(role="user", content="question"),
        Message(role="assistant", content="answer", provider_replay=_state()),
    ]
    projected, changed = rebase_incomplete_reasoning_history(history, compatible=_compatible)
    assert not changed and projected is history


def test_completed_legacy_non_tool_round_still_requires_new_request_context():
    legacy = Message(
        role="assistant", content="old completed answer", reasoning_content="aggregate"
    )
    current = Message(role="user", content="new user question")
    history = [Message(role="user", content="old question"), legacy, current]
    before = [message.model_dump() for message in history]
    projected, changed = rebase_incomplete_reasoning_history(history, compatible=_compatible)
    assert changed and len(projected) == 2
    assert projected[0].role == "user" and "old completed answer" in projected[0].content
    assert "aggregate" not in projected[0].content
    assert projected[1] is current
    assert [message.model_dump() for message in history] == before


def test_rebase_consumes_legacy_tool_results_and_keeps_complete_tail_boundaries():
    legacy = Message(
        role="assistant",
        content=[
            ContentBlockThinking(thinking="private thinking", signature="private signature"),
            ContentBlockToolUse(id="old-call", name="record", input={"value": 7}),
        ],
    )
    result = Message(
        role="user",
        content=[
            ContentBlockToolResult(
                tool_use_id="old-call",
                content="already executed",
                is_error=False,
            )
        ],
    )
    complete = Message(
        role="assistant",
        content=[
            ContentBlockToolUse(id="new-call", name="record", input={"value": 8}),
        ],
        reasoning_content="native thinking",
        provider_replay=_state(),
    )
    tail = [
        complete,
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="new-call",
                    content="second result",
                )
            ],
        ),
        Message(role="user", content="continue"),
    ]
    projected, changed = rebase_incomplete_reasoning_history(
        [legacy, result, *tail], compatible=_compatible
    )
    assert changed and projected[1:] == tail
    assert projected[1] is complete
    assert "old-call" in projected[0].content and "already executed" in projected[0].content
    assert "private thinking" not in projected[0].content
    assert "private signature" not in projected[0].content
    assert all(
        not isinstance(block, ContentBlockToolResult)
        for block in projected[0].content
        if not isinstance(projected[0].content, str)
    )


def test_missing_last_record_rebases_prior_native_state_as_facts_not_opaque_data():
    complete = Message(role="assistant", content="known answer", provider_replay=_state())
    incompatible = Message(
        role="assistant",
        content="foreign answer",
        provider_replay=_state().model_copy(
            update={"source": "foreign-route"},
        ),
    )
    projected, changed = rebase_incomplete_reasoning_history(
        [complete, incompatible], compatible=_compatible
    )
    assert changed and len(projected) == 1
    assert "known answer" in projected[0].content and "foreign answer" in projected[0].content
    assert "opaque" not in projected[0].content and "provider_replay" not in projected[0].content


@pytest.mark.parametrize("nested_result", [False, True])
async def test_rebase_keeps_historical_media_typed_and_outside_quoted_json(nested_result):
    image = ContentBlockImage(
        media_type="image/png",
        data="c3ludGhldGljLWltYWdlLWJ5dGVz",
        attachment_id="synthetic-image",
        durable_retained=True,
    )
    document = ContentBlockDocument(
        media_type="application/pdf", data="JVBERi1zeW50aGV0aWM=", title="Synthetic document"
    )
    legacy = Message(role="assistant", content="legacy answer")
    if nested_result:
        legacy = Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="read-old",
                    name="read_media",
                    input={},
                )
            ],
        )
        history = [
            legacy,
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(
                        tool_use_id="read-old",
                        content=[image, document.model_dump()],
                    )
                ],
            ),
        ]
    else:
        history = [Message(role="user", content=[image, document]), legacy]
    before = [message.model_dump() for message in history]
    current = Message(role="user", content="continue with the historical attachment")
    projected, changed = rebase_incomplete_reasoning_history(
        [*history, current], compatible=_compatible
    )
    assert changed and projected[1] is current
    blocks = projected[0].content
    assert isinstance(blocks, list)
    text = next(block.text for block in blocks if isinstance(block, ContentBlockText))
    assert image.data not in text and document.data not in text
    assert "historical_media_1" in text and "historical_media_2" in text
    assert (
        next(block for block in blocks if isinstance(block, ContentBlockImage)).data == image.data
    )
    assert (
        next(block for block in blocks if isinstance(block, ContentBlockDocument)).data
        == document.data
    )
    assert [message.model_dump() for message in history] == before
    if not nested_result:
        restored = next(block for block in blocks if isinstance(block, ContentBlockImage))
        assert restored.attachment_id == "synthetic-image"
        assert restored.durable_retained is True


@pytest.mark.parametrize("vision_supported", [False, True])
def test_selector_projects_rebased_image_for_actual_route_without_rewriting_context(
    vision_supported,
):
    image = ContentBlockImage(
        media_type="image/png",
        data="c3ludGhldGljLWltYWdlLWJ5dGVz",
        attachment_id="synthetic-image",
        durable_retained=True,
    )
    messages, changed = rebase_incomplete_reasoning_history(
        [
            Message(role="user", content=[image]),
            Message(role="assistant", content="legacy answer"),
            Message(role="user", content="continue"),
        ],
        compatible=_compatible,
    )
    assert changed
    selector = SimpleNamespace(
        current_config=SimpleNamespace(
            provider="synthetic",
            model="synthetic-model",
            base_url="https://example.invalid",
            api_key="",
        )
    )
    metadata = {}
    wrapper = _SelectorFallbackProvider(
        SimpleNamespace(provider_name="synthetic"), selector, metadata
    )
    projected = wrapper._project_image_messages_for_active_leg(
        messages,
        ChatConfig(
            model_capabilities=ModelCapabilities(supports_vision=vision_supported),
            model_vision_support="supported" if vision_supported else "unsupported",
        ),
        stage="primary",
    )
    image_blocks = [
        block
        for message in projected
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockImage)
    ]
    assert bool(image_blocks) is vision_supported
    text = "\n".join(
        block.text
        for message in projected
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockText)
    )
    assert image.data not in text
    assert "legacy answer" in text
    assert metadata["image_input_mode"] == ("native" if vision_supported else "marker")
    if not vision_supported:
        assert "图片未分析" in text
        assert metadata["image_input_reason"] == "model_vision_unsupported"
    assert any(
        isinstance(block, ContentBlockImage) and block.data == image.data
        for block in messages[0].content
    )


def test_selector_forwarder_uses_the_current_provider_replay_policy():
    provider = SimpleNamespace(
        requires_complete_reasoning_history=lambda **kwargs: bool(
            kwargs["tools"] and kwargs["thinking"]
        ),
        can_replay_reasoning=_compatible,
    )
    wrapper = _SelectorFallbackProvider(provider, SimpleNamespace())
    message = Message(role="assistant", content="answer", provider_replay=_state())
    assert wrapper.requires_complete_reasoning_history(tools=True, thinking=True)
    assert not wrapper.requires_complete_reasoning_history(tools=[], thinking=True)
    assert wrapper.can_replay_reasoning(message)
    wrapper._provider = SimpleNamespace()
    assert not wrapper.requires_complete_reasoning_history(tools=True, thinking=True)
    assert not wrapper.can_replay_reasoning(message)


@pytest.mark.parametrize("fallback_model", ["origin", "foreign"])
async def test_agent_selector_preserves_canonical_replay_for_each_fallback_leg(fallback_model):
    class Provider:
        provider_name = "synthetic"

        def __init__(self, model, *, fail=False):
            self.model = model
            self.fail = fail
            self.calls = []

        def requires_complete_reasoning_history(self, *, tools, thinking):
            return bool(tools and thinking)

        def can_replay_reasoning(self, message):
            return (
                message.provider_replay is not None
                and message.provider_replay.model == self.model
            )

        async def chat(self, messages, tools=None, config=None):
            self.calls.append([message.model_copy(deep=True) for message in messages])
            if self.fail:
                yield ProviderError(message="synthetic unavailable", code="503")
            else:
                yield ProviderText(text="new answer")
                yield ProviderDone(stop_reason="stop", model=self.model)

    primary = Provider("primary", fail=True)
    fallback = Provider(fallback_model)

    class Selector:
        def __init__(self):
            self.chain = [
                SimpleNamespace(provider="synthetic", model=provider.model)
                for provider in (primary, fallback)
            ]
            self.current_config = self.chain[0]

        def remaining_chain(self):
            return list(self.chain)

        def next_fallback_after_failure(self, exc):
            self.chain = self.chain[1:]
            self.current_config = self.chain[0]
            return fallback

    wrapper = _SelectorFallbackProvider(primary, Selector())
    state = _state().model_copy(update={"model": "origin"})
    old = Message(role="assistant", content="old answer", provider_replay=state)
    agent = Agent(
        provider=wrapper,
        config=AgentConfig(
            model_id="primary",
            thinking=ThinkingLevel.LOW,
            max_iterations=1,
            max_provider_retries=0,
            model_capabilities=ModelCapabilities(supports_reasoning=True, supports_tools=True),
        ),
        tool_definitions=[
            ToolDefinition(
                name="record",
                description="pure synthetic tool",
                input_schema=ToolInputSchema(properties={}),
            )
        ],
    )
    agent.set_history([Message(role="user", content="old question"), old])

    events = [event async for event in agent.run_turn("new question")]

    assert not any(event.kind == "error" for event in events)
    assert len(primary.calls) == len(fallback.calls) == 1
    assert not any(message.role == "assistant" for message in primary.calls[0])
    assert "old answer" in json.dumps([message.model_dump() for message in primary.calls[0]])
    fallback_assistants = [
        message for message in fallback.calls[0] if message.role == "assistant"
    ]
    if fallback_model == "origin":
        assert fallback_assistants == [old]
        assert fallback_assistants[0].provider_replay == state
    else:
        assert not fallback_assistants
        assert "old answer" in json.dumps(
            [message.model_dump() for message in fallback.calls[0]]
        )
    assert old in agent.history_snapshot()


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [True, False])
async def test_agent_legacy_rebase_is_request_local_and_new_tool_calls_remain_native(required):
    class Provider:
        provider_name = "synthetic"

        def __init__(self):
            self.calls = []

        def requires_complete_reasoning_history(self, *, tools, thinking):
            return required and bool(tools and thinking)

        def can_replay_reasoning(self, message):
            return _compatible(message)

        async def chat(self, messages, tools=None, config=None):
            self.calls.append([message.model_copy(deep=True) for message in messages])
            if len(self.calls) == 1:
                yield ProviderToolStart(tool_use_id="new-call", tool_name="record")
                yield ProviderToolEnd(tool_use_id="new-call", tool_name="record", arguments={})
                yield ProviderDone(
                    stop_reason="tool_use",
                    provider_replay=_state(),
                    reasoning_content="tool thinking",
                )
            else:
                yield ProviderText(text="new answer")
                yield ProviderDone(
                    stop_reason="stop", provider_replay=_state(), reasoning_content="final thinking"
                )

    provider = Provider()
    executed = []

    async def handler(call):
        executed.append(call.tool_use_id)
        return ToolResult(
            tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="recorded"
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.LOW,
            max_iterations=2,
            max_provider_retries=0,
            model_capabilities=ModelCapabilities(supports_reasoning=True, supports_tools=True),
        ),
        tool_definitions=[
            ToolDefinition(
                name="record",
                description="pure synthetic tool",
                input_schema=ToolInputSchema(properties={}),
            )
        ],
        tool_handler=handler,
    )
    old = Message(role="assistant", content="old completed answer")
    agent.set_history([Message(role="user", content="old question"), old])
    events = [event async for event in agent.run_turn("new question")]
    assert not any(event.kind == "error" for event in events)
    assert executed == ["new-call"]
    warnings = [
        event
        for event in events
        if event.kind == "warning" and event.code == "reasoning_replay_context_rebuilt"
    ]
    assert len(warnings) == int(required)
    first_assistants = [message for message in provider.calls[0] if message.role == "assistant"]
    assert len(first_assistants) == int(not required)
    current = [
        message
        for message in provider.calls[1]
        if message.role == "assistant" and message.provider_replay is not None
    ]
    assert len(current) == 1 and current[0].provider_replay == _state()
    assert any(
        message.role == "assistant" and message.content == "old completed answer"
        for message in agent.history_snapshot()
    )
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert "old completed answer" not in json.dumps(done.assistant_replay)
    assert all(
        isinstance(block, ContentBlockText)
        for message in provider.calls[0]
        if isinstance(message.content, list)
        for block in message.content
    )
