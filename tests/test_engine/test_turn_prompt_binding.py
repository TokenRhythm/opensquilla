"""A turn's history is bound to the specific persisted user message it answers.

Ingress persists a user message before the turn runs, and — when sends are
queued — a later prompt can be persisted while an earlier turn is still running.
The transcript then holds the bound message mid-stream with an unanswered future
prompt after it. Binding history positionally ("drop the last user entry") then
duplicates the current prompt and leaks the future prompt into context. These
tests pin id-based binding: the bound message is excluded (the caller re-appends
it), later still-queued user prompts are excluded, and the intervening assistant
replies are kept — with a positional-trim fallback when no id is supplied.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.history import (
    HistoryReplayProjection,
    project_history_replay_capacity,
)
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.turn_runner.prompt_assembler_stage import (
    RouterHistoryReplayRequest,
)
from opensquilla.engine.turn_runner.transcript_snapshot import TurnTranscriptSnapshot
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider import (
    ChatConfig,
    ContentBlockToolResult,
    ContentBlockToolUse,
    DoneEvent,
    Message,
    TextDeltaEvent,
)
from opensquilla.provider.request_proof import estimate_provider_media_tokens
from opensquilla.session.compaction import estimate_entry_model_replay_tokens
from opensquilla.session.context_view import (
    build_compaction_context_records,
    build_provider_compaction_context,
    format_compaction_summary_context,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionContextState, SessionIntent, SessionSummary
from opensquilla.session.storage import SessionStorage
from opensquilla.token_estimation import estimate_tokens


@dataclass
class _TranscriptEntry:
    role: str
    content: str
    message_id: str
    tool_calls: list[Any] | None = None
    reasoning_content: str | None = None
    token_count: int | None = None


@dataclass
class _SessionNode:
    session_key: str
    session_id: str


class _FakeSessionManager:
    """Minimal session manager whose entries carry a stable ``message_id``."""

    def __init__(self) -> None:
        self._nodes: dict[str, _SessionNode] = {}
        self._transcripts: dict[str, list[_TranscriptEntry]] = {}
        self._summaries: dict[str, list[SessionSummary]] = {}
        self._context_states: dict[str, list[SessionContextState]] = {}
        self._counter = 0

    async def create(self, session_key: str) -> _SessionNode:
        node = _SessionNode(session_key=session_key, session_id=f"id-{len(self._nodes) + 1}")
        self._nodes[session_key] = node
        self._transcripts.setdefault(session_key, [])
        return node

    async def append_message(self, session_key: str, role: str, content: str) -> _TranscriptEntry:
        self._counter += 1
        entry = _TranscriptEntry(role=role, content=content, message_id=f"m{self._counter}")
        self._transcripts.setdefault(session_key, []).append(entry)
        return entry

    async def get_transcript(self, session_key: str) -> list[_TranscriptEntry]:
        return list(self._transcripts.get(session_key, []))

    async def get_session(self, session_key: str) -> _SessionNode | None:
        return self._nodes.get(session_key)

    async def get_context_states(self, session_key: str) -> list[SessionContextState]:
        return list(self._context_states.get(session_key, []))

    async def get_summaries(self, session_key: str) -> list[SessionSummary]:
        return list(self._summaries.get(session_key, []))


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
        self.calls.append({"messages": list(messages)})
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield TextDeltaEvent(text="ok")
        yield DoneEvent(stop_reason="end_turn", input_tokens=3, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


def _new_agent(provider: _CapturingProvider) -> Agent:
    return Agent(provider=provider, config=AgentConfig(max_iterations=1))


def _new_runner(manager: _FakeSessionManager) -> TurnRunner:
    return TurnRunner(
        provider_selector=MagicMock(), session_manager=manager, config=GatewayConfig()
    )


def _user_texts(messages: list[Message]) -> list[str]:
    return [m.content for m in messages if m.role == "user" and isinstance(m.content, str)]


def _history_user_texts(messages: list[Message]) -> list[str]:
    # Everything except the final (current) message, which ``run_turn`` may
    # decorate with runtime context.
    return [
        m.content
        for m in messages[:-1]
        if m.role == "user" and isinstance(m.content, str)
    ]


def _inline_images(text: str, *payloads: bytes) -> str:
    return json.dumps(
        {
            "text": text,
            "attachments": [
                {
                    "type": "image/png",
                    "name": f"image-{index}.png",
                    "size": len(payload),
                    "data": base64.b64encode(payload).decode("ascii"),
                }
                for index, payload in enumerate(payloads, start=1)
            ],
        },
        separators=(",", ":"),
    )


async def _run_and_capture(
    runner: TurnRunner,
    provider: _CapturingProvider,
    key: str,
    current_text: str,
    *,
    bound_user_message_id: str | None,
) -> list[Message]:
    agent = _new_agent(provider)
    await runner._load_history(agent, key, bound_user_message_id=bound_user_message_id)
    async for _ in agent.run_turn(current_text):
        pass
    return provider.calls[-1]["messages"]


@pytest.mark.asyncio
async def test_queued_followup_is_not_duplicated_or_scrambled() -> None:
    # Turn B answers prompt B, which was persisted at ingress WHILE turn A ran.
    # The transcript order is A, B, A_reply (A_reply persisted at A's completion,
    # after B's ingress). Binding to B must yield [A, A_reply, B] — B exactly
    # once as the current input, A's reply preserved.
    manager = _FakeSessionManager()
    key = "agent:main:queued-followup"
    runner = _new_runner(manager)
    await manager.create(key)
    entry_a = await manager.append_message(key, "user", "First question A")
    entry_b = await manager.append_message(key, "user", "Second question B")
    await manager.append_message(key, "assistant", "Answer to A")

    provider = _CapturingProvider()
    messages = await _run_and_capture(
        runner, provider, key, "Second question B", bound_user_message_id=entry_b.message_id
    )

    # The bound prompt appears exactly once — as the current input, not echoed
    # into history.
    assert sum(1 for t in _user_texts(messages) if t.startswith("Second question B")) == 1
    assert _history_user_texts(messages) == ["First question A"]
    assert messages[-1].role == "user"
    assert messages[-1].content.startswith("Second question B")
    # A's assistant reply survives in context, before the current prompt.
    assert any(m.role == "assistant" and m.content == "Answer to A" for m in messages[:-1])
    # entry_a is referenced so the fixture reads as the intended A/B/reply order.
    assert entry_a.message_id != entry_b.message_id


@pytest.mark.asyncio
async def test_earlier_turn_excludes_future_queued_prompt() -> None:
    # Turn A loads history while prompt B is already persisted (queued during A)
    # but A_reply does not exist yet: transcript is [A, B]. Binding to A must
    # exclude the future prompt B AND not duplicate A.
    manager = _FakeSessionManager()
    key = "agent:main:earlier-turn"
    runner = _new_runner(manager)
    await manager.create(key)
    entry_a = await manager.append_message(key, "user", "First question A")
    await manager.append_message(key, "user", "Second question B")

    provider = _CapturingProvider()
    messages = await _run_and_capture(
        runner, provider, key, "First question A", bound_user_message_id=entry_a.message_id
    )

    # The future queued prompt B is absent; A is not duplicated into history.
    assert "Second question B" not in _user_texts(messages)
    assert _history_user_texts(messages) == []
    assert messages[-1].content.startswith("First question A")


@pytest.mark.asyncio
async def test_positional_fallback_without_bound_id() -> None:
    # Legacy path: no bound id → the historical positional trim still applies
    # (transcript ends on the current user entry, which is dropped and re-added).
    manager = _FakeSessionManager()
    key = "agent:main:fallback"
    runner = _new_runner(manager)
    await manager.create(key)
    await manager.append_message(key, "user", "First question A")
    await manager.append_message(key, "assistant", "Answer to A")
    await manager.append_message(key, "user", "Second question B")

    provider = _CapturingProvider()
    messages = await _run_and_capture(
        runner, provider, key, "Second question B", bound_user_message_id=None
    )

    assert _history_user_texts(messages) == ["First question A"]
    assert sum(1 for t in _user_texts(messages) if t.startswith("Second question B")) == 1
    assert messages[-1].content.startswith("Second question B")


@pytest.mark.asyncio
async def test_missing_bound_id_falls_back_and_warns() -> None:
    # An id that is not in the (e.g. compacted) transcript must not crash: fall
    # back to positional trim.
    manager = _FakeSessionManager()
    key = "agent:main:missing-id"
    runner = _new_runner(manager)
    await manager.create(key)
    await manager.append_message(key, "user", "First question A")
    await manager.append_message(key, "assistant", "Answer to A")
    await manager.append_message(key, "user", "Second question B")

    provider = _CapturingProvider()
    messages = await _run_and_capture(
        runner, provider, key, "Second question B", bound_user_message_id="does-not-exist"
    )

    assert _history_user_texts(messages) == ["First question A"]
    assert sum(1 for t in _user_texts(messages) if t.startswith("Second question B")) == 1


@pytest.mark.asyncio
async def test_router_context_excludes_current_bound_prompt_when_followup_queued() -> None:
    manager = _FakeSessionManager()
    key = "agent:main:router-queued"
    runner = _new_runner(manager)
    await manager.create(key)
    entry_a = await manager.append_message(key, "user", "First question A")
    await manager.append_message(key, "user", "Second question B")

    ctx = await runner._router_previous_assistant_context(
        key, exclude_last_user=True, bound_user_message_id=entry_a.message_id
    )

    history = ctx.get("history_user_texts") or []
    assert "First question A" not in history
    assert "Second question B" not in history


@pytest.mark.asyncio
async def test_router_context_excludes_queued_prompts_when_last_entry_is_assistant() -> None:
    # While turn Z runs, A and B are persisted at ingress; Z's reply lands last,
    # so a positional last-entry trim would skip nothing.
    manager = _FakeSessionManager()
    key = "agent:main:router-queued-reply-after"
    runner = _new_runner(manager)
    await manager.create(key)
    await manager.append_message(key, "user", "Prior question Z")
    entry_a = await manager.append_message(key, "user", "First question A")
    await manager.append_message(key, "user", "Second question B")
    await manager.append_message(key, "assistant", "Answer to Z")

    ctx = await runner._router_previous_assistant_context(
        key, exclude_last_user=True, bound_user_message_id=entry_a.message_id
    )

    assert ctx.get("history_user_texts") == ["Prior question Z"]


@pytest.mark.asyncio
async def test_router_capacity_does_not_double_count_bound_attachment_across_compaction() -> None:
    manager = _FakeSessionManager()
    key = "agent:main:router-capacity-compaction"
    node = await manager.create(key)
    await manager.append_message(key, "user", "u" * 8_000)
    await manager.append_message(key, "assistant", "a" * 8_000)
    attachment_envelope = (
        '{"text":"current","attachments":['
        '{"type":"text/plain","data":"' + ("Z" * 12_000) + '"}]}'
    )
    current = await manager.append_message(key, "user", attachment_envelope)

    before = await _new_runner(manager)._router_previous_assistant_context(
        key,
        exclude_last_user=True,
        bound_user_message_id=current.message_id,
        include_capacity=True,
    )

    summary_text = "checkpoint " + ("s" * 1_000)
    manager._transcripts[key] = [current]
    manager._summaries[key] = [
        SessionSummary(
            session_id=node.session_id,
            session_key=key,
            summary_text=summary_text,
            covered_through_id=2,
        )
    ]
    manager._context_states[key] = [
        SessionContextState(
            session_id=node.session_id,
            session_key=key,
            provider="portable",
            state_kind="structured_summary_v1",
            payload={
                "schema_version": 1,
                "current_status": summary_text,
            },
            covered_through_id=2,
            portable=True,
            cacheable=True,
        )
    ]
    after = await _new_runner(manager)._router_previous_assistant_context(
        key,
        exclude_last_user=True,
        bound_user_message_id=current.message_id,
        include_capacity=True,
    )

    records = build_compaction_context_records(
        context_states=manager._context_states[key],
        summaries=manager._summaries[key],
    )
    assert len(records) == 1
    expected_summary = format_compaction_summary_context([records[0].text])
    assert expected_summary is not None
    assert after["history_capacity_estimated_tokens"] == estimate_tokens(
        expected_summary
    )
    assert after["history_capacity_message_count"] == 1
    assert (
        after["history_capacity_estimated_tokens"]
        < before["history_capacity_estimated_tokens"]
    )


@pytest.mark.asyncio
async def test_router_capacity_projects_inline_images_after_route() -> None:
    manager = _FakeSessionManager()
    key = "agent:main:router-capacity-inline-images"
    await manager.create(key)
    payloads = [bytes(range(256)) * 100, bytes(reversed(range(256))) * 100]
    envelope = _inline_images("inspect both images", *payloads)
    historical_user = await manager.append_message(key, "user", envelope)
    await manager.append_message(key, "assistant", "historical answer")
    current = await manager.append_message(key, "user", "current image turn")
    entries = await manager.get_transcript(key)

    context = await _new_runner(manager)._router_history_capacity_context(
        key,
        entries,
        exclude_last_user=True,
        bound_user_message_id=current.message_id,
        bound_index=2,
        max_history_turns=1,
        preserve_image_attachments=True,
    )

    raw_tokens = estimate_entry_model_replay_tokens(historical_user)
    media_reserve = sum(
        estimate_provider_media_tokens("image", len(payload)) for payload in payloads
    )
    assert context["history_capacity_estimate_complete"] is True
    assert context["history_capacity_message_count"] == 2
    assert media_reserve <= context["history_capacity_estimated_tokens"] < raw_tokens

    text_route_context = await _new_runner(manager)._router_history_capacity_context(
        key,
        entries,
        exclude_last_user=True,
        bound_user_message_id=current.message_id,
        bound_index=2,
        max_history_turns=1,
        preserve_image_attachments=False,
    )
    assert text_route_context["history_capacity_estimate_complete"] is True
    assert text_route_context["history_capacity_estimated_tokens"] >= raw_tokens

    ordinary_json = json.dumps(
        {
            "payload": "ordinary application data",
            "preview_url": (
                "data:image/png;base64," + base64.b64encode(payloads[0]).decode("ascii")
            ),
            "attachments": [
                {
                    "type": "image/png",
                    "data": base64.b64encode(payloads[0]).decode("ascii"),
                }
            ],
        },
        separators=(",", ":"),
    )
    manager._transcripts[key] = [
        _TranscriptEntry(
            role="user",
            content=ordinary_json,
            message_id="ordinary-json",
        ),
        current,
    ]
    ordinary_entries = await manager.get_transcript(key)
    ordinary_context = await _new_runner(manager)._router_history_capacity_context(
        key,
        ordinary_entries,
        exclude_last_user=True,
        bound_user_message_id=current.message_id,
        bound_index=1,
        max_history_turns=1,
        preserve_image_attachments=True,
    )
    assert ordinary_context["history_capacity_estimate_complete"] is True
    assert ordinary_context["history_capacity_estimated_tokens"] >= estimate_tokens(
        ordinary_json
    )


def test_router_capacity_does_not_discount_tool_argument_data_url() -> None:
    data_url = "data:image/png;base64," + base64.b64encode(bytes(range(256)) * 80).decode(
        "ascii"
    )
    projection = HistoryReplayProjection(
        messages=(
            Message(role="user", content="inspect tool input"),
            Message(
                role="assistant",
                content=[
                    ContentBlockToolUse(
                        id="tool-data-url",
                        name="inspect",
                        input={"preview_url": data_url},
                    )
                ],
            ),
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(
                        tool_use_id="tool-data-url",
                        content="done",
                    )
                ],
            ),
        )
    )

    capacity = project_history_replay_capacity(projection)

    assert capacity.estimate_complete is True
    assert capacity.media_block_count == 0
    assert capacity.media_reserve_tokens == 0
    assert capacity.estimated_tokens >= estimate_tokens(data_url)


@pytest.mark.asyncio
async def test_router_capacity_applies_route_history_limit_and_bound_slice() -> None:
    manager = _FakeSessionManager()
    key = "agent:main:router-capacity-route-limit"
    await manager.create(key)
    await manager.append_message(key, "user", "first user " + "x" * 8_000)
    await manager.append_message(key, "assistant", "first answer " + "a" * 8_000)
    await manager.append_message(key, "user", "second user " + "y" * 4_000)
    await manager.append_message(key, "assistant", "second answer " + "b" * 4_000)
    current = await manager.append_message(key, "user", "bound current " + "c" * 40_000)
    await manager.append_message(key, "user", "queued future " + "q" * 40_000)
    entries = await manager.get_transcript(key)
    runner = _new_runner(manager)

    async def project(max_history_turns: int) -> dict[str, Any]:
        return await runner._router_history_capacity_context(
            key,
            entries,
            exclude_last_user=True,
            bound_user_message_id=current.message_id,
            bound_index=4,
            max_history_turns=max_history_turns,
            preserve_image_attachments=False,
        )

    unlimited = await project(0)
    one_turn = await project(1)
    two_turns = await project(2)

    assert unlimited["history_capacity_estimate_complete"] is True
    assert one_turn["history_capacity_estimate_complete"] is True
    assert two_turns["history_capacity_estimate_complete"] is True
    assert unlimited["history_capacity_message_count"] == 4
    assert one_turn["history_capacity_message_count"] == 2
    assert two_turns["history_capacity_message_count"] == 4
    assert one_turn["history_capacity_estimated_tokens"] < two_turns[
        "history_capacity_estimated_tokens"
    ]
    assert two_turns["history_capacity_estimated_tokens"] == unlimited[
        "history_capacity_estimated_tokens"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attachment",
    [
        {
            "type": "image/png",
            "name": "broken.png",
            "data": "this is not valid base64 ***",
        },
        {
            "type": "application/x-unknown",
            "name": "unknown.bin",
            "data": base64.b64encode(b"unknown").decode("ascii"),
        },
        {
            "type": "image/png",
            "name": "missing.png",
            "size": 1,
            "sha256_ref": "0" * 64,
        },
    ],
    ids=["invalid-base64", "unknown-mime", "missing-sha256-ref"],
)
async def test_router_capacity_invalid_inline_image_fails_closed(
    attachment: dict[str, Any],
) -> None:
    manager = _FakeSessionManager()
    key = "agent:main:router-capacity-invalid-inline"
    await manager.create(key)
    invalid = json.dumps(
        {
            "text": "broken historical image",
            "attachments": [attachment],
        },
        separators=(",", ":"),
    )
    await manager.append_message(key, "user", invalid)
    await manager.append_message(key, "assistant", "historical answer")
    current = await manager.append_message(key, "user", "current image turn")
    entries = await manager.get_transcript(key)

    context = await _new_runner(manager)._router_history_capacity_context(
        key,
        entries,
        exclude_last_user=True,
        bound_user_message_id=current.message_id,
        bound_index=2,
        max_history_turns=1,
        preserve_image_attachments=True,
    )

    assert context["history_capacity_estimate_complete"] is False


@pytest.mark.asyncio
async def test_router_capacity_only_fails_closed_for_invalid_media_retained_by_route() -> None:
    manager = _FakeSessionManager()
    key = "agent:main:router-capacity-invalid-route-tail"
    await manager.create(key)
    invalid = json.dumps(
        {
            "text": "broken historical image",
            "attachments": [
                {
                    "type": "image/png",
                    "name": "broken.png",
                    "data": "this is not valid base64 ***",
                }
            ],
        },
        separators=(",", ":"),
    )
    current = _TranscriptEntry(role="user", content="current", message_id="current")
    runner = _new_runner(manager)

    async def project(entries: list[_TranscriptEntry]) -> dict[str, Any]:
        manager._transcripts[key] = entries
        return await runner._router_history_capacity_context(
            key,
            entries,
            exclude_last_user=True,
            bound_user_message_id=current.message_id,
            bound_index=len(entries) - 1,
            max_history_turns=1,
            preserve_image_attachments=True,
        )

    cropped_invalid = await project(
        [
            _TranscriptEntry("user", invalid, "old-broken"),
            _TranscriptEntry("assistant", "old answer", "old-answer"),
            _TranscriptEntry("user", "recent valid turn", "recent-valid"),
            _TranscriptEntry("assistant", "recent answer", "recent-answer"),
            current,
        ]
    )
    retained_invalid = await project(
        [
            _TranscriptEntry("user", "old valid turn", "old-valid"),
            _TranscriptEntry("assistant", "old answer", "old-answer"),
            _TranscriptEntry("user", invalid, "recent-broken"),
            _TranscriptEntry("assistant", "recent answer", "recent-answer"),
            current,
        ]
    )

    assert cropped_invalid["history_capacity_message_count"] == 2
    assert cropped_invalid["history_capacity_estimate_complete"] is True
    assert retained_invalid["history_capacity_message_count"] == 2
    assert retained_invalid["history_capacity_estimate_complete"] is False


@pytest.mark.asyncio
async def test_router_capacity_accepts_retained_missing_reason_marker() -> None:
    manager = _FakeSessionManager()
    key = "agent:main:router-capacity-missing-reason"
    await manager.create(key)
    missing = json.dumps(
        {
            "text": "inspect the unavailable image",
            "attachments": [
                {
                    "type": "image/png",
                    "name": "lost.png",
                    "missing_reason": "attachment persistence disabled",
                }
            ],
        },
        separators=(",", ":"),
    )
    await manager.append_message(key, "user", missing)
    await manager.append_message(key, "assistant", "historical answer")
    current = await manager.append_message(key, "user", "current image turn")
    entries = await manager.get_transcript(key)
    runner = _new_runner(manager)

    replay = runner._project_history_replay(
        entries,
        excluded_entry_indexes={2},
        trim_last_user=True,
        bound_slice_applied=True,
        image_replay_entry_indexes={0},
        media_root=runner._attachment_media_root(),
        session_id=key,
        require_capacity_proof=True,
    )
    context = await runner._router_history_capacity_context(
        key,
        entries,
        exclude_last_user=True,
        bound_user_message_id=current.message_id,
        bound_index=2,
        max_history_turns=1,
        preserve_image_attachments=True,
    )

    marker = "[historical attachment omitted: lost.png (image/png)]"
    marker_count = sum(
        message.content.count(marker)
        for message in replay.messages
        if isinstance(message.content, str)
    )
    assert marker_count == 1
    assert replay.estimate_complete is True
    assert context["history_capacity_message_count"] == 2
    assert context["history_capacity_estimate_complete"] is True


@pytest.mark.asyncio
async def test_router_capacity_preserves_plain_token_floor_but_clears_inline_media_floor() -> None:
    manager = _FakeSessionManager()
    key = "agent:main:router-capacity-token-floor"
    await manager.create(key)
    current = _TranscriptEntry(role="user", content="current", message_id="current")
    runner = _new_runner(manager)
    high_floor = 50_000

    async def project(historical: _TranscriptEntry) -> dict[str, Any]:
        entries = [
            historical,
            _TranscriptEntry("assistant", "short answer", f"{historical.message_id}-answer"),
            current,
        ]
        manager._transcripts[key] = entries
        return await runner._router_history_capacity_context(
            key,
            entries,
            exclude_last_user=True,
            bound_user_message_id=current.message_id,
            bound_index=2,
            max_history_turns=1,
            preserve_image_attachments=True,
        )

    plain = await project(
        _TranscriptEntry(
            "user",
            "small ordinary history row",
            "plain",
            token_count=high_floor,
        )
    )
    inline_envelope = _inline_images(
        "image history row",
        bytes(range(256)) * 120,
    )
    inline_raw_floor = estimate_tokens(inline_envelope)
    inline_media = await project(
        _TranscriptEntry(
            "user",
            inline_envelope,
            "inline-media",
            token_count=inline_raw_floor,
        )
    )

    assert plain["history_capacity_estimated_tokens"] >= high_floor
    assert inline_media["history_capacity_estimate_complete"] is True
    assert inline_media["history_capacity_estimated_tokens"] < inline_raw_floor


@pytest.mark.asyncio
@pytest.mark.parametrize("persist_raw_floor", [False, True])
async def test_router_capacity_mixed_envelope_discounts_only_typed_image_data(
    persist_raw_floor: bool,
) -> None:
    manager = _FakeSessionManager()
    key = "agent:main:router-capacity-mixed-image-document"
    await manager.create(key)
    image_data = base64.b64encode(bytes(range(256)) * 120).decode("ascii")
    document_data = base64.b64encode(bytes(reversed(range(256))) * 80).decode("ascii")
    envelope = json.dumps(
        {
            "text": "inspect the image and document",
            "attachments": [
                {"type": "image/png", "name": "image.png", "data": image_data},
                {"type": "application/pdf", "name": "document.pdf", "data": document_data},
            ],
        },
        separators=(",", ":"),
    )
    raw_floor = estimate_tokens(envelope)
    historical = _TranscriptEntry(
        "user",
        envelope,
        "mixed-history",
        token_count=raw_floor if persist_raw_floor else None,
    )
    answer = _TranscriptEntry("assistant", "answer", "mixed-answer")
    current = _TranscriptEntry("user", "current", "current")
    entries = [historical, answer, current]
    manager._transcripts[key] = entries

    context = await _new_runner(manager)._router_history_capacity_context(
        key,
        entries,
        exclude_last_user=True,
        bound_user_message_id=current.message_id,
        bound_index=2,
        max_history_turns=1,
        preserve_image_attachments=True,
    )

    # The image data becomes a typed media block, while the PDF base64 remains
    # in the conservative residual token floor for this mixed row.
    residual_envelope = envelope.replace(
        json.dumps(image_data),
        json.dumps(f"[history_image_omitted: {len(image_data)} chars]"),
    )
    expected_image_reserve = estimate_provider_media_tokens(
        "image",
        len(base64.b64decode(image_data, validate=True)),
    )
    assert context["history_capacity_estimate_complete"] is True
    assert (
        estimate_tokens(residual_envelope) + expected_image_reserve
        <= context["history_capacity_estimated_tokens"]
    )
    assert context["history_capacity_estimated_tokens"] < raw_floor


@pytest.mark.asyncio
async def test_router_capacity_projection_exception_fails_closed_at_request_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeSessionManager()
    key = "agent:main:router-capacity-projection-error"
    await manager.create(key)
    current = await manager.append_message(key, "user", "current")
    runner = _new_runner(manager)

    async def fail_projection(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("synthetic projection failure with private content")

    monkeypatch.setattr(runner, "_router_history_capacity_context", fail_projection)
    context = await runner._router_history_capacity_for_request(
        key,
        RouterHistoryReplayRequest(
            exclude_last_user=True,
            bound_user_message_id=current.message_id,
        ),
        max_history_turns=1,
        preserve_image_attachments=True,
    )

    assert context == {"history_capacity_estimate_complete": False}


@pytest.mark.asyncio
async def test_router_capacity_request_resolves_bound_and_queued_users_from_snapshot() -> None:
    manager = _FakeSessionManager()
    key = "agent:main:router-capacity-request-snapshot"
    await manager.create(key)
    prior_user = _TranscriptEntry("user", "prior user", "prior-user")
    prior_answer = _TranscriptEntry("assistant", "prior answer", "prior-answer")
    bound = _TranscriptEntry("user", "bound current " + "b" * 20_000, "bound")
    queued = _TranscriptEntry("user", "queued future " + "q" * 20_000, "queued")

    async def load_snapshot() -> list[_TranscriptEntry]:
        return [prior_user, prior_answer, bound, queued]

    snapshot = TurnTranscriptSnapshot(load_snapshot)
    context = await _new_runner(manager)._router_history_capacity_for_request(
        key,
        RouterHistoryReplayRequest(
            exclude_last_user=True,
            bound_user_message_id=bound.message_id,
            transcript_snapshot=snapshot,
        ),
        max_history_turns=1,
        preserve_image_attachments=False,
        reachable_provider_kinds={"tokenrhythm"},
    )

    assert snapshot.load_count == 1
    assert context["history_capacity_estimate_complete"] is True
    assert context["history_capacity_message_count"] == 2
    assert context["history_capacity_estimated_tokens"] < estimate_tokens(bound.content)


@pytest.mark.asyncio
async def test_router_capacity_exact_owner_rejects_replacement_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "router-capacity-owner.db"
    reader_storage = SessionStorage(str(db_path))
    reset_storage = SessionStorage(str(db_path))
    await reader_storage.connect()
    await reset_storage.connect()
    reader = SessionManager(reader_storage, inject_time_prefix=False)
    resetter = SessionManager(reset_storage, inject_time_prefix=False)
    key = "agent:main:router-capacity-owner"
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(tmp_path / "archives"))
    admitted = await reader.create(key)
    bound = await reader.append_message(key, "user", "owner A attachment turn")
    owner = (admitted.session_id, int(admitted.epoch or 0))

    async def load_owner_a() -> list[Any]:
        return await reader.get_transcript(
            key,
            expected_session_id=owner[0],
            expected_session_epoch=owner[1],
        )

    snapshot = TurnTranscriptSnapshot(load_owner_a)
    await snapshot.get_entries()
    try:
        replacement, rotated = await resetter.apply_intent(
            key,
            SessionIntent.RESET_SAME_KEY,
        )
        assert rotated is True
        await reset_storage.save_summary(
            SessionSummary(
                session_id=replacement.session_id,
                session_key=key,
                summary_text="replacement private summary " + ("x" * 12_000),
                covered_through_id=1,
            )
        )

        context = await _new_runner(reader)._router_history_capacity_for_request(
            key,
            RouterHistoryReplayRequest(
                exclude_last_user=True,
                bound_user_message_id=bound.message_id,
                transcript_snapshot=snapshot,
                expected_session_id=owner[0],
                expected_session_epoch=owner[1],
            ),
            max_history_turns=1,
            preserve_image_attachments=False,
            reachable_provider_kinds={"tokenrhythm"},
        )

        assert snapshot.load_count == 1
        assert context["history_capacity_estimated_tokens"] == 0
        assert context["history_capacity_message_count"] == 0
        assert context["history_capacity_estimate_complete"] is False
    finally:
        await reset_storage.close()
        await reader_storage.close()


@pytest.mark.asyncio
async def test_router_capacity_durable_kwargs_only_readers_fail_closed() -> None:
    class _KwargsOnlyDurableManager(_FakeSessionManager):
        def __init__(self) -> None:
            super().__init__()
            self._storage = object()
            self.side_read_count = 0

        async def get_summaries(self, session_key: str, **kwargs: Any) -> list[Any]:
            self.side_read_count += 1
            return await super().get_summaries(session_key)

        async def get_context_states(
            self,
            session_key: str,
            **kwargs: Any,
        ) -> list[Any]:
            self.side_read_count += 1
            return await super().get_context_states(session_key)

    manager = _KwargsOnlyDurableManager()
    key = "agent:main:router-capacity-kwargs-only"
    admitted = await manager.create(key)
    bound = await manager.append_message(key, "user", "current")
    snapshot = TurnTranscriptSnapshot(lambda: manager.get_transcript(key))

    context = await _new_runner(manager)._router_history_capacity_for_request(
        key,
        RouterHistoryReplayRequest(
            exclude_last_user=True,
            bound_user_message_id=bound.message_id,
            transcript_snapshot=snapshot,
            expected_session_id=admitted.session_id,
            expected_session_epoch=0,
        ),
        max_history_turns=1,
        preserve_image_attachments=False,
    )

    assert manager.side_read_count == 0
    assert context["history_capacity_estimate_complete"] is False


@pytest.mark.asyncio
async def test_router_capacity_image_namespace_uses_admitted_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ExactOwnerManager(_FakeSessionManager):
        def __init__(self) -> None:
            super().__init__()
            self._storage = object()
            self.owner_reads: list[tuple[str | None, int | None]] = []

        async def get_summaries(
            self,
            session_key: str,
            *,
            expected_session_id: str | None = None,
            expected_session_epoch: int | None = None,
        ) -> list[Any]:
            self.owner_reads.append((expected_session_id, expected_session_epoch))
            return await super().get_summaries(session_key)

        async def get_context_states(
            self,
            session_key: str,
            *,
            expected_session_id: str | None = None,
            expected_session_epoch: int | None = None,
        ) -> list[Any]:
            self.owner_reads.append((expected_session_id, expected_session_epoch))
            return await super().get_context_states(session_key)

    manager = _ExactOwnerManager()
    key = "agent:main:router-capacity-image-owner"
    admitted = await manager.create(key)
    historical = await manager.append_message(key, "user", "historical image")
    bound = await manager.append_message(key, "user", "current")
    snapshot = TurnTranscriptSnapshot(lambda: manager.get_transcript(key))
    runner = _new_runner(manager)
    projected_session_ids: list[str | None] = []
    original_project = runner._project_history_replay

    def capture_project(*args: Any, **kwargs: Any):
        projected_session_ids.append(kwargs.get("session_id"))
        return original_project(*args, **kwargs)

    monkeypatch.setattr(runner, "_project_history_replay", capture_project)
    context = await runner._router_history_capacity_for_request(
        key,
        RouterHistoryReplayRequest(
            exclude_last_user=True,
            bound_user_message_id=bound.message_id,
            transcript_snapshot=snapshot,
            expected_session_id=admitted.session_id,
            expected_session_epoch=0,
        ),
        max_history_turns=1,
        preserve_image_attachments=True,
    )

    assert historical.message_id is not None
    assert projected_session_ids == [admitted.session_id]
    assert manager.owner_reads == [(admitted.session_id, 0), (admitted.session_id, 0)]
    assert context["history_capacity_estimate_complete"] is True


@pytest.mark.asyncio
async def test_router_capacity_request_snapshot_read_failure_is_incomplete() -> None:
    manager = _FakeSessionManager()
    key = "agent:main:router-capacity-request-snapshot-failure"
    await manager.create(key)

    async def fail_snapshot() -> list[_TranscriptEntry]:
        raise RuntimeError("private transcript read failure")

    context = await _new_runner(manager)._router_history_capacity_for_request(
        key,
        RouterHistoryReplayRequest(
            exclude_last_user=True,
            transcript_snapshot=TurnTranscriptSnapshot(fail_snapshot),
        ),
        max_history_turns=1,
        preserve_image_attachments=True,
    )

    assert context == {"history_capacity_estimate_complete": False}


def test_router_capacity_route_tail_repairs_tool_pairing() -> None:
    projection = HistoryReplayProjection(
        messages=(
            Message(role="user", content="old turn"),
            Message(
                role="assistant",
                content=[ContentBlockToolUse(id="old", name="lookup", input={})],
            ),
            Message(role="user", content="retained turn"),
            Message(
                role="user",
                content=[ContentBlockToolResult(tool_use_id="old", content="old result")],
            ),
            Message(
                role="assistant",
                content=[ContentBlockToolUse(id="retained", name="lookup", input={})],
            ),
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(
                        tool_use_id="retained",
                        content="retained result",
                    )
                ],
            ),
        )
    )

    capacity = project_history_replay_capacity(projection, max_history_turns=1)
    use_ids = {
        block.id
        for message in capacity.messages
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolUse)
    }
    result_ids = {
        block.tool_use_id
        for message in capacity.messages
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolResult)
    }

    assert capacity.message_count == 3
    assert use_ids == result_ids == {"retained"}


@pytest.mark.asyncio
async def test_router_capacity_adds_native_state_and_uncovered_portable_summary() -> None:
    manager = _FakeSessionManager()
    key = "agent:main:router-capacity-mixed-compaction"
    node = await manager.create(key)
    current = await manager.append_message(key, "user", "synthetic current")
    manager._context_states[key] = [
        SessionContextState(
            session_id=node.session_id,
            session_key=key,
            provider="anthropic",
            model="synthetic-model",
            state_kind="anthropic_compaction_block",
            payload={"content": "native checkpoint"},
            covered_through_id=2,
            portable=False,
            cacheable=True,
        )
    ]
    manager._summaries[key] = [
        SessionSummary(
            session_id=node.session_id,
            session_key=key,
            summary_text="newer portable checkpoint",
            covered_through_id=4,
        )
    ]

    context = await _new_runner(manager)._router_previous_assistant_context(
        key,
        exclude_last_user=True,
        bound_user_message_id=current.message_id,
        include_capacity=True,
    )

    native = build_provider_compaction_context(
        context_states=manager._context_states[key],
        provider_kind="anthropic",
    )
    native_payload = [
        message.model_dump(mode="json", exclude_none=True)
        for message in native.messages
    ]
    residual = build_compaction_context_records(
        context_states=manager._context_states[key],
        summaries=manager._summaries[key],
        skip_covered_through_ids=native.covered_through_ids,
    )
    rendered_residual = format_compaction_summary_context(
        [record.text for record in residual]
    )
    assert rendered_residual is not None
    expected = estimate_tokens(
        json.dumps(native_payload, ensure_ascii=False, sort_keys=True, default=str)
    ) + estimate_tokens(rendered_residual)
    assert context["history_capacity_estimated_tokens"] == expected
    assert context["history_capacity_message_count"] == 2


@pytest.mark.asyncio
async def test_router_capacity_limits_native_state_with_combined_provider_replay() -> None:
    manager = _FakeSessionManager()
    key = "agent:main:router-capacity-native-route-tail"
    node = await manager.create(key)
    await manager.append_message(key, "user", "old user " + "u" * 8_000)
    await manager.append_message(key, "assistant", "old answer " + "a" * 8_000)
    await manager.append_message(key, "user", "recent user")
    await manager.append_message(key, "assistant", "recent answer")
    current = await manager.append_message(key, "user", "current")
    entries = await manager.get_transcript(key)
    runner = _new_runner(manager)

    without_native = await runner._router_history_capacity_context(
        key,
        entries,
        exclude_last_user=True,
        bound_user_message_id=current.message_id,
        bound_index=4,
        max_history_turns=1,
    )
    manager._context_states[key] = [
        SessionContextState(
            session_id=node.session_id,
            session_key=key,
            provider="anthropic",
            model="synthetic-model",
            state_kind="anthropic_compaction_block",
            payload={"content": "native checkpoint " + "n" * 20_000},
            covered_through_id=2,
            portable=False,
            cacheable=True,
        )
    ]

    with_native = await runner._router_history_capacity_context(
        key,
        entries,
        exclude_last_user=True,
        bound_user_message_id=current.message_id,
        bound_index=4,
        max_history_turns=1,
    )

    # _load_history prepends native state before Agent.limit_turns. The older
    # native assistant checkpoint is therefore cropped together with the old
    # transcript turn and must not be added back by Router admission.
    assert with_native == without_native
    assert with_native["history_capacity_message_count"] == 2


@pytest.mark.asyncio
async def test_router_capacity_ignores_unreachable_provider_native_state() -> None:
    manager = _FakeSessionManager()
    key = "agent:main:router-capacity-unreachable-native"
    node = await manager.create(key)
    await manager.append_message(key, "user", "history")
    await manager.append_message(key, "assistant", "answer")
    current = await manager.append_message(key, "user", "current")
    entries = await manager.get_transcript(key)
    manager._context_states[key] = [
        SessionContextState(
            session_id=node.session_id,
            session_key=key,
            provider="anthropic",
            model="synthetic-model",
            state_kind="anthropic_compaction_block",
            payload={"content": "stale native checkpoint " + "n" * 20_000},
            covered_through_id=0,
            portable=False,
            cacheable=True,
        )
    ]
    runner = _new_runner(manager)

    async def project(reachable: set[str]) -> dict[str, Any]:
        return await runner._router_history_capacity_context(
            key,
            entries,
            exclude_last_user=True,
            bound_user_message_id=current.message_id,
            bound_index=2,
            reachable_provider_kinds=reachable,
        )

    tokenrhythm_view = await project({"tokenrhythm"})
    anthropic_view = await project({"anthropic"})

    assert tokenrhythm_view["history_capacity_message_count"] == 2
    assert anthropic_view["history_capacity_message_count"] == 3
    assert anthropic_view["history_capacity_estimated_tokens"] > tokenrhythm_view[
        "history_capacity_estimated_tokens"
    ]


@pytest.mark.asyncio
async def test_router_capacity_marks_transcript_read_failure_incomplete() -> None:
    class _FailingTranscriptManager(_FakeSessionManager):
        async def get_transcript(self, session_key: str) -> list[_TranscriptEntry]:
            raise RuntimeError("synthetic transcript failure")

    manager = _FailingTranscriptManager()
    context = await _new_runner(manager)._router_previous_assistant_context(
        "agent:main:failed-transcript",
        include_capacity=True,
    )

    assert context == {"history_capacity_estimate_complete": False}


@pytest.mark.asyncio
async def test_router_capacity_without_session_manager_is_known_empty() -> None:
    runner = TurnRunner(
        provider_selector=MagicMock(),
        session_manager=None,
        config=GatewayConfig(),
    )

    context = await runner._router_previous_assistant_context(
        "agent:main:no-session-manager",
        include_capacity=True,
    )

    assert context == {
        "history_capacity_estimated_tokens": 0,
        "history_capacity_message_count": 0,
        "history_capacity_estimate_complete": True,
    }


@pytest.mark.asyncio
async def test_router_capacity_marks_compaction_state_read_failure_incomplete() -> None:
    class _FailingCompactionManager(_FakeSessionManager):
        async def get_summaries(self, session_key: str) -> list[SessionSummary]:
            raise RuntimeError("synthetic compaction failure")

    manager = _FailingCompactionManager()
    key = "agent:main:failed-compaction"
    await manager.create(key)
    current = await manager.append_message(key, "user", "synthetic current")

    context = await _new_runner(manager)._router_previous_assistant_context(
        key,
        exclude_last_user=True,
        bound_user_message_id=current.message_id,
        include_capacity=True,
    )

    assert context["history_capacity_estimate_complete"] is False
