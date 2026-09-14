from __future__ import annotations

import base64
import io
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.history import decode_assistant_replay
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
from opensquilla.provider.request_proof import project_provider_payload
from opensquilla.provider.types import ContentBlockImage, ContentBlockText, ContentBlockToolResult
from opensquilla.tools.types import ToolContext
from tests.helpers.image_bytes import image_bytes


def test_live_request_capacity_uses_shared_media_estimate() -> None:
    agent = Agent(provider=_RecordingProvider(), config=AgentConfig())
    estimates = []
    for compression in (0, 9):
        output = io.BytesIO()
        Image.new("RGB", (512, 512), "blue").save(output, format="PNG", compress_level=compression)
        data = base64.b64encode(output.getvalue()).decode()
        messages = [Message(role="user", content=[ContentBlockImage(
            media_type="image/png", data=data,
        )])]
        proof = project_provider_payload(
            {"messages": [message.model_dump(mode="json", exclude_none=True)
                          for message in messages]},
            projection_adapter="synthetic", proof_budget=0,
        )
        tokens = agent._estimate_live_request_tokens(messages)
        chars = agent._estimate_live_request_chars(messages)
        assert tokens == proof["estimated_tokens"]
        assert chars == proof["estimated_chars"]
        assert tokens < 3_000
        estimates.append((tokens, chars))
    assert estimates[0] == estimates[1]


@pytest.mark.parametrize("context_kind", ["retained", "disabled", "no_session", "no_workspace"])
def test_agent_compaction_image_paths_use_actual_retained_session(
    tmp_path: Path, context_kind: str
) -> None:
    context = ToolContext(
        workspace_dir=str(tmp_path / "workspace") if context_kind != "no_workspace" else None,
        artifact_media_root=str(tmp_path / "media"),
        artifact_session_id="real-session" if context_kind != "no_session" else None,
        sandbox_gateway_config=SimpleNamespace(
            attachments=SimpleNamespace(persist_transcripts=context_kind != "disabled")
        ),
    )
    agent = Agent(provider=_RecordingProvider(), config=AgentConfig(), tool_context=context)
    resolver = agent._build_compaction_config().attachment_path_resolver
    if context_kind != "retained":
        assert resolver is None
        assert not (tmp_path / "workspace").exists()
        return
    assert resolver is not None
    payload = image_bytes("JPEG")
    path = resolver(
        {"mime": "image/jpeg", "name": "photo.jpg", "data": base64.b64encode(payload).decode()},
        "agent-turn",
    )
    assert path is not None and "/real-session/" in path
    assert "agent-turn" not in path
    assert (tmp_path / "workspace" / path).read_bytes() == payload


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
    screenshot = base64.b64encode(image_bytes()).decode("ascii")

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


class _ToolImageContinuationProvider(_ToolThenRejectImageProvider):
    model = "text-model"

    def __init__(self, *, allow_upgrade: bool = False, reject_multiple: bool = False) -> None:
        super().__init__()
        self.allow_upgrade = allow_upgrade
        self.upgraded_tools_supported = True
        self.reject_multiple = reject_multiple
        self.prepared_image_counts: list[int] = []
        self.admitted_image_counts: list[int] = []

    async def _rejection_stream(self) -> AsyncIterator[Any]:
        async for event in self._stream():
            yield event

    async def prepare_image_continuation(
        self, messages: list[Message], config: ChatConfig
    ) -> ChatConfig | None:
        self.prepared_image_counts.append(count_provider_image_blocks(messages))
        if not self.allow_upgrade:
            return None
        self.model = "vision-model"
        return config.model_copy(update={
            "model_vision_support": "supported",
            "model_capabilities": ModelCapabilities(
                supports_vision=True, supports_tools=self.upgraded_tools_supported,
            ),
            "max_tokens": 128,
            "provider_request_max_chars": 120_000,
        })

    def active_context_window_tokens(self) -> int:
        return 32_000

    def validate_chat_admission(
        self, messages: list[Message], config: ChatConfig | None
    ) -> ProviderErrorEvent | None:
        image_count = count_provider_image_blocks(messages)
        self.admitted_image_counts.append(image_count)
        if self.reject_multiple and image_count > 1:
            return ProviderErrorEvent(
                code="image_capacity_exceeded", message="The complete image batch exceeds capacity."
            )
        return None


def _tool_images(messages: list[Message]) -> list[ContentBlockImage]:
    return [
        block
        for message in messages
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockImage)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["supported", "unsupported", "unknown", "routed", "routed_no_tools"],
)
async def test_loaded_tool_images_keep_all_formats_and_check_next_model(mode: str) -> None:
    provider = _ToolImageContinuationProvider(allow_upgrade=mode.startswith("routed"))
    provider.upgraded_tools_supported = mode != "routed_no_tools"
    tool_context = ToolContext()
    executions: list[str] = []
    support = mode if mode in {"supported", "unknown"} else "unsupported"
    media = [
        {
            "mime": mime,
            "data": base64.b64encode(image_bytes(fmt)).decode("ascii"),
            "attachment_id": f"attachment-{index}",
            "name": f"image-{index}.{fmt.lower()}",
            "local_path": f".opensquilla/attachments/session/image-{index}.{fmt.lower()}",
            "source_url": f"https://example.invalid/image-{index}",
        }
        for index, (fmt, mime) in enumerate([
            ("JPEG", "image/jpeg"), ("PNG", "image/png"),
            ("GIF", "image/gif"), ("WEBP", "image/webp"),
        ])
    ]

    async def load_images(call: ToolCall) -> ToolResult:
        executions.append(call.tool_use_id)
        tool_context.tool_result_media[call.tool_use_id] = list(media)
        return ToolResult(
            tool_use_id=call.tool_use_id, tool_name=call.tool_name,
            content='{"loaded":4,"analyzed":false}',
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2, max_tokens=256, model_id="text-model",
            model_vision_support=support,
            model_capabilities=ModelCapabilities(supports_vision=support == "supported"),
            preserve_historical_images=True,
        ),
        tool_definitions=[ToolDefinition(
            name="observe", description="Load stored images.",
            input_schema=ToolInputSchema(properties={}),
        )],
        tool_handler=load_images, tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Compare the stored pictures.")]

    assert executions == ["tool-1"]
    assert len(provider.calls) == 2
    assert provider.prepared_image_counts == ([] if mode == "supported" else [4])
    expected_images = 4 if mode in {"supported", "routed", "routed_no_tools"} else 0
    assert count_provider_image_blocks(provider.calls[1]["messages"]) == expected_images
    retained = _tool_images(agent.history_snapshot())
    assert [image.media_type for image in retained] == [item["mime"] for item in media]
    assert [image.attachment_id for image in retained] == [item["attachment_id"] for item in media]
    assert all(image.durable_retained for image in retained)
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.assistant_replay is not None
    replayed = _tool_images(decode_assistant_replay(done.assistant_replay))
    assert [image.local_path for image in replayed] == [item["local_path"] for item in media]
    assert [image.source_url for image in replayed] == [item["source_url"] for item in media]
    assert [image.name for image in replayed] == [item["name"] for item in media]
    for image in replayed:
        assert "local_path" not in image.model_dump(mode="json")
        assert "source_url" not in image.model_dump(mode="json")
    assert agent.current_assistant_replay() == done.assistant_replay
    assert tool_context.tool_result_media == {}
    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(isinstance(event, ErrorEvent) for event in events)
    if expected_images == 0:
        assert any(
            isinstance(block, ContentBlockText) and "图片未分析" in block.text
            for message in provider.calls[1]["messages"]
            if isinstance(message.content, list)
            for block in message.content
        )
        assert agent.config.model_id == "text-model"
    elif mode.startswith("routed"):
        assert bool(provider.calls[1]["tools"]) is provider.upgraded_tools_supported
        request_config = provider.calls[1]["config"]
        assert request_config.model_capabilities.supports_vision
        assert request_config.max_tokens == agent.config.max_tokens == 128
        assert agent.config.model_id == "vision-model"
        assert agent.config.context_window_tokens == 32_000
        assert request_config.provider_request_max_chars == 120_000


@pytest.mark.asyncio
async def test_loaded_tool_images_do_not_silently_drop_invalid_items_or_capacity() -> None:
    provider = _ToolImageContinuationProvider(reject_multiple=True)
    context = ToolContext()
    executed: list[str] = []

    async def load_images(call: ToolCall) -> ToolResult:
        executed.append(call.tool_use_id)
        context.tool_result_media[call.tool_use_id] = [
            {"mime": "image/jpeg", "data": base64.b64encode(image_bytes("JPEG")).decode()},
            {"mime": "image/png", "data": base64.b64encode(b"invalid image").decode()},
            {"mime": "image/webp", "data": base64.b64encode(image_bytes("WEBP")).decode()},
        ]
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="loaded")

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2, model_vision_support="supported"),
        tool_context=context, tool_handler=load_images,
        tool_definitions=[ToolDefinition(
            name="observe", description="Load stored images.",
            input_schema=ToolInputSchema(properties={}),
        )],
    )

    events = [event async for event in agent.run_turn("Inspect all loaded images.")]

    assert executed == ["tool-1"]
    assert len(provider.calls) == 1
    assert 2 in provider.admitted_image_counts
    assert any(
        isinstance(event, ErrorEvent) and event.code == "image_capacity_exceeded"
        for event in events
    )
    retained = decode_assistant_replay(agent.current_assistant_replay())
    assert len(_tool_images(retained)) == 2
    assert any(
        isinstance(block, ContentBlockText) and "Tool image 2 could not be loaded" in block.text
        for message in retained
        if isinstance(message.content, list)
        for block in message.content
    )


@pytest.mark.asyncio
async def test_history_image_preservation_ignores_obsolete_natural_language_gate() -> None:
    provider = _RecordingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1, preserve_historical_images=True, model_vision_support="supported",
            metadata={"router_vision_followup_gate_source": "explicit_opt_out"},
        ),
    )
    images = [ContentBlockImage(
        media_type="image/png", data=base64.b64encode(image_bytes(color=color)).decode(),
        attachment_id=f"image-{color}",
    ) for color in ["red", "blue"]]
    agent.set_history([
        Message(role="user", content=images),
        Message(role="assistant", content="Two images received."),
    ])

    events = [
        event async for event in agent.run_turn("Ignore the first picture; inspect the second.")
    ]

    assert count_provider_image_blocks(provider.calls[0]["messages"]) == 2
    assert [image.attachment_id for image in _tool_images(agent.history_snapshot())] == [
        "image-red", "image-blue",
    ]
    assert any(isinstance(event, DoneEvent) for event in events)
