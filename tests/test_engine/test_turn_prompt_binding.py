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

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider import ChatConfig, DoneEvent, Message, TextDeltaEvent
from opensquilla.session.context_view import (
    build_compaction_context_records,
    build_provider_compaction_context,
    format_compaction_summary_context,
)
from opensquilla.session.models import SessionContextState, SessionSummary
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
