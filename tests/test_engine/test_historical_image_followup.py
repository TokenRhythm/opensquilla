from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from opensquilla.attachment_refs import write_transcript_material
from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.runtime import TurnRunner, _SelectorFallbackProvider
from opensquilla.engine.steps.squilla_router import apply_squilla_router
from opensquilla.engine.steps.vision_followup_gate import apply_vision_followup_gate
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    ModelCapabilities,
    TextDeltaEvent,
)
from opensquilla.provider.types import ContentBlockImage, ContentBlockText
from opensquilla.session.attachment_manifest import (
    ATTACHMENT_MANIFEST_PROVIDER,
    ATTACHMENT_MANIFEST_STATE_KIND,
    attachment_manifest_from_context_state,
    build_attachment_manifest,
    manifest_context_state,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from tests.helpers.image_bytes import image_bytes


@dataclass
class _TranscriptEntry:
    role: str
    content: str
    message_id: str | None = None
    tool_calls: list[Any] | None = None
    reasoning_content: str | None = None
    token_count: int | None = None


@dataclass
class _SessionNode:
    session_key: str
    session_id: str


class _FakeSessionManager:
    def __init__(self) -> None:
        self._nodes: dict[str, _SessionNode] = {}
        self._transcripts: dict[str, list[_TranscriptEntry]] = {}

    async def create(self, session_key: str) -> _SessionNode:
        node = _SessionNode(session_key=session_key, session_id=f"id-{len(self._nodes) + 1}")
        self._nodes[session_key] = node
        self._transcripts.setdefault(session_key, [])
        return node

    async def append_message(
        self,
        session_key: str,
        role: str,
        content: str,
        message_id: str | None = None,
    ) -> _TranscriptEntry:
        entry = _TranscriptEntry(role=role, content=content, message_id=message_id)
        self._transcripts.setdefault(session_key, []).append(entry)
        return entry

    async def get_transcript(self, session_key: str) -> list[_TranscriptEntry]:
        return list(self._transcripts.get(session_key, []))

    async def get_session(self, session_key: str) -> _SessionNode | None:
        return self._nodes.get(session_key)

    async def get_context_states(self, session_key: str) -> list[Any]:  # noqa: ARG002
        return []


class _CanonicalSessionManager(_FakeSessionManager):
    def __init__(self) -> None:
        super().__init__()
        self._canonical: dict[str, list[_TranscriptEntry]] = {}

    async def get_canonical_transcript(
        self,
        session_key: str,
    ) -> list[_TranscriptEntry]:
        return list(self._canonical.get(session_key, self._transcripts.get(session_key, [])))


class _CapturingProvider:
    provider_name = "fake"

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
        yield TextDeltaEvent(text="ok")
        yield DoneEvent(stop_reason="end_turn", input_tokens=3, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _GateThenCaptureProvider(_CapturingProvider):
    def __init__(self, gate_payload: str) -> None:
        super().__init__()
        self.gate_payload = gate_payload
        self.gate_calls = 0

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        if (
            config is not None
            and isinstance(config.system, str)
            and "requires reusing a previous image" in config.system
        ):
            self.gate_calls += 1
            return self._gate_stream()
        return super().chat(messages, tools=tools, config=config)

    async def _gate_stream(self) -> AsyncIterator[Any]:
        yield TextDeltaEvent(text=self.gate_payload)
        yield DoneEvent(stop_reason="end_turn", input_tokens=9, output_tokens=5)


def _b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def _inline_image_envelope(text: str, payload: bytes = image_bytes()) -> str:
    return json.dumps(
        {
            "text": text,
            "attachments": [
                {
                    "type": "image/png",
                    "name": "first.png",
                    "data": _b64(payload),
                }
            ],
        }
    )


def _inline_image_envelope_many(text: str, *payloads: bytes) -> str:
    return json.dumps(
        {
            "text": text,
            "attachments": [
                {
                    "type": "image/png",
                    "name": f"image-{index}.png",
                    "data": _b64(payload),
                }
                for index, payload in enumerate(payloads)
            ],
        }
    )


def _message_has_image(message: Message) -> bool:
    return isinstance(message.content, list) and any(
        isinstance(block, ContentBlockImage) for block in message.content
    )


def _message_has_marker(message: Message, marker: str) -> bool:
    return isinstance(message.content, list) and any(
        isinstance(block, ContentBlockText) and marker in block.text
        for block in message.content
    ) or isinstance(message.content, str) and marker in message.content


@pytest.mark.parametrize("image_source", ["active", "archive", "bound", "agent_history"])
async def test_text_primary_fallback_recovers_selected_historical_original(
    image_source: str,
) -> None:
    manager = _CanonicalSessionManager()
    key = "agent:main:historical-selector-fallback"
    await manager.create(key)
    image_entry = _TranscriptEntry(
        "user",
        _inline_image_envelope("Describe this image.", image_bytes(color="#000001")),
        "image-source",
    )
    current = _TranscriptEntry("user", "Use the previous image.", "current")
    manager._canonical[key] = [image_entry, current]
    manager._transcripts[key] = (
        [current] if image_source == "archive" else [image_entry, current]
    )
    primary_config = SimpleNamespace(provider="openai", model="configured-text")
    fallback_config = SimpleNamespace(provider="openai", model="configured-vision")

    class _TextPrimary(_CapturingProvider):
        async def _stream(self):
            yield ErrorEvent(code="503", message="Provider unavailable")

    primary = _TextPrimary()
    fallback = _CapturingProvider()

    class _Selector:
        current_config = primary_config

        def next_fallback_after_failure(self, _error):
            self.current_config = fallback_config
            return fallback

    wrapper = _SelectorFallbackProvider(primary, _Selector())
    wrapper.configure_fallback_deployment_vision_support([(fallback_config, "supported")])
    wrapper.configure_fallback_deployment_limits([
        (fallback_config, 0, 0, ModelCapabilities(supports_vision=True))
    ])
    agent = Agent(
        provider=wrapper,
        config=AgentConfig(
            model_id="configured-text",
            model_vision_support="unsupported",
            preserve_historical_images=image_source != "bound",
            metadata={"attachment_count": 0},
            max_provider_retries=0,
        ),
    )
    if image_source == "agent_history":
        agent.set_history([
            Message(role="user", content=[
                ContentBlockImage(media_type="image/png", data=_b64(image_bytes(color="#000001")))
            ])
        ])
    else:
        runner = TurnRunner(
            provider_selector=MagicMock(), session_manager=manager,
            config=GatewayConfig(llm={"provider": "openai"}),
        )
        await runner._load_history(
            agent, key,
            bound_user_message_id="image-source" if image_source == "bound" else "current",
        )

    events = [event async for event in agent.run_turn(current.content)]

    assert not any(event.kind == "error" for event in events)
    assert len(primary.calls) == len(fallback.calls) == 1
    assert not any(_message_has_image(message) for message in primary.calls[0]["messages"])
    assert [
        block.data
        for message in fallback.calls[0]["messages"]
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockImage)
    ] == [_b64(image_bytes(color="#000001"))]


@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("opt_out", [False, True])
async def test_current_upload_and_previous_image_selection_are_independent(
    archived: bool, opt_out: bool,
) -> None:
    from opensquilla.engine.turn_runner.agent_bootstrap_stage import _preserve_historical_images

    manager = _CanonicalSessionManager()
    key = "agent:main:compare-current-and-previous"
    await manager.create(key)
    previous = _TranscriptEntry(
        "user", _inline_image_envelope("Previous upload.", image_bytes(color="#000002")), "previous"
    )
    text = (
        "Ignore the previous image; describe the new upload."
        if opt_out else "Compare the new upload with the previous image."
    )
    current = _TranscriptEntry(
        "user", _inline_image_envelope(text, image_bytes(color="#000003")), "current"
    )
    manager._canonical[key] = [previous, current]
    manager._transcripts[key] = [current] if archived else [previous, current]
    config = GatewayConfig(llm={"provider": "openai"})
    metadata: dict[str, Any] = {
        "attachment_count": 1,
        "image_attachment_ids": ["att_current"],
        "router_history_has_recent_image": True,
        "router_turns_since_last_image": 1,
    }
    if opt_out:
        metadata["image_intent_attachment_ids"] = ["att_previous"]
        metadata["image_route_reason"] = "gate_history"
    ctx = TurnContext(
        message=text, raw_message=text, session_key=key,
        model="configured-vision", config=config,
        attachments=[{"mime": "image/png", "data": _b64(image_bytes(color="#000003"))}],
        provider=_CapturingProvider(), tool_defs=[], system_prompt="", metadata=metadata,
    )
    await apply_vision_followup_gate(ctx)
    assert _preserve_historical_images(ctx.metadata) is not opt_out
    provider = _CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            model_vision_support="supported", metadata=ctx.metadata,
            # The explicit veto must beat an older bootstrap/config flag too.
            preserve_historical_images=True,
        ),
    )
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager, config=config)
    await runner._load_history(agent, key, bound_user_message_id="current")
    current_message = Message(role="user", content=[
        ContentBlockImage(
            media_type="image/png", data=_b64(image_bytes(color="#000003")),
            attachment_id="att_current",
        ),
    ])
    events = [event async for event in agent.run_turn(text, extra_messages=[current_message])]

    assert not any(event.kind == "error" for event in events)
    payloads = [
        block.data for message in provider.calls[0]["messages"]
        if isinstance(message.content, list) for block in message.content
        if isinstance(block, ContentBlockImage)
    ]
    assert payloads == (
        [_b64(image_bytes(color="#000003"))]
        if opt_out else [_b64(image_bytes(color="#000002")), _b64(image_bytes(color="#000003"))]
    )


@pytest.mark.parametrize("gate_enabled", [False, True])
@pytest.mark.parametrize("current_upload", [False, True])
async def test_current_text_image_opt_out_reaches_provider_when_gate_disabled(
    gate_enabled: bool, current_upload: bool,
) -> None:
    from opensquilla.engine.turn_runner.agent_bootstrap_stage import _preserve_historical_images

    manager = _CanonicalSessionManager()
    key = "agent:main:image-reference-opt-out"
    node = await manager.create(key)
    previous = _TranscriptEntry(
        "user", _inline_image_envelope("Previous upload.", image_bytes(color="#000002")), "previous"
    )
    previous_id = build_attachment_manifest(
        [previous], session_id=node.session_id, session_key=key,
    ).occurrences[0].attachment_id
    text = f"Ignore the previous image {previous_id}; answer only the text question."
    current = _TranscriptEntry(
        "user",
        _inline_image_envelope(text, image_bytes(color="#000003")) if current_upload else text,
        "current",
    )
    manager._canonical[key] = [previous, current]
    manager._transcripts[key] = [previous, current]
    config = GatewayConfig(llm={"provider": "openai"})
    config.squilla_router.vision_followup_gate_enabled = gate_enabled
    metadata: dict[str, Any] = {
        "attachment_count": int(current_upload),
        "image_intent_attachment_ids": [previous_id],
        "router_vision_followup_needs_image": True,
        "router_vision_followup_gate_source": "explicit_attachment_id",
    }
    attachments = (
        [{"mime": "image/png", "data": _b64(image_bytes(color="#000003"))}]
        if current_upload else []
    )
    provider = _CapturingProvider()
    ctx = TurnContext(
        message=text, raw_message=text, session_key=key, config=config,
        model="configured-vision", provider=provider, tool_defs=[],
        system_prompt="", attachments=attachments, metadata=metadata,
    )
    await apply_vision_followup_gate(ctx)
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            model_vision_support="supported", metadata=ctx.metadata,
            preserve_historical_images=_preserve_historical_images(ctx.metadata),
        ),
    )
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager, config=config)
    await runner._load_history(agent, key, bound_user_message_id="current")
    extra_messages = (
        [Message(role="user", content=[
            ContentBlockImage(media_type="image/png", data=_b64(image_bytes(color="#000003")))
        ])]
        if current_upload else None
    )
    events = [event async for event in agent.run_turn(text, extra_messages=extra_messages)]

    assert not any(event.kind == "error" for event in events)
    payloads = [
        block.data for message in provider.calls[0]["messages"]
        if isinstance(message.content, list) for block in message.content
        if isinstance(block, ContentBlockImage)
    ]
    assert payloads == ([_b64(image_bytes(color="#000003"))] if current_upload else [])
    assert ctx.metadata["router_vision_followup_gate_source"] == "explicit_opt_out"
    assert ctx.metadata["router_vision_followup_needs_image"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("vision_support", "expects_image"),
    [("supported", True), ("unknown", True), ("unsupported", False)],
)
async def test_bound_image_message_reprojects_after_model_switch(
    vision_support: str,
    expects_image: bool,
) -> None:
    manager = _FakeSessionManager()
    key = f"agent:main:bound-image-switch-{vision_support}"
    config = GatewayConfig(llm={"provider": "openrouter"})
    config.squilla_router.vision_history_lookback_turns = 0
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager, config=config)
    await manager.create(key)
    envelope = _inline_image_envelope("Describe this image.")
    await manager.append_message(
        key,
        "user",
        envelope,
        message_id="bound-image-message",
    )

    provider = _CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_id=f"configured-{vision_support}",
            model_vision_support=vision_support,
            metadata={"attachment_count": 0},
        ),
    )
    await runner._load_history(
        agent,
        key,
        bound_user_message_id="bound-image-message",
    )
    events = [event async for event in agent.run_turn("Describe this image.")]

    assert any(event.kind == "done" for event in events)
    sent = provider.calls[0]["messages"]
    assert any(_message_has_image(message) for message in sent) is expects_image
    assert any(_message_has_marker(message, "图片") for message in sent) is (
        not expects_image
    )
    assert any(
        _message_has_marker(message, "Image replay context for this request")
        for message in sent
    )
    assert manager._transcripts[key][0].content == envelope


@pytest.mark.asyncio
@pytest.mark.parametrize("vision_support", ["supported", "unknown", "unsupported"])
async def test_bound_plain_text_message_does_not_replay_unrelated_archived_image(
    vision_support: str,
) -> None:
    manager = _CanonicalSessionManager()
    key = f"agent:main:bound-plain-text-{vision_support}"
    config = GatewayConfig(llm={"provider": "openrouter"})
    config.squilla_router.vision_history_lookback_turns = 3
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager, config=config)
    await manager.create(key)
    archived_image = _TranscriptEntry(
        role="user",
        content=_inline_image_envelope("Archived unrelated image."),
        message_id="archived-image-message",
    )
    archived_answer = _TranscriptEntry(
        role="assistant",
        content="Archived answer.",
        message_id="archived-answer-message",
    )
    current_user = _TranscriptEntry(
        role="user",
        content="Answer this unrelated text question.",
        message_id="bound-plain-text-message",
    )
    manager._transcripts[key] = [current_user]
    manager._canonical[key] = [archived_image, archived_answer, current_user]

    provider = _CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_id=f"configured-{vision_support}",
            model_vision_support=vision_support,
            metadata={"attachment_count": 0},
        ),
    )
    await runner._load_history(
        agent,
        key,
        bound_user_message_id="bound-plain-text-message",
    )
    events = [event async for event in agent.run_turn(current_user.content)]

    assert any(event.kind == "done" for event in events)
    sent = provider.calls[0]["messages"]
    assert not any(_message_has_image(message) for message in sent)
    assert "Archived unrelated image." not in str(sent)
    assert "historical attachment omitted" not in str(sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("vision_support", ["supported", "unknown", "unsupported"])
async def test_explicit_images_survive_a_full_history_window(
    archived: bool,
    vision_support: str,
) -> None:
    manager = _CanonicalSessionManager()
    key = "agent:main:referenced-image-window"
    config = GatewayConfig(llm={"provider": "openrouter"})
    config.squilla_router.vision_history_lookback_turns = 0
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager, config=config)
    node = await manager.create(key)
    image_entries = [
        _TranscriptEntry(
            role="user",
            content=_inline_image_envelope(
                f"Old instruction {index}.", image_bytes(color=f"#{index:06x}"),
            ),
            message_id=f"historical-image-{index}",
        )
        for index in range(3)
    ]
    tail = [
        _TranscriptEntry(role="user", content="Recent question.", message_id="recent-user"),
        _TranscriptEntry(role="assistant", content="Recent answer.", message_id="recent-answer"),
        _TranscriptEntry(role="user", content="Compare the selected images.", message_id="current"),
        _TranscriptEntry(role="user", content="Queued future input.", message_id="queued"),
    ]
    manager._canonical[key] = [*image_entries, *tail]
    manager._transcripts[key] = tail if archived else [*image_entries, *tail]
    manifest = build_attachment_manifest(
        image_entries, session_id=node.session_id, session_key=key,
    )
    requested_ids = [item.attachment_id for item in manifest.occurrences[:2]]
    provider = _CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            max_history_turns=1,
            model_id="configured-model",
            model_vision_support=vision_support,
            metadata={"image_intent_attachment_ids": requested_ids},
        ),
    )

    await runner._load_history(agent, key, bound_user_message_id="current")
    # A second load replaces request context instead of accumulating images.
    await runner._load_history(agent, key, bound_user_message_id="current")
    admission_messages = agent._assemble_compaction_consumer_request(
        replay_summary="Earlier conversation.",
        kept_entries=[],
        active_user_message=tail[2].content,
        active_user_in_history=False,
        bound_user_message_id=None,
        attachment_messages=None,
        runtime_context_message=Message(role="user", content="[Runtime context for this turn]"),
    )
    assert admission_messages is not None
    assert all(attachment_id in str(admission_messages) for attachment_id in requested_ids)

    events = [event async for event in agent.run_turn(tail[2].content)]

    assert any(event.kind == "done" for event in events)
    sent = provider.calls[0]["messages"]
    image_blocks = [
        block
        for message in sent
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockImage)
    ]
    assert [block.data for block in image_blocks] == (
        [] if vision_support == "unsupported" else [
            _b64(image_bytes(color="#000000")), _b64(image_bytes(color="#000001")),
        ]
    )
    assert all(attachment_id in str(sent) for attachment_id in requested_ids)
    assert manifest.occurrences[2].attachment_id not in str(sent)
    assert "Recent question." in str(sent)
    assert "Recent answer." in str(sent)
    assert "Old instruction" not in str(sent)
    assert "Queued future input." not in str(sent)
    if vision_support == "unsupported":
        assert "图片" in str(sent)
    assert manager._canonical[key][0] is image_entries[0]
    assert "attachment_id" not in image_entries[0].content

    agent.clear_history()
    async for _ in agent.run_turn("Answer without historical input."):
        pass
    assert not any(_message_has_image(message) for message in provider.calls[1]["messages"])
    assert not any(attachment_id in str(provider.calls[1]) for attachment_id in requested_ids)


@pytest.mark.asyncio
async def test_explicit_attachment_id_rehydrates_image_from_compacted_archive() -> None:
    manager = _CanonicalSessionManager()
    key = "agent:main:compacted-image-id-replay"
    config = GatewayConfig(llm={"provider": "openrouter"})
    config.squilla_router.vision_history_lookback_turns = 0
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager, config=config)
    node = await manager.create(key)
    archived_image = _TranscriptEntry(
        role="user",
        content=_inline_image_envelope("Archived image."),
        message_id="archived-image-message",
    )
    archived_answer = _TranscriptEntry(
        role="assistant",
        content="Earlier textual analysis.",
        message_id="archived-answer-message",
    )
    active_user = _TranscriptEntry(
        role="user",
        content="Analyze attachment att-id again.",
        message_id="current-message",
    )
    manager._transcripts[key] = [active_user]
    manager._canonical[key] = [archived_image, archived_answer, active_user]
    manifest = build_attachment_manifest(
        manager._canonical[key],
        session_id=node.session_id,
        session_key=key,
    )
    attachment_id = manifest.occurrences[0].attachment_id

    provider = _CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_id="configured-vision",
            model_vision_support="supported",
            metadata={"image_attachment_ids": [attachment_id]},
        ),
    )
    await runner._load_history(agent, key)
    events = [event async for event in agent.run_turn("Analyze it again.")]

    assert any(event.kind == "done" for event in events)
    sent = provider.calls[0]["messages"]
    assert any(_message_has_image(message) for message in sent)
    assert manager._canonical[key][0] is archived_image


@pytest.mark.asyncio
async def test_current_image_and_one_archived_image_id_are_merged_and_exact() -> None:
    manager = _CanonicalSessionManager()
    key = "agent:main:current-plus-one-archived-image"
    config = GatewayConfig(llm={"provider": "openrouter"})
    config.squilla_router.vision_history_lookback_turns = 0
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager, config=config)
    node = await manager.create(key)
    archived_first = image_bytes(color="#000004")
    archived_second = image_bytes(color="#000005")
    current_payload = image_bytes(color="#000006")
    archived_image = _TranscriptEntry(
        role="user",
        content=_inline_image_envelope_many(
            "Two archived images.",
            archived_first,
            archived_second,
        ),
        message_id="archived-two-image-message",
    )
    archived_answer = _TranscriptEntry(
        role="assistant",
        content="Earlier answer.",
        message_id="archived-two-image-answer",
    )
    current_user = _TranscriptEntry(
        role="user",
        content=_inline_image_envelope("Compare these images.", current_payload),
        message_id="current-image-message",
    )
    manager._transcripts[key] = [current_user]
    manager._canonical[key] = [archived_image, archived_answer, current_user]
    manifest = build_attachment_manifest(
        manager._canonical[key],
        session_id=node.session_id,
        session_key=key,
    )
    archived_second_id = next(
        occurrence.attachment_id
        for occurrence in manifest.occurrences
        if occurrence.source_message_id == archived_image.message_id
        and occurrence.ordinal == 1
    )
    current_id = next(
        occurrence.attachment_id
        for occurrence in manifest.occurrences
        if occurrence.source_message_id == current_user.message_id
    )

    provider = _CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_id="configured-vision",
            model_vision_support="supported",
            metadata={
                "attachment_count": 1,
                "image_attachment_ids": [current_id],
                "image_intent_attachment_ids": [archived_second_id],
            },
        ),
    )
    await runner._load_history(agent, key)
    current_message = Message(
        role="user",
        content=[
            ContentBlockText(text="Compare these images."),
            ContentBlockImage(
                media_type="image/png",
                data=_b64(current_payload),
                attachment_id=current_id,
            ),
        ],
    )
    events = [
        event
        async for event in agent.run_turn("", extra_messages=[current_message])
    ]

    assert any(event.kind == "done" for event in events)
    image_payloads = [
        block.data
        for message in provider.calls[0]["messages"]
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockImage)
    ]
    assert image_payloads == [_b64(archived_second), _b64(current_payload)]
    assert _b64(archived_first) not in image_payloads


@pytest.mark.asyncio
async def test_persisted_compacted_archive_rehydrates_explicit_attachment_id() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        key = "agent:main:persisted-compacted-image-id-replay"
        node = await manager.create(key)
        payload = image_bytes()
        image_entry = await manager.append_message(
            key,
            "user",
            _inline_image_envelope("Archived image.", payload),
            message_id="persisted-archived-image",
        )
        current_entry = await manager.append_message(
            key,
            "user",
            "Analyze the archived attachment again.",
            message_id="persisted-current-message",
        )
        manifest = build_attachment_manifest(
            [image_entry, current_entry],
            session_id=node.session_id,
            session_key=key,
        )
        attachment_id = manifest.occurrences[0].attachment_id
        source = await manager.capture_compaction_source(
            key,
            boundary_message_id=current_entry.message_id,
        )
        installed = await manager.persist_compaction_result(
            key,
            f"Archived image attachment_id={attachment_id}",
            [{"role": "user", "content": current_entry.content}],
            compaction_id="cmp-persisted-image",
            removed_count=1,
            source_entries=source.entries,
            source_preimage=source.preimage,
            source_boundary_message_id=source.boundary_message_id,
            source_boundary_entry_id=source.boundary_entry_id,
        )
        assert installed is True
        assert [entry.message_id for entry in await manager.get_transcript(key)] == [
            current_entry.message_id
        ]
        assert [
            entry.message_id for entry in await manager.get_canonical_transcript(key)
        ] == [image_entry.message_id, current_entry.message_id]

        config = GatewayConfig(llm={"provider": "openrouter"})
        config.squilla_router.vision_history_lookback_turns = 0
        runner = TurnRunner(
            provider_selector=MagicMock(),
            session_manager=manager,
            config=config,
        )
        provider = _CapturingProvider()
        current_prompt = f"Analyze attachment_id={attachment_id} again."
        turn, returned_provider = await runner._run_pipeline(
            current_prompt,
            key,
            provider,
            None,
            [],
            "system",
            [],
            semantic_message=current_prompt,
        )
        assert returned_provider is provider
        assert turn.metadata["image_intent_attachment_ids"] == [attachment_id]
        assert turn.metadata["router_vision_followup_needs_image"] is True
        agent = Agent(
            provider=provider,
            config=AgentConfig(
                max_iterations=1,
                model_id="configured-vision",
                model_vision_support="supported",
                metadata=turn.metadata,
            ),
        )

        await runner._load_history(agent, key)
        events = [event async for event in agent.run_turn(current_prompt)]

        assert any(event.kind == "done" for event in events)
        image_blocks = [
            block
            for message in provider.calls[0]["messages"]
            if isinstance(message.content, list)
            for block in message.content
            if isinstance(block, ContentBlockImage)
        ]
        assert [block.data for block in image_blocks] == [_b64(payload)]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_full_fork_rehydrates_parent_legacy_attachment_id() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        parent_key = "agent:main:legacy-image-parent"
        parent = await manager.create(parent_key)
        payload = image_bytes()
        await manager.append_message(
            parent_key,
            "user",
            _inline_image_envelope("Legacy archived image.", payload),
            message_id="legacy-full-fork-image",
        )
        await manager.append_message(parent_key, "assistant", "Earlier analysis.")
        active_tail = await manager.append_message(
            parent_key,
            "user",
            "Keep this active tail.",
            message_id="legacy-full-fork-tail",
        )
        parent_manifest = build_attachment_manifest(
            await manager.get_canonical_transcript(parent_key),
            session_id=parent.session_id,
            session_key=parent_key,
        )
        attachment_id = parent_manifest.occurrences[0].attachment_id
        source = await manager.capture_compaction_source(
            parent_key,
            boundary_message_id=active_tail.message_id,
        )
        assert await manager.persist_compaction_result(
            parent_key,
            f"Legacy image attachment_id={attachment_id}",
            [{"role": "user", "content": active_tail.content}],
            compaction_id="cmp-legacy-full-fork-image",
            removed_count=2,
            source_entries=source.entries,
            source_preimage=source.preimage,
            source_boundary_message_id=source.boundary_message_id,
            source_boundary_entry_id=source.boundary_entry_id,
        )

        child_key = "agent:main:legacy-image-child"
        child = await manager.branch(parent_key, child_key, fork_transcript=True)
        prompt = f"Analyze attachment_id={attachment_id} again."
        current = await manager.append_message(
            child_key,
            "user",
            prompt,
            message_id="legacy-full-fork-current",
        )
        child_manifest = build_attachment_manifest(
            await manager.get_canonical_transcript(child_key),
            session_id=child.session_id,
            session_key=child_key,
        )
        assert child_manifest.by_id(attachment_id) is not None

        config = GatewayConfig(llm={"provider": "openrouter"})
        config.squilla_router.vision_history_lookback_turns = 0
        runner = TurnRunner(
            provider_selector=MagicMock(),
            session_manager=manager,
            config=config,
        )
        provider = _CapturingProvider()
        agent = Agent(
            provider=provider,
            config=AgentConfig(
                max_iterations=1,
                model_id="configured-vision",
                model_vision_support="supported",
                metadata={
                    "attachment_count": 0,
                    "image_intent_attachment_ids": [attachment_id],
                },
            ),
        )
        await runner._load_history(
            agent,
            child_key,
            bound_user_message_id=current.message_id,
        )
        events = [event async for event in agent.run_turn(prompt)]

        assert any(event.kind == "done" for event in events)
        sent_images = [
            block.data
            for message in provider.calls[0]["messages"]
            if isinstance(message.content, list)
            for block in message.content
            if isinstance(block, ContentBlockImage)
        ]
        assert sent_images == [_b64(payload)]
        assert any(
            attachment_id in str(message.content)
            for message in provider.calls[0]["messages"]
        )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_lazy_manifest_backfill_monotonically_merges_active_subset() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        key = "agent:main:manifest-active-subset-merge"
        node = await manager.create(key)
        archived = await manager.append_message(
            key,
            "user",
            _inline_image_envelope("Archived A.", image_bytes(color="#000007")),
            message_id="manifest-image-a",
        )
        active = await manager.append_message(
            key,
            "user",
            _inline_image_envelope("Active B.", image_bytes(color="#000008")),
            message_id="manifest-image-b",
        )
        archived_manifest = build_attachment_manifest(
            [archived],
            session_id=node.session_id,
            session_key=key,
        )
        await manager.save_context_state(manifest_context_state(archived_manifest))
        runner = TurnRunner(
            provider_selector=MagicMock(),
            session_manager=manager,
            config=GatewayConfig(llm={"provider": "openrouter"}),
        )

        await runner._persist_attachment_manifest_best_effort(key, [active])
        states = await manager.get_context_states(
            key,
            provider=ATTACHMENT_MANIFEST_PROVIDER,
            state_kind=ATTACHMENT_MANIFEST_STATE_KIND,
        )
        latest = max(states, key=lambda state: (state.created_at, state.id or 0))
        merged = attachment_manifest_from_context_state(latest)
        assert [item.source_message_id for item in merged.occurrences] == [
            "manifest-image-a",
            "manifest-image-b",
        ]

        state_count = len(states)
        await runner._persist_attachment_manifest_best_effort(key, [active])
        states = await manager.get_context_states(
            key,
            provider=ATTACHMENT_MANIFEST_PROVIDER,
            state_kind=ATTACHMENT_MANIFEST_STATE_KIND,
        )
        assert len(states) == state_count
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_non_image_attachment_id_does_not_force_vision_route() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        key = "agent:main:non-image-attachment-reference"
        node = await manager.create(key)
        document_entry = await manager.append_message(
            key,
            "user",
            json.dumps(
                {
                    "text": "Saved document.",
                    "attachments": [
                        {
                            "type": "application/pdf",
                            "name": "notes.pdf",
                            "data": _b64(b"synthetic-pdf"),
                        }
                    ],
                }
            ),
            message_id="manifest-document",
        )
        manifest = build_attachment_manifest(
            [document_entry],
            session_id=node.session_id,
            session_key=key,
        )
        document_id = manifest.occurrences[0].attachment_id
        config = GatewayConfig(llm={"provider": "openrouter"})
        config.squilla_router.enabled = False
        runner = TurnRunner(
            provider_selector=MagicMock(),
            session_manager=manager,
            config=config,
        )
        provider = _CapturingProvider()
        prompt = f"Summarize attachment_id={document_id}."

        turn, returned_provider = await runner._run_pipeline(
            prompt,
            key,
            provider,
            None,
            [],
            "system",
            [],
            semantic_message=prompt,
        )

        assert returned_provider is provider
        assert "image_intent_attachment_ids" not in turn.metadata
        assert turn.metadata.get("router_vision_followup_needs_image") is not True
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_image_followup_routes_vision_and_replays_inline_history_image() -> None:
    manager = _FakeSessionManager()
    key = "agent:main:image-followup-inline"
    config = GatewayConfig(llm={"provider": "openrouter"})
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager, config=config)
    await manager.create(key)
    await manager.append_message(key, "user", _inline_image_envelope("Describe this image."))
    await manager.append_message(key, "assistant", "It shows a small test image.")
    await manager.append_message(key, "user", "Continue from that image.")

    router_context = await runner._router_previous_assistant_context(
        key,
        exclude_last_user=True,
    )
    assert router_context["history_has_recent_image"] is True
    assert router_context["history_image_turn_count"] == 1
    assert router_context["vision_sticky_remaining"] == 3
    assert router_context["turns_since_last_image"] == 0
    assert router_context["last_image_turn_text"].startswith("Describe this image.")

    gate_provider = _GateThenCaptureProvider(
        '{"decision":"needs_image","confidence":0.92,"reason":"explicit continuation"}'
    )
    ctx = TurnContext(
        message="Continue from that image.",
        session_key=key,
        config=config,
        provider=gate_provider,
        model=config.llm.model,
        tool_defs=[],
        system_prompt="system",
        metadata={
            "router_history_user_texts": ["Describe this image."],
            "router_history_has_recent_image": True,
            "router_history_image_turn_count": 1,
            "router_vision_sticky_remaining": 3,
            "router_turns_since_last_image": 0,
            "router_last_image_turn_text": "Describe this image.",
            "router_vision_candidate_turns": 8,
        },
        raw_message="Continue from that image.",
    )
    gated = await apply_vision_followup_gate(ctx)
    routed = await apply_squilla_router(gated)
    assert gate_provider.gate_calls == 0
    assert routed.metadata["router_vision_followup_gate_decision"] == "needs_image"
    assert routed.metadata["router_vision_followup_gate_source"] == "explicit_image_reference"
    assert routed.metadata["router_vision_followup_needs_image"] is True
    assert routed.metadata["routing_source"] == "image_route"
    assert routed.metadata["image_route_reason"] == "gate_history"
    assert routed.metadata["route_max_history_turns"] == 8

    provider = _CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_id=routed.model,
            model_capabilities=ModelCapabilities(supports_vision=True),
            preserve_historical_images=True,
        ),
    )
    await runner._load_history(agent, key)
    events = [event async for event in agent.run_turn("Continue from that image.")]

    assert any(event.kind == "done" for event in events)
    sent_messages = provider.calls[0]["messages"]
    assert any(_message_has_image(message) for message in sent_messages)
    assert isinstance(sent_messages[-1].content, str)
    assert sent_messages[-1].content.startswith("Continue from that image.")


@pytest.mark.asyncio
async def test_gate_text_only_followup_does_not_image_route() -> None:
    key = "agent:main:image-followup-gate-text"
    config = GatewayConfig(llm={"provider": "openrouter"})
    provider = _GateThenCaptureProvider(
        '{"decision":"text_only","confidence":0.9,"reason":"new coding task"}'
    )
    ctx = TurnContext(
        message="Now write a Python script.",
        session_key=key,
        config=config,
        provider=provider,
        model=config.llm.model,
        tool_defs=[],
        system_prompt="system",
        metadata={
            "router_history_user_texts": ["Describe this image."],
            "router_history_has_recent_image": True,
            "router_history_image_turn_count": 1,
            "router_vision_sticky_remaining": 3,
            "router_turns_since_last_image": 0,
            "router_last_image_turn_text": "Describe this image.",
            "router_vision_candidate_turns": 8,
        },
        raw_message="Now write a Python script.",
    )

    gated = await apply_vision_followup_gate(ctx)
    turn = await apply_squilla_router(gated)

    assert provider.gate_calls == 1
    assert turn.metadata["router_vision_followup_gate_decision"] == "text_only"
    assert turn.metadata["router_vision_followup_needs_image"] is False
    assert turn.metadata.get("image_route_reason") is None


@pytest.mark.asyncio
async def test_gate_unknown_recent_fallback_routes_image() -> None:
    config = GatewayConfig(llm={"provider": "openrouter"})
    provider = _GateThenCaptureProvider(
        '{"decision":"unknown","confidence":0.2,"reason":"ambiguous pronoun"}'
    )
    ctx = TurnContext(
        message="What about this?",
        session_key="agent:main:image-followup-unknown-recent",
        config=config,
        provider=provider,
        model=config.llm.model,
        tool_defs=[],
        system_prompt="system",
        metadata={
            "router_history_has_recent_image": True,
            "router_history_image_turn_count": 1,
            "router_turns_since_last_image": 1,
            "router_last_image_turn_text": "Describe this image.",
            "router_vision_candidate_turns": 8,
        },
        raw_message="What about this?",
    )

    gated = await apply_vision_followup_gate(ctx)
    routed = await apply_squilla_router(gated)

    assert provider.gate_calls == 1
    assert routed.metadata["router_vision_followup_gate_decision"] == "unknown"
    assert routed.metadata["router_vision_followup_needs_image"] is True
    assert routed.metadata["router_vision_followup_fallback"] == "image_if_recent"
    assert routed.metadata["image_route_reason"] == "gate_history"


@pytest.mark.asyncio
async def test_gate_unknown_old_fallback_uses_text_router() -> None:
    config = GatewayConfig(llm={"provider": "openrouter"})
    provider = _GateThenCaptureProvider(
        '{"decision":"unknown","confidence":0.2,"reason":"ambiguous but old"}'
    )
    ctx = TurnContext(
        message="What about this?",
        session_key="agent:main:image-followup-unknown-old",
        config=config,
        provider=provider,
        model=config.llm.model,
        tool_defs=[],
        system_prompt="system",
        metadata={
            "router_history_has_recent_image": True,
            "router_history_image_turn_count": 1,
            "router_turns_since_last_image": 3,
            "router_last_image_turn_text": "Describe this image.",
            "router_vision_candidate_turns": 8,
        },
        raw_message="What about this?",
    )

    gated = await apply_vision_followup_gate(ctx)
    routed = await apply_squilla_router(gated)

    assert provider.gate_calls == 1
    assert routed.metadata["router_vision_followup_gate_decision"] == "unknown"
    assert routed.metadata["router_vision_followup_needs_image"] is False
    assert routed.metadata.get("image_route_reason") is None


@pytest.mark.asyncio
async def test_historical_image_ref_replays_from_real_material_store(tmp_path: Path) -> None:
    manager = _FakeSessionManager()
    key = "agent:main:image-followup-ref"
    config = GatewayConfig(llm={"provider": "openrouter"})
    config.attachments.media_root = str(tmp_path / "media")
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager, config=config)
    node = await manager.create(key)
    payload = image_bytes()
    sha, _, _ = write_transcript_material(
        media_root=Path(config.attachments.media_root),
        session_id=node.session_id,
        payload=payload,
    )
    envelope = json.dumps(
        {
            "text": "Describe the stored image.",
            "attachments": [
                {
                    "mime": "image/png",
                    "name": "stored.png",
                    "sha256_ref": sha,
                }
            ],
        }
    )
    await manager.append_message(key, "user", envelope)
    await manager.append_message(key, "assistant", "It is stored on disk.")
    await manager.append_message(key, "user", "Use that stored image again.")

    router_context = await runner._router_previous_assistant_context(
        key,
        exclude_last_user=True,
    )
    assert router_context["history_has_recent_image"] is True
    assert router_context["vision_sticky_remaining"] == 3

    provider = _CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_capabilities=ModelCapabilities(supports_vision=True),
            preserve_historical_images=True,
        ),
    )
    await runner._load_history(agent, key)
    events = [event async for event in agent.run_turn("Use that stored image again.")]

    assert any(event.kind == "done" for event in events)
    image_blocks = [
        block
        for message in provider.calls[0]["messages"]
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockImage)
    ]
    assert len(image_blocks) == 1
    assert base64.b64decode(image_blocks[0].data) == payload


@pytest.mark.asyncio
async def test_default_sticky_window_keeps_third_followup_active() -> None:
    manager = _FakeSessionManager()
    key = "agent:main:image-followup-third"
    config = GatewayConfig(llm={"provider": "openrouter"})
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager, config=config)

    await manager.create(key)
    await manager.append_message(key, "user", _inline_image_envelope("Describe this image."))
    await manager.append_message(key, "assistant", "It shows a small test image.")
    await manager.append_message(key, "user", "First follow-up.")
    await manager.append_message(key, "assistant", "First answer.")
    await manager.append_message(key, "user", "Second follow-up.")
    await manager.append_message(key, "assistant", "Second answer.")
    await manager.append_message(key, "user", "Third follow-up.")

    router_context = await runner._router_previous_assistant_context(
        key,
        exclude_last_user=True,
    )

    assert router_context["history_has_recent_image"] is True
    assert router_context["history_image_turn_count"] == 1
    assert router_context["vision_sticky_remaining"] == 1


@pytest.mark.asyncio
async def test_image_history_outside_sticky_window_does_not_route_or_replay() -> None:
    manager = _FakeSessionManager()
    key = "agent:main:image-followup-sticky-expired"
    config = GatewayConfig(llm={"provider": "openrouter"})
    config.squilla_router.vision_history_lookback_turns = 4
    config.squilla_router.vision_sticky_followup_turns = 1
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager, config=config)
    await manager.create(key)
    await manager.append_message(key, "user", _inline_image_envelope("Old image."))
    await manager.append_message(key, "assistant", "Old answer.")
    await manager.append_message(key, "user", "Plain intervening question.")
    await manager.append_message(key, "assistant", "Plain answer.")
    await manager.append_message(key, "user", "Current follow-up.")

    router_context = await runner._router_previous_assistant_context(
        key,
        exclude_last_user=True,
    )
    assert router_context["history_has_recent_image"] is True
    assert router_context["history_image_turn_count"] == 1
    assert "vision_sticky_remaining" not in router_context

    ctx = TurnContext(
        message="Current follow-up.",
        session_key=key,
        config=config,
        provider=None,
        model=config.llm.model,
        tool_defs=[],
        system_prompt="system",
        metadata={
            "router_history_has_recent_image": True,
            "router_history_image_turn_count": 1,
        },
    )
    routed = await apply_squilla_router(ctx)
    assert routed.metadata["routing_source"] != "image_route"

    provider = _CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_id=routed.model,
            model_capabilities=ModelCapabilities(supports_vision=False),
        ),
    )
    await runner._load_history(agent, key)
    events = [event async for event in agent.run_turn("Current follow-up.")]

    assert any(event.kind == "done" for event in events)
    assert not any(_message_has_image(message) for message in provider.calls[0]["messages"])


@pytest.mark.asyncio
async def test_queued_prompts_do_not_consume_image_replay_window() -> None:
    # Queued sends persisted after the bound message are excluded from replayed
    # history, so they must not occupy tail slots of the vision lookback window.
    manager = _FakeSessionManager()
    key = "agent:main:image-replay-queued"
    config = GatewayConfig()
    config.squilla_router.vision_history_lookback_turns = 3
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager, config=config)
    await manager.create(key)
    for index in range(1, 4):
        await manager.append_message(
            key,
            "user",
            _inline_image_envelope(f"Image {index}.", image_bytes(color=f"#{index:06x}")),
            message_id=f"m{index}",
        )
        await manager.append_message(key, "assistant", f"Answer {index}.")
    await manager.append_message(
        key, "user", "Tell me about all three images.", message_id="m-current"
    )
    await manager.append_message(key, "user", "Queued follow-up B.", message_id="m-queued-b")
    await manager.append_message(key, "user", "Queued follow-up C.", message_id="m-queued-c")

    provider = _CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_capabilities=ModelCapabilities(supports_vision=True),
            preserve_historical_images=True,
        ),
    )
    await runner._load_history(agent, key, bound_user_message_id="m-current")
    async for _ in agent.run_turn("Tell me about all three images."):
        pass

    replayed = sum(
        1 for message in provider.calls[0]["messages"] if _message_has_image(message)
    )
    assert replayed == 3


@pytest.mark.asyncio
async def test_text_model_history_keeps_image_as_marker_not_provider_image() -> None:
    manager = _FakeSessionManager()
    key = "agent:main:image-followup-text-model"
    config = GatewayConfig(llm={"provider": "openrouter"})
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager, config=config)
    node = await manager.create(key)
    image_entry = await manager.append_message(
        key,
        "user",
        _inline_image_envelope("Describe this image."),
        message_id="legacy-image-message",
    )
    manifest = build_attachment_manifest(
        [image_entry],
        session_id=node.session_id,
        session_key=key,
    )
    await manager.append_message(key, "assistant", "It shows a small test image.")
    await manager.append_message(key, "user", "Continue as text.")

    provider = _CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_capabilities=ModelCapabilities(supports_vision=False),
        ),
    )
    await runner._load_history(agent, key)
    events = [event async for event in agent.run_turn("Continue as text.")]

    assert any(event.kind == "done" for event in events)
    sent_messages = provider.calls[0]["messages"]
    assert not any(_message_has_image(message) for message in sent_messages)
    assert "historical attachment omitted" in str(sent_messages[0].content)
    assert manifest.occurrences[0].attachment_id in str(sent_messages[0].content)


@pytest.mark.asyncio
async def test_text_only_followup_does_not_replay_history_image_on_vision_model() -> None:
    manager = _FakeSessionManager()
    key = "agent:main:image-followup-text-only-vision-model"
    config = GatewayConfig(llm={"provider": "openrouter"})
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager, config=config)
    await manager.create(key)
    await manager.append_message(key, "user", _inline_image_envelope("Describe this image."))
    await manager.append_message(key, "assistant", "It shows a small test image.")
    await manager.append_message(key, "user", "Now write a Python script.")

    provider = _CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_capabilities=ModelCapabilities(supports_vision=True),
        ),
    )
    await runner._load_history(agent, key)
    events = [event async for event in agent.run_turn("Now write a Python script.")]

    assert any(event.kind == "done" for event in events)
    sent_messages = provider.calls[0]["messages"]
    assert not any(_message_has_image(message) for message in sent_messages)
    assert "historical attachment omitted" in str(sent_messages[0].content)
