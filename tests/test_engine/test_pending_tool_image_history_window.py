"""A failed image handoff remains usable across the ordinary turn window."""

from __future__ import annotations

import base64
import io
import random
from collections.abc import AsyncIterator
from typing import Any

import pytest
from PIL import Image

from opensquilla.contracts.image_preparation import MAX_TOOL_IMAGE_BASE64_CHARS
from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.agent import (
    _active_user_message_index_for_request,
    _pending_tool_image_window_context,
    _serialize_assistant_replay,
)
from opensquilla.engine.history import limit_turns, reconstruct_messages_from_entry
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import DoneEvent
from opensquilla.provider import ChatConfig, Message
from opensquilla.provider import DoneEvent as ProviderDoneEvent
from opensquilla.provider import TextDeltaEvent as ProviderTextDeltaEvent
from opensquilla.provider.types import (
    ContentBlockImage,
    ContentBlockToolResult,
    ContentBlockToolUse,
)
from tests.helpers.image_bytes import image_bytes


class _CapturingProvider:
    provider_name = "synthetic"

    def __init__(self) -> None:
        self.messages: list[Message] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,  # noqa: ARG002
        config: ChatConfig | None = None,  # noqa: ARG002
    ) -> AsyncIterator[Any]:
        self.messages = messages
        return self._answer()

    async def _answer(self) -> AsyncIterator[Any]:
        yield ProviderTextDeltaEvent(text="Frame analyzed.")
        yield ProviderDoneEvent(stop_reason="end_turn", input_tokens=3, output_tokens=2)

    async def list_models(self) -> list[Any]:
        return []


def test_request_only_image_context_is_not_the_active_user_turn() -> None:
    image = ContentBlockImage(
        media_type="image/png", data=base64.b64encode(image_bytes()).decode("ascii")
    )
    tool_result = Message(
        role="user",
        content=[ContentBlockToolResult(tool_use_id="view-1", content="loaded"), image],
    )
    context = _pending_tool_image_window_context([tool_result])
    assert context is not None
    request = [Message(role="user", content="Inspect this frame."), context]

    assert _active_user_message_index_for_request(request) == 0
    assert limit_turns(request, max_turns=1) == request


@pytest.mark.asyncio
async def test_pending_tool_image_survives_one_failed_user_turn_with_tiny_window() -> None:
    encoded = base64.b64encode(image_bytes()).decode("ascii")
    replay = _serialize_assistant_replay(
        [
            Message(
                role="assistant",
                content=[ContentBlockToolUse(id="view-1", name="observe", input={})],
            ),
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(tool_use_id="view-1", content="frame loaded"),
                    ContentBlockImage(media_type="image/png", data=encoded),
                ],
            ),
        ]
    )
    history = [Message(role="user", content="Inspect this frame.")]
    history.extend(
        reconstruct_messages_from_entry("assistant", "", None, assistant_replay=replay)
    )
    history.append(Message(role="user", content="Please try again after the failed request."))
    provider = _CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            max_history_turns=1,
            model_vision_support="supported",
            preserve_historical_images=True,
        ),
    )
    agent.set_history(history)

    events = [event async for event in agent.run_turn("Continue analyzing that frame.")]

    assert any(isinstance(event, DoneEvent) for event in events)
    images = [
        block
        for message in provider.messages
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockImage)
    ]
    assert [image.data for image in images] == [encoded]
    assert all(
        not message._request_only_pending_tool_image
        for message in agent.history_snapshot()
    )


def test_nested_tool_images_drop_unverified_retained_path() -> None:
    image = ContentBlockImage(
        media_type="image/png",
        data=base64.b64encode(image_bytes()).decode("ascii"),
        name="frame.png",
        local_path=".opensquilla/attachments/other/123456789abc-frame.png",
        durable_retained=True,
    )
    message = Message(
        role="user",
        content=[
            ContentBlockToolResult(
                tool_use_id="view-1",
                content=[image, {"type": "image", "local_path": "stale.png",
                                  "durable_retained": True, "data": image.data,
                                  "media_type": "image/png"}],
            )
        ],
    )

    TurnRunner._restore_retained_history_image_paths(
        [message], materializer=None, session_id="current"
    )

    assert isinstance(message.content, list)
    result = message.content[0]
    assert isinstance(result, ContentBlockToolResult)
    assert isinstance(result.content, list)
    native_image = result.content[0]
    assert isinstance(native_image, ContentBlockImage)
    assert native_image.local_path is None
    assert native_image.durable_retained is None
    mapped_image = result.content[1]
    assert isinstance(mapped_image, dict)
    assert "local_path" not in mapped_image
    assert "durable_retained" not in mapped_image


@pytest.mark.asyncio
@pytest.mark.parametrize("max_history_turns", [1, 20])
@pytest.mark.parametrize("image_count", [6, 12])
async def test_pending_batch_caps_frames_and_describes_omissions(
    max_history_turns: int,
    image_count: int,
) -> None:
    images = [
        ContentBlockImage(
            media_type="image/png",
            data=base64.b64encode(
                image_bytes(color=f"#{index:02x}{index:02x}{index:02x}")
            ).decode("ascii"),
            attachment_id=f"frame-{index}",
        )
        for index in range(image_count)
    ]
    replay = _serialize_assistant_replay(
        [
            Message(
                role="assistant",
                content=[ContentBlockToolUse(id="view-1", name="observe", input={})],
            ),
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(tool_use_id="view-1", content="frames loaded"),
                    *images,
                ],
            ),
        ]
    )
    history = [Message(role="user", content="Inspect the video frames.")]
    history.extend(
        reconstruct_messages_from_entry("assistant", "", None, assistant_replay=replay)
    )
    history.append(Message(role="user", content="Try again after the failure."))
    provider = _CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            max_history_turns=max_history_turns,
            model_vision_support="supported",
            preserve_historical_images=True,
        ),
    )
    agent.set_history(history)

    events = [event async for event in agent.run_turn("Continue.")]
    assert any(isinstance(event, DoneEvent) for event in events)

    request_images = [
        block
        for message in provider.messages
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockImage)
    ]
    assert [image.data for image in request_images] == [
        image.data for image in images[-4:]
    ]
    assert any(
        f"{image_count - 4} images omitted by recency or size limits" in block.text
        for message in provider.messages
        if isinstance(message.content, list)
        for block in message.content
        if hasattr(block, "text")
    )
    assert all(
        not message._request_only_pending_tool_image
        for message in agent.history_snapshot()
    )
    if max_history_turns == 20:
        assert sum(
            isinstance(block, ContentBlockImage)
            for message in agent.history_snapshot()
            if isinstance(message.content, list)
            for block in message.content
        ) == image_count


@pytest.mark.asyncio
async def test_legacy_oversized_pending_image_is_reprepared_only_for_request() -> None:
    pixels = random.Random(7).randbytes(1300 * 1300 * 3)
    output = io.BytesIO()
    Image.frombytes("RGB", (1300, 1300), pixels).save(output, format="PNG")
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    assert len(encoded) > MAX_TOOL_IMAGE_BASE64_CHARS
    replay = _serialize_assistant_replay([
        Message(
            role="assistant",
            content=[ContentBlockToolUse(id="view-1", name="observe", input={})],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(tool_use_id="view-1", content="large frame loaded"),
                ContentBlockImage(media_type="image/png", data=encoded),
            ],
        ),
    ])
    history = [Message(role="user", content="Inspect the frame.")]
    history.extend(reconstruct_messages_from_entry(
        "assistant", "", None, assistant_replay=replay,
    ))
    provider = _CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            max_history_turns=20,
            model_vision_support="supported",
            preserve_historical_images=True,
        ),
    )
    agent.set_history(history)

    events = [event async for event in agent.run_turn("Continue.")]

    assert any(isinstance(event, DoneEvent) for event in events)
    request_images = [
        block
        for message in provider.messages
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockImage)
    ]
    assert len(request_images) == 1
    assert len(request_images[0].data) <= MAX_TOOL_IMAGE_BASE64_CHARS
    assert request_images[0].data != encoded
    assert any(
        isinstance(block, ContentBlockImage) and block.data == encoded
        for message in agent.history_snapshot()
        if isinstance(message.content, list)
        for block in message.content
    )
