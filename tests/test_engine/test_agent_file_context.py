from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.provider import (
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText


@pytest.mark.parametrize(
    ("provider_name", "choices"),
    [
        ("openai_compat", ["read_file", "browser"]),
        ("anthropic", ["browser", "read_file"]),
        ("ollama", []),
    ],
)
async def test_page_context_keeps_tool_order_and_answer_timing_with_provider(
    provider_name, choices
):
    from opensquilla.engine import ToolResult
    from opensquilla.provider import ToolUseEndEvent as ProviderToolEnd
    from opensquilla.provider import ToolUseStartEvent as ProviderToolStart

    handled = []
    requests = []

    class Provider:
        def __init__(self):
            self.provider_name = provider_name

        def chat(self, messages, tools=None, config=None):
            index = len(requests)
            requests.append(list(messages))
            assert {definition.name for definition in tools} == {"browser", "read_file"}
            return self.stream(index)

        async def stream(self, index):
            if index < len(choices):
                tool = choices[index]
                yield ProviderToolStart(tool_use_id=f"choice-{index}", tool_name=tool)
                yield ProviderToolEnd(tool_use_id=f"choice-{index}", tool_name=tool, arguments={})
                yield ProviderDone(stop_reason="tool_use")
            else:
                yield ProviderText(text="The heading is the page introduction.")
                yield ProviderDone(stop_reason="stop")

        async def list_models(self):
            return []

    async def handle(call):
        handled.append(call.tool_name)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="The page contains an introduction heading.",
        )

    agent = Agent(
        provider=Provider(),
        config=AgentConfig(),
        tool_handler=handle,
        tool_definitions=[
            ToolDefinition(
                name=name,
                description="Ordinary workspace capability.",
                input_schema=ToolInputSchema(properties={}),
            )
            for name in ("browser", "read_file")
        ],
    )
    events = [
        event
        async for event in agent.run_turn(
            'Explain this heading. <page_context>{"workingFile":"site/index.html",'
            '"targetRef":"page-1","annotations":[{"text":"What does this heading mean?",'
            '"selectionText":"Introduction"}]}</page_context>'
        )
    ]
    assert handled == choices
    assert len(requests) == len(choices) + 1
    assert events[-1].kind == "done"
    assert not any(event.kind == "error" for event in events)


async def test_file_selection_context_preserves_workspace_and_history_tool_pairs() -> None:
    requests: list[list[Message]] = []
    exposed_tools: list[list[str]] = []
    system_prompts: list[str] = []

    class RecordingProvider:
        provider_name = "test"

        def chat(self, messages: list[Message], tools=None, config=None) -> AsyncIterator[Any]:
            requests.append(list(messages))
            system_prompts.append(config.system or "")
            exposed_tools.append([tool.name for tool in tools or []])
            return self.stream()

        async def stream(self) -> AsyncIterator[Any]:
            yield ProviderText(text="The selected heading uses the shared page styles.")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

        async def list_models(self) -> list[Any]:
            return []

    tool_use = ContentBlockToolUse(
        id="read-before-selection",
        name="read_file",
        input={"path": "site/index.html"},
    )
    tool_result = ContentBlockToolResult(
        tool_use_id=tool_use.id,
        content="<h1 class='title'>Page heading</h1>",
    )
    history = [
        Message(role="user", content="Read the page source."),
        Message(role="assistant", content=[tool_use]),
        Message(role="user", content=[tool_result]),
    ]
    agent = Agent(
        provider=RecordingProvider(),
        config=AgentConfig(system_prompt="Workspace guidance: use the shared page styles."),
        tool_definitions=[
            ToolDefinition(
                name="read_file",
                description="Read a workspace file.",
                input_schema=ToolInputSchema(
                    properties={"path": {"type": "string"}},
                    required=["path"],
                ),
            ),
        ],
    )
    agent.set_history(history)

    events = [
        event
        async for event in agent.run_turn(
            "Explain this selection. File: site/index.html; selector: h1.title; text: Page heading."
        )
    ]

    assert len(requests) == 1
    assert exposed_tools == [["read_file"]]
    assert "Workspace guidance" in system_prompts[0]
    blocks = [
        block
        for message in requests[0]
        if isinstance(message.content, list)
        for block in message.content
    ]
    assert tool_use in blocks
    assert tool_result in blocks
    assert history[1].content == [tool_use]
    assert history[2].content == [tool_result]
    assert any(event.kind == "done" for event in events)
    assert not any(event.kind in {"error", "tool_use_start"} for event in events)


@pytest.mark.parametrize(
    ("vision_support", "provider_name"),
    [("unsupported", "test"), ("supported", "test"), ("supported", "ensemble"),
     ("unknown", "test")],
)
async def test_image_tool_result_reports_when_the_model_receives_no_image(
    vision_support: str, provider_name: str,
) -> None:
    from opensquilla.engine import ToolResult
    from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
    from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart
    from opensquilla.provider.types import ContentBlockImage, ContentBlockText, ModelCapabilities
    from opensquilla.tools.types import ToolContext

    requests: list[list[Message]] = []
    context = ToolContext()

    class ScreenshotProvider:
        def __init__(self) -> None:
            self.provider_name = provider_name

        def chat(self, messages: list[Message], tools=None, config=None) -> AsyncIterator[Any]:
            requests.append(list(messages))
            return self.stream(len(requests))

        async def stream(self, call_number: int) -> AsyncIterator[Any]:
            if call_number == 1:
                yield ProviderToolUseStart(tool_use_id="capture-1", tool_name="capture_page")
                yield ProviderToolUseEnd(
                    tool_use_id="capture-1", tool_name="capture_page", arguments={}
                )
                yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)
                return
            yield ProviderText(text="Capture complete.")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

        async def list_models(self) -> list[Any]:
            return []

    async def capture(tool_call: Any) -> ToolResult:
        context.tool_result_media[tool_call.tool_use_id] = [
            {"mime": "image/png", "data": "c3ludGhldGljLWltYWdl"}
        ]
        return ToolResult(
            tool_use_id=tool_call.tool_use_id,
            tool_name=tool_call.tool_name,
            content='{"imageAvailable":true}',
        )

    provider = ScreenshotProvider()
    config = AgentConfig(
        model_capabilities=ModelCapabilities(supports_vision=vision_support == "supported"),
        model_vision_support=vision_support,
    )
    agent = Agent(
        provider=provider,
        config=config,
        tool_context=context,
        tool_definitions=[
            ToolDefinition(
                name="capture_page",
                description="Capture the current page.",
                input_schema=ToolInputSchema(properties={}),
            )
        ],
        tool_handler=capture,
    )
    events = [event async for event in agent.run_turn("Capture the page.")]

    assert len(requests) == 2
    blocks = [
        block
        for message in requests[1]
        if isinstance(message.content, list)
        for block in message.content
    ]
    image_expected = vision_support == "supported" and provider_name != "ensemble"
    assert any(isinstance(block, ContentBlockImage) for block in blocks) is image_expected
    assert any(
        isinstance(block, ContentBlockText) and "图片未分析" in block.text
        for block in blocks
    ) is not image_expected
    result = next(block for block in blocks if isinstance(block, ContentBlockToolResult))
    assert result.content == '{"imageAvailable":true}'
    assert context.tool_result_media == {}
    assert not any(event.kind == "error" for event in events)
    canonical_images = [
        block
        for message in agent.history_snapshot()
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockImage)
    ]
    assert len(canonical_images) == 1
    assert canonical_images[0].data == "c3ludGhldGljLWltYWdl"

    provider.provider_name = "test"
    config.model_vision_support = "supported"
    config.model_capabilities = ModelCapabilities(supports_vision=True)
    config.preserve_historical_images = True
    followup = [event async for event in agent.run_turn("Inspect the retained capture.")]
    assert len(requests) == 3
    assert any(
        isinstance(block, ContentBlockImage) and block.data == canonical_images[0].data
        for message in requests[2]
        if isinstance(message.content, list)
        for block in message.content
    )
    assert not any(event.kind == "error" for event in followup)
