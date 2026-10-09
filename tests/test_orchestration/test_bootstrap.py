from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.gateway.orchestration_routing import ChildTreeRoute
from opensquilla.gateway.orchestration_runtime import build_orchestration_runtime
from opensquilla.gateway.session_lifecycle import TaskLifecycleEvent
from opensquilla.orchestration.models import (
    AgentSessionRecord,
    DelegatedTaskRecord,
    OrchestrationMode,
    TaskOutcome,
)
from opensquilla.orchestration.repository import OrchestrationRepository
from opensquilla.orchestration.service import DelegateRequest
from opensquilla.session.models import AgentTaskStatus

pytestmark = pytest.mark.asyncio


async def test_complex_child_progress_targets_root_session_with_brief_state(tmp_path) -> None:
    repository = await OrchestrationRepository.open(tmp_path / "orchestration.db")
    events: list[tuple[str, str, dict[str, object]]] = []

    async def emit(session_key: str, name: str, payload: dict[str, object]) -> None:
        events.append((session_key, name, payload))

    runtime = build_orchestration_runtime(
        repository=repository,
        session_manager=SimpleNamespace(),
        task_runtime=SimpleNamespace(),
        config=SimpleNamespace(llm=SimpleNamespace(model="configured-model")),
        registry=SimpleNamespace(),
        event_emitter=emit,
    )
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    try:
        await runtime.service.start_run(
            run_id="run-1",
            root_session_id="root-session",
            root_task_id="root-task",
            root_task_key="root",
            root_task="Implement",
            mode=OrchestrationMode.COMPLEX,
            worker_template_tools=tools,
            registered_tools=tools,
            root_runtime_session_key="agent:main:webchat:root",
        )
        step = await runtime.service.add_task_board_item(
            run_id="run-1",
            caller_session_id="root-session",
            parent_task_id="root-task",
            task_key="inspect",
            description="Inspect issue",
            acceptance_criteria="Return cause",
        )
        await runtime.executor.publish_progress(
            run_id="run-1", task_id=step.task_id, phase="waiting"
        )

        assert len(events) == 1
        session_key, name, payload = events[0]
        assert session_key == "agent:main:webchat:root"
        assert name == "session.event.task_group.waiting"
        assert payload["group_id"] == "run-1"
        assert payload["task_id"] == "root-task"
        assert "inspect" in str(payload["message"])
        assert "Next:" in str(payload["message"])
        assert "Inspect issue" not in str(payload["message"])
    finally:
        await runtime.close()


async def test_single_agent_completion_event_refs_durable_child_result(tmp_path) -> None:
    repository = await OrchestrationRepository.open(tmp_path / "orchestration.db")
    events: list[tuple[str, str, dict[str, object]]] = []

    async def emit(session_key: str, name: str, payload: dict[str, object]) -> None:
        events.append((session_key, name, payload))

    runtime = build_orchestration_runtime(
        repository=repository,
        session_manager=SimpleNamespace(),
        task_runtime=SimpleNamespace(),
        config=SimpleNamespace(llm=SimpleNamespace(model="configured-model")),
        registry=SimpleNamespace(),
        event_emitter=emit,
    )
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    try:
        await runtime.service.start_run(
            run_id="single-run",
            root_session_id="single-root",
            root_task_id="single-task",
            root_task_key="root",
            root_task="Write the report",
            mode=OrchestrationMode.COMPLEX,
            worker_template_tools=tools,
            registered_tools=tools,
            root_runtime_session_key="agent:main:webchat:root",
            root_runtime_context={"single_agent_mode": True},
        )
        child = await runtime.service.delegate(
            DelegateRequest(
                run_id="single-run",
                parent_session_id="single-root",
                parent_task_id="single-task",
                task_key="whole-report",
                task="Write the report",
                acceptance_criteria="Complete the original request",
                inherited_tools=tools,
                registered_tools=tools,
                runtime_context={"single_agent_mode": True},
            )
        )
        await runtime.service.complete(
            child.activation.activation_id,
            result={"summary": "done", "deliverable": "The full report."},
        )
        await runtime.executor.publish_progress(
            run_id="single-run", task_id=child.task.task_id, phase="completed"
        )

        session_key, name, payload = events[-1]
        assert session_key == "agent:main:webchat:root"
        assert name == "session.event.task_group.waiting"
        assert payload["result_task_id"] == child.task.task_id
        assert "The full report." not in str(payload)
    finally:
        await runtime.close()


async def test_bootstrap_routes_new_child_once_through_existing_tree_adapter(
    tmp_path,
    monkeypatch,
) -> None:
    repository = await OrchestrationRepository.open(tmp_path / "orchestration.db")
    calls: list[dict[str, object]] = []

    async def route(task: str, **kwargs):
        calls.append({"task": task, **kwargs})
        return ChildTreeRoute(model="tree-model")

    monkeypatch.setattr(
        "opensquilla.gateway.orchestration_runtime.select_child_tree_route",
        route,
    )
    runtime = build_orchestration_runtime(
        repository=repository,
        session_manager=SimpleNamespace(),
        task_runtime=SimpleNamespace(),
        config=SimpleNamespace(llm=SimpleNamespace(model="configured-model")),
        registry=SimpleNamespace(),
    )
    session = AgentSessionRecord(
        session_id="child-session",
        run_id="run-1",
        profile="inherit",
        runtime_session_key="agent:main:subagent:child",
        runtime_context={"active_model": "parent-model"},
    )
    task = DelegatedTaskRecord(
        task_id="task-1",
        run_id="run-1",
        task_key="inspect",
        owner_session_id=session.session_id,
        description="Inspect runtime",
        runtime_context={"active_model": "parent-model"},
    )
    try:
        selected = await runtime.executor.route_child(task, session)
        assert selected == ChildTreeRoute(model="tree-model")
        assert calls == [
            {
                "task": "Inspect runtime",
                "session_key": "agent:main:subagent:child",
                "baseline_model": "parent-model",
                "config": runtime.config,
            }
        ]
    finally:
        await runtime.close()


async def test_single_task_child_routes_on_original_user_request(
    tmp_path,
    monkeypatch,
) -> None:
    repository = await OrchestrationRepository.open(tmp_path / "orchestration.db")
    calls: list[str] = []

    async def route(task: str, **kwargs):
        del kwargs
        calls.append(task)
        return ChildTreeRoute(model="tree-model")

    monkeypatch.setattr(
        "opensquilla.gateway.orchestration_runtime.select_child_tree_route",
        route,
    )
    runtime = build_orchestration_runtime(
        repository=repository,
        session_manager=SimpleNamespace(),
        task_runtime=SimpleNamespace(),
        config=SimpleNamespace(llm=SimpleNamespace(model="configured-model")),
        registry=SimpleNamespace(),
    )
    tools = frozenset({"delegate_task", "read_file"})
    original_request = "Analyze prices.csv and write a report.md with monthly returns."
    try:
        await runtime.service.start_run(
            run_id="single-run",
            root_session_id="root-session",
            root_task_id="root-task",
            root_task_key="root",
            root_task=original_request,
            mode=OrchestrationMode.COMPLEX,
            worker_template_tools=tools,
            registered_tools=tools,
            root_runtime_context={"single_agent_mode": True},
        )
        child = await runtime.service.delegate(
            DelegateRequest(
                run_id="single-run",
                parent_session_id="root-session",
                parent_task_id="root-task",
                task_key="report",
                task="Inspect every row, calculate every statistic, then write a full report.",
                acceptance_criteria="Write the report.",
                inherited_tools=tools,
                registered_tools=tools,
                runtime_context={"single_agent_mode": True},
            )
        )

        selected = await runtime.executor.route_child(child.task, child.session)

        assert selected == ChildTreeRoute(model="tree-model")
        assert calls == [original_request]
    finally:
        await runtime.close()


async def test_bootstrap_applies_configured_delegation_depth(tmp_path) -> None:
    repository = await OrchestrationRepository.open(tmp_path / "orchestration.db")
    runtime = build_orchestration_runtime(
        repository=repository,
        session_manager=SimpleNamespace(),
        task_runtime=SimpleNamespace(),
        config=SimpleNamespace(
            llm=SimpleNamespace(model="configured-model"),
            subagents=SimpleNamespace(max_spawn_depth=5, max_task_attempts=4),
        ),
        registry=SimpleNamespace(),
    )
    try:
        assert runtime.service.max_delegation_depth == 5
        assert runtime.service.max_direct_children == 8
        assert runtime.service.max_task_attempts == 4
    finally:
        await runtime.close()


async def test_archive_uses_configured_retention_minutes(tmp_path, monkeypatch) -> None:
    repository = await OrchestrationRepository.open(tmp_path / "orchestration.db")
    runtime = build_orchestration_runtime(
        repository=repository,
        session_manager=SimpleNamespace(),
        task_runtime=SimpleNamespace(),
        config=SimpleNamespace(
            llm=SimpleNamespace(model="configured-model"),
            subagents=SimpleNamespace(archive_after_minutes=7),
        ),
        registry=SimpleNamespace(),
    )
    cutoffs: list[int] = []

    async def capture_archive(*, cutoff: int) -> list[str]:
        cutoffs.append(cutoff)
        return []

    monkeypatch.setattr(repository, "archive_eligible_runs", capture_archive)
    monkeypatch.setattr(
        "opensquilla.gateway.orchestration_runtime.time.time",
        lambda: 1_000.0,
    )
    try:
        await runtime._archive_once()
        assert cutoffs == [580_000]
    finally:
        await runtime.close()


async def test_zero_archive_retention_disables_auto_archive(tmp_path, monkeypatch) -> None:
    repository = await OrchestrationRepository.open(tmp_path / "orchestration.db")
    runtime = build_orchestration_runtime(
        repository=repository,
        session_manager=SimpleNamespace(),
        task_runtime=SimpleNamespace(),
        config=SimpleNamespace(
            llm=SimpleNamespace(model="configured-model"),
            subagents=SimpleNamespace(archive_after_minutes=0),
        ),
        registry=SimpleNamespace(),
    )
    called = False

    async def capture_archive(*, cutoff: int) -> list[str]:
        del cutoff
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(repository, "archive_eligible_runs", capture_archive)
    try:
        await runtime._archive_once()
        assert called is False
    finally:
        await runtime.close()


async def test_startup_redelivers_durable_result_from_released_activation(tmp_path) -> None:
    now = [1_000]
    repository = await OrchestrationRepository.open(
        tmp_path / "orchestration.db",
        clock=lambda: now[0],
    )
    sent: list[tuple[str, str, dict[str, object], dict[str, object]]] = []

    class ParentRuntime:
        async def status(self, task_id: str):
            raise KeyError(task_id)

        async def steer(self, session_key: str, message: str, **kwargs):
            return None

        async def send(
            self,
            session_key: str,
            message: str,
            *,
            provenance,
            metadata,
            task_id: str,
        ):
            sent.append((session_key, message, provenance, metadata))

    runtime = build_orchestration_runtime(
        repository=repository,
        session_manager=SimpleNamespace(),
        task_runtime=ParentRuntime(),
        config=SimpleNamespace(llm=SimpleNamespace(model="configured-model")),
        registry=SimpleNamespace(),
    )
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await runtime.service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
        root_runtime_session_key="agent:main:webchat:root",
    )
    child = await runtime.service.delegate(
        DelegateRequest(
            run_id="run-1",
            parent_session_id="root-session",
            parent_task_id="root-task",
            task_key="unfinished-child",
            task="Keep working",
            acceptance_criteria="Return the completed work with evidence",
            inherited_tools=tools,
            registered_tools=tools,
            background=True,
        )
    )
    child = await runtime.service.delegate(
        DelegateRequest(
            run_id="run-1",
            parent_session_id="root-session",
            parent_task_id="root-task",
            task_key="inspect",
            task="Inspect runtime",
            acceptance_criteria="Return the runtime owner with evidence",
            inherited_tools=tools,
            registered_tools=tools,
            background=True,
        )
    )
    claimed = await repository.claim_activation(child.activation.activation_id)
    assert claimed is not None
    now[0] = 3_000

    try:
        await runtime.start()

        task = await repository.get_task(child.task.task_id)
        assert task is not None and task.outcome is TaskOutcome.INTERRUPTED
        assert len(sent) == 1
        assert sent[0][0] == "agent:main:webchat:root"
        assert "gateway_restarted" in sent[0][1]
        assert sent[0][3]["orchestration_run_id"] == "run-1"
        assert sent[0][3]["orchestration_session_id"] == "root-session"
        pending = await repository.list_pending_messages("root-session")
        assert len(pending) == 1
        assert pending[0].kind == "child_result"
    finally:
        await runtime.close()


async def test_run_completes_only_after_terminal_parent_synthesis(tmp_path) -> None:
    repository = await OrchestrationRepository.open(tmp_path / "orchestration.db")
    runtime = build_orchestration_runtime(
        repository=repository,
        session_manager=SimpleNamespace(),
        task_runtime=SimpleNamespace(),
        config=SimpleNamespace(llm=SimpleNamespace(model="configured-model")),
        registry=SimpleNamespace(),
    )
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await runtime.service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
        root_runtime_session_key="agent:main:webchat:root",
    )
    child = await runtime.service.delegate(
        DelegateRequest(
            run_id="run-1",
            parent_session_id="root-session",
            parent_task_id="root-task",
            task_key="inspect",
            task="Inspect runtime",
            acceptance_criteria="Return the runtime owner with evidence",
            inherited_tools=tools,
            registered_tools=tools,
            background=True,
        )
    )
    root_terminal = TaskLifecycleEvent(
        phase="terminal",
        session_key="agent:main:webchat:root",
        task_id="root-task",
        task_status=AgentTaskStatus.SUCCEEDED,
        run_kind="web_turn",
        orchestration_run_id="run-1",
    )

    try:
        await runtime.on_task_lifecycle(root_terminal)
        pending_run = await repository.get_run("run-1")
        assert pending_run is not None
        assert pending_run.final_synthesis_completed is False

        await runtime.service.complete(
            child.activation.activation_id,
            result={"summary": "inspected"},
        )
        await runtime.service.acknowledge_result_delivery(child.task.task_id)
        await runtime.on_task_lifecycle(
            TaskLifecycleEvent(
                phase="terminal",
                session_key="agent:main:webchat:root",
                task_id="unrelated-success",
                task_status=AgentTaskStatus.SUCCEEDED,
                run_kind="runtime_send",
                orchestration_run_id="run-1",
            )
        )
        not_synthesized = await repository.get_run("run-1")
        assert not_synthesized is not None
        assert not_synthesized.final_synthesis_completed is False
        await runtime.on_task_lifecycle(
            TaskLifecycleEvent(
                phase="terminal",
                session_key="agent:main:webchat:root",
                task_id=f"orchestration-result:{child.task.task_id}",
                task_status=AgentTaskStatus.SUCCEEDED,
                run_kind="runtime_send",
                orchestration_run_id="run-1",
            )
        )

        completed_run = await repository.get_run("run-1")
        assert completed_run is not None
        assert completed_run.final_synthesis_completed is True
        assert completed_run.lifecycle.value == "completed"
    finally:
        await runtime.close()


async def test_failed_root_task_terminates_run_without_final_synthesis(tmp_path) -> None:
    repository = await OrchestrationRepository.open(tmp_path / "orchestration.db")
    runtime = build_orchestration_runtime(
        repository=repository,
        session_manager=SimpleNamespace(),
        task_runtime=SimpleNamespace(),
        config=SimpleNamespace(llm=SimpleNamespace(model="configured-model")),
        registry=SimpleNamespace(),
    )
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await runtime.service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
        root_runtime_session_key="agent:main:webchat:root",
    )
    child = await runtime.service.delegate(
        DelegateRequest(
            run_id="run-1",
            parent_session_id="root-session",
            parent_task_id="root-task",
            task_key="unfinished-child",
            task="Keep working",
            acceptance_criteria="Return the completed work with evidence",
            inherited_tools=tools,
            registered_tools=tools,
            background=True,
        )
    )

    try:
        await runtime.on_task_lifecycle(
            TaskLifecycleEvent(
                phase="terminal",
                session_key="agent:main:webchat:root",
                task_id="root-task",
                task_status=AgentTaskStatus.FAILED,
                run_kind="web_turn",
                orchestration_run_id="run-1",
            )
        )

        run = await repository.get_run("run-1")
        root_task = await repository.get_task("root-task")
        child_task = await repository.get_task(child.task.task_id)
        child_activation = await repository.get_activation(child.activation.activation_id)
        assert run is not None
        assert run.lifecycle.value == "completed"
        assert run.final_synthesis_completed is False
        assert root_task is not None
        assert root_task.outcome is TaskOutcome.FAILED
        assert root_task.result == {
            "reason": "root_task_failed",
            "task_status": "failed",
        }
        assert child_task is not None
        assert child_task.outcome is TaskOutcome.INTERRUPTED
        assert child_activation is not None
        assert child_activation.phase.value == "released"
    finally:
        await runtime.close()


async def test_run_waits_for_every_background_result_followup(tmp_path) -> None:
    repository = await OrchestrationRepository.open(tmp_path / "orchestration.db")
    runtime = build_orchestration_runtime(
        repository=repository,
        session_manager=SimpleNamespace(),
        task_runtime=SimpleNamespace(),
        config=SimpleNamespace(llm=SimpleNamespace(model="configured-model")),
        registry=SimpleNamespace(),
    )
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await runtime.service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
    )
    children = [
        await runtime.service.delegate(
            DelegateRequest(
                run_id="run-1",
                parent_session_id="root-session",
                parent_task_id="root-task",
                task_key=f"child-{index}",
                task=f"Child {index}",
                acceptance_criteria=f"Return the complete child {index} result with evidence",
                inherited_tools=tools,
                registered_tools=tools,
                background=True,
            )
        )
        for index in range(2)
    ]
    try:
        for child in children:
            await runtime.service.complete(
                child.activation.activation_id,
                result={"summary": child.task.task_key},
            )
            await runtime.service.acknowledge_result_delivery(child.task.task_id)

        await runtime.on_task_lifecycle(
            TaskLifecycleEvent(
                phase="terminal",
                session_key="root-session",
                task_id=f"orchestration-result:{children[0].task.task_id}",
                task_status=AgentTaskStatus.SUCCEEDED,
                run_kind="runtime_send",
                orchestration_run_id="run-1",
            )
        )
        run = await repository.get_run("run-1")
        assert run is not None and run.final_synthesis_completed is False

        await runtime.on_task_lifecycle(
            TaskLifecycleEvent(
                phase="terminal",
                session_key="root-session",
                task_id=f"orchestration-result:{children[1].task.task_id}",
                task_status=AgentTaskStatus.SUCCEEDED,
                run_kind="runtime_send",
                orchestration_run_id="run-1",
            )
        )
        run = await repository.get_run("run-1")
        assert run is not None and run.final_synthesis_completed is True
    finally:
        await runtime.close()


async def test_retried_notification_completion_marks_original_child_result_processed(
    tmp_path,
) -> None:
    repository = await OrchestrationRepository.open(tmp_path / "orchestration.db")
    runtime = build_orchestration_runtime(
        repository=repository,
        session_manager=SimpleNamespace(),
        task_runtime=SimpleNamespace(),
        config=SimpleNamespace(llm=SimpleNamespace(model="configured-model")),
        registry=SimpleNamespace(),
    )
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await runtime.service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
    )
    child = await runtime.service.delegate(
        DelegateRequest(
            run_id="run-1",
            parent_session_id="root-session",
            parent_task_id="root-task",
            task_key="inspect",
            task="Inspect runtime",
            acceptance_criteria="Return the runtime owner with evidence",
            inherited_tools=tools,
            registered_tools=tools,
            background=True,
        )
    )
    try:
        await runtime.service.complete(
            child.activation.activation_id,
            result={"summary": "done"},
        )
        await runtime.service.acknowledge_result_delivery(child.task.task_id)
        await runtime.on_task_lifecycle(
            TaskLifecycleEvent(
                phase="terminal",
                session_key="root-session",
                task_id=f"orchestration-result:{child.task.task_id}:delivery:2",
                task_status=AgentTaskStatus.SUCCEEDED,
                run_kind="runtime_send",
                orchestration_run_id="run-1",
            )
        )

        run = await repository.get_run("run-1")
        assert run is not None and run.final_synthesis_completed is True
    finally:
        await runtime.close()


async def test_failed_notification_requeues_child_result_for_delivery(tmp_path) -> None:
    repository = await OrchestrationRepository.open(tmp_path / "orchestration.db")
    runtime = build_orchestration_runtime(
        repository=repository,
        session_manager=SimpleNamespace(),
        task_runtime=SimpleNamespace(),
        config=SimpleNamespace(llm=SimpleNamespace(model="configured-model")),
        registry=SimpleNamespace(),
    )
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await runtime.service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
    )
    child = await runtime.service.delegate(
        DelegateRequest(
            run_id="run-1",
            parent_session_id="root-session",
            parent_task_id="root-task",
            task_key="inspect",
            task="Inspect runtime",
            acceptance_criteria="Return the runtime owner with evidence",
            inherited_tools=tools,
            registered_tools=tools,
            background=True,
        )
    )
    try:
        await runtime.service.complete(
            child.activation.activation_id,
            result={"summary": "done"},
        )
        await runtime.service.acknowledge_result_delivery(child.task.task_id)

        await runtime.on_task_lifecycle(
            TaskLifecycleEvent(
                phase="terminal",
                session_key="root-session",
                task_id=f"orchestration-result:{child.task.task_id}",
                task_status=AgentTaskStatus.FAILED,
                run_kind="runtime_send",
                orchestration_run_id="run-1",
            )
        )

        pending = await repository.list_pending_child_result_messages()
        assert [message.payload["task_id"] for message in pending] == [child.task.task_id]
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    ("runtime_status", "expect_pending", "expect_processed"),
    [
        (None, True, False),
        (AgentTaskStatus.RUNNING, False, False),
        (AgentTaskStatus.SUCCEEDED, False, True),
    ],
)
async def test_startup_reconciles_accepted_unprocessed_result_delivery(
    tmp_path,
    runtime_status,
    expect_pending,
    expect_processed,
) -> None:
    repository = await OrchestrationRepository.open(tmp_path / "orchestration.db")

    class RestartTaskRuntime:
        async def status(self, task_id: str):
            if runtime_status is None:
                raise KeyError(task_id)
            return SimpleNamespace(status=runtime_status)

    runtime = build_orchestration_runtime(
        repository=repository,
        session_manager=SimpleNamespace(),
        task_runtime=RestartTaskRuntime(),
        config=SimpleNamespace(llm=SimpleNamespace(model="configured-model")),
        registry=SimpleNamespace(),
    )
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await runtime.service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
    )
    child = await runtime.service.delegate(
        DelegateRequest(
            run_id="run-1",
            parent_session_id="root-session",
            parent_task_id="root-task",
            task_key="inspect",
            task="Inspect runtime",
            acceptance_criteria="Return the runtime owner with evidence",
            inherited_tools=tools,
            registered_tools=tools,
            background=True,
        )
    )
    await runtime.service.complete(
        child.activation.activation_id,
        result={"summary": "done"},
    )
    await repository.acknowledge_child_result_message(
        child.task.task_id,
        activation_id=child.activation.activation_id,
    )

    try:
        await runtime._reconcile_result_deliveries_once()
        pending = await repository.list_pending_child_result_messages()
        unprocessed = await repository.list_unprocessed_child_result_messages()
        assert bool(pending) is expect_pending
        assert (not unprocessed) is expect_processed
    finally:
        await runtime.close()


async def test_result_continuation_is_settled_before_final_synthesis(tmp_path) -> None:
    repository = await OrchestrationRepository.open(tmp_path / "orchestration.db")
    runtime = build_orchestration_runtime(
        repository=repository,
        session_manager=SimpleNamespace(),
        task_runtime=SimpleNamespace(),
        config=SimpleNamespace(llm=SimpleNamespace(model="configured-model")),
        registry=SimpleNamespace(),
    )
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await runtime.service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
    )
    child = await runtime.service.delegate(
        DelegateRequest(
            run_id="run-1",
            parent_session_id="root-session",
            parent_task_id="root-task",
            task_key="inspect",
            task="Inspect runtime",
            acceptance_criteria="Return the runtime owner with evidence",
            inherited_tools=tools,
            registered_tools=tools,
            background=True,
        )
    )
    await runtime.service.complete(child.activation.activation_id, result={"summary": "done"})
    try:
        await runtime.on_task_lifecycle(
            TaskLifecycleEvent(
                phase="terminal",
                session_key="root-session",
                task_id=(
                    f"orchestration-result:{child.task.task_id}:activation:"
                    f"{child.activation.activation_id}"
                ),
                task_status=AgentTaskStatus.SUCCEEDED,
                run_kind="runtime_send",
                orchestration_run_id="run-1",
                continuation_task_id="next-turn",
            )
        )
        assert await repository.list_unprocessed_child_result_messages() == []
        run = await repository.get_run("run-1")
        assert run is not None and run.final_synthesis_completed is True
    finally:
        await runtime.close()


async def test_root_continuation_defers_final_synthesis(tmp_path) -> None:
    repository = await OrchestrationRepository.open(tmp_path / "orchestration.db")
    runtime = build_orchestration_runtime(
        repository=repository,
        session_manager=SimpleNamespace(),
        task_runtime=SimpleNamespace(),
        config=SimpleNamespace(llm=SimpleNamespace(model="configured-model")),
        registry=SimpleNamespace(),
    )
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await runtime.service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
    )
    try:
        await runtime.on_task_lifecycle(
            TaskLifecycleEvent(
                phase="terminal",
                session_key="root-session",
                task_id="root-task",
                task_status=AgentTaskStatus.SUCCEEDED,
                run_kind="web_turn",
                orchestration_run_id="run-1",
                continuation_task_id="next-root-turn",
            )
        )
        run = await repository.get_run("run-1")
        assert run is not None and run.final_synthesis_completed is False
    finally:
        await runtime.close()
