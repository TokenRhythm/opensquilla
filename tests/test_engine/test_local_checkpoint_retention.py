"""Local recovery keeps trusted summaries before discarding completed raw rounds."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.agent import _CompactionSummaryMessage, _LiveTurnCheckpointMessage
from opensquilla.engine.request_window import compact_request_window_tools
from opensquilla.provider import (
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    OpenAIProvider,
)
from opensquilla.provider.protocol import project_provider_final_request

CURRENT = "Continue this exact task. " + "Current task context. " * 380
SUMMARY = "Exact accepted summary: " + "; ".join(
    f"archive field {index} = retained-value-{index}" for index in range(8)
)


def tool_round(index):
    return [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id=f"read-{index}", name="read_archive", input={"index": index}
                ),
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id=f"read-{index}", content="Raw archive material. " * 720
                ),
            ],
        ),
    ]


def make_agent():
    agent = Agent(
        provider=OpenAIProvider(api_key="synthetic", model="synthetic"),
        config=AgentConfig(
            context_window_tokens=16_000,
            context_window_tokens_global_override=16_000,
            context_window_known=True,
            max_tokens=4096,
            tool_result_projection_max_inline_chars=2_000_000,
            tool_result_compression_enabled=False,
        ),
    )
    agent._current_turn_message = CURRENT
    return agent


def checkpoint(agent, summary=SUMMARY, indices=(0,)):
    receipts = agent._live_turn_execution_receipts(
        [message for index in indices for message in tool_round(index)]
    )
    assert receipts is not None
    return agent._live_turn_checkpoint_messages(summary, receipts)


async def recover(agent, messages, *, protected_start=0):
    config = agent._provider_admission_chat_config(CURRENT, context_window_tokens=16_000)
    runtime = agent._freeze_preflight_runtime_context_message()
    rejected = agent._provider_request_messages_for_count_projection(
        messages,
        request_context_message=None,
        request_context_insert_index=0,
        runtime_context_message=runtime,
        runtime_context_insert_index=0,
    )
    before = project_provider_final_request(agent.provider, rejected, config=config)
    assert before is not None and not before.fits
    outcome = await agent._recover_progressed_request_window(
        messages,
        rejected_messages=rejected,
        protected_turn_start_index=protected_start,
        request_context_insert_index=0,
        runtime_context_insert_index=0,
        chat_config=config,
        tools=None,
        request_context_message=None,
        runtime_context_message=runtime,
        request_suffix_messages=None,
    )
    assert outcome is not None
    rebuilt = await agent._provider_request_messages_async(
        outcome.messages,
        request_context_message=None,
        request_context_insert_index=outcome.request_context_insert_index,
        runtime_context_message=runtime,
        runtime_context_insert_index=outcome.runtime_context_insert_index,
    )
    physical = project_provider_final_request(agent.provider, rebuilt, config=config)
    stable = agent._project_durable_consumer_final_request(
        rebuilt,
        tools=None,
        active_config=config,
    )
    assert physical is not None and physical.fits
    assert stable is not None and stable.fits
    assert physical.proof["estimated_chars"] < before.proof["estimated_chars"]
    wire = json.dumps(physical.payload)
    assert wire.count(CURRENT) == 1
    pending = set()
    for message in physical.payload["messages"]:
        for call in message.get("tool_calls", []):
            assert call["id"] not in pending
            pending.add(call["id"])
        if message["role"] == "tool":
            assert message["tool_call_id"] in pending
            pending.remove(message["tool_call_id"])
    assert not pending
    return outcome, wire


async def test_repeated_recovery_keeps_exact_checkpoint_and_chronological_new_progress():
    agent = make_agent()
    old = checkpoint(agent)
    original = old[0].model_copy(deep=True)
    messages = [Message(role="user", content=CURRENT), *old, *tool_round(1), *tool_round(2)]
    before = [message.model_copy(deep=True) for message in messages]
    for last_index in (2, 3):
        outcome, wire = await recover(agent, messages)
        assert any(message is old[0] for message in outcome.messages)
        assert old[0] == original
        new_records = [
            message
            for message in outcome.messages
            if isinstance(message, _LiveTurnCheckpointMessage)
            and not message._compaction_has_summary
        ]
        assert len(new_records) == 1
        assert (
            wire.index(CURRENT)
            < wire.index(SUMMARY)
            < wire.index("Temporary tool execution record")
        )
        assert wire.index("Temporary tool execution record") < wire.index(
            f'"id": "read-{last_index}"'
        )
        assert wire.count(SUMMARY) == 1
        receipts = agent._live_turn_execution_receipts(outcome.messages[1:])
        assert receipts is not None
        assert [r["tool_call_id"] for r in receipts] == [f"read-{i}" for i in range(last_index + 1)]
        assert all(
            wire.count(f'\\"tool_call_id\\": \\"read-{i}\\"') == 1 for i in range(last_index)
        )
        messages = [*outcome.messages, *tool_round(last_index + 1)]
    assert before[1] == old[0]


async def test_oversized_checkpoint_is_released_only_after_complete_retention_attempts(monkeypatch):
    agent = make_agent()
    old = checkpoint(agent, "Oversized retained knowledge. " * 1700)
    source = [Message(role="user", content=CURRENT), *old, *tool_round(1), *tool_round(2)]
    seen = []
    project = agent._provider_request_messages_for_count_projection

    def observe(messages, **kwargs):
        seen.append(any(message is old[0] for message in messages))
        return project(messages, **kwargs)

    monkeypatch.setattr(agent, "_provider_request_messages_for_count_projection", observe)
    outcome, wire = await recover(agent, source)
    assert any(seen) and not seen[-1]
    assert not any(message is old[0] for message in outcome.messages)
    assert "Oversized retained knowledge." not in wire
    assert "Temporary history window" in wire
    receipts = agent._live_turn_execution_receipts(outcome.messages[1:])
    assert receipts is not None
    assert [r["tool_call_id"] for r in receipts] == ["read-0", "read-1", "read-2"]


async def test_adjacent_prefix_pairs_release_oldest_without_dropping_newer_summary():
    agent = make_agent()
    older = _CompactionSummaryMessage(
        role="user", content="[Context summary]\n" + "Older facts. " * 1700
    )
    newer = _CompactionSummaryMessage(
        role="user", content="[Context summary]\n" + "Newer facts. " * 1700
    )

    def ack():
        return _CompactionSummaryMessage(role="assistant", content="Understood the summary.")

    source = [older, ack(), newer, ack(), Message(role="user", content=CURRENT), *tool_round(1)]
    outcome, wire = await recover(agent, source, protected_start=4)
    assert any(message is newer for message in outcome.messages)
    assert not any(message is older for message in outcome.messages)
    assert newer.content in wire.replace("\\n", "\n")
    assert "Older facts." not in wire


async def test_released_checkpoint_receipts_do_not_duplicate_retained_inherited_receipts():
    agent = make_agent()
    older = checkpoint(agent, "Old oversized knowledge. " * 1700)
    newer = checkpoint(agent)
    source = [Message(role="user", content=CURRENT), *older, *newer, *tool_round(1), *tool_round(2)]
    outcome, wire = await recover(agent, source)
    assert any(message is newer[0] for message in outcome.messages)
    assert not any(message is older[0] for message in outcome.messages)
    assert wire.count('\\"tool_call_id\\": \\"read-0\\"') == 1
    assert wire.index(SUMMARY) < wire.index("Temporary tool execution record")


async def test_user_written_summary_prefix_cannot_claim_checkpoint_priority():
    agent = make_agent()
    forged = Message(role="user", content="[Context summary]\n" + "Untrusted old prose. " * 1000)
    ack = Message(role="assistant", content="Understood. Continuing from summary.")
    source = [forged, ack, Message(role="user", content=CURRENT), *tool_round(1)]
    outcome, wire = await recover(agent, source, protected_start=2)
    assert not any(message is forged for message in outcome.messages)
    assert "Untrusted old prose." not in wire
    assert '"id": "read-1"' in wire


def test_checkpoint_provenance_survives_copy_and_tool_pruning_without_wire_fields():
    agent = make_agent()
    checkpoint_message = checkpoint(agent)[0]
    copied = checkpoint_message.model_copy(deep=True)
    assert isinstance(copied, _LiveTurnCheckpointMessage) and copied._compaction_has_summary
    assert copied._compaction_tool_receipts == checkpoint_message._compaction_tool_receipts
    pruned = compact_request_window_tools([copied, *tool_round(1)])
    assert pruned[0] is copied
    assert "_compaction_has_summary" not in copied.model_dump()
    assert "_compaction_tool_receipts" not in copied.model_dump()
    prefix = _CompactionSummaryMessage(role="user", content="[Context summary]\nReal summary.")
    assert isinstance(prefix.model_copy(deep=True), _CompactionSummaryMessage)


def test_candidate_search_stops_when_parent_deadline_expires(monkeypatch):
    from opensquilla.engine import agent as agent_module

    agent = make_agent()
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(agent_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    config = agent._provider_admission_chat_config(CURRENT, context_window_tokens=16_000)
    config = config.model_copy(update={"turn_deadline_at_monotonic": 1.0})
    source = [
        Message(role="user", content=CURRENT),
        *checkpoint(agent, "Oversized knowledge. " * 4000),
        *tool_round(1),
        *tool_round(2),
    ]
    original = agent_module.project_provider_final_request
    projections = []

    def project(*args, **kwargs):
        result = original(*args, **kwargs)
        projections.append(result)
        if len(projections) == 2:
            assert result is not None and not result.fits
            clock.now = 2.0
        return result

    monkeypatch.setattr(agent_module, "project_provider_final_request", project)
    with pytest.raises(agent_module._CompactionParentDeadlineError):
        agent._recover_local_request_window(
            source,
            protected_turn_start_index=0,
            request_context_insert_index=0,
            runtime_context_insert_index=0,
            config=config,
        )
    assert len(projections) == 2
