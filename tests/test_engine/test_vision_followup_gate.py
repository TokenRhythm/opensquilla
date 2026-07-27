from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.steps.vision_followup_gate import apply_vision_followup_gate
from opensquilla.engine.usage_accounting import (
    UsageCallResult,
    UsageCallStart,
    UsageExecutionContext,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider.types import (
    ChatConfig,
    DoneEvent,
    Message,
    ModelInfo,
    ProviderRequestCorrelation,
    StreamEvent,
    TextDeltaEvent,
    ToolDefinition,
)


class _FailProvider:
    provider_name = "fail"

    def chat(
        self,
        messages: list[Message],  # noqa: ARG002
        tools: list[ToolDefinition] | None = None,  # noqa: ARG002
        config: ChatConfig | None = None,  # noqa: ARG002
    ) -> AsyncIterator[StreamEvent]:
        raise AssertionError("gate should not call provider")

    async def list_models(self) -> list[ModelInfo]:
        return []


class _JsonProvider:
    provider_name = "json"

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append({"messages": messages, "tools": tools, "config": config})
        yield TextDeltaEvent(text=self.payload)
        yield DoneEvent()

    async def list_models(self) -> list[ModelInfo]:
        return []


class _RaisingProvider:
    provider_name = "raising"

    async def chat(
        self,
        messages: list[Message],  # noqa: ARG002
        tools: list[ToolDefinition] | None = None,  # noqa: ARG002
        config: ChatConfig | None = None,  # noqa: ARG002
    ) -> AsyncIterator[StreamEvent]:
        raise RuntimeError("provider echoed private detail from local image")
        yield TextDeltaEvent(text="unreachable")

    async def list_models(self) -> list[ModelInfo]:
        return []


class _ReasoningOnlyProvider:
    provider_name = "reasoning-only"

    def __init__(self, payload: str) -> None:
        self.payload = payload

    async def chat(
        self,
        messages: list[Message],  # noqa: ARG002
        tools: list[ToolDefinition] | None = None,  # noqa: ARG002
        config: ChatConfig | None = None,  # noqa: ARG002
    ) -> AsyncIterator[StreamEvent]:
        yield DoneEvent(reasoning_content=self.payload)

    async def list_models(self) -> list[ModelInfo]:
        return []


class _RecordingGateChat:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append({"messages": messages, "tools": tools, "config": config})
        yield TextDeltaEvent(text=self.payload)
        yield DoneEvent()


class _RecordingSelector:
    def __init__(self) -> None:
        self.clones: list[_RecordingSelector] = []
        self.model: str | None = None

    def clone(self) -> _RecordingSelector:
        child = _RecordingSelector()
        self.clones.append(child)
        return child

    def override_model(self, model: str) -> None:
        self.model = model

    def resolve(self) -> _JsonProvider:
        return _JsonProvider(
            '{"decision":"text_only","confidence":0.88,"reason":"selector gate"}'
        )


class _UsageSink:
    def __init__(self) -> None:
        self.started: list[UsageCallStart] = []
        self.finalized: list[tuple[UsageCallStart, UsageCallResult]] = []
        self.unknown: list[tuple[UsageCallStart, str]] = []

    async def start(self, call: UsageCallStart) -> None:
        self.started.append(call)

    async def finalize(self, call: UsageCallStart, result: UsageCallResult) -> None:
        self.finalized.append((call, result))

    async def mark_unknown(self, call: UsageCallStart, reason: str) -> None:
        self.unknown.append((call, reason))


def _ctx(message: str, metadata: dict[str, Any] | None = None) -> TurnContext:
    config = GatewayConfig(llm={"provider": "openrouter"})
    return TurnContext(
        message=message,
        session_key="agent:main:test",
        config=config,
        provider=_FailProvider(),
        model="text-model",
        tool_defs=[],
        system_prompt="system",
        metadata=metadata or {},
        raw_message=message,
    )


@pytest.mark.asyncio
async def test_gate_skips_when_no_history_image() -> None:
    ctx = await apply_vision_followup_gate(_ctx("plain text"))

    assert ctx.metadata["router_vision_followup_gate_decision"] == "not_applicable"
    assert ctx.metadata.get("router_vision_followup_needs_image") is not True


@pytest.mark.asyncio
async def test_gate_skips_when_current_turn_has_image() -> None:
    ctx = _ctx("describe this", {"router_history_has_recent_image": True})
    ctx.attachments.append({"mime": "image/png", "data": "abc"})

    out = await apply_vision_followup_gate(ctx)

    assert out.metadata["router_vision_followup_gate_decision"] == "current_image"
    assert out.metadata.get("router_vision_followup_needs_image") is not True


@pytest.mark.asyncio
async def test_gate_accepts_needs_image_json() -> None:
    provider = _JsonProvider(
        '{"decision":"needs_image","confidence":0.91,"reason":"spatial reference"}'
    )
    ctx = _ctx(
        "What is in the upper right?",
        {
            "router_history_has_recent_image": True,
            "router_turns_since_last_image": 1,
            "router_last_image_turn_text": "Describe this screenshot.",
            "router_history_user_texts": ["Describe this screenshot."],
        },
    )
    ctx.provider = provider

    out = await apply_vision_followup_gate(ctx)

    assert out.metadata["router_vision_followup_gate_decision"] == "needs_image"
    assert out.metadata["router_vision_followup_needs_image"] is True
    assert out.metadata["router_vision_followup_gate_confidence"] == 0.91
    assert out.metadata["router_vision_followup_gate_reason"] == "spatial reference"
    assert provider.calls[0]["tools"] == []


@pytest.mark.asyncio
async def test_gate_prefers_dedicated_gate_chat_over_primary_provider() -> None:
    gate_chat = _RecordingGateChat(
        '{"decision":"needs_image","confidence":0.93,"reason":"dedicated gate"}'
    )
    ctx = _ctx(
        "Does the right side matter?",
        {
            "router_history_has_recent_image": True,
            "router_turns_since_last_image": 1,
            "router_vision_followup_gate_chat": gate_chat,
            "router_vision_followup_gate_model": "deepseek/deepseek-v4-flash",
        },
    )
    ctx.provider_request_correlation = ProviderRequestCorrelation(
        session_id="session-1",
        turn_id="turn-1",
        execution_id="root-execution",
        call_kind="agent.chat",
    )

    out = await apply_vision_followup_gate(ctx)

    assert out.metadata["router_vision_followup_gate_decision"] == "needs_image"
    assert out.metadata["router_vision_followup_gate_source"] == "llm"
    assert out.metadata["router_vision_followup_gate_model"] == "deepseek/deepseek-v4-flash"
    assert gate_chat.calls
    correlation = gate_chat.calls[0]["config"].provider_request_correlation
    assert correlation.session_id == "session-1"
    assert correlation.turn_id == "turn-1"
    assert correlation.execution_id != "root-execution"
    assert correlation.call_kind == "auxiliary.vision_gate"


@pytest.mark.asyncio
async def test_runtime_gate_chat_uses_configured_lightweight_tier_model() -> None:
    config = GatewayConfig(llm={"provider": "openrouter"})
    runner = TurnRunner(provider_selector=None, config=config)
    selector = _RecordingSelector()

    chat, model = runner._make_vision_followup_gate_chat(selector)

    assert model == "deepseek/deepseek-v4-flash"
    assert callable(chat)
    assert selector.clones[0].model == "deepseek/deepseek-v4-flash"
    events = [
        event
        async for event in chat(
            [Message(role="user", content="{}")],
            tools=[],
            config=ChatConfig(),
        )
    ]
    assert any(isinstance(event, TextDeltaEvent) for event in events)


@pytest.mark.asyncio
async def test_runtime_gate_chat_records_one_child_execution() -> None:
    sink = _UsageSink()
    config = GatewayConfig(llm={"provider": "openrouter"})
    runner = TurnRunner(provider_selector=None, config=config, usage_event_sink=sink)
    selector = _RecordingSelector()
    parent = UsageExecutionContext(
        execution_id="turn-1",
        agent_run_id="turn-1",
        turn_id="turn-1",
        session_id="session-1",
        session_epoch=3,
        agent_id="main",
        run_kind="webchat",
    )

    chat, _ = runner._make_vision_followup_gate_chat(selector, parent)
    assert callable(chat)
    _ = [
        event
        async for event in chat(
            [Message(role="user", content="{}")],
            tools=[],
            config=ChatConfig(),
        )
    ]

    assert len(sink.started) == 1
    call = sink.started[0]
    assert call.execution_id != "turn-1"
    assert call.parent_turn_id == "turn-1"
    assert call.session_id == "session-1"
    assert call.session_epoch == 3
    assert call.run_kind == "vision_followup_gate"
    assert len(sink.finalized) == 1
    assert sink.unknown == []


@pytest.mark.asyncio
async def test_gate_accepts_json_from_done_reasoning_content() -> None:
    ctx = _ctx(
        "Does the right side matter?",
        {
            "router_history_has_recent_image": True,
            "router_turns_since_last_image": 1,
        },
    )
    ctx.provider = _ReasoningOnlyProvider(
        '{"decision":"needs_image","confidence":0.77,"reason":"reasoning json"}'
    )

    out = await apply_vision_followup_gate(ctx)

    assert out.metadata["router_vision_followup_gate_decision"] == "needs_image"
    assert out.metadata["router_vision_followup_needs_image"] is True
    assert out.metadata["router_vision_followup_gate_confidence"] == 0.77
    assert out.metadata["router_vision_followup_gate_reason"] == "reasoning json"
    assert out.metadata["router_vision_followup_gate_source"] == "llm"


@pytest.mark.asyncio
async def test_gate_accepts_text_only_json() -> None:
    provider = _JsonProvider(
        '{"decision":"text_only","confidence":0.84,"reason":"asks for code"}'
    )
    ctx = _ctx("Write a Python script.", {"router_history_has_recent_image": True})
    ctx.provider = provider

    out = await apply_vision_followup_gate(ctx)

    assert out.metadata["router_vision_followup_gate_decision"] == "text_only"
    assert out.metadata["router_vision_followup_needs_image"] is False
    assert out.metadata["router_vision_followup_gate_confidence"] == 0.84


@pytest.mark.asyncio
async def test_gate_respects_explicit_english_image_opt_out() -> None:
    ctx = _ctx(
        "Do not use or inspect the previous image. Reply exactly: TEXT-ONLY",
        {
            "router_history_has_recent_image": True,
            "router_turns_since_last_image": 1,
        },
    )

    out = await apply_vision_followup_gate(ctx)

    assert out.metadata["router_vision_followup_gate_decision"] == "text_only"
    assert out.metadata["router_vision_followup_gate_source"] == "explicit_opt_out"
    assert out.metadata["router_vision_followup_needs_image"] is False


@pytest.mark.asyncio
async def test_gate_respects_explicit_chinese_image_opt_out() -> None:
    ctx = _ctx(
        "不要看上一张图片，直接回答：TEXT-ONLY",
        {
            "router_history_has_recent_image": True,
            "router_turns_since_last_image": 1,
        },
    )

    out = await apply_vision_followup_gate(ctx)

    assert out.metadata["router_vision_followup_gate_decision"] == "text_only"
    assert out.metadata["router_vision_followup_gate_source"] == "explicit_opt_out"
    assert out.metadata["router_vision_followup_needs_image"] is False


@pytest.mark.asyncio
async def test_gate_accepts_explicit_chinese_previous_image_reference() -> None:
    ctx = _ctx(
        "上一张图片是什么颜色？",
        {
            "router_history_has_recent_image": True,
            "router_turns_since_last_image": 1,
        },
    )

    out = await apply_vision_followup_gate(ctx)

    assert out.metadata["router_vision_followup_gate_decision"] == "needs_image"
    assert out.metadata["router_vision_followup_gate_source"] == "explicit_image_reference"
    assert out.metadata["router_vision_followup_needs_image"] is True


@pytest.mark.asyncio
async def test_gate_accepts_explicit_english_previous_image_reference() -> None:
    ctx = _ctx(
        "What color was the previous image?",
        {
            "router_history_has_recent_image": True,
            "router_turns_since_last_image": 1,
        },
    )

    out = await apply_vision_followup_gate(ctx)

    assert out.metadata["router_vision_followup_gate_decision"] == "needs_image"
    assert out.metadata["router_vision_followup_gate_source"] == "explicit_image_reference"
    assert out.metadata["router_vision_followup_needs_image"] is True


@pytest.mark.asyncio
async def test_gate_unknown_recent_falls_back_to_image() -> None:
    provider = _JsonProvider(
        '{"decision":"unknown","confidence":0.2,"reason":"ambiguous pronoun"}'
    )
    ctx = _ctx(
        "What about this?",
        {
            "router_history_has_recent_image": True,
            "router_turns_since_last_image": 1,
        },
    )
    ctx.provider = provider

    out = await apply_vision_followup_gate(ctx)

    assert out.metadata["router_vision_followup_gate_decision"] == "unknown"
    assert out.metadata["router_vision_followup_needs_image"] is True
    assert out.metadata["router_vision_followup_fallback"] == "image_if_recent"


@pytest.mark.asyncio
async def test_gate_provider_error_fails_closed_without_raw_error_reason() -> None:
    ctx = _ctx(
        "What about this?",
        {
            "router_history_has_recent_image": True,
            "router_turns_since_last_image": 1,
        },
    )
    ctx.provider = _RaisingProvider()

    out = await apply_vision_followup_gate(ctx)

    assert out.metadata["router_vision_followup_gate_decision"] == "unknown"
    assert out.metadata["router_vision_followup_gate_source"] == "error"
    assert out.metadata["router_vision_followup_gate_reason"] == "RuntimeError"
    assert out.metadata["router_vision_followup_needs_image"] is False
    assert "router_vision_followup_fallback" not in out.metadata


@pytest.mark.asyncio
async def test_gate_unknown_old_falls_back_to_text() -> None:
    provider = _JsonProvider(
        '{"decision":"unknown","confidence":0.2,"reason":"ambiguous but old"}'
    )
    ctx = _ctx(
        "What about this?",
        {
            "router_history_has_recent_image": True,
            "router_turns_since_last_image": 3,
        },
    )
    ctx.provider = provider

    out = await apply_vision_followup_gate(ctx)

    assert out.metadata["router_vision_followup_gate_decision"] == "unknown"
    assert out.metadata["router_vision_followup_needs_image"] is False
