from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import DoneEvent, ErrorEvent, WarningEvent
from opensquilla.gateway.config import GatewayConfig, SquillaRouterConfig
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import Message, ModelInfo
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.registry import ToolRegistry, ToolSpec
from opensquilla.tools.types import CallerKind, ToolContext


class _SelectorClone:
    current_config = SimpleNamespace(model="test/model")

    def __init__(self, provider: Any) -> None:
        self.provider = provider

    def override_model(self, model: str) -> None:
        self.current_config = SimpleNamespace(model=model)
        self.provider.model = model

    def resolve(self) -> Any:
        return self.provider


class _ProviderSelector:
    def __init__(self, provider: Any) -> None:
        self.provider = provider

    def clone(self) -> _SelectorClone:
        return _SelectorClone(self.provider)


class _PlanStorage:
    def __init__(self) -> None:
        self.run = SimpleNamespace(
            run_id="run-1",
            status="running",
            state_revision=1,
            current_step_id="step-1",
            step_states=[
                {
                    "step_id": "step-1",
                    "title": "Implement",
                    "status": "in_progress",
                }
            ],
        )

    async def get_plan_run(self, run_id: str) -> SimpleNamespace:
        assert run_id == self.run.run_id
        return self.run

    def complete(self) -> None:
        # The final checkpoint enters delivery-ready state. TaskRuntime owns
        # the later running -> completed transition after the turn succeeds.
        self.run.status = "running"
        self.run.state_revision += 1
        self.run.current_step_id = None
        self.run.step_states[0]["status"] = "completed"


class _ReconcilesCheckpointProvider:
    provider_name = "test"

    def __init__(self) -> None:
        self.calls = 0
        self.model = "test/model"
        self.requests: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: Any = None,
        config: Any = None,
    ) -> AsyncIterator[Any]:
        self.calls += 1
        self.requests.append(list(messages))
        return self._stream(self.calls)

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderText(text="The implementation is complete.")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        if call_number == 2:
            yield ProviderToolUseStart(
                tool_use_id="checkpoint-1",
                tool_name="plan_run_checkpoint",
            )
            yield ProviderToolUseEnd(
                tool_use_id="checkpoint-1",
                tool_name="plan_run_checkpoint",
                arguments={"step_id": "step-1", "step_status": "completed"},
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text="Implementation and verification are complete.")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[ModelInfo]:
        return []


class _IgnoresReconciliationProvider(_ReconcilesCheckpointProvider):
    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        yield ProviderText(text=f"Premature completion {call_number}.")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)


class _CheckpointThenMutateProvider(_ReconcilesCheckpointProvider):
    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            for tool_use_id, tool_name, arguments in (
                (
                    "checkpoint-1",
                    "plan_run_checkpoint",
                    {"step_id": "step-1", "step_status": "completed"},
                ),
                (
                    "write-1",
                    "write_file",
                    {"path": "after-completion.txt", "content": "must not run"},
                ),
            ):
                yield ProviderToolUseStart(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                )
                yield ProviderToolUseEnd(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    arguments=arguments,
                )
            yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text="The implementation is complete.")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)


class _CheckpointThenPublishProvider(_ReconcilesCheckpointProvider):
    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            for tool_use_id, tool_name, arguments in (
                (
                    "checkpoint-1",
                    "plan_run_checkpoint",
                    {"step_id": "step-1", "step_status": "completed"},
                ),
                (
                    "publish-1",
                    "publish_artifact",
                    {"path": "report.txt"},
                ),
            ):
                yield ProviderToolUseStart(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                )
                yield ProviderToolUseEnd(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    arguments=arguments,
                )
            yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text="The report is ready.")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)


class _SubmitThenCheckpointProvider(_ReconcilesCheckpointProvider):
    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderToolUseStart(tool_use_id="submit-1", tool_name="submit")
            yield ProviderToolUseEnd(
                tool_use_id="submit-1",
                tool_name="submit",
                arguments={},
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return
        if call_number == 2:
            yield ProviderToolUseStart(
                tool_use_id="checkpoint-1",
                tool_name="plan_run_checkpoint",
            )
            yield ProviderToolUseEnd(
                tool_use_id="checkpoint-1",
                tool_name="plan_run_checkpoint",
                arguments={"step_id": "step-1", "step_status": "completed"},
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text="The implementation is complete.")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)


def _revision() -> SimpleNamespace:
    return SimpleNamespace(
        revision_id="revision-1",
        plan_id="plan-1",
        generation=1,
        title="Implementation plan",
        markdown="## Implementation plan",
        steps=[{"step_id": "step-1", "title": "Implement"}],
        content_hash="hash-1",
    )


def _registry(
    storage: _PlanStorage,
    observed_calls: list[str] | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()

    async def checkpoint(step_id: str, step_status: str) -> str:
        assert step_id == "step-1"
        assert step_status == "completed"
        storage.complete()
        return json.dumps(
            {
                "status": "checkpoint_recorded",
                "plan_run": {
                    "runId": storage.run.run_id,
                    "status": storage.run.status,
                    "currentStepId": storage.run.current_step_id,
                    "steps": [
                        {
                            "stepId": state["step_id"],
                            "status": state["status"],
                        }
                        for state in storage.run.step_states
                    ],
                },
            }
        )

    registry.register(
        ToolSpec(
            name="plan_run_checkpoint",
            description="Checkpoint the current plan step",
            parameters={
                "type": "object",
                "properties": {
                    "step_id": {"type": "string"},
                    "step_status": {"type": "string"},
                },
                "required": ["step_id", "step_status"],
            },
        ),
        checkpoint,
    )

    async def write_file(path: str, content: str) -> str:
        if observed_calls is not None:
            observed_calls.append(f"write_file:{path}:{content}")
        return "written"

    registry.register(
        ToolSpec(
            name="write_file",
            description="Write a workspace file",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        ),
        write_file,
    )

    async def submit() -> str:
        if observed_calls is not None:
            observed_calls.append("submit")
        return "submitted"

    registry.register(
        ToolSpec(
            name="submit",
            description="Submit implementation",
            parameters={"type": "object", "properties": {}},
        ),
        submit,
    )

    async def publish_artifact(path: str) -> str:
        if observed_calls is not None:
            observed_calls.append(f"publish_artifact:{path}")
        return "published"

    registry.register(
        ToolSpec(
            name="publish_artifact",
            description="Publish a generated artifact",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
        publish_artifact,
    )
    return registry


async def _run(
    tmp_path: Path,
    provider: Any,
    plan_storage: _PlanStorage,
    *,
    observed_calls: list[str] | None = None,
) -> list[Any]:
    session_storage = SessionStorage(":memory:")
    await session_storage.connect()
    manager = SessionManager(session_storage)
    session_key = "agent:main:webchat:plan-reconciliation"
    await manager.create(session_key)
    runner = TurnRunner(
        provider_selector=_ProviderSelector(provider),
        tool_registry=_registry(plan_storage, observed_calls),
        session_manager=manager,
        config=GatewayConfig(
            workspace_dir=str(tmp_path),
            squilla_router=SquillaRouterConfig(enabled=False),
        ),
    )
    context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(tmp_path),
        session_key=session_key,
        task_id="task-1",
        plan_run_id=plan_storage.run.run_id,
        plan_storage=plan_storage,
        plan_revision=_revision(),
        plan_run=plan_storage.run,
    )
    try:
        return [
            event
            async for event in runner.run(
                "Implement the approved plan",
                session_key,
                tool_context=context,
                history_has_persisted_user=False,
                no_memory_capture=True,
            )
        ]
    finally:
        await session_storage.close()


@pytest.mark.asyncio
async def test_plan_run_final_response_gets_one_checkpoint_reconciliation(
    tmp_path: Path,
) -> None:
    plan_storage = _PlanStorage()
    provider = _ReconcilesCheckpointProvider()

    events = await _run(tmp_path, provider, plan_storage)

    assert provider.calls == 3
    assert plan_storage.run.status == "running"
    assert plan_storage.run.current_step_id is None
    assert any(
        isinstance(event, WarningEvent) and event.code == "plan_run_reconciliation"
        for event in events
    )
    assert not any(isinstance(event, ErrorEvent) for event in events)
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.text == "Implementation and verification are complete."
    second_request = "\n".join(str(message.content) for message in provider.requests[1])
    assert "[PlanRun reconciliation]" in second_request
    assert '"currentStepId": "step-1"' in second_request


@pytest.mark.asyncio
async def test_plan_run_cannot_succeed_after_ignoring_reconciliation(
    tmp_path: Path,
) -> None:
    plan_storage = _PlanStorage()
    provider = _IgnoresReconciliationProvider()

    events = await _run(tmp_path, provider, plan_storage)

    assert provider.calls == 2
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert [event.code for event in errors] == ["plan_run_checkpoint_required"]
    assert plan_storage.run.status == "running"


@pytest.mark.asyncio
async def test_final_checkpoint_rejects_later_workspace_mutation(
    tmp_path: Path,
) -> None:
    plan_storage = _PlanStorage()
    provider = _CheckpointThenMutateProvider()
    observed_calls: list[str] = []

    events = await _run(
        tmp_path,
        provider,
        plan_storage,
        observed_calls=observed_calls,
    )

    assert observed_calls == []
    assert plan_storage.run.status == "running"
    assert plan_storage.run.current_step_id is None
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert next(event for event in events if isinstance(event, DoneEvent)).text == (
        "The implementation is complete."
    )
    denied_result = "\n".join(str(message.content) for message in provider.requests[1])
    assert "plan_run_delivery_only" in denied_result


@pytest.mark.asyncio
async def test_final_checkpoint_allows_later_artifact_delivery(
    tmp_path: Path,
) -> None:
    plan_storage = _PlanStorage()
    provider = _CheckpointThenPublishProvider()
    observed_calls: list[str] = []

    events = await _run(
        tmp_path,
        provider,
        plan_storage,
        observed_calls=observed_calls,
    )

    assert observed_calls == ["publish_artifact:report.txt"]
    assert plan_storage.run.status == "running"
    assert plan_storage.run.current_step_id is None
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert next(event for event in events if isinstance(event, DoneEvent)).text == (
        "The report is ready."
    )


@pytest.mark.asyncio
async def test_attached_plan_run_rejects_submit_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_SUBMIT_REVIEW", "on")
    plan_storage = _PlanStorage()
    provider = _SubmitThenCheckpointProvider()
    observed_calls: list[str] = []

    events = await _run(
        tmp_path,
        provider,
        plan_storage,
        observed_calls=observed_calls,
    )

    assert "submit" not in observed_calls
    assert plan_storage.run.status == "running"
    assert plan_storage.run.current_step_id is None
    assert not any(isinstance(event, ErrorEvent) for event in events)
    submit_result = "\n".join(str(message.content) for message in provider.requests[1])
    assert "plan_run_checkpoint_required" in submit_result


@pytest.mark.asyncio
async def test_goal_driver_can_yield_without_manual_reconciliation(
    tmp_path: Path,
) -> None:
    plan_storage = _PlanStorage()
    plan_storage.run.driver_kind = "goal"
    provider = _IgnoresReconciliationProvider()

    events = await _run(tmp_path, provider, plan_storage)

    assert provider.calls == 1
    assert not any(isinstance(event, WarningEvent) for event in events)
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert next(event for event in events if isinstance(event, DoneEvent)).text == (
        "Premature completion 1."
    )
