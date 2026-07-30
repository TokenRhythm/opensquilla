from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine import runtime as runtime_module
from opensquilla.engine.runtime import TurnRunner, _prepend_request_context_prompt
from opensquilla.provider import (
    ChatConfig,
    ContentBlockCompaction,
    DoneEvent,
    Message,
    TextDeltaEvent,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionContextState, SessionSummary
from opensquilla.session.storage import SessionStorage


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


class _CapturingAnthropicProvider(_CapturingProvider):
    provider_name = "anthropic"


def _message_item_hash(message: Message) -> str:
    payload = json.dumps(
        message.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@pytest.fixture
async def session_manager() -> AsyncIterator[SessionManager]:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    yield manager
    await storage.close()


@pytest.mark.asyncio
async def test_load_history_skips_legacy_summary_marker_and_returns_dynamic_context(
    session_manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "agent:main:stable"
    node = await session_manager.create(key)
    await session_manager.append_message(key, "system", "[Context Summary]\nlegacy summary")
    await session_manager.append_message(key, "user", "old question")
    await session_manager.append_message(key, "assistant", "old answer")
    await session_manager._storage.save_summary(
        SessionSummary(
            session_id=node.session_id,
            session_key=key,
            compaction_id="cmp_stored_1",
            summary_text="stored durable summary",
        )
    )
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        runtime_module,
        "notify_compaction",
        lambda session_key, **payload: events.append((session_key, payload)),
    )

    runner = TurnRunner(provider_selector=MagicMock(), session_manager=session_manager)
    agent = Agent(provider=_CapturingProvider(), config=AgentConfig(system_prompt="stable base"))

    summary_context = await runner._load_history(agent, key, trim_last_user=False)

    assert summary_context is not None
    assert "[Compacted Session Summaries]" in summary_context
    assert "stored durable summary" in summary_context
    assert "legacy summary" in summary_context
    assert [message.content for message in agent._history] == ["old question", "old answer"]
    assert agent.config.system_prompt == "stable base"
    replayed = [payload for _, payload in events if payload["status"] == "replayed"]
    assert len(replayed) == 1
    assert replayed[0]["event"] == "compaction.replayed"
    assert replayed[0]["compaction_id"] == "cmp_stored_1"
    assert replayed[0]["replayed_compaction_ids"] == ["cmp_stored_1"]
    assert replayed[0]["summary_count"] == 2
    assert replayed[0]["source"] == "automatic"


@pytest.mark.asyncio
async def test_load_history_surfaces_cancelled_subagent_as_terminal_runtime_notice(
    session_manager: SessionManager,
) -> None:
    key = "agent:main:parent"
    await session_manager.create(key)
    await session_manager.append_message(key, "assistant", "Waiting for the child.")
    await session_manager.append_message(
        key,
        "system",
        json.dumps(
            {
                "type": "subagent_completion",
                "child_session_key": "agent:main:subagent:cancelled",
                "status": "cancelled",
                "terminal_reason": "user_abort",
            }
        ),
        provenance={
            "kind": "internal_system",
            "source_tool": "subagent_completion",
        },
    )
    await session_manager.append_message(key, "user", "continue")
    await session_manager.append_message(key, "assistant", "The child is still running.")
    await session_manager.append_message(key, "user", "continue again")

    runner = TurnRunner(provider_selector=MagicMock(), session_manager=session_manager)
    agent = Agent(provider=_CapturingProvider(), config=AgentConfig(system_prompt="stable base"))

    await runner._load_history(agent, key)

    assert [message.role for message in agent._history] == [
        "assistant",
        "user",
        "assistant",
        "assistant",
    ]
    notice = str(agent._history[-1].content)
    assert "agent:main:subagent:cancelled" in notice
    assert "cancelled" in notice
    assert "no longer running" in notice
    assert "sessions_yield" in notice


@pytest.mark.asyncio
async def test_load_history_prefers_valid_structured_context_state(
    session_manager: SessionManager,
) -> None:
    key = "agent:main:structured-state"
    node = await session_manager.create(key)
    await session_manager.append_message(key, "user", "old question")
    await session_manager.append_message(key, "assistant", "old answer")
    await session_manager._storage.save_summary(
        SessionSummary(
            session_id=node.session_id,
            session_key=key,
            summary_text="plain summary fallback",
            summary_format="structured_v1",
            summary_payload={"current_status": "plain summary fallback"},
            covered_through_id=7,
        )
    )
    await session_manager.save_context_state(
        SessionContextState(
            session_id=node.session_id,
            session_key=key,
            provider="portable",
            state_kind="structured_summary_v1",
            payload={
                "schema_version": 1,
                "user_goal": "continue structured replay",
                "current_status": "structured portable state",
                "critical_carry_forward": ["keep src/opensquilla/session/context_view.py"],
            },
            covered_through_id=7,
            portable=True,
            cacheable=True,
        )
    )

    runner = TurnRunner(provider_selector=MagicMock(), session_manager=session_manager)
    agent = Agent(provider=_CapturingProvider(), config=AgentConfig(system_prompt="stable base"))

    summary_context = await runner._load_history(agent, key, trim_last_user=False)

    assert summary_context is not None
    assert "[Structured Compaction Summary]" in summary_context
    assert "structured portable state" in summary_context
    assert "keep src/opensquilla/session/context_view.py" in summary_context
    assert "plain summary fallback" not in summary_context
    assert '"schema_version"' not in summary_context
    assert [message.content for message in agent._history] == ["old question", "old answer"]


@pytest.mark.asyncio
async def test_load_history_falls_back_to_summary_text_when_context_state_invalid(
    session_manager: SessionManager,
) -> None:
    key = "agent:main:invalid-state"
    node = await session_manager.create(key)
    await session_manager.append_message(key, "user", "old question")
    await session_manager._storage.save_summary(
        SessionSummary(
            session_id=node.session_id,
            session_key=key,
            summary_text="plain summary fallback",
            covered_through_id=7,
        )
    )
    await session_manager.save_context_state(
        SessionContextState(
            session_id=node.session_id,
            session_key=key,
            provider="portable",
            state_kind="structured_summary_v1",
            payload={"schema_version": 1, "current_status": "invalid state text"},
            covered_through_id=7,
            portable=True,
            cacheable=True,
            valid=False,
            invalid_reason="provider switched",
        )
    )

    runner = TurnRunner(provider_selector=MagicMock(), session_manager=session_manager)
    agent = Agent(provider=_CapturingProvider(), config=AgentConfig(system_prompt="stable base"))

    summary_context = await runner._load_history(agent, key, trim_last_user=False)

    assert summary_context is not None
    assert "plain summary fallback" in summary_context
    assert "invalid state text" not in summary_context


@pytest.mark.asyncio
async def test_load_history_replays_anthropic_compaction_state_as_provider_message(
    session_manager: SessionManager,
) -> None:
    key = "agent:main:anthropic-native-state"
    node = await session_manager.create(key)
    await session_manager.append_message(key, "user", "old question")
    await session_manager._storage.save_summary(
        SessionSummary(
            session_id=node.session_id,
            session_key=key,
            summary_text="plain summary fallback",
            covered_through_id=7,
        )
    )
    await session_manager.save_context_state(
        SessionContextState(
            session_id=node.session_id,
            session_key=key,
            provider="anthropic",
            model="claude-opus-4-7",
            state_kind="anthropic_compaction_block",
            payload={
                "content": "native compact state",
                "cache_control": {"type": "ephemeral"},
            },
            covered_through_id=7,
            portable=False,
            cacheable=True,
        )
    )

    runner = TurnRunner(provider_selector=MagicMock(), session_manager=session_manager)
    agent = Agent(
        provider=_CapturingAnthropicProvider(),
        config=AgentConfig(system_prompt="stable base"),
    )

    summary_context = await runner._load_history(agent, key, trim_last_user=False)

    assert summary_context is None
    assert agent._history[0].role == "assistant"
    assert isinstance(agent._history[0].content, list)
    native_block = agent._history[0].content[0]
    assert isinstance(native_block, ContentBlockCompaction)
    assert native_block.content == "native compact state"
    assert native_block.cache_control == {"type": "ephemeral"}
    assert agent._history[1] == Message(role="user", content="old question")


@pytest.mark.asyncio
async def test_load_history_replays_only_latest_anthropic_compaction_state(
    session_manager: SessionManager,
) -> None:
    key = "agent:main:latest-anthropic-native-state"
    node = await session_manager.create(key)
    await session_manager.append_message(key, "user", "old question")
    await session_manager.save_context_state(
        SessionContextState(
            session_id=node.session_id,
            session_key=key,
            provider="anthropic",
            model="claude-opus-4-7",
            state_kind="anthropic_compaction_block",
            payload={"content": "older native compact state"},
            covered_through_id=4,
            portable=False,
            cacheable=True,
        )
    )
    await session_manager.save_context_state(
        SessionContextState(
            session_id=node.session_id,
            session_key=key,
            provider="anthropic",
            model="claude-opus-4-7",
            state_kind="anthropic_compaction_block",
            payload={"content": "newer native compact state"},
            covered_through_id=7,
            portable=False,
            cacheable=True,
        )
    )

    runner = TurnRunner(provider_selector=MagicMock(), session_manager=session_manager)
    agent = Agent(
        provider=_CapturingAnthropicProvider(),
        config=AgentConfig(system_prompt="stable base"),
    )

    await runner._load_history(agent, key, trim_last_user=False)

    native_messages = [
        message
        for message in agent._history
        if isinstance(message.content, list)
        and isinstance(message.content[0], ContentBlockCompaction)
    ]
    assert len(native_messages) == 1
    native_block = native_messages[0].content[0]
    assert isinstance(native_block, ContentBlockCompaction)
    assert native_block.content == "newer native compact state"


@pytest.mark.asyncio
async def test_load_history_keeps_native_compaction_state_out_of_other_providers(
    session_manager: SessionManager,
) -> None:
    key = "agent:main:native-state-other-provider"
    node = await session_manager.create(key)
    await session_manager.append_message(key, "user", "old question")
    await session_manager._storage.save_summary(
        SessionSummary(
            session_id=node.session_id,
            session_key=key,
            summary_text="plain summary fallback",
            covered_through_id=7,
        )
    )
    await session_manager.save_context_state(
        SessionContextState(
            session_id=node.session_id,
            session_key=key,
            provider="anthropic",
            model="claude-opus-4-7",
            state_kind="anthropic_compaction_block",
            payload={"content": "native compact state"},
            covered_through_id=7,
            portable=False,
            cacheable=True,
        )
    )

    runner = TurnRunner(provider_selector=MagicMock(), session_manager=session_manager)
    agent = Agent(provider=_CapturingProvider(), config=AgentConfig(system_prompt="stable base"))

    summary_context = await runner._load_history(agent, key, trim_last_user=False)

    assert summary_context is not None
    assert "plain summary fallback" in summary_context
    assert "native compact state" not in summary_context
    assert agent._history == [Message(role="user", content="old question")]


@pytest.mark.asyncio
async def test_forked_compacted_archive_stays_out_of_provider_messages(
    session_manager: SessionManager,
) -> None:
    parent_key = "agent:main:archive-parent"
    child_key = "agent:main:archive-child"
    await session_manager.create(parent_key)
    for index in range(4):
        await session_manager.append_message(
            parent_key,
            "user",
            f"archive-only old message {index}",
            token_count=5,
        )
    await session_manager.persist_compaction_result(
        parent_key,
        "portable summary without archived row text",
        [{"role": "assistant", "content": "active kept reply"}],
        compaction_id="cmp_provider_boundary",
    )
    await session_manager.branch(parent_key, child_key, fork_transcript=True)

    assert [
        entry.content for entry in await session_manager.get_canonical_transcript(child_key)
    ] == [
        "archive-only old message 0",
        "archive-only old message 1",
        "archive-only old message 2",
        "active kept reply",
    ]
    assert [entry.content for entry in await session_manager.get_transcript(child_key)] == [
        "active kept reply"
    ]

    provider = _CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            system_prompt="stable base",
            cache_breakpoints=[{"text": "stable base", "cache": "true"}],
            cache_mode="auto",
            max_iterations=1,
        ),
        session_key=child_key,
    )
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=session_manager)

    summary_context = await runner._load_history(agent, child_key, trim_last_user=False)
    agent.config.request_context_prompt = _prepend_request_context_prompt(
        agent.config.request_context_prompt,
        summary_context,
    )
    events = [event async for event in agent.run_turn("current question")]

    assert any(event.kind == "done" for event in events)
    messages_payload = json.dumps(
        [
            message.model_dump(mode="json", exclude_none=True)
            for message in provider.calls[0]["messages"]
        ],
        ensure_ascii=False,
    )
    assert "active kept reply" in messages_payload
    assert "portable summary without archived row text" in messages_payload
    assert "archive-only old message" not in messages_payload


@pytest.mark.asyncio
async def test_branch_before_message_does_not_copy_future_summary_context(
    session_manager: SessionManager,
) -> None:
    parent_key = "agent:main:future-parent"
    child_key = "agent:main:future-child"
    parent = await session_manager.create(parent_key)
    await session_manager.append_message(parent_key, "user", "A marker")
    before_entry = await session_manager.append_message(parent_key, "user", "B marker")
    await session_manager.append_message(parent_key, "assistant", "C marker must not leak")
    await session_manager._storage.save_summary(
        SessionSummary(
            session_id=parent.session_id,
            session_key=parent_key,
            summary_text="future summary mentions C marker must not leak",
            summary_format="structured_v1",
            covered_through_id=999,
        )
    )
    await session_manager.save_context_state(
        SessionContextState(
            session_id=parent.session_id,
            session_key=parent_key,
            provider="portable",
            state_kind="structured_summary_v1",
            payload={"future": "C marker must not leak"},
            covered_through_id=999,
            portable=True,
            cacheable=True,
        )
    )

    await session_manager.branch(
        parent_key,
        child_key,
        fork_transcript=True,
        fork_before_message_id=before_entry.message_id,
    )

    runner = TurnRunner(provider_selector=MagicMock(), session_manager=session_manager)
    agent = Agent(provider=_CapturingProvider(), config=AgentConfig(system_prompt="stable base"))

    summary_context = await runner._load_history(agent, child_key, trim_last_user=False)

    assert [message.content for message in agent._history] == ["A marker"]
    assert summary_context is None or "C marker must not leak" not in summary_context
    assert "C marker must not leak" not in json.dumps(
        [message.model_dump(mode="json", exclude_none=True) for message in agent._history],
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_summary_context_is_request_only_and_keeps_system_cache_anchor(
    session_manager: SessionManager,
) -> None:
    key = "agent:main:stable"
    node = await session_manager.create(key)
    await session_manager.append_message(key, "user", "old question")
    await session_manager.append_message(key, "assistant", "old answer")
    await session_manager._storage.save_summary(
        SessionSummary(
            session_id=node.session_id,
            session_key=key,
            summary_text="summary outside transcript",
        )
    )
    provider = _CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            system_prompt="stable base",
            request_context_prompt="<memory_context>volatile recall</memory_context>",
            cache_breakpoints=[{"text": "stable base", "cache": "true"}],
            cache_mode="auto",
            max_iterations=1,
        ),
        session_key=key,
    )
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=session_manager)

    summary_context = await runner._load_history(agent, key, trim_last_user=False)
    agent.config.request_context_prompt = _prepend_request_context_prompt(
        agent.config.request_context_prompt,
        summary_context,
    )
    events = [event async for event in agent.run_turn("current question")]

    assert any(event.kind == "done" for event in events)
    call = provider.calls[0]
    assert call["config"].system == "stable base"
    assert call["config"].cache_breakpoints == [{"text": "stable base", "cache": "true"}]
    assert [message.content for message in call["messages"][0:2]] == [
        "old question",
        "old answer",
    ]
    request_context = call["messages"][2].content
    assert "[Request context for this turn]" in request_context
    assert "[Compacted Session Summaries]" in request_context
    assert "summary outside transcript" in request_context
    assert "<memory_context>volatile recall</memory_context>" in request_context
    assert call["messages"][-1].role == "user"
    assert call["messages"][-1].content.startswith("current question")
    assert "[Runtime context for this turn]" in call["messages"][-1].content
    assert all(
        "summary outside transcript" not in message.content
        for message in agent._history
        if isinstance(message.content, str)
    )


@pytest.mark.asyncio
async def test_changing_request_context_does_not_pollute_persisted_history_prefix() -> None:
    provider = _CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            system_prompt="stable base",
            request_context_prompt="<memory_context>volatile one</memory_context>",
            cache_breakpoints=[{"text": "stable base", "cache": "true"}],
            cache_mode="auto",
            max_iterations=1,
        ),
    )
    agent.set_history(
        [
            Message(role="user", content="old question"),
            Message(role="assistant", content="old answer"),
        ]
    )

    first_events = [event async for event in agent.run_turn("first question")]
    agent.config.request_context_prompt = "<memory_context>volatile two</memory_context>"
    second_events = [event async for event in agent.run_turn("second question")]

    assert any(event.kind == "done" for event in first_events)
    assert any(event.kind == "done" for event in second_events)
    first_messages = provider.calls[0]["messages"]
    second_messages = provider.calls[1]["messages"]
    assert first_messages[0] == Message(role="user", content="old question")
    assert first_messages[1] == Message(role="assistant", content="old answer")
    assert second_messages[0] == Message(role="user", content="old question")
    assert second_messages[1] == Message(role="assistant", content="old answer")
    assert [_message_item_hash(message) for message in first_messages[0:2]] == [
        _message_item_hash(message) for message in second_messages[0:2]
    ]
    old_prefix_payload = json.dumps(
        [message.model_dump(mode="json", exclude_none=True) for message in second_messages[0:2]],
        ensure_ascii=False,
    )
    assert "<memory_context>volatile two</memory_context>" not in old_prefix_payload
