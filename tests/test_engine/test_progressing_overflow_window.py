"""A later tool round may use local recovery without another paid summary."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine import ErrorEvent as AgentErrorEvent
from opensquilla.provider import (
    ContentBlockToolResult,
    ContentBlockToolUse,
    DoneEvent,
    ErrorEvent,
    Message,
    ModelCapabilities,
    OpenAIProvider,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
    ToolUseEndEvent,
    ToolUseStartEvent,
)
from opensquilla.session.compaction import CompactionResult

CURRENT = "Continue the current request from the available context."
RESULT = "Historical archive material. " * 540


class GrowingProvider(OpenAIProvider):
    def __init__(self, *, refuse_retry=False):
        super().__init__(api_key="synthetic", model="synthetic")
        self.projections = []
        self.calls = 0
        self.tool_rounds = 0
        self.rejections = 0
        self.refuse_retry = refuse_retry
        self.repeat_full_round = False

    async def chat(self, messages, tools=None, config=None):
        projection = self.project_final_request(messages, tools, config)
        self.projections.append(projection)
        self.calls += 1
        if not projection.fits or (self.refuse_retry and self.rejections >= 2):
            self.rejections += 1
            yield ErrorEvent(
                code="provider_request_budget_exhausted", message=json.dumps(projection.proof)
            )
            return
        if self.tool_rounds < 2:
            indices = (0, 1) if not self.tool_rounds or self.repeat_full_round else (1,)
            for index in indices:
                identifier = f"live-{self.tool_rounds}-{index}"
                yield ToolUseStartEvent(tool_use_id=identifier, tool_name="read_archive")
                yield ToolUseEndEvent(
                    tool_use_id=identifier, tool_name="read_archive", arguments={"index": index}
                )
            self.tool_rounds += 1
            yield DoneEvent(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return
        yield TextDeltaEvent(text="Continued after local window recovery.")
        yield DoneEvent(stop_reason="stop", input_tokens=1, output_tokens=1)


def setup_case(monkeypatch, *, retries=1, refuse_retry=False):
    summaries = []

    async def compact(request):
        summaries.append(request)
        return CompactionResult(
            summary="",
            kept_entries=request.entries,
            removed_count=0,
            chunks_processed=1,
            skip_reason="summary_failed",
            failure_kind="incomplete_summary",
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", compact)
    provider = GrowingProvider(refuse_retry=refuse_retry)
    executed = []

    async def tool(call):
        executed.append(call.tool_use_id)
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content=RESULT)

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=16_000,
            context_window_tokens_global_override=16_000,
            context_window_known=True,
            max_tokens=4096,
            max_iterations=6,
            max_provider_retries=0,
            max_overflow_retries=retries,
            model_capabilities=ModelCapabilities(supports_tools=True),
            tool_result_compression_enabled=False,
            tool_result_projection_max_inline_chars=2_000_000,
        ),
        tool_definitions=[
            ToolDefinition(
                name="read_archive",
                description="Read an archive.",
                input_schema=ToolInputSchema(
                    properties={"index": {"type": "integer"}}, required=["index"]
                ),
            )
        ],
        tool_handler=tool,
    )
    history = [
        Message(role="user", content="Prior completed request. " * 360),
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(id=f"old-{i}", name="read_archive", input={"index": i})
                for i in (0, 1)
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(tool_use_id=f"old-{i}", content=RESULT) for i in (0, 1)
            ],
        ),
        Message(role="assistant", content="The prior request is complete."),
    ]
    before = [m.model_dump(mode="json") for m in history]
    agent.set_history(history)
    return agent, provider, summaries, executed, history, before


def assert_pairs(payload):
    pending = set()
    for message in payload["messages"]:
        for call in message.get("tool_calls", []):
            assert call["id"] not in pending
            pending.add(call["id"])
        if message["role"] == "tool":
            assert message["tool_call_id"] in pending
            pending.remove(message["tool_call_id"])
    assert not pending


async def test_new_tool_progress_after_failed_summary_gets_local_only_second_recovery(monkeypatch):
    agent, provider, summaries, executed, history, before = setup_case(monkeypatch)
    events = [event async for event in agent.run_turn(CURRENT)]
    assert len(summaries) == 1
    assert provider.rejections == 2
    assert len(executed) == 3
    assert not [e for e in events if isinstance(e, AgentErrorEvent)]
    assert any(getattr(e, "text", "") == "Continued after local window recovery." for e in events)
    final = provider.projections[-1]
    assert final.fits
    rejected = provider.projections[-2]
    assert final.proof["estimated_chars"] < rejected.proof["estimated_chars"]
    assert CURRENT in json.dumps(final.payload)
    assert_pairs(final.payload)
    assert [m.model_dump(mode="json") for m in history] == before
    assert [m.model_dump(mode="json") for m in agent.history_snapshot()[: len(history)]] == before


async def test_zero_overflow_retry_configuration_remains_disabled(monkeypatch):
    agent, provider, summaries, executed, _, _ = setup_case(monkeypatch, retries=0)
    events = [event async for event in agent.run_turn(CURRENT)]
    assert len(provider.projections) == 1
    assert not summaries and not executed
    assert any(isinstance(e, AgentErrorEvent) for e in events)


async def test_unchanged_second_rejection_does_not_loop_or_buy_another_summary(monkeypatch):
    agent, provider, summaries, executed, _, _ = setup_case(monkeypatch, refuse_retry=True)
    events = [event async for event in agent.run_turn(CURRENT)]
    assert len(summaries) == 1
    assert len(executed) == 3
    assert provider.calls == 5
    assert provider.rejections == 3
    assert any(isinstance(e, AgentErrorEvent) for e in events)


@pytest.mark.parametrize("missing", [False, True])
async def test_second_recovery_requires_stable_consumer_admission(monkeypatch, missing):
    agent, provider, summaries, _, _, _ = setup_case(monkeypatch)
    original = agent._project_durable_consumer_final_request

    def stable(*args, **kwargs):
        value = original(*args, **kwargs)
        if provider.rejections >= 2:
            return None if missing else SimpleNamespace(fits=False, proof=value.proof)
        return value

    monkeypatch.setattr(agent, "_project_durable_consumer_final_request", stable)
    events = [e async for e in agent.run_turn(CURRENT)]
    assert len(summaries) == 1 and provider.calls == 4
    assert any(isinstance(e, AgentErrorEvent) for e in events)


async def test_second_recovery_checks_final_rebuilt_request_not_just_local_candidate(monkeypatch):
    agent, provider, summaries, _, _, _ = setup_case(monkeypatch)
    original = agent._provider_request_messages_async

    async def rebuild(*args, **kwargs):
        messages = await original(*args, **kwargs)
        if provider.rejections >= 2:
            return [*messages, Message(role="user", content="Unavoidable final framing. " * 5000)]
        return messages

    monkeypatch.setattr(agent, "_provider_request_messages_async", rebuild)
    events = [e async for e in agent.run_turn(CURRENT)]
    assert len(summaries) == 1 and provider.calls == 4
    assert any(isinstance(e, AgentErrorEvent) for e in events)


async def test_second_recovery_never_reenters_a_paid_recovery_path(monkeypatch):
    agent, provider, summaries, _, _, _ = setup_case(monkeypatch)
    original = agent._recover_live_turn_request_overflow

    async def recover(*args, **kwargs):
        assert provider.rejections < 2
        return await original(*args, **kwargs)

    monkeypatch.setattr(agent, "_recover_live_turn_request_overflow", recover)
    events = [e async for e in agent.run_turn(CURRENT)]
    assert len(summaries) == 1 and provider.calls == 5
    assert not any(isinstance(e, AgentErrorEvent) for e in events)


async def test_cancellation_during_second_local_rebuild_propagates(monkeypatch):
    agent, provider, summaries, _, _, _ = setup_case(monkeypatch)
    original = agent._provider_request_messages_async

    async def rebuild(*args, **kwargs):
        if provider.rejections >= 2:
            raise asyncio.CancelledError()
        return await original(*args, **kwargs)

    monkeypatch.setattr(agent, "_provider_request_messages_async", rebuild)
    with pytest.raises(asyncio.CancelledError):
        _ = [e async for e in agent.run_turn(CURRENT)]
    assert len(summaries) == 1 and provider.calls == 4


async def test_parent_deadline_during_second_rebuild_is_not_capacity_failure(monkeypatch):
    from opensquilla.engine.agent import _CompactionParentDeadlineError

    agent, provider, summaries, _, _, _ = setup_case(monkeypatch)
    original = agent._provider_request_messages_async

    async def rebuild(*args, **kwargs):
        if provider.rejections >= 2:
            raise _CompactionParentDeadlineError("parent turn expired")
        return await original(*args, **kwargs)

    monkeypatch.setattr(agent, "_provider_request_messages_async", rebuild)
    events = [e async for e in agent.run_turn(CURRENT)]
    errors = [e for e in events if isinstance(e, AgentErrorEvent)]
    assert len(summaries) == 1 and provider.calls == 4
    assert [e.code for e in errors] == ["agent_runtime_timeout"]


async def test_accounting_failure_during_second_rebuild_propagates(monkeypatch):
    from opensquilla.engine.usage_accounting import UsageAccountingUnavailableError

    agent, provider, summaries, _, _, _ = setup_case(monkeypatch)
    original = agent._provider_request_messages_async

    async def rebuild(*args, **kwargs):
        if provider.rejections >= 2:
            raise UsageAccountingUnavailableError("accounting unavailable")
        return await original(*args, **kwargs)

    monkeypatch.setattr(agent, "_provider_request_messages_async", rebuild)
    with pytest.raises(UsageAccountingUnavailableError):
        _ = [e async for e in agent.run_turn(CURRENT)]
    assert len(summaries) == 1 and provider.calls == 4


async def test_unpressured_metric_may_grow_when_complete_request_still_fits(monkeypatch):
    agent, provider, summaries, _, _, _ = setup_case(monkeypatch)
    original = provider.project_final_request
    last_rejected_tokens = 0
    changed = []

    def project(*args, **kwargs):
        nonlocal last_rejected_tokens
        result = original(*args, **kwargs)
        if not result.fits:
            last_rejected_tokens = result.proof["estimated_tokens"]
        elif provider.rejections >= 2:
            # Model an estimator where a replacement marker adds tokens while
            # the actually pressured character metric falls. Admission remains
            # within both original capacities; this is not a second size gate.
            tokens = last_rejected_tokens + 1
            assert tokens < result.proof["effective_proof_token_budget"]
            result.proof["estimated_tokens"] = tokens
            changed.append(result.proof["estimated_chars"])
        return result

    monkeypatch.setattr(provider, "project_final_request", project)
    events = [e async for e in agent.run_turn(CURRENT)]
    assert changed and len(summaries) == 1 and provider.calls == 5
    assert not any(isinstance(e, AgentErrorEvent) for e in events)


async def test_pending_tool_results_cannot_be_dropped_to_force_second_recovery(monkeypatch):
    agent, provider, summaries, _, history, before = setup_case(monkeypatch)

    from opensquilla.execution_status import normalize_execution_status

    async def error_tool(call):
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content=RESULT,
            execution_status=normalize_execution_status(
                {
                    "status": "unknown",
                    "reason": "pending",
                    "source": "tool_runtime",
                    "preservation_class": "ephemeral",
                }
            ),
        )

    agent.tool_handler = error_tool
    agent._raw_tool_handler = error_tool
    events = [e async for e in agent.run_turn(CURRENT)]
    assert len(summaries) == 1 and provider.calls == 4
    assert any(isinstance(e, AgentErrorEvent) for e in events)
    assert [m.model_dump(mode="json") for m in history] == before


async def test_durable_first_compaction_can_shrink_history_before_new_tool_progress(monkeypatch):
    agent, provider, summaries, executed, history, _ = setup_case(monkeypatch)
    earlier = [
        Message(role=role, content="Earlier completed exchange.")
        for _ in range(50)
        for role in ("user", "assistant")
    ]
    agent.set_history([*earlier, *history])
    original_count = len(agent.history_snapshot())

    async def compact(request):
        summaries.append(request)
        protected = int(request.config.protected_recent_messages or 0)
        cut = max(0, len(request.entries) - protected)
        return CompactionResult(
            summary="Earlier request finished.",
            kept_entries=request.entries[cut:],
            removed_count=cut,
            kept_start_index=cut,
            chunks_processed=1,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", compact)
    events = [e async for e in agent.run_turn(CURRENT)]
    assert len(summaries) == 1 and len(executed) == 3
    assert provider.rejections == 2 and provider.calls == 5
    assert any(getattr(e, "kind", "") == "compaction" for e in events)
    assert not any(isinstance(e, AgentErrorEvent) for e in events)
    assert len(agent.history_snapshot()) < original_count
    assert_pairs(provider.projections[-1].payload)


@pytest.mark.parametrize("reject", [False, True])
async def test_local_emergency_event_waits_for_both_final_proofs(monkeypatch, reject):
    agent, provider, _, _, _, _ = setup_case(monkeypatch)
    agent._session_key = "progress-window-event"
    notifications = []
    monkeypatch.setattr(
        "opensquilla.engine.agent.notify_compaction",
        lambda *args, **kwargs: notifications.append(kwargs),
    )
    if reject:
        original = agent._project_durable_consumer_final_request

        def stable(*args, **kwargs):
            value = original(*args, **kwargs)
            return (
                SimpleNamespace(fits=False, proof=value.proof)
                if provider.rejections >= 2
                else value
            )

        monkeypatch.setattr(agent, "_project_durable_consumer_final_request", stable)
    _ = [e async for e in agent.run_turn(CURRENT)]
    local = [event for event in notifications if event.get("reason") == "later_tool_progress"]
    assert len(local) == (0 if reject else 1)
    if local:
        assert local[0]["status"] == "emergency_ephemeral"


async def test_second_overflow_preserves_exact_live_summary_without_new_paid_call(monkeypatch):
    from opensquilla.engine.agent import _LiveTurnCheckpointMessage

    agent, provider, summaries, executed, _, _ = setup_case(monkeypatch)
    agent.set_history([])
    provider.repeat_full_round = True
    current = CURRENT + " Current synthetic task context." * 260
    exact_summary = "Eight retained associations: " + "; ".join(
        f"archive field {i} = preserved-value-{i}" for i in range(8)
    )

    async def compact(request):
        summaries.append(request)
        return CompactionResult(
            summary=exact_summary, kept_entries=[], removed_count=len(request.entries),
            kept_start_index=len(request.entries), chunks_processed=1,
        )

    observed = []
    original = agent._recover_progressed_request_window

    async def local(messages, **kwargs):
        checkpoints = [m for m in messages if isinstance(m, _LiveTurnCheckpointMessage)
                       and m._compaction_has_summary]
        assert len(checkpoints) == 1 and exact_summary in checkpoints[0].content
        before = checkpoints[0].model_dump(mode="json")
        result = await original(messages, **kwargs)
        assert result is not None
        assert any(m is checkpoints[0] for m in result.messages)
        assert checkpoints[0].model_dump(mode="json") == before
        observed.append(checkpoints[0])
        return result

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", compact)
    monkeypatch.setattr(agent, "_recover_progressed_request_window", local)
    events = [e async for e in agent.run_turn(current)]
    assert len(summaries) == 1 and len(observed) == 1
    assert provider.rejections == 2 and len(executed) == 4
    assert not any(isinstance(e, AgentErrorEvent) for e in events)
    final = provider.projections[-1]
    assert final.fits and exact_summary in json.dumps(final.payload)
    assert json.dumps(final.payload).count(current) == 1
    assert_pairs(final.payload)
    wire = json.dumps(final.payload)
    assert wire.index(exact_summary) < wire.index("live-1-0")
    assert "_compaction_has_summary" not in wire
    assert "_compaction_tool_receipts" not in wire
