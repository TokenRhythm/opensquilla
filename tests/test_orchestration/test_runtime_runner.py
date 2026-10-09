from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from opensquilla.engine.subagent_delegation import SUBAGENT_EXECUTION_PROMPT
from opensquilla.gateway.orchestration_routing import ChildTreeRoute
from opensquilla.gateway.orchestration_runtime_runner import (
    TaskRuntimeActivationRunner,
    TaskRuntimeParentNotifier,
    _continuation_prompt,
    _parse_child_report,
    _task_prompt,
)
from opensquilla.orchestration.models import (
    ActivationPhase,
    AgentActivationRecord,
    AgentSessionRecord,
    DelegatedTaskRecord,
    OrchestrationMode,
    TaskOutcome,
)
from opensquilla.orchestration.repository import OrchestrationRepository
from opensquilla.orchestration.service import OrchestrationService
from opensquilla.orchestration.session_recall import SessionRecallEngine
from opensquilla.session.models import AgentTaskStatus

pytestmark = pytest.mark.asyncio


class FakeRecallEmbedder:
    model = "BAAI/bge-small-zh-v1.5"

    async def embed_query(self, text: str) -> list[float]:
        del text
        return [1.0, 0.0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _text in texts]


async def test_child_report_accepts_trailing_json_after_progress_prose():
    outcome, payload = _parse_child_report(
        "I inspected the requested files.\n\n"
        '{"status":"completed","summary":"Found the cause.",'
        '"deliverable":"Cause at Example.java:42.",'
        '"error":null,"retry_same_agent":false,"unresolved":[]}'
    )

    assert outcome is TaskOutcome.SUCCEEDED
    assert payload == {
        "status": "completed",
        "summary": "Found the cause.",
        "deliverable": "Cause at Example.java:42.",
        "error": None,
        "retry_same_agent": False,
        "unresolved": [],
    }


async def test_child_report_accepts_trailing_fenced_json_after_progress_prose():
    outcome, payload = _parse_child_report(
        "I inspected the requested files.\n\n"
        "```json\n"
        '{"status":"completed","summary":"Found the cause with ```java snippets.",'
        '"deliverable":"Cause at Example.java:42.",'
        '"error":null,"retry_same_agent":false,"unresolved":[]}\n'
        "```"
    )

    assert outcome is TaskOutcome.SUCCEEDED
    assert payload == {
        "status": "completed",
        "summary": "Found the cause with ```java snippets.",
        "deliverable": "Cause at Example.java:42.",
        "error": None,
        "retry_same_agent": False,
        "unresolved": [],
    }


async def test_child_report_accepts_single_result_wrapper():
    outcome, payload = _parse_child_report(
        "Progress notes.\n"
        '{"result":{"status":"completed","summary":"Found the test command.",'
        '"deliverable":"Run mvn test -Dtest=ExampleTest.",'
        '"error":null,"retry_same_agent":false,"unresolved":[]}}'
    )

    assert outcome is TaskOutcome.SUCCEEDED
    assert payload == {
        "status": "completed",
        "summary": "Found the test command.",
        "deliverable": "Run mvn test -Dtest=ExampleTest.",
        "error": None,
        "retry_same_agent": False,
        "unresolved": [],
    }


@pytest.mark.parametrize(
    "report",
    [
        (
            '{"status":"completed","summary":"Found the cause.",'
            '"error":null,"retry_same_agent":false,"unresolved":[]}'
        ),
        (
            '{"status":"completed","summary":"Found the cause.",'
            '"deliverable":"Cause at Example.java:42.",'
            '"error":null,"retry_same_agent":false}'
        ),
    ],
)
async def test_child_report_rejects_completed_status_missing_completion_fields(
    report: str,
):
    outcome, payload = _parse_child_report(report)

    assert outcome is TaskOutcome.FAILED
    assert payload["status"] == "failed"
    assert payload["error_type"] == "InvalidSubagentResult"
    assert payload["retry_same_agent"] is True


async def test_child_report_rejects_json_object_missing_required_contract_fields():
    outcome, payload = _parse_child_report(
        "Progress notes that must not become a successful summary.\n"
        '{"deliverable":{"root_cause":"Found a possible location."},'
        '"unresolved":["Confirm the exact production method."]}'
    )

    assert outcome is TaskOutcome.FAILED
    assert payload["status"] == "failed"
    assert payload["error_type"] == "InvalidSubagentResult"
    assert payload["retry_same_agent"] is True


async def test_child_report_rejects_completed_status_with_unresolved_criteria():
    outcome, payload = _parse_child_report(
        '{"status":"completed","summary":"Located the boundary bug.",'
        '"deliverable":"JodaUtils.java:42\\nReturn an empty result for zero-length intervals.",'
        '"error":null,"retry_same_agent":false,'
        '"unresolved":["Confirm the public API expectation."]}'
    )

    assert outcome is TaskOutcome.FAILED
    assert payload == {
        "status": "failed",
        "summary": "Located the boundary bug.",
        "deliverable": (
            "JodaUtils.java:42\nReturn an empty result for zero-length intervals."
        ),
        "error": "Unable to complete: not all assigned acceptance criteria were satisfied.",
        "error_type": "UnsatisfiedSubagentCriteria",
        "retry_same_agent": False,
        "unresolved": ["Confirm the public API expectation."],
    }


async def test_child_report_rejects_json_followed_by_more_prose():
    outcome, payload = _parse_child_report(
        '{"status":"completed","summary":"Found the cause.",'
        '"error":null,"retry_same_agent":false}\nextra text'
    )

    assert outcome is TaskOutcome.FAILED
    assert payload["error_type"] == "InvalidSubagentResult"
    assert payload["retry_same_agent"] is True


async def test_child_report_rejects_plain_final_answer_without_required_fields():
    outcome, payload = _parse_child_report(
        "Found the target method, its failing boundary condition, and the focused test command."
    )

    assert outcome is TaskOutcome.FAILED
    assert payload["error_type"] == "InvalidSubagentResult"
    assert payload["retry_same_agent"] is True


async def test_single_child_report_accepts_plain_final_answer_as_deliverable():
    answer = "Wrote anomaly_report.json and checked all three requested anomalies."

    outcome, payload = _parse_child_report(answer, single_mode=True)

    assert outcome is TaskOutcome.SUCCEEDED
    assert payload["status"] == "completed"
    assert payload["deliverable"] == answer
    assert payload["retry_same_agent"] is False


async def test_single_child_report_accepts_user_requested_json_as_deliverable():
    answer = '{"anomalies":[{"type":"impossible_travel"}]}'

    outcome, payload = _parse_child_report(answer, single_mode=True)

    assert outcome is TaskOutcome.SUCCEEDED
    assert payload["deliverable"] == answer


async def test_single_child_report_preserves_explicit_structured_failure():
    outcome, payload = _parse_child_report(
        '{"status":"failed","summary":"Cannot access the repository.",'
        '"error":"Capability not supported: gh CLI",'
        '"retry_same_agent":false}',
        single_mode=True,
    )

    assert outcome is TaskOutcome.FAILED
    assert payload["error"] == "Capability not supported: gh CLI"
    assert payload["retry_same_agent"] is False


async def test_single_child_report_does_not_hide_malformed_structured_report():
    outcome, payload = _parse_child_report(
        '{"status":"completed","summary":"Done without a deliverable."}',
        single_mode=True,
    )

    assert outcome is TaskOutcome.FAILED
    assert payload["error_type"] == "InvalidSubagentResult"


async def test_single_child_report_rejects_empty_final_answer():
    outcome, payload = _parse_child_report("   ", single_mode=True)

    assert outcome is TaskOutcome.FAILED
    assert payload["error_type"] == "InvalidSubagentResult"


async def test_child_report_rejects_unanswered_clarification_as_success():
    outcome, payload = _parse_child_report(
        "I looked at the source files but did not identify the cause. "
        "What would you like me to work on?"
    )

    assert outcome is TaskOutcome.FAILED
    assert payload["error_type"] == "InvalidSubagentResult"
    assert payload["retry_same_agent"] is True


async def test_child_report_rejects_empty_final_answer():
    outcome, payload = _parse_child_report("   ")

    assert outcome is TaskOutcome.FAILED
    assert payload["error_type"] == "InvalidSubagentResult"


async def test_first_child_prompt_uses_only_assigned_task_and_criteria():
    prompt = _task_prompt(
        DelegatedTaskRecord(
            task_id="child-task",
            run_id="run-1",
            task_key="implement",
            owner_session_id="child-session",
            description="Use helper X and expect one internal placeholder.",
            acceptance_criteria=(
                "Return the changed file, exact behavior, and focused test result."
            ),
        ),
        root_task=("Fix the public lookup API: a zero-length request should return no matches."),
    )

    assert "Original root request" not in prompt
    assert "a zero-length request should return no matches" not in prompt
    assert "Assigned task (the only work to execute):\nUse helper X" in prompt
    assert "Acceptance criteria (return immediately once all are satisfied):" in prompt
    assert "Return the changed file, exact behavior, and focused test result." in prompt
    assert "Final acceptance authority" not in prompt


async def test_child_prompt_does_not_inject_root_request():
    prompt = _task_prompt(
        DelegatedTaskRecord(
            task_id="child-task",
            run_id="run-1",
            task_key="verify",
            owner_session_id="child-session",
            description=(
                "Confirm that returning one empty internal holder means callers match nothing."
            ),
            acceptance_criteria="Return the public behavior and supporting test evidence.",
        ),
        root_task="The public lookup API must return zero matching results.",
    )

    assert "The public lookup API must return zero matching results" not in prompt
    assert "returning one empty internal holder" in prompt


async def test_single_agent_child_prompt_requests_full_answer_to_user():
    prompt = _task_prompt(
        DelegatedTaskRecord(
            task_id="child-task",
            run_id="run-1",
            task_key="figure-captions",
            owner_session_id="child-session",
            description="Write captions for three figures.",
            acceptance_criteria="Return three accurate captions with source notes.",
            runtime_context={"single_agent_mode": True},
        ),
    )

    assert "Write captions for three figures." in prompt
    assert "complete the assigned task" in prompt
    assert "entire assigned user request" not in prompt
    assert "full final answer" in prompt
    assert SUBAGENT_EXECUTION_PROMPT not in prompt


async def test_retry_prompt_keeps_existing_context_without_replaying_full_assignment():
    task = DelegatedTaskRecord(
        task_id="child-task",
        run_id="run-1",
        task_key="verify",
        owner_session_id="child-session",
        description="Repeat the complete original task description.",
        acceptance_criteria="Repeat the complete original acceptance criteria.",
        retry_of_activation_id="activation-1",
    )

    prompt = _continuation_prompt(task)

    assert "Continue the previous task in this existing conversation" in prompt
    assert "Reuse its completed work and tool results" in prompt
    assert "Finish only the remaining work" in prompt
    assert "Repeat the complete original task description" not in prompt
    assert "Repeat the complete original acceptance criteria" not in prompt
    assert SUBAGENT_EXECUTION_PROMPT not in prompt


async def test_follow_up_prompt_appends_only_the_new_delta():
    task = DelegatedTaskRecord(
        task_id="follow-up-task",
        run_id="run-1",
        task_key="clarify-one-field",
        owner_session_id="child-session",
        description="Revise only the existing affected-files field with exact paths.",
        acceptance_criteria="Return exact paths for that existing field.",
    )

    prompt = _continuation_prompt(task)

    assert "Continue in this existing conversation with only this follow-up" in prompt
    assert "Revise only the existing affected-files field with exact paths." in prompt
    assert "Return exact paths for that existing field." in prompt
    assert "Assigned task key" not in prompt
    assert SUBAGENT_EXECUTION_PROMPT not in prompt


class FakeSessionManager:
    def __init__(self) -> None:
        self.nodes = {
            "agent:main:root": SimpleNamespace(
                session_key="agent:main:root",
                session_id="runtime-root-id",
                epoch=4,
                workspace_id="workspace-1",
                model="parent-model",
                origin={},
            )
        }
        self.create_calls: list[dict[str, object]] = []
        self.append_calls: list[tuple[str, str, str]] = []
        self.transcripts: dict[str, list[object]] = {}

    async def get_session(self, session_key: str):
        return self.nodes.get(session_key)

    async def get_or_create(self, session_key: str, agent_id: str = "main", **kwargs):
        existing = self.nodes.get(session_key)
        if existing is not None:
            return existing, False
        self.create_calls.append({"session_key": session_key, "agent_id": agent_id, **kwargs})
        node = SimpleNamespace(
            session_key=session_key,
            session_id=f"runtime-{len(self.nodes)}",
            epoch=0,
            workspace_id=kwargs.get("workspace_id"),
            model=kwargs.get("model"),
            origin=kwargs.get("origin"),
        )
        self.nodes[session_key] = node
        return node, True

    async def update(self, session_key: str, **fields):
        node = self.nodes[session_key]
        for key, value in fields.items():
            if key not in {"expected_session_id", "expected_session_epoch"}:
                setattr(node, key, value)
        return node

    async def append_message(self, session_key: str, role: str, content: str, **kwargs):
        self.append_calls.append((session_key, role, content))
        return SimpleNamespace(message_id=f"message-{len(self.append_calls)}")

    async def get_transcript(self, session_key: str, **kwargs):
        return self.transcripts.get(session_key, [])


class FakeTaskRuntime:
    def __init__(
        self,
        status: AgentTaskStatus = AgentTaskStatus.SUCCEEDED,
        *,
        terminal_details: dict[str, object] | None = None,
    ) -> None:
        self.status_value = status
        self.terminal_details = terminal_details
        self.enqueues: list[tuple[object, str, dict[str, object]]] = []
        self.cancelled: list[dict[str, str]] = []
        self.steer_result: str | None = None
        self.steers: list[tuple[str, str]] = []
        self.sends: list[tuple[str, str]] = []
        self.send_kwargs: list[dict[str, object]] = []
        self.known_task_ids: set[str] = set()
        self.task_statuses: dict[str, AgentTaskStatus] = {}

    async def enqueue(self, envelope, message: str, **kwargs):
        task_id = str(kwargs["task_id"])
        if task_id in self.known_task_ids:
            raise ValueError(f"duplicate runtime task: {task_id}")
        self.known_task_ids.add(task_id)
        self.enqueues.append((envelope, message, kwargs))
        return SimpleNamespace(task_id=task_id)

    async def wait(self, task_id: str, timeout=None):
        return SimpleNamespace(
            status=self.status_value,
            terminal_reason=(
                "provider_failed" if self.status_value is AgentTaskStatus.FAILED else None
            ),
            error_class="ProviderError" if self.status_value is AgentTaskStatus.FAILED else None,
            error_message="boom" if self.status_value is AgentTaskStatus.FAILED else None,
            details=self.terminal_details,
        )

    async def cancel_exact(self, **kwargs):
        self.cancelled.append(kwargs)
        return 1

    async def steer(self, session_key: str, message: str, **kwargs):
        self.steers.append((session_key, message))
        return self.steer_result

    async def send(self, session_key: str, message: str, **kwargs):
        self.sends.append((session_key, message))
        self.send_kwargs.append(kwargs)
        task_id = str(kwargs.get("task_id") or "notification-task")
        self.known_task_ids.add(task_id)
        return SimpleNamespace(task_id=task_id)

    async def status(self, task_id: str):
        if task_id not in self.known_task_ids:
            raise KeyError(task_id)
        return SimpleNamespace(
            task_id=task_id,
            status=self.task_statuses.get(task_id, AgentTaskStatus.SUCCEEDED),
        )


async def _records(tmp_path):
    repo = await OrchestrationRepository.open(tmp_path / "runtime.db")
    service = OrchestrationService(repo)
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    runtime_context = {
        "principal_is_owner": True,
        "principal_host_execute": False,
        "run_mode": "safe",
        "active_model": "parent-model",
        "active_provider": "parent-provider",
    }
    await service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Root",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
        root_runtime_session_key="agent:main:root",
        root_runtime_context=runtime_context,
    )
    session = AgentSessionRecord(
        session_id="child-session",
        run_id="run-1",
        profile="inherit",
        runtime_session_key="agent:main:subagent:child",
        parent_session_id="root-session",
        depth=1,
        effective_tools=tools,
        runtime_context=runtime_context,
    )
    task = DelegatedTaskRecord(
        task_id="child-task",
        run_id="run-1",
        task_key="inspect",
        owner_session_id=session.session_id,
        parent_task_id="root-task",
        description="Inspect the implementation",
        background=True,
        effective_tools=tools,
        runtime_context=runtime_context,
    )
    activation = AgentActivationRecord(
        activation_id="activation-1",
        session_id=session.session_id,
        task_id=task.task_id,
        phase=ActivationPhase.RUNNING,
    )
    await repo.create_session(session)
    await repo.create_task(task)
    await repo.create_activation(activation)
    return repo, session, task, activation


async def test_runner_creates_runtime_session_with_tree_route_and_returns_transcript(tmp_path):
    repo, session, task, activation = await _records(tmp_path)
    manager = FakeSessionManager()
    manager.transcripts[session.runtime_session_key] = [
        SimpleNamespace(
            role="assistant",
            content=(
                '{"status":"completed","summary":"Inspected; all good.",'
                '"deliverable":"Confirmed the assigned behavior.",'
                '"error":null,"retry_same_agent":false,"unresolved":[]}'
            ),
        )
    ]
    runtime = FakeTaskRuntime()
    runner = TaskRuntimeActivationRunner(
        repository=repo,
        session_manager=manager,
        task_runtime=runtime,
        config=SimpleNamespace(llm=SimpleNamespace(model="fallback-model")),
        session_recall=SessionRecallEngine(FakeRecallEmbedder()),
    )
    try:
        result = await runner.run(
            activation=activation,
            session=session,
            task=task,
            route=ChildTreeRoute(
                model="tree-model",
                provider="tree-provider",
                tier="c3",
                source="tree",
                confidence=0.9,
                thinking_level="xhigh",
            ),
        )
        assert result.outcome is TaskOutcome.SUCCEEDED
        assert result.result["summary"] == "Inspected; all good."
        assert result.result["status"] == "completed"
        assert result.result["retry_same_agent"] is False
        assert result.result["recall_index"]["model"] == "BAAI/bge-small-zh-v1.5"
        assert result.result["recall_index"]["embedding"] == [1.0, 0.0]
        assert manager.create_calls[0]["model"] == "tree-model"
        assert manager.create_calls[0]["origin"]["routing"] == {
            "model": "tree-model",
            "provider": "tree-provider",
            "tier": "c3",
            "source": "tree",
            "confidence": 0.9,
            "thinking_level": "xhigh",
        }
        envelope, message, kwargs = runtime.enqueues[0]
        assert "do not repeat completed work" in message
        assert "Original root request" not in message
        assert "Assigned task (the only work to execute):" in message
        assert kwargs["task_id"] == f"orchestration-activation:{activation.activation_id}"
        assert envelope.metadata["orchestration_run_id"] == "run-1"
        assert envelope.metadata["orchestration_session_id"] == "child-session"
        assert envelope.metadata["orchestration_task_id"] == "child-task"
        assert envelope.metadata["complex_task_mode"] is True
        assert envelope.metadata["subagent_tools"] == sorted(task.effective_tools)
        assert "subagent_effort_tier" not in envelope.metadata
        assert "subagent_iteration_soft_limit" not in envelope.metadata
        assert "subagent_iteration_hard_limit" not in envelope.metadata
        accepted = kwargs["accepted_run_mode_override"]
        assert accepted.run_mode.value == "safe"
    finally:
        await repo.close()


async def test_single_agent_child_route_preserves_mode_after_cold_start(tmp_path):
    repo, session, task, activation = await _records(tmp_path)
    task.runtime_context["single_agent_mode"] = True
    manager = FakeSessionManager()
    runtime = FakeTaskRuntime()
    runner = TaskRuntimeActivationRunner(
        repository=repo,
        session_manager=manager,
        task_runtime=runtime,
        config=SimpleNamespace(llm=SimpleNamespace(model="fallback-model")),
    )
    try:
        await runner.run(activation=activation, session=session, task=task, route=None)

        envelope, message, _kwargs = runtime.enqueues[0]
        assert envelope.metadata["single_agent_mode"] is True
        assert "full final answer" in message
    finally:
        await repo.close()


async def test_single_agent_runner_preserves_plain_child_answer(tmp_path):
    repo, session, task, activation = await _records(tmp_path)
    task.runtime_context["single_agent_mode"] = True
    answer = "Saved the requested report to anomaly_report.json."
    manager = FakeSessionManager()
    manager.transcripts[session.runtime_session_key] = [
        SimpleNamespace(role="assistant", content=answer)
    ]
    runner = TaskRuntimeActivationRunner(
        repository=repo,
        session_manager=manager,
        task_runtime=FakeTaskRuntime(),
        config=SimpleNamespace(llm=SimpleNamespace(model="fallback-model")),
    )
    try:
        result = await runner.run(
            activation=activation,
            session=session,
            task=task,
            route=None,
        )

        assert result.outcome is TaskOutcome.SUCCEEDED
        assert result.result["deliverable"] == answer
    finally:
        await repo.close()


async def test_retry_uses_a_new_runtime_identity_for_the_same_durable_task(tmp_path):
    repo, session, task, activation = await _records(tmp_path)
    manager = FakeSessionManager()
    runtime = FakeTaskRuntime()
    runner = TaskRuntimeActivationRunner(
        repository=repo,
        session_manager=manager,
        task_runtime=runtime,
        config=SimpleNamespace(llm=SimpleNamespace(model="fallback-model")),
    )
    retry_activation = AgentActivationRecord(
        activation_id="activation-2",
        session_id=session.session_id,
        task_id=task.task_id,
        phase=ActivationPhase.RUNNING,
    )
    task.retry_of_activation_id = activation.activation_id
    try:
        await runner.run(
            activation=activation,
            session=session,
            task=task,
            route=None,
        )
        await runner.run(
            activation=retry_activation,
            session=session,
            task=task,
            route=None,
        )

        assert [call[2]["task_id"] for call in runtime.enqueues] == [
            "orchestration-activation:activation-1",
            "orchestration-activation:activation-2",
        ]
        first_prompt = runtime.enqueues[0][1]
        retry_prompt = runtime.enqueues[1][1]
        assert SUBAGENT_EXECUTION_PROMPT in first_prompt
        assert "Assigned task (the only work to execute):" in first_prompt
        assert "Continue the previous task in this existing conversation" in retry_prompt
        assert "Inspect the implementation" not in retry_prompt
        assert SUBAGENT_EXECUTION_PROMPT not in retry_prompt
        assert manager.create_calls[0]["model"] == "parent-model"
        assert len(manager.create_calls) == 1
    finally:
        await repo.close()


async def test_same_session_follow_up_appends_only_delta_prompt(tmp_path):
    repo, session, task, activation = await _records(tmp_path)
    manager = FakeSessionManager()
    runtime = FakeTaskRuntime()
    runner = TaskRuntimeActivationRunner(
        repository=repo,
        session_manager=manager,
        task_runtime=runtime,
        config=SimpleNamespace(llm=SimpleNamespace(model="fallback-model")),
    )
    follow_up = DelegatedTaskRecord(
        task_id="follow-up-task",
        run_id=task.run_id,
        task_key="clarify-one-field",
        owner_session_id=session.session_id,
        parent_task_id=task.parent_task_id,
        description="Revise only the existing affected-files field with exact paths.",
        acceptance_criteria="Return exact paths for that existing field.",
        effective_tools=task.effective_tools,
        runtime_context=task.runtime_context,
    )
    follow_up_activation = AgentActivationRecord(
        activation_id="activation-2",
        session_id=session.session_id,
        task_id=follow_up.task_id,
        phase=ActivationPhase.RUNNING,
    )
    try:
        await runner.run(activation=activation, session=session, task=task, route=None)
        await runner.run(
            activation=follow_up_activation,
            session=session,
            task=follow_up,
            route=None,
        )

        prompt = runtime.enqueues[1][1]
        assert "Continue in this existing conversation with only this follow-up" in prompt
        assert "Revise only the existing affected-files field with exact paths." in prompt
        assert "Return exact paths for that existing field." in prompt
        assert SUBAGENT_EXECUTION_PROMPT not in prompt
        assert len(manager.create_calls) == 1
    finally:
        await repo.close()


async def test_runner_uses_activation_task_authority_not_stale_session_authority(tmp_path):
    repo, session, task, activation = await _records(tmp_path)
    session.runtime_context = {
        "principal_is_owner": True,
        "principal_host_execute": True,
        "elevated": "full",
        "run_mode": "full",
    }
    task.runtime_context = {
        "principal_is_owner": False,
        "principal_host_execute": False,
        "elevated": None,
        "run_mode": "safe",
    }
    manager = FakeSessionManager()
    manager.nodes[session.runtime_session_key] = SimpleNamespace(
        session_key=session.runtime_session_key,
        session_id="runtime-child-old",
        epoch=2,
        workspace_id="old-trusted-workspace",
        model="old-model",
        origin={"sandbox_run_context": {"run_mode": "full"}},
    )
    manager.nodes["agent:main:root"].workspace_id = None
    runtime = FakeTaskRuntime()
    runner = TaskRuntimeActivationRunner(
        repository=repo,
        session_manager=manager,
        task_runtime=runtime,
        config=SimpleNamespace(llm=SimpleNamespace(model="fallback-model")),
    )
    try:
        await runner.run(activation=activation, session=session, task=task, route=None)

        envelope, _message, kwargs = runtime.enqueues[0]
        assert envelope.metadata["principal_is_owner"] is False
        assert envelope.metadata["principal_host_execute"] is False
        assert envelope.metadata["run_mode"] == "safe"
        assert "elevated" not in envelope.metadata
        assert kwargs["accepted_run_mode_override"].run_mode.value == "safe"
        child = manager.nodes[session.runtime_session_key]
        assert child.origin["sandbox_run_context"]["run_mode"] == "safe"
        assert child.workspace_id is None
    finally:
        await repo.close()


async def test_runner_uses_task_workspace_snapshot_not_mutable_parent_workspace(tmp_path):
    repo, session, task, activation = await _records(tmp_path)
    task.runtime_context = {
        **task.runtime_context,
        "workspace_id": "workspace-at-acceptance",
    }
    manager = FakeSessionManager()
    manager.nodes["agent:main:root"].workspace_id = "workspace-at-execution"
    runtime = FakeTaskRuntime()
    runner = TaskRuntimeActivationRunner(
        repository=repo,
        session_manager=manager,
        task_runtime=runtime,
        config=SimpleNamespace(llm=SimpleNamespace(model="fallback-model")),
    )
    try:
        await runner.run(activation=activation, session=session, task=task, route=None)

        child = manager.nodes[session.runtime_session_key]
        assert child.workspace_id == "workspace-at-acceptance"
    finally:
        await repo.close()


async def test_runner_maps_runtime_failure_and_interrupts_exact_task(tmp_path):
    repo, session, task, activation = await _records(tmp_path)
    manager = FakeSessionManager()
    runtime = FakeTaskRuntime(AgentTaskStatus.FAILED)
    runner = TaskRuntimeActivationRunner(
        repository=repo,
        session_manager=manager,
        task_runtime=runtime,
        config=SimpleNamespace(llm=SimpleNamespace(model="fallback-model")),
    )
    try:
        result = await runner.run(
            activation=activation,
            session=session,
            task=task,
            route=None,
        )
        assert result.outcome is TaskOutcome.FAILED
        assert result.result["status"] == "failed"
        assert result.result["retry_same_agent"] is True
        assert result.result["error"] == "boom"

        await runner.interrupt(activation=activation, reason="watchdog")
        assert runtime.cancelled == [
            {
                "task_id": f"orchestration-activation:{activation.activation_id}",
                "session_key": session.runtime_session_key,
                "source": "orchestration_interrupt",
                "reason": "watchdog",
            }
        ]
    finally:
        await repo.close()


async def test_runner_does_not_retry_same_agent_after_non_retryable_runtime_failure(tmp_path):
    repo, session, task, activation = await _records(tmp_path)
    manager = FakeSessionManager()
    runtime = FakeTaskRuntime(
        AgentTaskStatus.FAILED,
        terminal_details={
            "turn_outcome": {
                "kind": "failed",
                "reason": "unknown_fatal_error",
                "error_class": "unknown_fatal_error",
                "error_message": "The child failed.",
                "retryable": False,
            }
        },
    )
    runner = TaskRuntimeActivationRunner(
        repository=repo,
        session_manager=manager,
        task_runtime=runtime,
        config=SimpleNamespace(llm=SimpleNamespace(model="fallback-model")),
    )
    try:
        result = await runner.run(
            activation=activation,
            session=session,
            task=task,
            route=None,
        )

        assert result.outcome is TaskOutcome.FAILED
        assert result.result["terminal_reason"] == "provider_failed"
        assert result.result["retry_same_agent"] is False
    finally:
        await repo.close()


async def test_runner_can_resume_session_after_repeated_tool_call_interrupt(tmp_path):
    repo, session, task, activation = await _records(tmp_path)
    manager = FakeSessionManager()
    runtime = FakeTaskRuntime(
        AgentTaskStatus.FAILED,
        terminal_details={
            "turn_outcome": {
                "kind": "interrupted",
                "reason": "repeated_tool_call_blocked",
                "error_class": "repeated_tool_call_blocked",
                "error_message": "Exact tool call repeated five times.",
                "retryable": True,
            }
        },
    )
    runner = TaskRuntimeActivationRunner(
        repository=repo,
        session_manager=manager,
        task_runtime=runtime,
        config=SimpleNamespace(llm=SimpleNamespace(model="fallback-model")),
    )
    try:
        result = await runner.run(
            activation=activation,
            session=session,
            task=task,
            route=None,
        )

        assert result.outcome is TaskOutcome.FAILED
        assert result.result["retry_same_agent"] is True
    finally:
        await repo.close()


async def test_runner_treats_child_reported_incomplete_work_as_failure(tmp_path):
    repo, session, task, activation = await _records(tmp_path)
    manager = FakeSessionManager()
    manager.transcripts[session.runtime_session_key] = [
        SimpleNamespace(
            role="assistant",
            content=(
                '{"status":"failed","summary":"Image was not generated.",'
                '"error":"image generation tool is unavailable",'
                '"retry_same_agent":false}'
            ),
        )
    ]
    runtime = FakeTaskRuntime(AgentTaskStatus.SUCCEEDED)
    runner = TaskRuntimeActivationRunner(
        repository=repo,
        session_manager=manager,
        task_runtime=runtime,
        config=SimpleNamespace(llm=SimpleNamespace(model="fallback-model")),
    )
    try:
        result = await runner.run(
            activation=activation,
            session=session,
            task=task,
            route=None,
        )

        assert result.outcome is TaskOutcome.FAILED
        assert result.result["status"] == "failed"
        assert result.result["summary"] == "Image was not generated."
        assert result.result["error"] == "image generation tool is unavailable"
        assert result.result["retry_same_agent"] is False
    finally:
        await repo.close()


async def test_parent_notifier_queues_one_idempotent_followup(tmp_path):
    repo, session, task, activation = await _records(tmp_path)
    runtime = FakeTaskRuntime()
    notifier = TaskRuntimeParentNotifier(repository=repo, task_runtime=runtime)
    try:
        await notifier(
            session=session,
            task=task,
            activation_id=activation.activation_id,
            outcome=TaskOutcome.SUCCEEDED,
            result={"summary": "done"},
        )
        await notifier(
            session=session,
            task=task,
            activation_id=activation.activation_id,
            outcome=TaskOutcome.SUCCEEDED,
            result={"summary": "done again"},
        )
        assert runtime.steers == []
        assert len(runtime.sends) == 1
        assert runtime.sends[0][0] == "agent:main:root"
        assert "Report its status and follow the next planned task" in runtime.sends[0][1]
        payload = json.loads(runtime.sends[0][1].splitlines()[-1])
        assert payload["result"]["follow_up"].startswith("Report this step as completed")
        assert "next planned task" in payload["result"]["follow_up"]
        assert runtime.send_kwargs[0]["task_id"] == (
            f"orchestration-result:child-task:activation:{activation.activation_id}"
        )
    finally:
        await repo.close()


async def test_single_agent_background_notice_omits_full_deliverable(tmp_path):
    repo, session, task, activation = await _records(tmp_path)
    task.runtime_context["single_agent_mode"] = True
    runtime = FakeTaskRuntime()
    notifier = TaskRuntimeParentNotifier(repository=repo, task_runtime=runtime)
    try:
        await notifier(
            session=session,
            task=task,
            activation_id=activation.activation_id,
            outcome=TaskOutcome.SUCCEEDED,
            result={"summary": "done", "deliverable": "full final answer for user"},
        )

        payload = json.loads(runtime.sends[0][1].splitlines()[-1])
        assert payload["result"]["task_id"] == task.task_id
        assert payload["result"]["summary"] == "done"
        assert "deliverable" not in payload["result"]
        assert "follow_up" not in payload["result"]
        assert "retry_same_agent" not in payload["result"]
        assert "Do not delegate again for this user request" in runtime.sends[0][1]
        assert runtime.send_kwargs[0]["metadata"]["single_agent_mode"] is True
    finally:
        await repo.close()


async def test_parent_notifier_separates_board_state_agent_state_and_outcome(tmp_path):
    repo, session, task, activation = await _records(tmp_path)
    service = OrchestrationService(repo)
    await service.complete(activation.activation_id, result={"summary": "done"})
    runtime = FakeTaskRuntime()
    notifier = TaskRuntimeParentNotifier(
        repository=repo,
        task_runtime=runtime,
        agent_state_check=service.agent_state,
    )
    try:
        await notifier(
            session=session,
            task=task,
            activation_id=activation.activation_id,
            outcome=TaskOutcome.SUCCEEDED,
            result={"summary": "done"},
        )

        payload = json.loads(runtime.sends[0][1].splitlines()[-1])
        result = payload["result"]
        assert result["board_status"] == "completed"
        assert result["agent_state"] == "idle"
        assert result["outcome"] == "succeeded"
        assert "task_status" not in result
    finally:
        await repo.close()


async def test_parent_notifier_includes_child_deliverable(tmp_path):
    repo, session, task, activation = await _records(tmp_path)
    runtime = FakeTaskRuntime()
    notifier = TaskRuntimeParentNotifier(repository=repo, task_runtime=runtime)
    try:
        await notifier(
            session=session,
            task=task,
            activation_id=activation.activation_id,
            outcome=TaskOutcome.SUCCEEDED,
            result={
                "summary": "Located the boundary bug.",
                "deliverable": "JodaUtils.java:42 contains the exact failing branch.",
                "unresolved": ["Confirm the public API expectation."],
            },
        )

        payload = json.loads(runtime.sends[0][1].splitlines()[-1])
        assert payload["result"]["deliverable"] == (
            "JodaUtils.java:42 contains the exact failing branch."
        )
        assert payload["result"]["unresolved"] == [
            "Confirm the public API expectation."
        ]
        assert "evidence_sufficient" not in payload["result"]
        assert payload["result"]["follow_up"].startswith("Report this step as incomplete")
        assert 'session_id="child-session"' in payload["result"]["follow_up"]
        assert "next_action" not in payload["result"]
        assert "retry_same_agent" not in payload["result"]
    finally:
        await repo.close()


async def test_complex_root_explorer_notification_omits_detailed_deliverable(tmp_path):
    repo, session, task, activation = await _records(tmp_path)
    runtime = FakeTaskRuntime()
    notifier = TaskRuntimeParentNotifier(repository=repo, task_runtime=runtime)
    try:
        await notifier(
            session=replace(session, profile="explorer"),
            task=task,
            activation_id=activation.activation_id,
            outcome=TaskOutcome.SUCCEEDED,
            result={
                "summary": "Investigation complete; execution can proceed.",
                "deliverable": "Detailed source content stays in the child session.",
                "unresolved": [],
            },
        )

        message = runtime.sends[0][1]
        payload = json.loads(message.splitlines()[-1])
        assert "deliverable" not in payload["result"]
        assert "evidence" not in payload["result"]
        assert "acceptance_criteria" not in payload["result"]
        assert payload["result"]["summary"] == (
            "Investigation complete; execution can proceed."
        )
        assert "agent=worker" in payload["result"]["follow_up"]
        assert "after review" not in message
    finally:
        await repo.close()


async def test_parent_notifier_accepts_completed_task_with_no_unresolved_items(tmp_path):
    repo, session, task, activation = await _records(tmp_path)
    runtime = FakeTaskRuntime()
    notifier = TaskRuntimeParentNotifier(repository=repo, task_runtime=runtime)
    try:
        await notifier(
            session=session,
            task=task,
            activation_id=activation.activation_id,
            outcome=TaskOutcome.SUCCEEDED,
            result={
                "summary": "Located the smallest edit.",
                "deliverable": "Change the zero-key branch to return an empty result.",
                "unresolved": [],
            },
        )

        payload = json.loads(runtime.sends[0][1].splitlines()[-1])
        assert payload["result"]["unresolved"] == []
        assert payload["result"]["follow_up"].startswith("Report this step as completed")
        assert "next planned task" in payload["result"]["follow_up"]
    finally:
        await repo.close()


async def test_parent_notifier_clamps_same_session_retry_to_runtime_capacity(tmp_path):
    repo, session, task, activation = await _records(tmp_path)
    runtime = FakeTaskRuntime()

    async def retry_same_agent_check(*, task_id: str, session_id: str) -> bool:
        assert task_id == task.task_id
        assert session_id == session.session_id
        return False

    notifier = TaskRuntimeParentNotifier(
        repository=repo,
        task_runtime=runtime,
        retry_same_agent_check=retry_same_agent_check,
    )
    try:
        await notifier(
            session=session,
            task=task,
            activation_id=activation.activation_id,
            outcome=TaskOutcome.FAILED,
            result={
                "status": "failed",
                "summary": "blocked",
                "error": "provider failed",
                "retry_same_agent": True,
            },
        )

        payload = json.loads(runtime.sends[0][1].splitlines()[-1])
        assert payload["result"]["retry_same_agent"] is False
        assert payload["result"]["follow_up"] == (
            'Do not retry this failed task in session_id="child-session". If the result is still '
            'required, reroute by calling delegate_task with the same task_key="inspect" and '
            'replace_session_id="child-session"; otherwise stop.'
        )
    finally:
        await repo.close()


async def test_parent_notifier_names_exact_task_and_session_for_retry(tmp_path):
    repo, session, task, activation = await _records(tmp_path)
    runtime = FakeTaskRuntime()
    notifier = TaskRuntimeParentNotifier(repository=repo, task_runtime=runtime)
    try:
        await notifier(
            session=session,
            task=task,
            activation_id=activation.activation_id,
            outcome=TaskOutcome.FAILED,
            result={
                "status": "failed",
                "summary": "provider interrupted",
                "error": "response_incomplete",
                "retry_same_agent": True,
            },
        )

        payload = json.loads(runtime.sends[0][1].splitlines()[-1])
        assert payload["result"]["retry_same_agent"] is True
        assert payload["result"]["follow_up"] == (
            'If this task still needs work, you may retry it with delegate_task using '
            'task_key="inspect" and session_id="child-session", asking only for the missing '
            'acceptance criterion. Otherwise reroute with the same task_key and '
            'replace_session_id="child-session", or stop if the result is no longer needed.'
        )
    finally:
        await repo.close()


async def test_parent_notification_is_serialized_with_foreground_observation(tmp_path):
    repo, session, task, activation = await _records(tmp_path)
    service = OrchestrationService(repo)
    await service.complete(activation.activation_id, result={"summary": "done"})
    send_entered = asyncio.Event()
    release_send = asyncio.Event()

    class BlockingRuntime(FakeTaskRuntime):
        async def send(self, session_key: str, message: str, **kwargs):
            send_entered.set()
            await release_send.wait()
            result = await super().send(session_key, message, **kwargs)
            self.task_statuses[str(kwargs["task_id"])] = AgentTaskStatus.QUEUED
            return result

    runtime = BlockingRuntime()
    notifier = TaskRuntimeParentNotifier(
        repository=repo,
        task_runtime=runtime,
        serialization_lock=service.run_serialization_lock,
    )
    try:
        delivery = asyncio.create_task(
            notifier(
                session=session,
                task=task,
                activation_id=activation.activation_id,
                outcome=TaskOutcome.SUCCEEDED,
                result={"summary": "done"},
            )
        )
        await asyncio.wait_for(send_entered.wait(), timeout=1)
        observation = asyncio.create_task(
            service.observe_child_result(
                task.task_id,
                activation_id=activation.activation_id,
            )
        )
        await asyncio.sleep(0)
        assert not observation.done()

        release_send.set()
        await delivery
        await observation
        await notifier.cancel_pending_delivery(
            session=session,
            task=task,
            activation_id=activation.activation_id,
        )
        assert runtime.cancelled[0]["task_id"] == (
            f"orchestration-result:{task.task_id}:activation:{activation.activation_id}"
        )
        assert await repo.list_unprocessed_child_result_messages() == []
    finally:
        release_send.set()
        await repo.close()


async def test_parent_notifier_retries_a_failed_followup_with_stable_attempt_id(tmp_path):
    repo, session, task, activation = await _records(tmp_path)
    runtime = FakeTaskRuntime()
    failed_id = f"orchestration-result:{task.task_id}:activation:{activation.activation_id}"
    runtime.known_task_ids.add(failed_id)
    runtime.task_statuses[failed_id] = AgentTaskStatus.FAILED
    notifier = TaskRuntimeParentNotifier(repository=repo, task_runtime=runtime)
    try:
        await notifier(
            session=session,
            task=task,
            activation_id=activation.activation_id,
            outcome=TaskOutcome.SUCCEEDED,
            result={"summary": "done"},
        )
        await notifier(
            session=session,
            task=task,
            activation_id=activation.activation_id,
            outcome=TaskOutcome.SUCCEEDED,
            result={"summary": "done again"},
        )

        assert len(runtime.sends) == 1
        assert runtime.send_kwargs[0]["task_id"] == (
            f"orchestration-result:{task.task_id}:activation:{activation.activation_id}:delivery:2"
        )
    finally:
        await repo.close()


async def test_parent_notifier_uses_current_run_attachment_after_reattach(tmp_path):
    repo, session, _task, activation = await _records(tmp_path)
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    service = OrchestrationService(repo)
    await service.start_run(
        run_id="run-2",
        root_session_id="root-session-2",
        root_task_id="root-task-2",
        root_task_key="root",
        root_task="Continue",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
        root_runtime_session_key="agent:main:root",
    )
    await repo.attach_session(
        run_id="run-2",
        session_id=session.session_id,
        parent_session_id="root-session-2",
        depth=1,
    )
    task = DelegatedTaskRecord(
        task_id="child-task-2",
        run_id="run-2",
        task_key="continue",
        owner_session_id=session.session_id,
        parent_task_id="root-task-2",
        description="Continue inspection",
        background=True,
    )
    await repo.create_task(task)
    runtime = FakeTaskRuntime()
    notifier = TaskRuntimeParentNotifier(repository=repo, task_runtime=runtime)
    try:
        await notifier(
            session=session,
            task=task,
            activation_id=activation.activation_id,
            outcome=TaskOutcome.SUCCEEDED,
            result={"summary": "continued"},
        )

        assert runtime.send_kwargs[0]["metadata"]["orchestration_session_id"] == ("root-session-2")
    finally:
        await repo.close()
