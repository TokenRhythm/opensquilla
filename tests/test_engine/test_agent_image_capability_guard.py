from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.types import DoneEvent, ErrorEvent, TextDeltaEvent
from opensquilla.provider import ChatConfig, Message, ModelCapabilities
from opensquilla.provider.protocol import (
    IMAGE_INPUT_UNSUPPORTED_CODE,
    validate_provider_chat_admission,
)
from opensquilla.provider.types import ContentBlockImage, ContentBlockToolResult


class _RecordingProvider:
    provider_name = "recording"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def list_models(self) -> list[Any]:
        return []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append({"messages": messages, "tools": tools, "config": config})
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        from opensquilla.provider import DoneEvent as ProviderDoneEvent
        from opensquilla.provider import TextDeltaEvent as ProviderTextDeltaEvent

        yield ProviderTextDeltaEvent(text="image accepted")
        yield ProviderDoneEvent(stop_reason="end_turn", input_tokens=3, output_tokens=2)


def _image_message() -> Message:
    return Message(
        role="user",
        content=[
            ContentBlockImage(
                media_type="image/png",
                data=base64.b64encode(b"synthetic image").decode("ascii"),
            )
        ],
    )


def test_provider_admission_without_config_preserves_unknown_capability() -> None:
    assert (
        validate_provider_chat_admission(None, [Message(role="user", content="hi")], None)
        is None
    )
    assert validate_provider_chat_admission(None, [_image_message()], None) is None


@pytest.mark.asyncio
async def test_non_vision_model_returns_structured_error_without_provider_call() -> None:
    provider = _RecordingProvider()
    config = AgentConfig(
        model_id="text-only-model",
        model_capabilities=ModelCapabilities(supports_vision=False),
        model_vision_support="unsupported",
    )
    agent = Agent(provider=provider, config=config)

    events = [
        event
        async for event in agent.run_turn(
            "请分析这张图片。",
            extra_messages=[_image_message()],
        )
    ]

    assert provider.calls == []
    assert not any(isinstance(event, TextDeltaEvent) for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)
    error = next(event for event in events if isinstance(event, ErrorEvent))
    assert error.code == IMAGE_INPUT_UNSUPPORTED_CODE
    assert config.metadata["image_input_mode"] == "rejected"
    assert config.metadata["image_input_reason"] == "model_vision_unsupported"
    assert config.metadata["image_input_count"] == 1
    assert config.metadata["image_input_stage"] == "primary"


@pytest.mark.asyncio
async def test_vision_model_still_receives_current_turn_image() -> None:
    provider = _RecordingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_id="vision-model",
            model_capabilities=ModelCapabilities(supports_vision=True),
            model_vision_support="supported",
        ),
    )

    events = [
        event
        async for event in agent.run_turn(
            "Describe this image.",
            extra_messages=[_image_message()],
        )
    ]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert len(provider.calls) == 1
    assert any(
        isinstance(block, ContentBlockImage)
        for message in provider.calls[0]["messages"]
        if isinstance(message.content, list)
        for block in message.content
    )


@pytest.mark.asyncio
async def test_unknown_model_capability_defers_to_provider() -> None:
    provider = _RecordingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_id="custom-model",
            model_capabilities=None,
            model_vision_support="unknown",
        ),
    )

    events = [
        event
        async for event in agent.run_turn(
            "Describe this image.",
            extra_messages=[_image_message()],
        )
    ]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_tool_result_image_uses_the_same_admission_guard() -> None:
    provider = _RecordingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            model_id="text-only-model",
            model_vision_support="unsupported",
        ),
    )
    tool_result_message = Message(
        role="user",
        content=[
            ContentBlockToolResult(
                tool_use_id="synthetic-tool",
                content=[ContentBlockImage(media_type="image/png", data="c3ludGhldGlj")],
            )
        ],
    )

    events = [
        event
        async for event in agent.run_turn(
            "Inspect the tool result.",
            extra_messages=[tool_result_message],
        )
    ]

    assert provider.calls == []
    assert [event.code for event in events if isinstance(event, ErrorEvent)] == [
        IMAGE_INPUT_UNSUPPORTED_CODE
    ]


@pytest.mark.asyncio
async def test_forced_router_rejection_does_not_require_a_current_turn_image() -> None:
    provider = _RecordingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            model_id="text-only-model",
            model_vision_support="unknown",
            metadata={
                "image_input_forced_rejection_reason": "router_image_route_unavailable",
            },
        ),
    )

    events = [event async for event in agent.run_turn("Inspect the previous image.")]

    assert provider.calls == []
    assert [event.code for event in events if isinstance(event, ErrorEvent)] == [
        IMAGE_INPUT_UNSUPPORTED_CODE
    ]
    assert agent.config.metadata["image_input_reason"] == "router_image_route_unavailable"
    assert agent.config.metadata["image_input_count"] == 0
