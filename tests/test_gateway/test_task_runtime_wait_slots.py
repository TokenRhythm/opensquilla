"""External input waits lend compute capacity without releasing session ownership."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.gateway.user_input_broker import UserInputRequestNotFoundError
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus


@dataclass
class _Storage:
    records: dict[str, AgentTaskRecord] = field(default_factory=dict)
    starts: list[str] = field(default_factory=list)

    async def create_agent_task(self, record: AgentTaskRecord) -> None:
        self.records[record.task_id] = record

    async def get_agent_task(self, task_id: str) -> AgentTaskRecord | None:
        return self.records.get(task_id)

    async def list_agent_tasks(self, **_: Any) -> list[AgentTaskRecord]:
        return list(self.records.values())

    async def update_agent_task(self, task_id: str, **fields: Any) -> None:
        if "started_at" in fields:
            self.starts.append(task_id)
        for name, value in fields.items():
            setattr(self.records[task_id], name, value)


def _envelope(name: str, source_kind: SourceKind = SourceKind.WEB) -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=source_kind,
        source_name="wait-slot-test",
        agent_id="main",
        session_key=f"agent:main:webchat:{name}",
        metadata={"structured_user_input": source_kind is SourceKind.CLI},
    )


def _request(broker: Any, run: Any) -> str:
    return broker.open_request(
        session_key=run.envelope.session_key,
        task_id=run.task_id,
        tool_use_id="question",
        payload={"clarify_schema": {"fields": [
            {"name": "scope", "type": "string", "required": True},
        ]}},
    )["request_id"]


async def _drain_until(predicate: Any) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cli_without_reply_transport_retains_terminating_question_protocol() -> None:
    """Noninteractive/older clients must not be stranded behind a live waiter."""
    from opensquilla.engine import Agent, AgentConfig, ToolResult
    from opensquilla.provider import (
        DoneEvent,
        ToolDefinition,
        ToolInputSchema,
        ToolUseEndEvent,
        ToolUseStartEvent,
    )
    from opensquilla.tools.builtin.plan_control import request_user_input
    from opensquilla.tools.policy.finalize import _user_input_terminates_turn
    from opensquilla.tools.types import current_tool_context

    class Provider:
        provider_name = "fake"
        calls = 0

        async def list_models(self) -> list[Any]:
            return []

        async def chat(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
            self.calls += 1
            assert self.calls == 1
            yield ToolUseStartEvent(tool_use_id="question", tool_name="request_user_input")
            yield ToolUseEndEvent(
                tool_use_id="question", tool_name="request_user_input",
                arguments={"questions": [{"id": "scope", "question": "Which scope?"}]},
            )
            yield DoneEvent(stop_reason="tool_use", input_tokens=1, output_tokens=1)

    provider = Provider()

    async def handler(run: Any) -> None:
        context = run.envelope.tool_context(is_owner=True)

        async def tool_handler(call: Any) -> ToolResult:
            token = current_tool_context.set(context)
            try:
                content = await request_user_input(**call.arguments)
            finally:
                current_tool_context.reset(token)
            return ToolResult(
                tool_use_id=call.tool_use_id, tool_name=call.tool_name, content=content,
                terminates_turn=_user_input_terminates_turn(call.tool_name, content),
            )

        agent = Agent(
            provider=provider, config=AgentConfig(max_iterations=3),
            tool_definitions=[ToolDefinition(
                name="request_user_input", description="Ask a question",
                input_schema=ToolInputSchema(properties={}, required=[]),
            )],
            tool_handler=tool_handler, tool_context=context,
            session_key=run.envelope.session_key,
        )
        async for _event in agent.run_turn("prepare a plan"):
            pass

    runtime = TaskRuntime(storage=_Storage(), turn_handler=handler)
    try:
        envelope = _envelope("legacy", SourceKind.CLI)
        envelope.metadata.clear()
        task = await runtime.enqueue(envelope, "question")
        assert (await runtime.wait(task.task_id, timeout=2)).status == AgentTaskStatus.SUCCEEDED
        assert provider.calls == 1
        assert runtime.pending_user_inputs(task.session_key) == []
    finally:
        await runtime.shutdown(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", [SourceKind.WEB, SourceKind.CLI])
async def test_answer_reacquires_capacity_without_restarting_or_unlocking_session(
    source_kind: SourceKind,
) -> None:
    storage = _Storage()
    waiting = asyncio.Event()
    other_started = asyncio.Event()
    finish_other = asyncio.Event()
    resumed = asyncio.Event()
    order: list[str] = []
    question: dict[str, str] = {}

    async def handler(run: Any) -> None:
        order.append(run.message)
        if run.message == "question":
            frozen = run.envelope
            broker = run.envelope.runtime_services["user_input_provider"]
            async with run.envelope.runtime_services["suspend_compute_slot"]():
                question["id"] = _request(broker, run)
                waiting.set()
                assert await broker.wait_for_response(question["id"]) == {"scope": "full"}
            assert run.envelope is frozen
            assert runtime._global_in_flight == 1
            order.append("resumed")
            resumed.set()
        elif run.message == "other":
            other_started.set()
            await finish_other.wait()

    runtime = TaskRuntime(storage=storage, turn_handler=handler, max_concurrency=1)
    try:
        first = await runtime.enqueue(_envelope("one", source_kind), "question")
        await asyncio.wait_for(waiting.wait(), 2)
        started_at = storage.records[first.task_id].started_at
        assert runtime._global_in_flight == 0
        assert "suspend_compute_slot" not in runtime._last_envelope_by_session[
            _envelope("one").session_key
        ].runtime_services
        same = await runtime.enqueue(_envelope("one"), "same", mode="followup")
        other = await runtime.enqueue(_envelope("two"), "other")
        await asyncio.wait_for(other_started.wait(), 2)
        assert order == ["question", "other"]
        answer = await runtime.resolve_user_input(
            session_key=first.session_key, request_id=question["id"], fields={"scope": "full"},
        )
        assert answer["resolved"] is True
        replay = await runtime.resolve_user_input(
            session_key=first.session_key, request_id=question["id"], fields={"scope": "full"},
        )
        assert replay["replayed"] is True
        await _drain_until(lambda: bool(runtime._agent_slot_waiters.get("main")))
        assert not resumed.is_set()
        assert storage.records[first.task_id].status is AgentTaskStatus.RUNNING
        assert storage.records[first.task_id].started_at == started_at
        finish_other.set()
        for handle in (first, same, other):
            terminal = await runtime.wait(handle.task_id, timeout=2)
            assert terminal.status is AgentTaskStatus.SUCCEEDED
        assert order == ["question", "other", "resumed", "same"]
        assert storage.starts.count(first.task_id) == 1
        assert runtime._global_in_flight == 0
        assert runtime._agent_in_flight == {}
        assert runtime._agent_slot_waiters == {}
    finally:
        await runtime.shutdown(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["waiting", "answered", "reacquiring", "resumed"])
@pytest.mark.parametrize("source_kind", [SourceKind.WEB, SourceKind.CLI])
async def test_cancel_during_input_or_capacity_wait_never_leaks_or_resumes(
    when: str, source_kind: SourceKind,
) -> None:
    storage = _Storage()
    waiting = asyncio.Event()
    other_started = asyncio.Event()
    finish_other = asyncio.Event()
    resumed = asyncio.Event()
    finish_first = asyncio.Event()
    question: dict[str, str] = {}

    async def handler(run: Any) -> None:
        if run.message == "question":
            broker = run.envelope.runtime_services["user_input_provider"]
            async with run.envelope.runtime_services["suspend_compute_slot"]():
                question["id"] = _request(broker, run)
                waiting.set()
                await broker.wait_for_response(question["id"])
            resumed.set()
            await finish_first.wait()
        else:
            other_started.set()
            await finish_other.wait()

    runtime = TaskRuntime(storage=storage, turn_handler=handler, max_concurrency=1)
    try:
        first = await runtime.enqueue(_envelope("one", source_kind), "question")
        await asyncio.wait_for(waiting.wait(), 2)
        other = await runtime.enqueue(_envelope("two"), "other")
        await asyncio.wait_for(other_started.wait(), 2)
        if when != "waiting":
            await runtime.resolve_user_input(
                session_key=first.session_key, request_id=question["id"], fields={"scope": "full"},
            )
        if when == "reacquiring":
            await _drain_until(lambda: bool(runtime._agent_slot_waiters.get("main")))
        if when == "resumed":
            finish_other.set()
            await asyncio.wait_for(resumed.wait(), 2)
        await runtime.cancel(task_id=first.task_id)
        assert (await runtime.wait(first.task_id, timeout=2)).status is AgentTaskStatus.CANCELLED
        assert resumed.is_set() is (when == "resumed")
        assert runtime.pending_user_inputs(first.session_key) == []
        if when == "waiting":
            with pytest.raises(UserInputRequestNotFoundError):
                await runtime.resolve_user_input(
                    session_key=first.session_key,
                    request_id=question["id"], fields={"scope": "full"},
                )
        finish_other.set()
        await runtime.wait(other.task_id, timeout=2)
        assert runtime._global_in_flight == 0
        assert runtime._agent_in_flight == {}
        assert runtime._agent_slot_waiters == {}
    finally:
        await runtime.shutdown(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["shutdown", "quiesce", "error"])
async def test_suspended_turn_uses_existing_terminal_cleanup(stop: str) -> None:
    storage = _Storage()
    waiting = asyncio.Event()
    fail = asyncio.Event()

    async def handler(run: Any) -> None:
        async with run.envelope.runtime_services["suspend_compute_slot"]():
            waiting.set()
            await fail.wait()
            raise ValueError("synthetic input failure")

    runtime = TaskRuntime(storage=storage, turn_handler=handler, max_concurrency=1)
    try:
        first = await runtime.enqueue(_envelope("one"), "question")
        await asyncio.wait_for(waiting.wait(), 2)
        if stop == "shutdown":
            await runtime.shutdown(timeout=2)
        elif stop == "quiesce":
            async with runtime.quiesce_sessions([first.session_key]):
                assert runtime._global_in_flight == 0
        else:
            fail.set()
        terminal = await runtime.wait(first.task_id, timeout=2)
        assert terminal.status is (
            AgentTaskStatus.FAILED if stop == "error" else AgentTaskStatus.CANCELLED
        )
        assert storage.starts == [first.task_id]
        assert runtime._global_in_flight == 0
        assert runtime._agent_in_flight == {}
        assert runtime._agent_slot_waiters == {}
    finally:
        await runtime.shutdown(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_after_answer", [False, True])
async def test_real_agent_waits_for_runtime_capacity_before_next_provider_call(
    cancel_after_answer: bool,
) -> None:
    from opensquilla.engine import Agent, AgentConfig, ToolResult
    from opensquilla.engine.types import ToolResultEvent
    from opensquilla.provider import (
        DoneEvent,
        TextDeltaEvent,
        ToolDefinition,
        ToolInputSchema,
        ToolUseEndEvent,
        ToolUseStartEvent,
    )
    from opensquilla.tools.builtin.plan_control import request_user_input
    from opensquilla.tools.types import current_tool_context

    class Provider:
        provider_name = "fake"
        calls = 0

        async def list_models(self) -> list[Any]:
            return []

        async def chat(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
            self.calls += 1
            if self.calls == 1:
                yield ToolUseStartEvent(tool_use_id="question", tool_name="request_user_input")
                yield ToolUseEndEvent(
                    tool_use_id="question", tool_name="request_user_input",
                    arguments={"questions": [{"id": "scope", "question": "Which scope?"}]},
                )
                yield DoneEvent(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            else:
                yield TextDeltaEvent(text="The requested plan is ready.")
                yield DoneEvent(stop_reason="stop", input_tokens=1, output_tokens=1)

    storage = _Storage()
    provider = Provider()
    published = asyncio.Event()
    other_started = asyncio.Event()
    finish_other = asyncio.Event()
    request: dict[str, Any] = {}
    events: list[Any] = []

    async def handler(run: Any) -> None:
        if run.message == "other":
            other_started.set()
            await finish_other.wait()
            return
        context = run.envelope.tool_context(is_owner=True)

        async def tool_handler(call: Any) -> ToolResult:
            token = current_tool_context.set(context)
            try:
                content = await request_user_input(**call.arguments)
            finally:
                current_tool_context.reset(token)
            return ToolResult(
                tool_use_id=call.tool_use_id, tool_name=call.tool_name, content=content,
            )

        agent = Agent(
            provider=provider,
            config=AgentConfig(max_iterations=3),
            tool_definitions=[ToolDefinition(
                name="request_user_input", description="Ask a question",
                input_schema=ToolInputSchema(properties={}, required=[]),
            )],
            tool_handler=tool_handler,
            tool_context=context,
            session_key=run.envelope.session_key,
        )
        async for event in agent.run_turn("prepare a plan"):
            events.append(event)
            if isinstance(event, ToolResultEvent):
                payload = json.loads(event.result)
                if payload.get("status") == "input_required":
                    assert runtime._global_in_flight == 0
                    request.update(payload)
                    published.set()

    runtime = TaskRuntime(storage=storage, turn_handler=handler, max_concurrency=1)
    try:
        first = await runtime.enqueue(_envelope("one"), "question")
        await asyncio.wait_for(published.wait(), 2)
        other = await runtime.enqueue(_envelope("two"), "other")
        await asyncio.wait_for(other_started.wait(), 2)
        await runtime.resolve_user_input(
            session_key=first.session_key, request_id=request["request_id"],
            fields={"scope": "full"},
        )
        await _drain_until(lambda: bool(runtime._agent_slot_waiters.get("main")))
        assert provider.calls == 1
        if cancel_after_answer:
            await runtime.cancel(task_id=first.task_id)
        finish_other.set()
        await runtime.wait(other.task_id, timeout=2)
        result = await runtime.wait(first.task_id, timeout=2)
        assert result.status is (
            AgentTaskStatus.CANCELLED if cancel_after_answer else AgentTaskStatus.SUCCEEDED
        )
        assert provider.calls == (1 if cancel_after_answer else 2)
        assert storage.starts.count(first.task_id) == 1
        assert not any(event.kind == "error" for event in events)
        assert runtime.pending_user_inputs(first.session_key) == []
        assert runtime._global_in_flight == 0
    finally:
        await runtime.shutdown(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["denied", "approved", "expired", "rule_denied"])
async def test_approval_terminal_denial_needs_no_slot_but_execution_always_reacquires(
    decision: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.application import approval_queue as approval_module
    from opensquilla.engine import Agent, AgentConfig, ToolResult
    from opensquilla.engine.types import ToolResultEvent
    from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
    from tests.test_engine.test_interactive_approval_retry import (
        _DeniedApprovalThenAnswerProvider,
        _exec_definition,
    )

    monkeypatch.setattr(
        approval_module, "_DEFAULT_APPROVAL_QUEUE_PATH", tmp_path / "approval.sqlite",
    )
    reset_approval_queue()
    # Prepare the real SQLite queue before timing the compute-slot handoff.
    queue = get_approval_queue()
    published, other_started, finish_other, reacquiring = (asyncio.Event() for _ in range(4))
    events: list[Any] = []
    approval: dict[str, str] = {}
    tool_calls: list[str] = []
    first_task_id = ""

    class Provider(_DeniedApprovalThenAnswerProvider):
        def chat(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            assert runtime._tasks[first_task_id].acquired_slot
            return super().chat(*args, **kwargs)

    provider = Provider()

    async def handler(run: Any) -> None:
        nonlocal first_task_id
        if run.message == "other":
            other_started.set()
            await finish_other.wait()
            return
        first_task_id = run.task_id

        async def tool_handler(call: Any) -> ToolResult:
            assert runtime._tasks[run.task_id].acquired_slot
            tool_calls.append(call.tool_use_id)
            if len(tool_calls) > 1:
                return ToolResult(call.tool_use_id, call.tool_name, "executed")
            approval["id"] = queue.request("exec", {
                "toolName": call.tool_name, "command": call.arguments["command"],
                "args": dict(call.arguments),
                "reviewer": "auto_review" if decision == "rule_denied" else "user",
                "humanActionable": decision != "rule_denied",
            })
            return ToolResult(call.tool_use_id, call.tool_name, json.dumps({
                "status": "approval_required", "approval_id": approval["id"],
                "command": call.arguments["command"],
            }))

        agent = Agent(
            provider=provider, config=AgentConfig(max_iterations=3),
            tool_definitions=[_exec_definition()], tool_handler=tool_handler,
            tool_context=run.envelope.tool_context(is_owner=True),
            session_key=run.envelope.session_key,
        )
        async for event in agent.run_turn("Inspect the synthetic location"):
            events.append(event)
            if isinstance(event, ToolResultEvent) and "approval_required" in event.result:
                published.set()

    runtime = TaskRuntime(storage=_Storage(), turn_handler=handler, max_concurrency=1)
    original_acquire = runtime._acquire_fair_slot

    async def acquire(task: Any, *, mark_running: bool = True) -> None:
        if not mark_running:
            reacquiring.set()
        await original_acquire(task, mark_running=mark_running)

    monkeypatch.setattr(runtime, "_acquire_fair_slot", acquire)
    try:
        first = await runtime.enqueue(_envelope("approval"), "approval")
        await asyncio.wait_for(published.wait(), 2)
        other = await runtime.enqueue(_envelope("other"), "other")
        await asyncio.wait_for(other_started.wait(), 2)
        if decision == "expired":
            queue.expire_pending(approval["id"])
        else:
            queue.resolve(approval["id"], decision == "approved")
        if decision == "denied":
            await runtime.wait(first.task_id, timeout=2)
            assert not reacquiring.is_set()
            assert runtime._tasks[other.task_id].acquired_slot
            assert any(
                isinstance(event, ToolResultEvent) and "approval_denied" in event.result
                for event in events
            )
        else:
            await asyncio.wait_for(reacquiring.wait(), 2)
            assert not runtime._tasks[first.task_id].done.is_set()
        assert len(provider.calls) == len(tool_calls) == 1
        finish_other.set()
        await runtime.wait(other.task_id, timeout=2)
        await runtime.wait(first.task_id, timeout=2)
        assert len(provider.calls) == (1 if decision == "denied" else 2)
        assert len(tool_calls) == (2 if decision == "approved" else 1)
        assert runtime._global_in_flight == 0
        assert not runtime._agent_slot_waiters
    finally:
        finish_other.set()
        await runtime.shutdown(timeout=2)
        reset_approval_queue()
