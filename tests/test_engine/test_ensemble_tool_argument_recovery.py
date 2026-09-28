from __future__ import annotations

from collections import Counter

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.usage_accounting import UsageExecutionContext
from opensquilla.provider.ensemble import EnsembleMemberConfig, EnsembleProvider
from opensquilla.provider.request_proof import project_final_request_payload
from opensquilla.provider.selector import ProviderConfig
from opensquilla.provider.tool_argument_rejection import rejected_tool_arguments_error
from opensquilla.provider.types import (
    DoneEvent,
    ErrorEvent,
    RejectedToolArguments,
    TextDeltaEvent,
    ToolArgumentRejection,
    ToolDefinition,
    ToolInputSchema,
    ToolUseEndEvent,
    ToolUseStartEvent,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", ["rejection", "ordinary", "ordinary_fixed", "unknown_rejection", "fixed"],
)
async def test_agent_ensemble_continuation_counts_each_physical_receipt_once(monkeypatch, scenario):
    calls = []
    effects = []

    class UsageSink:
        def __init__(self):
            self.started = []
            self.finalized = []
            self.unknown = []

        async def start(self, call):
            self.started.append(call)

        async def finalize(self, call, result):
            self.finalized.append((call, result))

        async def mark_unknown(self, call, reason):
            self.unknown.append((call, reason))

    sink = UsageSink()
    rejected = rejected_tool_arguments_error(
        ToolArgumentRejection(
            calls=(RejectedToolArguments("bad", "record", "invalid_json"),),
            terminal_reason="tool_calls",
        ),
        usage=None if scenario == "unknown_rejection" else DoneEvent(
            input_tokens=11, output_tokens=5, billed_cost=0.11,
            cost_source="provider_billed", model="agg", provider="fake",
        ),
    )
    plans = {
        "draft": [[TextDeltaEvent(text="Candidate"), DoneEvent(
            input_tokens=3, output_tokens=2, billed_cost=0.03,
            cost_source="provider_billed", model="draft", provider="fake",
        )]],
        "agg": [
            [TextDeltaEvent(text="Preparing.\n"), rejected],
            [
                ToolUseStartEvent(tool_use_id="good", tool_name="record"),
                ToolUseEndEvent(
                    tool_use_id="good", tool_name="record", arguments={"value": "ok"},
                ),
                DoneEvent(
                    stop_reason="tool_use", input_tokens=7, output_tokens=4, billed_cost=0.07,
                    cost_source="provider_billed", model="agg", provider="fake",
                ),
            ],
            [TextDeltaEvent(text="Finished."), DoneEvent(
                input_tokens=13, output_tokens=6, billed_cost=0.13,
                cost_source="provider_billed", model="agg", provider="fake",
            )],
        ],
    }
    if scenario == "ordinary":
        plans["agg"] = plans["agg"][1:]
    elif scenario == "ordinary_fixed":
        plans["fixed"] = plans["agg"][2:]
        plans["fixed"][0][-1].model = "fixed"
        plans["agg"] = [plans["agg"][1], [ErrorEvent(message="unauthorized", code="401")]]
    elif scenario == "fixed":
        plans["fixed"] = plans["agg"][1:]
        for stream in plans["fixed"]:
            stream[-1].model = "fixed"
        plans["agg"] = [plans["agg"][0], [ErrorEvent(message="unauthorized", code="401")]]

    class PhysicalProvider:
        provider_name = "fake"

        def __init__(self, config):
            self.model = config.model

        async def chat(self, messages, tools=None, config=None):
            index = sum(call[0] == self.model for call in calls)
            calls.append((self.model, list(messages)))
            for event in plans[self.model][index]:
                yield event

        def project_final_request(self, messages, tools=None, config=None, *, message_limit=None):
            return project_final_request_payload(
                {"messages": [message.model_dump(mode="json") for message in messages]},
                projection_adapter="ensemble-rejection-test",
                proof_budget=int(config.provider_request_max_chars or 0),
                active_user_message_index=config.active_user_message_index,
                message_limit=message_limit,
            )

    monkeypatch.setattr("opensquilla.provider.ensemble._build_provider", PhysicalProvider)
    provider = EnsembleProvider(
        profile_name="rejection-test",
        proposers=[EnsembleMemberConfig(ProviderConfig("fake", "draft"))],
        aggregator=EnsembleMemberConfig(ProviderConfig("fake", "agg")),
        min_successful_proposers=1, shuffle_candidates=False,
        fallback_provider=PhysicalProvider(ProviderConfig("fake", "fixed")),
        fallback_provider_name="fake", fallback_model="fixed",
    )

    async def handle(call):
        effects.append(call.arguments)
        return ToolResult(
            tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="recorded",
        )

    agent = Agent(
        provider=provider,
        tool_definitions=[ToolDefinition(
            name="record", description="Record one value", input_schema=ToolInputSchema(
                properties={"value": {"type": "string"}}, required=["value"],
            ),
        )],
        tool_handler=handle,
        config=AgentConfig(max_iterations=8, max_provider_retries=0),
        usage_event_sink=sink,
        usage_execution_context=UsageExecutionContext(
            execution_id="ensemble-rejection-test", agent_run_id="ensemble-rejection-run",
        ),
    )
    events = [event async for event in agent.run_turn("Record a value")]

    assert not [event for event in events if event.kind == "error"]
    assert effects == [{"value": "ok"}]
    expected_calls = {
        "draft": 1, "agg": 2 if scenario in {"ordinary", "ordinary_fixed", "fixed"} else 3,
    }
    if scenario == "fixed":
        expected_calls["fixed"] = 2
    elif scenario == "ordinary_fixed":
        expected_calls["fixed"] = 1
    assert Counter(model for model, _ in calls) == expected_calls
    if scenario not in {"ordinary", "ordinary_fixed"}:
        assert "No tools in THIS batch ran" in str(calls[2][1])
    assert "recorded" in str(calls[-1][1])
    has_rejection_receipt = scenario in {"rejection", "fixed"}
    expected_input = 34 if has_rejection_receipt else 23
    expected_output = 17 if has_rejection_receipt else 12
    expected_missing = int(scenario in {"unknown_rejection", "fixed", "ordinary_fixed"})
    assert len(sink.started) == sum(expected_calls.values())
    assert len(sink.finalized) == sum(expected_calls.values()) - expected_missing
    assert len(sink.unknown) == expected_missing
    assert sum(usage.input_tokens for _, usage in sink.finalized) == expected_input
    assert sum(usage.billed_cost_nanos for _, usage in sink.finalized) == (
        expected_input * 10_000_000
    )
    done = next(event for event in events if event.kind == "done")
    assert done.text.endswith("Finished.")
    assert done.input_tokens == expected_input
    assert done.output_tokens == expected_output
    assert done.billed_cost == pytest.approx(expected_input / 100)
    assert done.missing_cost_entries == expected_missing
    assert sum(row["input_tokens"] for row in done.model_usage_breakdown) == expected_input
    assert sum(row["output_tokens"] for row in done.model_usage_breakdown) == expected_output
