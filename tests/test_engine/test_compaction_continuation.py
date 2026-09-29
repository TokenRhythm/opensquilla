"""Real request assembly and continuation when the auxiliary model is unavailable."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine import DoneEvent as AgentDoneEvent
from opensquilla.engine import ErrorEvent as AgentErrorEvent
from opensquilla.engine.runtime import TurnRunner
from opensquilla.execution_status import normalize_execution_status
from opensquilla.provider import (
    ContentBlockToolResult,
    ContentBlockToolUse,
    DoneEvent,
    ErrorEvent,
    Message,
    OpenAIProvider,
    TextDeltaEvent,
)


class ContinuingProvider(OpenAIProvider):
    def __init__(self, failure="503"):
        super().__init__(api_key="synthetic-unused", model="synthetic",
                         base_url="https://invalid.example/v1")
        self.calls = []
        self.local_refusals = []
        self.summary_available = False
        self.failure = failure

    async def chat(self, messages, tools=None, config=None):
        summary = config is not None and config.candidate_output_mode == "inert_artifact"
        projection = self.project_final_request(messages, tools, config)
        # Like the real adapter, refuse a locally unfit envelope before the
        # simulated transport boundary. These are not physical API requests.
        if not (projection.fits and projection.proof.get("fits", True)):
            self.local_refusals.append(projection)
            yield ErrorEvent(message="synthetic request exceeds capacity",
                             code="provider_request_budget_exhausted")
            return
        self.calls.append((summary, [m.model_copy(deep=True) for m in messages], projection))
        if summary and not self.summary_available:
            if self.failure in {"empty", "length", "tool_calls"}:
                if self.failure != "empty":
                    yield TextDeltaEvent(text="incomplete synthetic summary")
                yield DoneEvent(stop_reason="stop" if self.failure == "empty" else self.failure)
            else:
                yield ErrorEvent(message="synthetic summary unavailable", code=self.failure)
        else:
            yield TextDeltaEvent(text=(
                "Task constraint: retain violet setting. Earlier work is complete."
                if summary else "continued with violet setting"
            ))
            yield DoneEvent(stop_reason="stop", input_tokens=100, output_tokens=12)


@pytest.fixture(autouse=True)
def deterministic_estimation(monkeypatch):
    from opensquilla import token_estimation
    monkeypatch.setattr(token_estimation, "_encoding", token_estimation._ENCODING_UNAVAILABLE)


@pytest.mark.parametrize("failure", ["503", "timeout", "empty", "length", "tool_calls"])
async def test_twenty_turns_share_failure_circuit_and_recover_from_canonical_history(failure):
    provider = ContinuingProvider(failure)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=None)
    key = "synthetic-continuation"
    config = AgentConfig(context_window_tokens=16000, max_tokens=1024, model_id="synthetic")
    config.compaction_circuit_open = lambda: runner._compaction_circuit_open(key)
    config.compaction_outcome_reporter = lambda success: (
        runner._record_compaction_success(key) if success
        else runner._record_compaction_failure(key)
    )
    agent = Agent(provider=provider, config=config)
    original = [Message(
        role="user" if i % 2 == 0 else "assistant",
        content=f"original-{i}: retain violet setting. " + "a b c d " * 700,
    ) for i in range(8)]
    before = [m.model_copy(deep=True) for m in original]
    agent.set_history(original)
    for index in range(20):
        events = [event async for event in agent.run_turn(f"continue task, step {index}")]
        assert any(isinstance(event, AgentDoneEvent) for event in events), events
        assert not any(isinstance(event, AgentErrorEvent) for event in events)
        assert original == before
        assert agent.history_snapshot()[:len(original)] == before
    assert runner._compaction_failures[key].count == 3
    assert sum(summary for summary, _, _ in provider.calls) == 3
    main = [(messages, proof) for summary, messages, proof in provider.calls if not summary]
    assert len(main) == 20
    assert all(proof.fits and proof.proof.get("fits", True) for _, proof in main)
    assert any("Temporary history window" in str(m.content) for m in main[-1][0])
    # Re-enter the half-open state without sleeping or changing global clocks.
    runner._compaction_failures[key].opened_at = float("-inf")
    provider.summary_available = True
    start = len(provider.calls)
    events = [event async for event in agent.run_turn("continue after summary service recovery")]
    assert any(isinstance(event, AgentDoneEvent) for event in events), events
    assert not any(isinstance(event, AgentErrorEvent) for event in events)
    summaries = [messages for summary, messages, _ in provider.calls[start:] if summary]
    assert summaries
    assert any("original-0" in str(message.content) for message in summaries[0])
    assert key not in runner._compaction_failures


async def test_disabled_summary_still_continues_with_a_request_window():
    provider = ContinuingProvider()
    agent = Agent(provider=provider, config=AgentConfig(
        context_window_tokens=16000, max_tokens=1024, model_id="synthetic",
        compaction_enabled=False,
    ))
    original = [
        Message(role="user", content="old task"),
        Message(role="assistant", content="large completed reply " * 8000),
    ]
    agent.set_history(original)
    events = [event async for event in agent.run_turn("continue")]
    assert any(isinstance(event, AgentDoneEvent) for event in events), events
    assert not any(isinstance(event, AgentErrorEvent) for event in events)
    assert provider.calls and not any(summary for summary, _, _ in provider.calls)
    assert agent.history_snapshot()[:2] == original
    assert all(proof.fits for _, _, proof in provider.calls)


@pytest.mark.parametrize("live", [False, True])
def test_completed_error_can_leave_live_turn_but_pending_tool_cannot(live):
    provider = ContinuingProvider()
    agent = Agent(provider=provider, config=AgentConfig(
        context_window_tokens=16000, max_tokens=1024, model_id="synthetic",
    ))
    agent._current_turn_message = "continue exact task"
    messages = [
        Message(role="user", content=agent._current_turn_message),
        Message(role="assistant", content=[ContentBlockToolUse(
            id="work", name="read_file", input={"path": "synthetic.txt"},
        )]),
        Message(role="user", content=[ContentBlockToolResult(
            tool_use_id="work", content="huge result " * 16000, is_error=not live,
            execution_status=normalize_execution_status({
                "status": "unknown" if live else "error", "source": "runtime",
                "reason": "pending" if live else "nonzero_exit",
            }),
        )]),
    ]
    before = [message.model_copy(deep=True) for message in messages]
    outcome = agent._recover_local_request_window(
        messages, protected_turn_start_index=0, request_context_insert_index=0,
        runtime_context_insert_index=0, input_budget_tokens=1000,
    )
    assert messages == before
    if live:
        assert outcome is None
    else:
        assert outcome and outcome.ephemeral_only
        assert outcome.messages[-1] is messages[0]


def test_circuit_is_scoped_to_deployment_and_policy():
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=None)
    key = "synthetic-scope"
    runner._bind_compaction_failure_scope(key, ("provider", "model-a", "profile-a"))
    for _ in range(3):
        runner._record_compaction_failure(key)
    assert runner._compaction_circuit_open(key)
    runner._bind_compaction_failure_scope(key, ("provider", "model-a", "profile-a"))
    assert runner._compaction_circuit_open(key)
    runner._bind_compaction_failure_scope(key, ("provider", "model-b", "profile-a"))
    assert not runner._compaction_circuit_open(key)
