"""Completed receipts cannot consume an otherwise usable >=32K request."""

from __future__ import annotations

import copy
import json

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.agent import _LiveTurnCheckpointMessage
from opensquilla.execution_status import normalize_execution_status
from opensquilla.provider import (
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    OpenAIProvider,
)
from opensquilla.provider.protocol import project_provider_final_request
from opensquilla.session.compaction import CompactionResult

CURRENT = "Continue the same task. Do not repeat completed writes. Report unknown status honestly."


def make_agent():
    agent = Agent(
        provider=OpenAIProvider(api_key="synthetic-no-network", model="synthetic"),
        config=AgentConfig(context_window_tokens=32768, context_window_known=True, max_tokens=4096),
    )
    agent._current_turn_message = CURRENT
    config = agent._provider_admission_chat_config(CURRENT, context_window_tokens=32768)
    return agent, config


def tool_round(index, *, path_chars=300):
    return [
        Message(role="assistant", content=[ContentBlockToolUse(
            id=f"write-{index}", name="write_file",
            input={"path": f"output-{index}/" + "subdir/" * (path_chars // 7), "content": "done"},
        )]),
        Message(role="user", content=[ContentBlockToolResult(
            tool_use_id=f"write-{index}", content="Actual returned result.",
            is_error=index % 3 == 0,
            execution_status=normalize_execution_status({
                "status": ("error", "success", "unknown")[index % 3],
                "source": "tool_runtime", "reason": "returned",
            }),
        )]),
    ]


def project(agent, config, messages):
    assembled = agent._provider_request_messages_for_count_projection(
        messages, request_context_insert_index=0, runtime_context_insert_index=0,
        request_context_message=None,
        runtime_context_message=agent._freeze_preflight_runtime_context_message(),
    )
    projection = project_provider_final_request(agent.provider, assembled, config=config)
    assert projection is not None
    return projection


def assert_preserved(agent, original, messages, expected):
    assert original[0].content == CURRENT
    assert messages[0].content == CURRENT
    assert agent._live_turn_execution_receipts(messages[1:]) == expected
    checkpoints = [item for item in messages if isinstance(item, _LiveTurnCheckpointMessage)]
    assert checkpoints
    assert any('"omitted_receipts"' in str(item.content) for item in checkpoints)
    for item in checkpoints:
        if '"omitted_receipts"' not in str(item.content):
            continue
        visible = json.loads(item._compaction_receipt_text)
        assert visible["returned_calls"] == len(item._compaction_tool_receipts)
        by_id = {receipt["tool_call_id"]: receipt for receipt in expected}
        for receipt in visible["receipts"]:
            source = by_id[receipt["tool_call_id"]]
            assert receipt["execution_status"] == source["execution_status"]
            assert receipt["is_error"] == source["is_error"]
            assert receipt["arguments_complete"] is False
            assert "arguments" not in receipt


@pytest.mark.parametrize("count,path_chars", [(1000, 300), (1, 200000)])
def test_receipts_fit_full_adapter_request_without_changing_private_facts(count, path_chars):
    agent, config = make_agent()
    raw = [message for i in range(count) for message in tool_round(i, path_chars=path_chars)]
    receipts = agent._live_turn_execution_receipts(raw)
    before = copy.deepcopy(raw)
    messages = [
        Message(role="user", content=CURRENT),
        *agent._live_turn_checkpoint_messages("Completed work is recorded below.", receipts),
    ]
    original = copy.deepcopy(messages)
    assert not project(agent, config, messages).fits
    fitted, proof = agent._fit_live_turn_receipts(
        messages, project=lambda candidate: project(agent, config, candidate),
        fits=lambda candidate: candidate is not None and candidate.fits,
    )
    assert proof.fits
    assert project(agent, config, fitted).payload == proof.payload
    assert_preserved(agent, original, fitted, receipts)
    assert raw == before and messages == original
    assert "Completed work is recorded below." in fitted[1].content
    assert "_compaction_tool_receipts" not in json.dumps(proof.payload)


@pytest.mark.parametrize("use_summary", [False, True])
async def test_recovery_closures_budget_long_completed_paths(monkeypatch, use_summary):
    agent, config = make_agent()
    raw = [Message(role="user", content=CURRENT)]
    for index in range(25):
        raw.extend(tool_round(index, path_chars=8000))
    before = copy.deepcopy(raw)
    receipts = agent._live_turn_execution_receipts(raw[1:])
    assert not project(agent, config, raw).fits
    summary_calls = []

    async def summarize(request):
        summary_calls.append(request)
        assert request.consumer_admission("Returned calls are recorded below.", [])
        return CompactionResult(
            summary="Returned calls are recorded below.", kept_entries=[],
            removed_count=len(request.entries), kept_start_index=len(request.entries),
            chunks_processed=1,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", summarize)
    if use_summary:
        result = await agent._recover_live_turn_request_overflow(
            raw, protected_turn_start_index=0, context_window_tokens=32768,
            request_context_insert_index=0, runtime_context_insert_index=0,
            consumer_chat_config=config,
        )
    else:
        result = agent._recover_local_request_window(
            raw, protected_turn_start_index=0, request_context_insert_index=0,
            runtime_context_insert_index=0, config=config,
        )
    assert result is not None and result.ephemeral_only
    if use_summary:
        assert len(summary_calls) == 1
        assert "Returned calls are recorded below." in result.messages[1].content
    assert project(agent, config, result.messages).fits
    assert_preserved(agent, before, result.messages, receipts)
    assert raw == before
    # A later recovery reads private facts, not the shorter text as new truth.
    assert agent._live_turn_execution_receipts(result.messages[1:]) == receipts


def test_receipt_projection_does_not_hide_oversize_current_request():
    agent, config = make_agent()
    current = Message(role="user", content=CURRENT + " protected input" * 40000)
    messages = [current, *agent._live_turn_checkpoint_messages(None, [])]
    fitted, proof = agent._fit_live_turn_receipts(
        messages, project=lambda candidate: project(agent, config, candidate),
        fits=lambda candidate: candidate is not None and candidate.fits,
    )
    assert not proof.fits and fitted[0] is current


def test_receipt_projection_honors_consumer_only_parent_deadline(monkeypatch):
    import time

    from opensquilla.engine.agent import _CompactionParentDeadlineError

    agent, config = make_agent()
    agent._compaction_request_context = None
    expired = config.model_copy(update={"turn_deadline_at_monotonic": time.monotonic() - 1})
    receipts = agent._live_turn_execution_receipts(tool_round(0, path_chars=200000))
    messages = [Message(role="user", content=CURRENT),
                *agent._live_turn_checkpoint_messages(None, receipts)]
    projected = []

    def project_once(candidate):
        projected.append(candidate)
        return project(agent, config, candidate)

    with pytest.raises(_CompactionParentDeadlineError):
        agent._fit_live_turn_receipts(
            messages, project=project_once,
            fits=lambda proof: proof is not None and proof.fits,
            chat_config=expired,
        )
    assert not projected


async def test_live_summary_receipts_use_same_frozen_tools_as_consumer_admission(monkeypatch):
    from opensquilla.provider.types import ToolDefinition
    from opensquilla.session.compaction import CompactionRequestContext

    # The frozen schema must itself fit with the conservative offline estimator.
    monkeypatch.setattr("opensquilla.token_estimation._get_encoding", lambda: None)
    agent, config = make_agent()
    frozen_tools = [ToolDefinition(
        name="frozen_schema", description="schema material " * 2000,
        input_schema={"type": "object", "properties": {}},
    )]
    # A physical attempt freezes its selected schema. Its subsequent recovery
    # must budget that same envelope, not an empty/current registration list.
    assert not agent.tool_definitions
    agent._compaction_request_context = CompactionRequestContext(
        chat_config=config, tools=tuple(frozen_tools),
    )
    raw = [Message(role="user", content=CURRENT)]
    for index in range(300):
        raw.extend(tool_round(index, path_chars=1000))
    before = copy.deepcopy(raw)
    receipts = agent._live_turn_execution_receipts(raw[1:])
    naive = [raw[0], *agent._live_turn_checkpoint_messages("Returned work.", receipts)]
    without_tools, no_tools_proof = agent._fit_live_turn_receipts(
        naive, project=lambda view: project(agent, config, view),
        fits=lambda proof: proof is not None and proof.fits, chat_config=config,
    )
    assert no_tools_proof.fits

    def frozen_proof(view):
        assembled = agent._provider_request_messages_for_count_projection(
            view, request_context_insert_index=0, runtime_context_insert_index=0,
            request_context_message=None,
            runtime_context_message=agent._freeze_preflight_runtime_context_message(),
        )
        return project_provider_final_request(
            agent.provider, assembled, tools=frozen_tools, config=config,
        )

    assert not frozen_proof(without_tools).fits
    admissions = []

    async def summarize(request):
        admissions.append(request.consumer_admission("Returned work.", []))
        assert admissions[-1]
        return CompactionResult(
            summary="Returned work.", kept_entries=[], removed_count=len(request.entries),
            kept_start_index=len(request.entries), chunks_processed=1,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", summarize)
    outcome = await agent._recover_live_turn_request_overflow(
        raw, protected_turn_start_index=0, context_window_tokens=32768,
        request_context_insert_index=0, runtime_context_insert_index=0,
        consumer_chat_config=config,
    )
    assert admissions == [True]
    assert outcome is not None and outcome.ephemeral_only
    assert frozen_proof(outcome.messages).fits
    assert agent._live_turn_execution_receipts(outcome.messages[1:]) == receipts
    assert raw == before
