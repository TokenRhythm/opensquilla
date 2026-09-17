"""Offline regression coverage for tool recovery across turn boundaries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, DoneEvent, ErrorEvent, ToolCall, ToolResult
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.tool_failure_recovery import FAILURE_RECOVERY_CODE
from opensquilla.engine.turn_runner.harness import (
    _TurnRunnerAgentFactoryAdapter,
    _TurnRunnerAgentRunAdapter,
)
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import ErrorEvent as ProviderError
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolDefinition, ToolInputSchema
from opensquilla.provider import ToolUseEndEvent as ProviderToolEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolStart
from opensquilla.tool_boundary import ToolEffectOutcome
from opensquilla.tools.types import ToolContext

_Operation = tuple[str, dict[str, Any]]


class _EdgeProvider:
    provider_name = "offline"

    def __init__(self, script: list[list[_Operation]], *, final_mode: str = "text") -> None:
        self.script = script
        self.final_mode = final_mode
        self.position = 0
        self.requests: list[dict[str, Any]] = []
        self.fallbacks = 0
        self.final_started = asyncio.Event()
        self.final_closed = asyncio.Event()

    async def chat(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[Any]:
        self.requests.append({"messages": messages, **kwargs})
        if kwargs.get("tools") and self.position < len(self.script):
            batch = self.script[self.position]
            self.position += 1
            for index, (name, arguments) in enumerate(batch):
                call_id = f"edge-{len(self.requests)}-{index}"
                yield ProviderToolStart(tool_use_id=call_id, tool_name=name)
                yield ProviderToolEnd(tool_use_id=call_id, tool_name=name, arguments=arguments)
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        self.final_started.set()
        try:
            if self.final_mode == "block":
                await asyncio.Event().wait()
            if self.final_mode == "error":
                yield ProviderError(message="Synthetic provider unavailable", code="request_error")
                return
            if self.final_mode == "raise":
                raise ConnectionError("Synthetic transport unavailable")
            if self.final_mode in {
                "text", "text_and_tool", "length_capped", "incomplete", "incomplete_tool",
            }:
                yield ProviderText(text="The available result is unverified sample code.")
            if self.final_mode in {"text_and_tool", "incomplete_tool"}:
                yield ProviderToolStart(tool_use_id="ignored-final-tool", tool_name="probe")
            if self.final_mode == "text_and_tool":
                yield ProviderToolEnd(
                    tool_use_id="ignored-final-tool", tool_name="probe", arguments={},
                )
            if self.final_mode in {"incomplete", "incomplete_tool"}:
                return
            yield ProviderDone(
                stop_reason="length" if self.final_mode == "length_capped" else "stop",
                input_tokens=1, output_tokens=1,
            )
        finally:
            self.final_closed.set()

    def fallback_after_invalid_response(self, reason: str) -> bool:
        self.fallbacks += 1
        return True

    async def list_models(self) -> list[Any]:
        return []


def _agent(
    provider: _EdgeProvider,
    handler: Callable[[ToolCall], Awaitable[ToolResult]],
    *,
    timeout: float = 2,
    context: ToolContext | None = None,
) -> Agent:
    return Agent(
        provider=provider,
        config=AgentConfig(
            timeout=timeout, max_provider_retries=5,
            retry_base_backoff_ms=0, retry_max_backoff_ms=0,
        ),
        tool_definitions=[
            ToolDefinition(name=name, description="Synthetic tool", input_schema=ToolInputSchema())
            for name in ("probe", "repair", "meta_invoke", "read_file", "exec_command", "process")
        ],
        tool_handler=handler,
        tool_context=context,
    )


def _failed(call: ToolCall, *, permanent: bool = False) -> ToolResult:
    payload = {"code": "SYNTHETIC_UNAVAILABLE", "retryable": not permanent}
    return ToolResult(call.tool_use_id, call.tool_name, json.dumps(payload), is_error=True)


def _succeeded(call: ToolCall) -> ToolResult:
    return ToolResult(call.tool_use_id, call.tool_name, "ready")


async def test_failure_counts_start_fresh_on_each_user_turn() -> None:
    provider = _EdgeProvider([[('probe', {"target": "sample"})]] * 2)
    calls: list[ToolCall] = []

    async def handler(call: ToolCall) -> ToolResult:
        calls.append(call)
        return _failed(call)

    agent = _agent(provider, handler)
    for _ in range(2):
        provider.position = 0
        events = [event async for event in agent.run_turn("Inspect the sample.")]
        assert not any(isinstance(event, ErrorEvent) for event in events)
    assert len(calls) == 4
    assert len(provider.requests) == 6
    assert all(request["tools"] for request in provider.requests)


async def test_argument_mapping_order_does_not_renew_failed_call_budget() -> None:
    variants = [
        {"target": "sample", "options": {"first": 1, "second": 2}},
        {"options": {"second": 2, "first": 1}, "target": "sample"},
    ]
    provider = _EdgeProvider([[('probe', variants[index % 2])] for index in range(8)])
    calls: list[ToolCall] = []

    async def handler(call: ToolCall) -> ToolResult:
        calls.append(call)
        return _failed(call)

    events = [event async for event in _agent(provider, handler).run_turn("Inspect the sample.")]
    assert len(calls) == 3
    assert len(provider.requests) == 4
    assert provider.requests[-1]["tools"] is None
    assert any(isinstance(event, DoneEvent) and event.text for event in events)


async def test_success_of_same_call_resets_its_failure_count() -> None:
    provider = _EdgeProvider([[('probe', {"target": "sample"})]] * 5)
    calls: list[ToolCall] = []

    async def handler(call: ToolCall) -> ToolResult:
        calls.append(call)
        return _succeeded(call) if len(calls) == 3 else _failed(call)

    events = [event async for event in _agent(provider, handler).run_turn("Inspect the sample.")]
    assert len(calls) == 5
    assert all(request["tools"] for request in provider.requests)
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.parametrize("evidence", ["committed_outcome", "workspace_receipt"])
async def test_permanent_failure_can_be_retested_after_recorded_repair(evidence: str) -> None:
    operations = [
        ("probe", {"target": "sample"}),
        ("repair", {"target": "sample"}),
        ("probe", {"target": "sample"}),
    ]
    provider = _EdgeProvider([[operation] for operation in operations])
    context = ToolContext()
    calls: list[tuple[str, dict[str, Any]]] = []
    repaired = False

    async def handler(call: ToolCall) -> ToolResult:
        nonlocal repaired
        calls.append((call.tool_name, call.arguments))
        if call.tool_name == "repair":
            repaired = True
            result = _succeeded(call)
            if evidence == "committed_outcome":
                result.effect_outcome = ToolEffectOutcome(
                    effect_state="committed", retry_policy="same_turn",
                    loop_action="continue", outcome_code="synthetic_repair_completed",
                )
            else:
                assert agent._tool_context is not None
                agent._tool_context.workspace_mutation_receipts.append(
                    {"path": "sample.conf", "operation": "write"}
                )
            return result
        return _succeeded(call) if repaired else _failed(call, permanent=True)

    agent = _agent(provider, handler, context=context)
    events = [event async for event in agent.run_turn("Repair and inspect the sample.")]
    assert calls == operations
    assert all(request["tools"] for request in provider.requests)
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.parametrize(
    "final_mode", ["empty", "error", "raise", "length_capped", "incomplete", "incomplete_tool"],
)
async def test_failed_final_response_never_retries_or_switches_provider(final_mode: str) -> None:
    provider = _EdgeProvider([[('probe', {"target": "sample"})]] * 8, final_mode=final_mode)

    async def handler(call: ToolCall) -> ToolResult:
        return _failed(call)

    events = [event async for event in _agent(provider, handler).run_turn("Inspect the sample.")]
    assert len(provider.requests) == 4
    assert provider.requests[-1]["tools"] is None
    assert provider.requests[-1]["config"].physical_attempt_limit == 1
    assert provider.fallbacks == 0
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(errors) == 1
    assert errors[0].code == FAILURE_RECOVERY_CODE
    assert "unverified" in errors[0].message


async def test_cancellation_during_final_response_closes_provider_without_retry() -> None:
    provider = _EdgeProvider([[('probe', {"target": "sample"})]] * 8, final_mode="block")

    async def handler(call: ToolCall) -> ToolResult:
        return _failed(call)

    agent = _agent(provider, handler)
    events: list[Any] = []

    async def consume() -> None:
        async for event in agent.run_turn("Inspect the sample."):
            events.append(event)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(provider.final_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    await asyncio.wait_for(provider.final_closed.wait(), timeout=1)
    assert len(provider.requests) == 4
    assert provider.fallbacks == 0
    assert not any(isinstance(event, ErrorEvent) for event in events)


async def test_total_timeout_during_final_response_preserves_timeout_outcome() -> None:
    provider = _EdgeProvider([[('probe', {"target": "sample"})]] * 8, final_mode="block")

    async def handler(call: ToolCall) -> ToolResult:
        return _failed(call)

    events = [
        event async for event in _agent(provider, handler, timeout=0.15).run_turn("Inspect sample.")
    ]
    assert provider.final_started.is_set()
    await asyncio.wait_for(provider.final_closed.wait(), timeout=1)
    assert len(provider.requests) == 4
    assert provider.fallbacks == 0
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(errors) == 1
    assert errors[0].code == "agent_runtime_timeout"


async def test_meta_dispatch_waiting_behind_failed_batch_does_not_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = [("probe", {"target": "sample"})] * 3
    batch.append(("meta_invoke", {"name": "synthetic", "inputs": {}}))
    provider = _EdgeProvider([batch])
    meta_calls: list[ToolCall] = []

    async def handler(call: ToolCall) -> ToolResult:
        return _failed(call)

    async def meta_handler(call: ToolCall, context: ToolContext) -> AsyncIterator[ToolResult]:
        meta_calls.append(call)
        yield _succeeded(call)

    agent = _agent(provider, handler)
    monkeypatch.setattr(agent, "_run_one_streaming", meta_handler)
    events = [event async for event in agent.run_turn("Inspect the sample.")]
    assert not meta_calls
    assert provider.requests[-1]["tools"] is None
    assert any(isinstance(event, DoneEvent) and event.text for event in events)


async def test_repeated_streaming_meta_failures_share_the_recovery_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _EdgeProvider([[('meta_invoke', {"name": "synthetic", "inputs": {}})]] * 8)
    meta_calls: list[ToolCall] = []

    async def handler(call: ToolCall) -> ToolResult:
        raise AssertionError("Streaming meta calls should not use the ordinary handler")

    async def meta_handler(call: ToolCall, context: ToolContext) -> AsyncIterator[ToolResult]:
        meta_calls.append(call)
        yield _failed(call)

    agent = _agent(provider, handler)
    monkeypatch.setattr(agent, "_run_one_streaming", meta_handler)
    events = [event async for event in agent.run_turn("Inspect the sample.")]
    assert len(meta_calls) == 3
    assert len(provider.requests) == 4
    assert provider.requests[-1]["tools"] is None
    assert any(isinstance(event, DoneEvent) and event.text for event in events)


async def test_final_text_survives_spurious_tool_calls_without_dispatch() -> None:
    provider = _EdgeProvider(
        [[('probe', {"target": "sample"})]] * 8, final_mode="text_and_tool",
    )
    calls: list[ToolCall] = []

    async def handler(call: ToolCall) -> ToolResult:
        calls.append(call)
        return _failed(call)

    events = [event async for event in _agent(provider, handler).run_turn("Inspect the sample.")]
    assert len(calls) == 3
    assert len(provider.requests) == 4
    assert provider.requests[-1]["tools"] is None
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert any(
        isinstance(event, DoneEvent) and "unverified sample code" in event.text for event in events
    )


async def test_meta_repair_receipt_reopens_permanent_failed_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _EdgeProvider([
        [("probe", {"target": "sample"})],
        [("meta_invoke", {"name": "synthetic", "inputs": {}})],
        [("probe", {"target": "sample"})],
    ])
    calls: list[ToolCall] = []

    async def handler(call: ToolCall) -> ToolResult:
        calls.append(call)
        return _failed(call, permanent=True) if len(calls) == 1 else _succeeded(call)

    async def meta_handler(call: ToolCall, context: ToolContext) -> AsyncIterator[ToolResult]:
        context.workspace_mutation_receipts.append({"path": "sample.conf", "operation": "write"})
        yield _succeeded(call)

    agent = _agent(provider, handler, context=ToolContext())
    monkeypatch.setattr(agent, "_run_one_streaming", meta_handler)
    events = [event async for event in agent.run_turn("Repair and inspect the sample.")]
    assert len(calls) == 2
    assert all(request["tools"] for request in provider.requests)
    assert not any(isinstance(event, ErrorEvent) for event in events)


async def test_shared_turn_runner_factory_and_run_adapter_enforce_recovery() -> None:
    provider = _EdgeProvider([[('probe', {"target": "sample"})]] * 8)
    calls: list[ToolCall] = []

    async def handler(call: ToolCall) -> ToolResult:
        calls.append(call)
        return _failed(call)

    runner = TurnRunner(provider_selector=None)
    agent = _TurnRunnerAgentFactoryAdapter(runner).build(
        provider=provider,
        config=AgentConfig(timeout=2, max_provider_retries=5),
        tool_definitions=[
            ToolDefinition(
                name="probe", description="Synthetic tool", input_schema=ToolInputSchema(),
            ),
        ],
        tool_handler=handler,
        session_key="agent:main:synthetic-recovery",
        turn_call_logger=None,
        memory_sync_manager=None,
        tool_context=ToolContext(),
    )
    events = [
        event async for event in _TurnRunnerAgentRunAdapter().run_turn(
            agent, turn_input="Inspect the sample.", extra_messages=None,
            semantic_message=None, pending_input_provider=None,
        )
    ]
    assert len(calls) == 3
    assert len(provider.requests) == 4
    assert provider.requests[-1]["tools"] is None
    assert any(isinstance(event, DoneEvent) and event.text for event in events)


async def test_parallel_failed_cohort_is_bounded_without_serializing_started_calls() -> None:
    provider = _EdgeProvider([[('read_file', {"path": "sample.txt"})] * 12])
    calls: list[ToolCall] = []
    first_cohort_started = asyncio.Event()
    releases = [asyncio.Event() for _ in range(3)]
    completions = [asyncio.Event() for _ in range(3)]

    async def handler(call: ToolCall) -> ToolResult:
        index = len(calls)
        calls.append(call)
        if len(calls) == 3:
            first_cohort_started.set()
        if index < 3:
            await first_cohort_started.wait()
            await releases[index].wait()
            completions[index].set()
        return _failed(call)

    async def consume() -> list[Any]:
        return [event async for event in _agent(provider, handler).run_turn("Inspect the sample.")]

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(first_cohort_started.wait(), timeout=1)
        for index in range(3):
            releases[index].set()
            await asyncio.wait_for(completions[index].wait(), timeout=1)
        events = await asyncio.wait_for(task, timeout=1)
    finally:
        for release in releases:
            release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert len(calls) == 3
    assert len(provider.requests) == 2
    assert provider.requests[-1]["tools"] is None
    assert any(isinstance(event, DoneEvent) and event.text for event in events)


async def test_shell_and_process_runtime_failures_share_one_capability_boundary() -> None:
    provider = _EdgeProvider([
        [("exec_command", {"command": "node --version"})],
        [("process", {"action": "poll", "session_id": "synthetic-process"})],
        [("exec_command", {"command": "npm --version"})],
    ])
    calls: list[ToolCall] = []

    async def handler(call: ToolCall) -> ToolResult:
        calls.append(call)
        missing = {"code": "RUNTIME_UNAVAILABLE", "componentId": "node", "retryable": False}
        payload = (
            {"session": {"runtime_failure": missing}} if call.tool_name == "process" else missing
        )
        return ToolResult(call.tool_use_id, call.tool_name, json.dumps(payload), is_error=True)

    events = [event async for event in _agent(provider, handler).run_turn("Inspect the sample.")]
    assert [call.tool_name for call in calls] == ["exec_command", "process"]
    assert len(provider.requests) == 3
    assert provider.requests[-1]["tools"] is None
    assert any(isinstance(event, DoneEvent) and event.text for event in events)


@pytest.mark.parametrize("different_field", ["env", "workdir"])
async def test_runtime_failures_in_different_execution_contexts_are_not_merged(
    different_field: str,
) -> None:
    variants = [
        {
            "command": "node --version",
            different_field: {"PATH": f"runtime-{index}"} if different_field == "env"
            else f"workspace-{index}",
        }
        for index in range(4)
    ]
    provider = _EdgeProvider([[('exec_command', arguments)] for arguments in variants])
    calls: list[ToolCall] = []

    async def handler(call: ToolCall) -> ToolResult:
        calls.append(call)
        return ToolResult(
            call.tool_use_id, call.tool_name,
            json.dumps({"code": "RUNTIME_UNAVAILABLE", "componentId": "node", "retryable": False}),
            is_error=True,
        )

    events = [event async for event in _agent(provider, handler).run_turn("Inspect the sample.")]
    assert [call.arguments for call in calls] == variants
    assert len(provider.requests) == 5
    assert all(request["tools"] for request in provider.requests)
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.parametrize(
    "driver,status,step_status,expected_error",
    [
        ("manual", "running", "in_progress", True),
        ("manual", "running", "completed", False),
        ("manual", "blocked", "blocked", False),
        ("goal", "running", "in_progress", False),
    ],
)
async def test_failure_finalization_preserves_plan_completion_guard(
    driver: str, status: str, step_status: str, expected_error: bool,
) -> None:
    plan = SimpleNamespace(
        run_id="synthetic-plan", driver_kind=driver, status=status, state_revision=1,
        current_step_id=None if step_status == "completed" else "check",
        step_states=[{"step_id": "check", "status": step_status}],
    )

    class Storage:
        async def get_plan_run(self, run_id: str) -> Any:
            assert run_id == plan.run_id
            return plan

    context = ToolContext(plan_run_id=plan.run_id, plan_run=plan, plan_storage=Storage())
    provider = _EdgeProvider([[('probe', {"target": "sample"})]] * 8)

    async def handler(call: ToolCall) -> ToolResult:
        return _failed(call)

    agent = _agent(provider, handler, context=context)
    events = [event async for event in agent.run_turn("Complete the attached work.")]
    assert len(provider.requests) == (1 if step_status == "completed" else 4)
    assert provider.requests[-1]["tools"] is None
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    if expected_error:
        assert [event.code for event in errors] == ["plan_run_checkpoint_required"]
    else:
        assert errors == []
    assert plan.status == status
