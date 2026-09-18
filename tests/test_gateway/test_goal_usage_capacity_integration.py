"""Physical model capacity and durable Goal accounting share ordinary Agent calls."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from opensquilla.engine import Agent, AgentConfig, SubagentSpec, ToolResult
from opensquilla.engine.types import DoneEvent as EngineDoneEvent
from opensquilla.engine.usage_accounting import (
    UsageCallResult,
    UsageCallStart,
    UsageExecutionContext,
)
from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink
from opensquilla.provider import (
    ChatConfig,
    Message,
    ToolDefinition,
    ToolInputSchema,
    ToolUseEndEvent,
    ToolUseStartEvent,
)
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.session.compaction import call_compaction_provider
from opensquilla.session.compaction_deployment import (
    CompactionExecutionPlan,
    CompactionExecutionTarget,
)
from opensquilla.session.models import AgentTaskStatus, SessionNode
from opensquilla.session.storage import SessionStorage
from tests.test_session.test_goal_storage import (
    SESSION_ID,
    SESSION_KEY,
    _command,
    _expected,
    _set_goal,
)


class _Provider:
    provider_name = "synthetic"

    def __init__(self, streams: list[list[Any]]) -> None:
        self.streams = streams
        self.configs: list[ChatConfig] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools
        assert config is not None
        events = self.streams[len(self.configs)]
        self.configs.append(config)

        async def stream() -> AsyncIterator[Any]:
            for event in events:
                yield event

        return stream()


class _RecordingDurableSink(SessionUsageEventSink):
    def __init__(self, storage: SessionStorage) -> None:
        super().__init__(storage, retry_delays=())
        self.receipts: list[tuple[UsageCallStart, UsageCallResult]] = []

    async def finalize(self, call: UsageCallStart, result: UsageCallResult) -> None:
        await super().finalize(call, result)
        self.receipts.append((call, result))


@pytest.mark.parametrize("context_window", [8192, 65536])
async def test_goal_counts_subagent_and_compaction_receipts_with_model_capacity(
    tmp_path, monkeypatch: pytest.MonkeyPatch, context_window: int,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")

    def no_http(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Synthetic accounting integration must not send HTTP requests")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_http)
    monkeypatch.setattr(httpx.Client, "send", no_http)
    storage = SessionStorage(str(tmp_path / "goal-capacity.sqlite"))
    await storage.connect()
    sink = _RecordingDurableSink(storage)
    try:
        await storage.upsert_session(SessionNode(
            session_key=SESSION_KEY, session_id=SESSION_ID, agent_id="main", epoch=0,
        ))
        accepted = await _set_goal(storage)
        assert accepted.goal is not None
        # Four provider receipts total 1,291 tokens, including 200 cache reads.
        # The 50 reasoning tokens are already included in the child's output.
        await storage.edit_goal(
            session_key=SESSION_KEY,
            expected=_expected(accepted.goal),
            objective=accepted.goal.objective,
            command=_command("edit"),
            settings={"tokenBudget": 1091},
        )
        await storage.update_agent_task(
            "task-1", status=AgentTaskStatus.RUNNING, started_at=201,
        )
        provider = _Provider([
            [
                ToolUseStartEvent(tool_use_id="spawn-1", tool_name="spawn_helper"),
                ToolUseEndEvent(
                    tool_use_id="spawn-1", tool_name="spawn_helper", arguments={},
                ),
                ProviderDone(stop_reason="tool_use", input_tokens=30, output_tokens=3),
            ],
            [
                ProviderText(text="Synthetic child result"),
                ProviderDone(
                    input_tokens=1000, output_tokens=200,
                    cached_tokens=200, reasoning_tokens=50,
                ),
            ],
            [
                ProviderText(text="Synthetic parent result"),
                ProviderDone(input_tokens=40, output_tokens=4),
            ],
        ])
        compaction = _Provider([[
            ProviderText(text="Synthetic archived summary"),
            ProviderDone(input_tokens=12, output_tokens=2),
        ]])
        plan = CompactionExecutionPlan(candidates=(CompactionExecutionTarget(
            provider=compaction,
            provider_id="synthetic",
            model="summary-model",
            context_window_tokens=context_window,
            max_output_tokens=768,
            provider_request_max_chars=120_000,
        ),))

        async def handle(call) -> ToolResult:
            run_id = await agent.spawn_subagent(
                SubagentSpec(task="Synthetic child work", timeout=0, max_iterations=1),
            )
            child = agent.subagent_manager.registry.get(run_id)
            assert child is not None
            await child.task
            assert await call_compaction_provider(
                "Synthetic completed history", "Preserve IDs", plan, timeout=2,
            ) == "Synthetic archived summary"
            return ToolResult(
                tool_use_id=call.tool_use_id, tool_name=call.tool_name,
                content="Synthetic child completed",
            )

        agent = Agent(
            provider=provider,
            config=AgentConfig(
                max_iterations=3, provider_id="synthetic", model_id="parent-model",
                context_window_tokens=context_window, context_window_known=True,
                max_tokens=1024,
            ),
            tool_definitions=[ToolDefinition(
                name="spawn_helper", description="Run synthetic child work",
                input_schema=ToolInputSchema(properties={}, required=[]),
            )],
            tool_handler=handle,
            usage_event_sink=sink,
            usage_execution_context=UsageExecutionContext(
                execution_id="task-1", agent_run_id="task-1", turn_id="task-1",
                root_turn_id="task-1", session_id=SESSION_ID, session_epoch=0,
                agent_id="main", run_kind="agent",
            ),
        )
        events = [event async for event in agent.run_turn("Delegate synthetic work")]
        assert any(isinstance(event, EngineDoneEvent) for event in events)
        assert len(provider.configs) == 3
        assert all(
            config.provider_context_window_tokens == context_window
            for config in provider.configs
        )
        assert len(compaction.configs) == 1
        assert compaction.configs[0].provider_context_window_tokens == context_window
        assert compaction.configs[0].max_tokens == 768
        assert len(sink.receipts) == 4
        child_calls = [call for call, _ in sink.receipts if call.run_kind == "subagent"]
        assert len(child_calls) == 1
        assert child_calls[0].turn_id != "task-1"
        assert child_calls[0].parent_turn_id == "task-1"

        # A repeated delivery must not count the child, compaction, or either
        # parent request twice after the budget has already paused the Goal.
        for call, receipt in tuple(sink.receipts):
            await sink.finalize(call, receipt)
        rows = await (await storage.conn.execute(
            "SELECT status, root_turn_id, goal_id FROM usage_events"
        )).fetchall()
        assert len(rows) == 4
        assert {row["status"] for row in rows} == {"finalized"}
        assert {row["root_turn_id"] for row in rows} == {"task-1"}
        assert {row["goal_id"] for row in rows} == {accepted.goal.goal_id}
        goal = await storage.get_goal(SESSION_KEY)
        assert goal is not None
        assert goal.total_tokens == 1291
        assert goal.budget_tokens_used == 1091
        assert goal.usage_coverage == "complete"
        assert (goal.status, goal.pause_reason) == ("paused", "token_budget")
    finally:
        await sink.close()
        await storage.close()
