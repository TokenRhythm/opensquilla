from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.types import DoneEvent, ErrorEvent, TextDeltaEvent, ToolCall
from opensquilla.provider import (
    ChatConfig,
    Message,
    ModelCapabilities,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import DoneEvent as ProviderDoneEvent
from opensquilla.provider import ErrorEvent as ProviderErrorEvent
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEndEvent
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStartEvent
from opensquilla.provider.protocol import (
    count_provider_image_blocks,
    validate_provider_chat_admission,
)
from opensquilla.provider.types import ContentBlockImage, ContentBlockText, ContentBlockToolResult
from opensquilla.tools.types import ToolContext


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


class _RejectImageOnceProvider(_RecordingProvider):
    rejection_code = "image_input_unsupported"
    rejection_message = "This model does not support image input."

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append({"messages": messages, "tools": tools, "config": config})
        has_image = any(
            isinstance(block, ContentBlockImage)
            for message in messages
            if isinstance(message.content, list)
            for block in message.content
        )
        return self._rejection_stream() if has_image else self._stream()

    async def _rejection_stream(self) -> AsyncIterator[Any]:
        yield ProviderErrorEvent(
            code=self.rejection_code,
            message=self.rejection_message,
        )


class _RouterProbeProvider(_RejectImageOnceProvider):
    provider_name = "router-probe"

    def __init__(self) -> None:
        super().__init__()
        self.models = ["configured-c0", "configured-c1", "configured-c2", "configured-c3"]
        self.index = 0

    @property
    def model(self) -> str:
        return self.models[self.index]

    def fallback_after_image_rejection(self, _reason: str) -> bool:
        if self.index >= len(self.models) - 1:
            return False
        self.index += 1
        return True

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(
            {
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "config": config,
            }
        )
        has_image = any(
            isinstance(block, ContentBlockImage)
            for message in messages
            if isinstance(message.content, list)
            for block in message.content
        )
        return self._rejection_stream() if has_image else self._stream()


class _TextThenRejectProvider(_RecordingProvider):
    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append({"messages": messages, "tools": tools, "config": config})
        return self._text_then_reject_stream()

    async def _text_then_reject_stream(self) -> AsyncIterator[Any]:
        from opensquilla.provider import TextDeltaEvent as ProviderTextDeltaEvent

        yield ProviderTextDeltaEvent(text="partial")
        yield ProviderErrorEvent(
            code="image_input_unsupported",
            message="This model does not support image input.",
        )


class _ThinkingTextThenRejectProvider(_TextThenRejectProvider):
    async def _text_then_reject_stream(self) -> AsyncIterator[Any]:
        from opensquilla.provider import TextDeltaEvent as ProviderTextDeltaEvent

        yield ProviderTextDeltaEvent(text="FIRST-PARTIAL")
        yield ProviderErrorEvent(
            code="image_input_unsupported",
            message="Image input is not supported while thinking is enabled.",
        )


class _PreflightRejectProvider(_RecordingProvider):
    def __init__(self, *, code: str) -> None:
        super().__init__()
        self.code = code

    def validate_chat_admission(
        self,
        messages: list[Message],
        _config: ChatConfig | None,
    ) -> ProviderErrorEvent | None:
        if any(
            isinstance(block, ContentBlockImage)
            for message in messages
            if isinstance(message.content, list)
            for block in message.content
        ):
            return ProviderErrorEvent(
                code=self.code,
                message="This deployment does not support image input.",
            )
        return None


class _ToolThenRejectImageProvider(_RecordingProvider):
    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append({"messages": messages, "tools": tools, "config": config})
        if len(self.calls) == 1:
            return self._tool_stream()
        return self._rejection_stream()

    async def _tool_stream(self) -> AsyncIterator[Any]:
        yield ProviderToolUseStartEvent(tool_use_id="tool-1", tool_name="observe")
        yield ProviderToolUseEndEvent(
            tool_use_id="tool-1",
            tool_name="observe",
            arguments={},
        )
        yield ProviderDoneEvent(stop_reason="tool_use", input_tokens=1, output_tokens=1)

    async def _rejection_stream(self) -> AsyncIterator[Any]:
        yield ProviderErrorEvent(
            code="image_input_unsupported",
            message="This model does not support image input.",
        )


class _ToolThenEmptyImageRejectProvider(_ToolThenRejectImageProvider):
    async def _rejection_stream(self) -> AsyncIterator[Any]:
        yield ProviderErrorEvent(
            code="empty_response",
            message="This model does not support image input.",
        )


class _ToolMediaProbeProvider(_ToolThenRejectImageProvider):
    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append({"messages": messages, "tools": tools, "config": config})
        if len(self.calls) == 1:
            return self._tool_stream()
        if count_provider_image_blocks(messages):
            return self._rejection_stream()
        return self._stream()


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
async def test_non_vision_model_receives_marker_and_continues() -> None:
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

    assert len(provider.calls) == 1
    sent = provider.calls[0]["messages"]
    assert not any(
        isinstance(block, ContentBlockImage)
        for message in sent
        if isinstance(message.content, list)
        for block in message.content
    )
    assert any(
        isinstance(block, ContentBlockText) and "图片未分析" in block.text
        for message in sent
        if isinstance(message.content, list)
        for block in message.content
    )
    assert any(isinstance(event, TextDeltaEvent) for event in events)
    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert config.metadata["image_input_mode"] == "marker"
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


@pytest.mark.parametrize(
    ("rejection_code", "rejection_message"),
    [
        ("image_input_unsupported", "This model does not support image input."),
        ("404", "No endpoints found that support image input."),
    ],
)
@pytest.mark.asyncio
async def test_unknown_model_retries_same_model_with_failure_marker(
    rejection_code: str,
    rejection_message: str,
) -> None:
    provider = _RejectImageOnceProvider()
    provider.provider_name = "openrouter"
    provider.rejection_code = rejection_code
    provider.rejection_message = rejection_message
    original = _image_message()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_id="custom-model",
            model_vision_support="unknown",
        ),
    )

    events = [
        event
        async for event in agent.run_turn(
            "Describe this image.",
            extra_messages=[original],
        )
    ]

    assert len(provider.calls) == 2
    assert any(
        isinstance(block, ContentBlockImage)
        for message in provider.calls[0]["messages"]
        if isinstance(message.content, list)
        for block in message.content
    )
    assert not any(
        isinstance(block, ContentBlockImage)
        for message in provider.calls[1]["messages"]
        if isinstance(message.content, list)
        for block in message.content
    )
    assert any(
        isinstance(block, ContentBlockText) and "图片分析失败" in block.text
        for message in provider.calls[1]["messages"]
        if isinstance(message.content, list)
        for block in message.content
    )
    assert isinstance(original.content, list)
    assert isinstance(original.content[0], ContentBlockImage)
    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.parametrize("error_code", ["unsupported_image", "bad_request"])
@pytest.mark.asyncio
async def test_precise_preflight_rejection_retries_with_marker(
    error_code: str,
) -> None:
    provider = _PreflightRejectProvider(code=error_code)
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_id="custom-model",
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

    # Preflight prevents the native physical call; the same configured model
    # then receives exactly one marker-projected request.
    assert len(provider.calls) == 1
    assert not any(
        isinstance(block, ContentBlockImage)
        for message in provider.calls[0]["messages"]
        if isinstance(message.content, list)
        for block in message.content
    )
    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.parametrize(
    ("rejection_code", "rejection_message"),
    [
        ("image_input_unsupported", "This model does not support image input."),
        ("404", "No endpoints found that support image input."),
    ],
)
@pytest.mark.asyncio
async def test_router_exhausts_four_configured_models_before_direct_marker(
    rejection_code: str,
    rejection_message: str,
) -> None:
    provider = _RouterProbeProvider()
    provider.provider_name = "openrouter"
    provider.rejection_code = rejection_code
    provider.rejection_message = rejection_message
    original = _image_message()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_id="configured-c0",
            model_vision_support="unknown",
            metadata={
                "routing_source": "image_route",
                "router_fallback_strict": True,
                "image_input_mode": "native",
            },
        ),
    )

    events = [
        event
        async for event in agent.run_turn(
            "Describe the image.",
            extra_messages=[original],
        )
    ]

    assert [call["model"] for call in provider.calls] == [
        "configured-c0",
        "configured-c1",
        "configured-c2",
        "configured-c3",
        "configured-c3",
    ]
    assert all(
        any(
            isinstance(block, ContentBlockImage)
            for message in call["messages"]
            if isinstance(message.content, list)
            for block in message.content
        )
        for call in provider.calls[:4]
    )
    assert not any(
        isinstance(block, ContentBlockImage)
        for message in provider.calls[-1]["messages"]
        if isinstance(message.content, list)
        for block in message.content
    )
    assert any(isinstance(event, DoneEvent) for event in events)


@pytest.mark.asyncio
async def test_image_marker_retry_is_suppressed_after_visible_output() -> None:
    provider = _TextThenRejectProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_id="custom-model",
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

    assert len(provider.calls) == 1
    assert any(isinstance(event, TextDeltaEvent) for event in events)
    assert any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.asyncio
async def test_provider_error_cannot_bypass_image_retry_barrier() -> None:
    provider = _ThinkingTextThenRejectProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_id="custom-model",
            model_vision_support="unknown",
            thinking=True,
        ),
    )

    events = [
        event
        async for event in agent.run_turn(
            "Describe this image.",
            extra_messages=[_image_message()],
        )
    ]

    assert len(provider.calls) == 1
    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == [
        "FIRST-PARTIAL"
    ]
    assert any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.asyncio
async def test_image_marker_retry_is_suppressed_after_prior_tool_execution() -> None:
    provider = _ToolThenRejectImageProvider()
    executions: list[str] = []

    async def _tool_handler(call: ToolCall) -> ToolResult:
        executions.append(call.tool_use_id)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="observed",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
            model_id="custom-model",
            model_vision_support="unknown",
        ),
        tool_definitions=[
            ToolDefinition(
                name="observe",
                description="Observe once.",
                input_schema=ToolInputSchema(properties={}, required=[]),
            )
        ],
        tool_handler=_tool_handler,
    )

    events = [
        event
        async for event in agent.run_turn(
            "Inspect this image with the tool.",
            extra_messages=[_image_message()],
        )
    ]

    assert executions == ["tool-1"]
    assert len(provider.calls) == 2
    assert any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.asyncio
async def test_precise_image_rejection_cannot_use_generic_post_tool_retry() -> None:
    provider = _ToolThenEmptyImageRejectProvider()
    executions: list[str] = []

    async def _tool_handler(call: ToolCall) -> ToolResult:
        executions.append(call.tool_use_id)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="observed",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
            max_provider_retries=2,
            model_id="custom-model",
            model_vision_support="unknown",
        ),
        tool_definitions=[
            ToolDefinition(
                name="observe",
                description="Observe once.",
                input_schema=ToolInputSchema(properties={}, required=[]),
            )
        ],
        tool_handler=_tool_handler,
    )

    events = [
        event
        async for event in agent.run_turn(
            "Inspect this image with the tool.",
            extra_messages=[_image_message()],
        )
    ]

    assert executions == ["tool-1"]
    assert len(provider.calls) == 2
    assert any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.asyncio
async def test_unknown_tool_image_is_projected_before_post_tool_request() -> None:
    provider = _ToolMediaProbeProvider()
    tool_context = ToolContext()
    screenshot = base64.b64encode(b"tool screenshot").decode("ascii")

    async def _tool_handler(call: ToolCall) -> ToolResult:
        tool_context.tool_result_media[call.tool_use_id] = [
            {"mime": "image/png", "data": screenshot}
        ]
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="captured",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
            model_id="unknown-vision-model",
            model_vision_support="unknown",
        ),
        tool_definitions=[
            ToolDefinition(
                name="observe",
                description="Capture once.",
                input_schema=ToolInputSchema(properties={}, required=[]),
            )
        ],
        tool_handler=_tool_handler,
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Capture and explain.")]

    assert len(provider.calls) == 2
    assert count_provider_image_blocks(provider.calls[1]["messages"]) == 0
    assert any(
        isinstance(block, ContentBlockText) and "图片未分析" in block.text
        for message in provider.calls[1]["messages"]
        if isinstance(message.content, list)
        for block in message.content
    )
    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert (
        agent.config.metadata["image_input_reason"]
        == "image_probe_unsafe_after_irreversible_effect"
    )


@pytest.mark.asyncio
async def test_text_projection_does_not_destroy_image_for_later_vision_turn() -> None:
    provider = _RecordingProvider()
    config = AgentConfig(
        max_iterations=1,
        model_id="text-model",
        model_capabilities=ModelCapabilities(supports_vision=False),
        model_vision_support="unsupported",
        preserve_historical_images=False,
    )
    agent = Agent(provider=provider, config=config)
    canonical_image = _image_message()
    agent.set_history(
        [
            canonical_image,
            Message(role="assistant", content="I could not inspect it yet."),
        ]
    )

    first_events = [event async for event in agent.run_turn("Keep it for later.")]
    assert any(isinstance(event, DoneEvent) for event in first_events)
    assert not any(
        isinstance(block, ContentBlockImage)
        for message in provider.calls[0]["messages"]
        if isinstance(message.content, list)
        for block in message.content
    )
    assert any(
        isinstance(block, ContentBlockText)
        and "历史图片本回合未重新读取" in block.text
        for message in provider.calls[0]["messages"]
        if isinstance(message.content, list)
        for block in message.content
    )
    assert any(
        isinstance(block, ContentBlockImage)
        for message in agent.history_snapshot()
        if isinstance(message.content, list)
        for block in message.content
    )
    assert not any(
        isinstance(block, ContentBlockText)
        and "历史图片本回合未重新读取" in block.text
        for message in agent.history_snapshot()
        if isinstance(message.content, list)
        for block in message.content
    )

    config.model_id = "vision-model"
    config.model_capabilities = ModelCapabilities(supports_vision=True)
    config.model_vision_support = "supported"
    config.preserve_historical_images = True
    second_events = [event async for event in agent.run_turn("Now inspect it.")]

    assert any(isinstance(event, DoneEvent) for event in second_events)
    assert any(
        isinstance(block, ContentBlockImage)
        for message in provider.calls[1]["messages"]
        if isinstance(message.content, list)
        for block in message.content
    )


@pytest.mark.asyncio
async def test_tool_result_image_uses_the_same_marker_projection() -> None:
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

    projected, result = agent._project_image_input_for_provider(
        [tool_result_message],
        force_marker=True,
    )

    assert result.input_image_count == 1
    assert result.output_image_count == 0
    nested = projected[0].content[0]
    assert isinstance(nested, ContentBlockToolResult)
    assert isinstance(nested.content, list)
    assert isinstance(nested.content[0], ContentBlockText)
    assert "图片未分析" in nested.content[0].text
    assert isinstance(tool_result_message.content[0].content[0], ContentBlockImage)


@pytest.mark.asyncio
async def test_forced_router_marker_does_not_require_a_current_turn_image() -> None:
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

    assert len(provider.calls) == 1
    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert agent.config.metadata["image_input_reason"] == "router_image_route_unavailable"
    assert agent.config.metadata["image_input_count"] == 0
