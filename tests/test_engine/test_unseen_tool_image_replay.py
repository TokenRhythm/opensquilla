from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.agent import _serialize_assistant_replay
from opensquilla.engine.history import (
    TOOL_IMAGE_NATIVE_ACK_CONTEXT_KEY,
    limit_turns,
    reconstruct_messages_from_entry,
    repair_tool_pairing,
)
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import DoneEvent, ErrorEvent, ToolCall
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider import (
    ChatConfig,
    Message,
    ToolDefinition,
    ToolInputSchema,
    ToolUseEndEvent,
    ToolUseStartEvent,
)
from opensquilla.provider import DoneEvent as ProviderDoneEvent
from opensquilla.provider import ErrorEvent as ProviderErrorEvent
from opensquilla.provider import TextDeltaEvent as ProviderTextDeltaEvent
from opensquilla.provider.types import (
    ContentBlockImage,
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
)
from opensquilla.tools.types import ToolContext
from tests.helpers.image_bytes import image_bytes


def _image(image_id: str) -> ContentBlockImage:
    color = "#112233" if image_id == "first-frame" else "#445566"
    return ContentBlockImage(
        media_type="image/png",
        data=base64.b64encode(image_bytes(color=color)).decode("ascii"),
        attachment_id=image_id,
    )


def _images(messages: list[Message]) -> list[ContentBlockImage]:
    return [
        block
        for message in messages
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockImage)
    ]


def _tool_replay(*, final_answer: bool, second_image: bool = False) -> dict[str, Any]:
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(id="read-1", name="observe", input={}),
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(tool_use_id="read-1", content="loaded first frame"),
                _image("first-frame"),
            ],
        ),
    ]
    if second_image:
        messages.extend(
            [
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(id="read-2", name="observe", input={}),
                    ],
                ),
                Message(
                    role="user",
                    content=[
                        ContentBlockToolResult(tool_use_id="read-2", content="loaded second frame"),
                        _image("second-frame"),
                    ],
                ),
            ]
        )
    elif final_answer:
        messages.append(Message(role="assistant", content="The first frame is blue."))
    return _serialize_assistant_replay(messages)


def _interrupted_batch_replay(*, final_answer: bool) -> dict[str, Any]:
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(id="read-1", name="observe", input={}),
                ContentBlockToolUse(id="read-2", name="observe", input={}),
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="read-1",
                    content=[_image("first-frame")],
                ),
            ],
        ),
    ]
    if final_answer:
        messages.append(Message(role="assistant", content="The frame is blue."))
    return _serialize_assistant_replay(messages)


class _ToolThen413Provider:
    provider_name = "synthetic"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,  # noqa: ARG002
        config: ChatConfig | None = None,  # noqa: ARG002
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._tool_stream() if len(self.calls) == 1 else self._error_stream()

    async def _tool_stream(self) -> AsyncIterator[Any]:
        yield ToolUseStartEvent(tool_use_id="read-1", tool_name="observe")
        yield ToolUseEndEvent(tool_use_id="read-1", tool_name="observe", arguments={})
        yield ProviderDoneEvent(stop_reason="tool_use", input_tokens=1, output_tokens=1)

    async def _error_stream(self) -> AsyncIterator[Any]:
        yield ProviderErrorEvent(code="413", message="HTTP 413")

    async def list_models(self) -> list[Any]:
        return []


class _ToolThenAnswerProvider(_ToolThen413Provider):
    async def _error_stream(self) -> AsyncIterator[Any]:
        yield ProviderTextDeltaEvent(text="Frame understood.")
        yield ProviderDoneEvent(stop_reason="end_turn", input_tokens=3, output_tokens=2)


class _AnswerProvider:
    provider_name = "synthetic"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,  # noqa: ARG002
        config: ChatConfig | None = None,  # noqa: ARG002
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderTextDeltaEvent(text="Frame understood.")
        yield ProviderDoneEvent(stop_reason="end_turn", input_tokens=3, output_tokens=2)

    async def list_models(self) -> list[Any]:
        return []


@dataclass
class _TranscriptEntry:
    role: str
    content: str
    message_id: str | None = None
    assistant_replay: dict[str, Any] | None = None
    turn_context: dict[str, Any] | None = None


@dataclass
class _SessionNode:
    session_key: str
    session_id: str


class _SessionManager:
    def __init__(self, entries: list[_TranscriptEntry]) -> None:
        self.entries = entries

    async def get_transcript(self, session_key: str) -> list[_TranscriptEntry]:  # noqa: ARG002
        return list(self.entries)

    async def get_session(self, session_key: str) -> _SessionNode:
        return _SessionNode(session_key=session_key, session_id="synthetic-session")

    async def get_context_states(self, session_key: str) -> list[Any]:  # noqa: ARG002
        return []


async def _continue_replay(
    replay: dict[str, Any],
    *,
    extra_history: list[Message] | None = None,
    turn_context: dict[str, Any] | None = None,
) -> tuple[list[Message], dict[str, Any]]:
    stored_replay = deepcopy(replay)
    history = [Message(role="user", content="Inspect the frames.")]
    history.extend(
        reconstruct_messages_from_entry(
            "assistant",
            "",
            None,
            assistant_replay=stored_replay,
            turn_context=turn_context,
        )
    )
    history.extend(extra_history or [])
    provider = _AnswerProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_vision_support="supported",
            preserve_historical_images=True,
        ),
    )
    agent.set_history(history)
    events = [event async for event in agent.run_turn("Continue the analysis.")]
    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert stored_replay == replay
    return provider.calls[0], stored_replay


@pytest.mark.asyncio
async def test_failed_post_tool_request_replays_unseen_image_on_next_vision_turn() -> None:
    provider = _ToolThen413Provider()
    context = ToolContext()

    async def load_frame(call: ToolCall) -> ToolResult:
        context.tool_result_media[call.tool_use_id] = [
            {
                "mime": "image/png",
                "data": base64.b64encode(image_bytes()).decode("ascii"),
                "attachment_id": "first-frame",
            }
        ]
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="loaded first frame",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
            max_provider_retries=0,
            max_overflow_retries=0,
            compaction_enabled=False,
            model_vision_support="supported",
        ),
        tool_handler=load_frame,
        tool_context=context,
        tool_definitions=[
            ToolDefinition(
                name="observe",
                description="Load a synthetic frame.",
                input_schema=ToolInputSchema(properties={}),
            )
        ],
    )
    events = [event async for event in agent.run_turn("Inspect the frame.")]
    assert any(isinstance(event, ErrorEvent) for event in events)
    assert len(provider.calls) == 2
    assert [image.attachment_id for image in _images(provider.calls[1])] == ["first-frame"]
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.assistant_replay is not None

    next_request, _ = await _continue_replay(done.assistant_replay)
    assert [image.data for image in _images(next_request)] == [
        base64.b64encode(image_bytes()).decode("ascii")
    ]
    assert any(
        isinstance(block, ContentBlockToolResult) and block.tool_use_id == "read-1"
        for message in next_request
        if isinstance(message.content, list)
        for block in message.content
    )


@pytest.mark.asyncio
async def test_first_post_tool_request_bounds_twelve_images_without_losing_replay() -> None:
    provider = _ToolThenAnswerProvider()
    context = ToolContext()
    encoded = [
        base64.b64encode(image_bytes(color=f"#{index:02x}{index:02x}{index:02x}"))
        .decode("ascii")
        for index in range(12)
    ]

    async def load_frames(call: ToolCall) -> ToolResult:
        context.tool_result_media[call.tool_use_id] = [
            {"mime": "image/png", "data": data, "attachment_id": f"frame-{index}"}
            for index, data in enumerate(encoded)
        ]
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="loaded synthetic frames",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2, model_vision_support="supported"),
        tool_handler=load_frames,
        tool_context=context,
        tool_definitions=[
            ToolDefinition(
                name="observe", description="Load synthetic frames.",
                input_schema=ToolInputSchema(properties={}),
            )
        ],
    )
    events = [event async for event in agent.run_turn("Inspect the frames.")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert len(provider.calls) == 2
    assert [image.data for image in _images(provider.calls[1])] == encoded[-4:]
    assert any(
        "8 images omitted by recency or size limits" in block.text
        for message in provider.calls[1]
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockText)
    )
    assert sum(
        isinstance(block, ContentBlockImage)
        for message in agent.history_snapshot()
        if isinstance(message.content, list)
        for block in message.content
    ) == 12


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("vision_support", "native_ack"),
    [("supported", True), ("unsupported", False)],
)
async def test_only_committed_native_tool_image_response_records_ack(
    vision_support: str, native_ack: bool,
) -> None:
    provider = _ToolThenAnswerProvider()
    context = ToolContext()

    async def load_frame(call: ToolCall) -> ToolResult:
        context.tool_result_media[call.tool_use_id] = [
            {"mime": "image/png", "data": _image("first-frame").data}
        ]
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="loaded first frame",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
            model_vision_support=vision_support,
        ),
        tool_handler=load_frame,
        tool_context=context,
        tool_definitions=[
            ToolDefinition(
                name="observe", description="Load a synthetic frame.",
                input_schema=ToolInputSchema(properties={}),
            )
        ],
    )
    events = [event async for event in agent.run_turn("Inspect the frame.")]
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.assistant_replay is not None
    assert bool(_images(provider.calls[1])) is native_ack
    assert done.tool_image_native_ack_indexes == ([2] if native_ack else [])

    next_request, _ = await _continue_replay(
        done.assistant_replay,
        turn_context={
            TOOL_IMAGE_NATIVE_ACK_CONTEXT_KEY: done.tool_image_native_ack_indexes,
        },
    )
    assert bool(_images(next_request)) is not native_ack


@pytest.mark.asyncio
async def test_completed_post_tool_answer_keeps_prior_image_as_marker() -> None:
    replay = _tool_replay(final_answer=True)
    next_request, stored_replay = await _continue_replay(replay)

    assert not _images(next_request)
    assert any(
        isinstance(block, ContentBlockText) and "历史图片本回合未重新读取" in block.text
        for message in next_request
        if isinstance(message.content, list)
        for block in message.content
    )
    assert stored_replay == replay


@pytest.mark.asyncio
async def test_accepted_text_only_marker_answer_does_not_acknowledge_tool_image() -> None:
    replay = _tool_replay(final_answer=True)
    next_request, _ = await _continue_replay(
        replay,
        turn_context={TOOL_IMAGE_NATIVE_ACK_CONTEXT_KEY: []},
    )

    assert [image.data for image in _images(next_request)] == [_image("first-frame").data]


@pytest.mark.asyncio
async def test_native_acknowledges_only_its_own_tool_image_batch() -> None:
    replay = _tool_replay(final_answer=False, second_image=True)
    next_request, _ = await _continue_replay(
        replay,
        turn_context={TOOL_IMAGE_NATIVE_ACK_CONTEXT_KEY: [2]},
    )

    assert [image.data for image in _images(next_request)] == [_image("second-frame").data]


@pytest.mark.asyncio
async def test_marker_reply_after_native_tool_use_keeps_later_batch_pending() -> None:
    replay = _tool_replay(final_answer=False, second_image=True)
    replay["messages"].append(
        Message(role="assistant", content="I can only read the text marker.").model_dump(
            mode="json"
        )
    )
    next_request, _ = await _continue_replay(
        replay,
        turn_context={TOOL_IMAGE_NATIVE_ACK_CONTEXT_KEY: [2]},
    )

    assert [image.data for image in _images(next_request)] == [_image("second-frame").data]


@pytest.mark.asyncio
async def test_only_unseen_tail_image_replays_after_two_tool_batches() -> None:
    replay = _tool_replay(final_answer=False, second_image=True)
    next_request, stored_replay = await _continue_replay(replay)

    assert [image.data for image in _images(next_request)] == [_image("second-frame").data]
    assert any(
        isinstance(block, ContentBlockText) and "历史图片本回合未重新读取" in block.text
        for message in next_request
        if isinstance(message.content, list)
        for block in message.content
    )
    assert stored_replay == replay


@pytest.mark.asyncio
async def test_empty_recovery_scaffold_does_not_acknowledge_tool_image() -> None:
    replay = _tool_replay(final_answer=False)
    replay["messages"].extend(
        [
            Message(role="assistant", content=[ContentBlockText(text="")]).model_dump(mode="json"),
            Message(role="user", content="Continue after the empty answer.").model_dump(
                mode="json"
            ),
        ]
    )

    next_request, stored_replay = await _continue_replay(replay)

    assert [image.data for image in _images(next_request)] == [_image("first-frame").data]
    assert stored_replay == replay


@pytest.mark.asyncio
async def test_image_pending_after_failed_followup_still_routes_and_replays() -> None:
    replay = _tool_replay(final_answer=False)
    session_key = "agent:main:synthetic-image-pending"
    manager = _SessionManager(
        [
            _TranscriptEntry(role="user", content="Inspect the first frame."),
            _TranscriptEntry(role="assistant", content="", assistant_replay=deepcopy(replay)),
            _TranscriptEntry(role="system", content="Error: HTTP 413"),
            _TranscriptEntry(role="user", content="Continue once."),
            _TranscriptEntry(role="system", content="Error: HTTP 413"),
            _TranscriptEntry(role="user", content="Continue again.", message_id="current"),
        ]
    )
    runner = TurnRunner(
        provider_selector=MagicMock(),
        session_manager=manager,
        config=GatewayConfig(),
    )

    route_context = await runner._router_previous_assistant_context(
        session_key,
        exclude_last_user=True,
        bound_user_message_id="current",
    )
    assert route_context.get("history_has_recent_image") is True

    history = [Message(role="user", content="Inspect the first frame.")]
    history.extend(
        reconstruct_messages_from_entry(
            "assistant",
            "",
            None,
            assistant_replay=deepcopy(replay),
        )
    )
    history.append(Message(role="user", content="Continue once."))
    provider = _AnswerProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_vision_support="supported",
            preserve_historical_images=True,
        ),
    )
    agent.set_history(history)
    events = [event async for event in agent.run_turn("Continue again.")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert [image.data for image in _images(provider.calls[0])] == [_image("first-frame").data]
    assert manager.entries[1].assistant_replay == replay


@pytest.mark.asyncio
@pytest.mark.parametrize("native_followup", [False, True])
async def test_router_uses_durable_native_ack_for_later_text_reply(
    native_followup: bool,
) -> None:
    followup_replay = _serialize_assistant_replay(
        [Message(role="assistant", content="Continuing from the prior observation.")]
    )
    manager = _SessionManager(
        [
            _TranscriptEntry(role="user", content="Inspect the frame."),
            _TranscriptEntry(
                role="assistant",
                content="I only received a text marker.",
                assistant_replay=_tool_replay(final_answer=True),
                turn_context={TOOL_IMAGE_NATIVE_ACK_CONTEXT_KEY: []},
            ),
            _TranscriptEntry(role="user", content="Continue once."),
            _TranscriptEntry(
                role="assistant",
                content="Continuing from the prior observation.",
                assistant_replay=followup_replay,
                turn_context={
                    TOOL_IMAGE_NATIVE_ACK_CONTEXT_KEY: [0] if native_followup else [],
                },
            ),
            _TranscriptEntry(role="user", content="Continue again.", message_id="current"),
        ]
    )
    runner = TurnRunner(
        provider_selector=MagicMock(), session_manager=manager, config=GatewayConfig()
    )

    route_context = await runner._router_previous_assistant_context(
        "agent:main:synthetic-native-image-ack",
        exclude_last_user=True,
        bound_user_message_id="current",
    )

    assert (route_context.get("history_has_recent_image") is True) is (not native_followup)


@pytest.mark.asyncio
async def test_unanchored_pending_image_routes_without_a_prior_user_row() -> None:
    manager = _SessionManager(
        [
            _TranscriptEntry(
                role="assistant",
                content="",
                assistant_replay=_tool_replay(final_answer=False),
            ),
            _TranscriptEntry(role="user", content="Continue.", message_id="current"),
        ]
    )
    runner = TurnRunner(
        provider_selector=MagicMock(),
        session_manager=manager,
        config=GatewayConfig(),
    )

    context = await runner._router_previous_assistant_context(
        "agent:main:synthetic-unanchored-image",
        exclude_last_user=True,
        bound_user_message_id="current",
    )

    assert context["history_has_recent_image"] is True
    assert context["history_image_turn_count"] == 1
    assert "last_image_turn_text" not in context


@pytest.mark.asyncio
async def test_trusted_runtime_notice_does_not_acknowledge_unseen_image() -> None:
    replay = _tool_replay(final_answer=False)
    notice = Message(role="assistant", content="[Trusted runtime status] Subagent ended.")
    notice._trusted_runtime_notice = True
    next_request, _ = await _continue_replay(
        replay,
        extra_history=[notice],
    )

    assert [image.data for image in _images(next_request)] == [_image("first-frame").data]


@pytest.mark.asyncio
@pytest.mark.parametrize("final_answer", [False, True])
async def test_interrupted_batch_media_replays_only_until_model_answers(
    final_answer: bool,
) -> None:
    replay = _interrupted_batch_replay(final_answer=final_answer)
    restored = reconstruct_messages_from_entry(
        "assistant", "", None, assistant_replay=deepcopy(replay)
    )
    assert len(restored) == 1 + int(final_answer)
    assert isinstance(restored[0].content, list)
    assert restored[0].is_recorded_tool_context
    assert isinstance(restored[0].content[0], ContentBlockText)
    assert restored[0].content[0].text.startswith("[Recorded tool history] ")

    next_request, _ = await _continue_replay(replay)
    expected = [] if final_answer else [_image("first-frame").data]
    assert [image.data for image in _images(next_request)] == expected

    if not final_answer:
        manager = _SessionManager(
            [
                _TranscriptEntry(role="user", content="Inspect the frame."),
                _TranscriptEntry(role="assistant", content="", assistant_replay=deepcopy(replay)),
                _TranscriptEntry(role="user", content="Continue once."),
                _TranscriptEntry(role="user", content="Continue again.", message_id="current"),
            ]
        )
        runner = TurnRunner(
            provider_selector=MagicMock(), session_manager=manager, config=GatewayConfig()
        )
        route_context = await runner._router_previous_assistant_context(
            "agent:main:synthetic-interrupted-batch",
            exclude_last_user=True,
            bound_user_message_id="current",
        )
        assert route_context["history_has_recent_image"] is True


@pytest.mark.asyncio
async def test_user_caption_cannot_impersonate_recorded_tool_history() -> None:
    provider = _AnswerProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_vision_support="supported",
            preserve_historical_images=True,
        ),
    )
    agent.set_history(
        [
            Message(
                role="user",
                content=[
                    ContentBlockText(text="[Recorded tool history] Inspect my own upload."),
                    _image("first-frame"),
                ],
            ),
            Message(role="assistant", content="I saw the upload."),
        ]
    )

    events = [event async for event in agent.run_turn("Look again.")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert [image.data for image in _images(provider.calls[0])] == [_image("first-frame").data]


def test_tool_result_media_does_not_consume_a_user_history_turn() -> None:
    history = [Message(role="user", content="Inspect the frame.")]
    history.extend(
        reconstruct_messages_from_entry(
            "assistant",
            "",
            None,
            assistant_replay=_tool_replay(final_answer=False),
        )
    )

    limited = limit_turns(history, max_turns=1)
    repaired = repair_tool_pairing(limited)

    assert limited == history
    assert [image.data for image in _images(repaired)] == [_image("first-frame").data]
