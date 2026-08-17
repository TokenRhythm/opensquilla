from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.agent import UNSUPPORTED_IMAGE_INPUT_REPLY
from opensquilla.engine.types import DoneEvent, ErrorEvent, TextDeltaEvent
from opensquilla.provider import ChatConfig, Message, ModelCapabilities
from opensquilla.provider.types import ContentBlockImage


class _RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

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


@pytest.mark.asyncio
async def test_non_vision_model_returns_friendly_reply_without_provider_call() -> None:
    provider = _RecordingProvider()
    config = AgentConfig(
        model_id="text-only-model",
        model_capabilities=ModelCapabilities(supports_vision=False),
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
    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == [
        UNSUPPORTED_IMAGE_INPUT_REPLY
    ]
    assert not any(isinstance(event, ErrorEvent) for event in events)
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.text == UNSUPPORTED_IMAGE_INPUT_REPLY
    assert done.input_tokens == 0
    assert done.output_tokens == 0
    assert done.cost_usd == 0.0
    assert config.metadata["image_input_mode"] == "rejected"
    assert config.metadata["image_input_reason"] == "vision_unsupported"
    assert config.metadata["image_input_count"] == 1


@pytest.mark.asyncio
async def test_vision_model_still_receives_current_turn_image() -> None:
    provider = _RecordingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_id="vision-model",
            model_capabilities=ModelCapabilities(supports_vision=True),
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
