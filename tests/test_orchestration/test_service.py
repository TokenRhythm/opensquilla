from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from opensquilla.orchestration.models import (
    ActivationPhase,
    OrchestrationMode,
    OrchestrationRunRecord,
    SessionLifecycle,
    TaskBoardStatus,
    TaskOutcome,
)
from opensquilla.orchestration.profiles import get_profile
from opensquilla.orchestration.repository import OrchestrationRepository
from opensquilla.orchestration.service import (
    DelegateDisposition,
    DelegateRequest,
    DelegationConflictError,
    DelegationCycleError,
    DelegationDepthError,
    DelegationPermissionError,
    InterruptFenceError,
    OrchestrationService,
)
from opensquilla.orchestration.session_recall import SessionRecallMatch

pytestmark = pytest.mark.asyncio


def _id_factory() -> Callable[[str], str]:
    counters: dict[str, int] = {}

    def allocate(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}-{counters[prefix]}"

    return allocate


@pytest.fixture
async def service(tmp_path):
    repo = await OrchestrationRepository.open(
        tmp_path / "orchestration.db",
        clock=lambda: 1_000,
        id_factory=_id_factory(),
    )
    service = OrchestrationService(repo)
    await service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement the requested change",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=frozenset(
            {
                "read_file",
                "grep_search",
                "exec_command",
                "apply_patch",
                "delegate_task",
                "interrupt_agent",
            }
        ),
        registered_tools=frozenset(
            {
                "read_file",
                "grep_search",
                "exec_command",
                "apply_patch",
                "delegate_task",
                "interrupt_agent",
            }
        ),
        root_runtime_context={"run_mode": "safe", "active_model": "parent-model"},
    )
    try:
        yield service
    finally:
        await repo.close()


def _request(task_key: str, *, parent_task_id: str = "root-task") -> DelegateRequest:
    return DelegateRequest(
        run_id="run-1",
        parent_session_id="root-session",
        parent_task_id=parent_task_id,
        task_key=task_key,
        task=f"Perform {task_key}",
        acceptance_criteria=f"Return the complete {task_key} result with evidence",
        profile="inherit",
        inherited_tools=frozenset(
            {
                "read_file",
                "grep_search",
                "exec_command",
                "apply_patch",
                "delegate_task",
                "interrupt_agent",
            }
        ),
        registered_tools=frozenset(
            {
                "read_file",
                "grep_search",
                "exec_command",
                "apply_patch",
                "delegate_task",
                "interrupt_agent",
            }
        ),
        runtime_context={"run_mode": "safe", "active_model": "child-model"},
    )


async def test_new_child_persists_the_frozen_runtime_context(service) -> None:
    child = await service.delegate(_request("inherit-runtime"))

    assert child.session.runtime_context == {
        "run_mode": "safe",
        "active_model": "child-model",
    }
    assert child.task.acceptance_criteria == (
        "Return the complete inherit-runtime result with evidence"
    )


async def test_delegate_rejects_missing_acceptance_criteria(service) -> None:
    request = replace(_request("missing-criteria"), acceptance_criteria="   ")

    with pytest.raises(ValueError, match="acceptance criteria must not be empty"):
        await service.delegate(request)


async def test_start_run_rejects_unknown_worker_tool_without_partial_records(tmp_path) -> None:
    repo = await OrchestrationRepository.open(
        tmp_path / "orchestration.db",
        clock=lambda: 1_000,
        id_factory=_id_factory(),
    )
    service = OrchestrationService(repo)
    try:
        with pytest.raises(ValueError, match="unregistered tools"):
            await service.start_run(
                run_id="bad-run",
                root_session_id="bad-root",
                root_task_id="bad-task",
                root_task_key="root",
                root_task="Do work",
                mode=OrchestrationMode.COMPLEX,
                worker_template_tools=frozenset({"missing_tool"}),
                registered_tools=frozenset(),
            )

        assert await repo.get_run("bad-run") is None
        assert await repo.get_session("bad-root") is None
        assert await repo.get_task("bad-task") is None
    finally:
        await repo.close()


async def test_ensure_run_repairs_legacy_partial_root_bundle(tmp_path) -> None:
    repo = await OrchestrationRepository.open(
        tmp_path / "orchestration.db",
        clock=lambda: 1_000,
        id_factory=_id_factory(),
    )
    service = OrchestrationService(repo)
    tools = frozenset({"read_file"})
    await repo.create_run(
        OrchestrationRunRecord(
            run_id="partial-run",
            root_session_id="partial-root",
            root_task_id="partial-task",
            mode=OrchestrationMode.COMPLEX,
            worker_template_tools=tools,
        )
    )
    try:
        await service.ensure_run(
            run_id="partial-run",
            root_session_id="partial-root",
            root_task_id="partial-task",
            root_task_key="root",
            root_task="Repair root",
            mode=OrchestrationMode.COMPLEX,
            worker_template_tools=tools,
            registered_tools=tools,
        )

        assert await repo.get_session("partial-root") is not None
        assert await repo.get_task("partial-task") is not None
    finally:
        await repo.close()


async def test_ninth_direct_child_queues_without_consuming_a_slot(service) -> None:
    children = [await service.delegate(_request(f"task-{index}")) for index in range(9)]

    assert [child.activation.phase for child in children[:8]] == [ActivationPhase.STARTING] * 8
    assert children[8].activation.phase is ActivationPhase.QUEUED
    assert await service.repository.live_direct_child_count("root-session") == 8


async def test_active_task_key_attaches_without_creating_another_session(service) -> None:
    first = await service.delegate(_request("inspect-runtime"))
    second = await service.delegate(_request("inspect-runtime"))

    assert second.disposition is DelegateDisposition.ATTACHED
    assert second.task.task_id == first.task.task_id
    assert second.session.session_id == first.session.session_id
    assert second.activation.activation_id == first.activation.activation_id


async def test_single_mode_allows_distinct_tasks_and_attaches_same(service) -> None:
    current = await service.repository.get_run("run-1")
    assert current is not None
    await service.start_run(
        run_id="single-run",
        root_session_id="single-root",
        root_task_id="single-task",
        root_task_key="root",
        root_task="Update the report and migrate the database",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=current.worker_template_tools,
        registered_tools=current.worker_template_tools,
        root_runtime_context={"single_agent_mode": True},
    )
    first_request = replace(
        _request("report-task"),
        run_id="single-run",
        parent_session_id="single-root",
        parent_task_id="single-task",
    )
    first = await service.delegate(first_request)
    attached = await service.delegate(first_request)

    assert attached.disposition is DelegateDisposition.ATTACHED
    assert attached.session.session_id == first.session.session_id
    second = await service.delegate(
        replace(
            first_request,
            task_key="database-migration",
            task="Migrate the database schema and verify the migration",
            acceptance_criteria="Return the applied migration and verification result",
        )
    )

    assert second.disposition is DelegateDisposition.CREATED
    assert second.task.task_id != first.task.task_id
    assert second.session.session_id != first.session.session_id
    assert second.task.description == "Migrate the database schema and verify the migration"


async def test_single_mode_rejects_new_delegation_after_terminal_child(service) -> None:
    current = await service.repository.get_run("run-1")
    assert current is not None
    await service.start_run(
        run_id="one-shot-run",
        root_session_id="one-shot-root",
        root_task_id="one-shot-task",
        root_task_key="root",
        root_task="Write the report",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=current.worker_template_tools,
        registered_tools=current.worker_template_tools,
        root_runtime_session_key="persistent-parent",
        root_runtime_context={"single_agent_mode": True},
    )
    request = replace(
        _request("report"),
        run_id="one-shot-run",
        parent_session_id="one-shot-root",
        parent_task_id="one-shot-task",
    )
    first = await service.delegate(request)
    await service.complete(first.activation.activation_id, result={"summary": "done"})

    with pytest.raises(DelegationConflictError, match="new user query"):
        await service.delegate(request)
    with pytest.raises(DelegationConflictError, match="new user query"):
        await service.delegate(replace(request, task_key="report-again"))
    with pytest.raises(DelegationConflictError, match="new user query"):
        await service.delegate(
            replace(
                request,
                task_key="report-replacement",
                replace_session_id=first.session.session_id,
            )
        )

    await service.start_run(
        run_id="next-query-run",
        root_session_id="next-query-root",
        root_task_id="next-query-task",
        root_task_key="root",
        root_task="Revise the report",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=current.worker_template_tools,
        registered_tools=current.worker_template_tools,
        root_runtime_session_key="persistent-parent",
        root_runtime_context={"single_agent_mode": True},
    )
    continued = await service.delegate(
        replace(
            request,
            run_id="next-query-run",
            parent_session_id="next-query-root",
            parent_task_id="next-query-task",
            task_key="revise-report",
            task="Revise the report with the user's new requirement",
            acceptance_criteria="Return the revised report",
            session_id=first.session.session_id,
        )
    )
    assert continued.session.session_id == first.session.session_id
    assert continued.task.run_id == "next-query-run"


async def test_completed_task_key_reuses_result(service) -> None:
    original = await service.delegate(_request("inspect-runtime"))
    await service.complete(
        original.activation.activation_id,
        result={"summary": "done"},
    )

    reused = await service.delegate(_request("inspect-runtime"))

    assert reused.disposition is DelegateDisposition.REUSED
    assert reused.result == {"summary": "done"}


async def test_complex_root_no_longer_forces_one_investigation_or_execution_lane(service) -> None:
    explorer_tools = get_profile("explorer").allow or frozenset()

    def explorer_request(task_key: str) -> DelegateRequest:
        request = _request(task_key)
        return replace(
            request,
            profile="explorer",
            registered_tools=request.registered_tools | explorer_tools,
        )

    first = await service.delegate(explorer_request("investigate-root-cause"))
    second = await service.delegate(explorer_request("inspect-independent-component"))
    worker = await service.delegate(_request("implement-fix"))
    another_worker = await service.delegate(_request("implement-independent-fix"))

    assert first.session.session_id != second.session.session_id
    assert worker.session.session_id != first.session.session_id
    assert worker.session.session_id != another_worker.session.session_id


async def test_automatic_recall_restores_a_similar_same_profile_session(service) -> None:
    first = await service.delegate(_request("inspect-redis-timeout"))
    await service.complete(first.activation.activation_id, result={"summary": "done"})

    class RecallStub:
        calls = 0

        async def find_reusable_session(self, repository, **kwargs):
            del repository
            self.calls += 1
            assert kwargs["profile"] == "inherit"
            return SessionRecallMatch(
                session_id=first.session.session_id,
                score=0.91,
                task_id=first.task.task_id,
            )

    recall = RecallStub()
    service.session_recall = recall
    restored = await service.delegate(
        replace(
            _request("inspect-redis-timeout-follow-up"),
            task="Inspect the Redis timeout call path",
        )
    )

    assert restored.disposition is DelegateDisposition.RESTORED
    assert restored.session.session_id == first.session.session_id
    assert recall.calls == 1


async def test_single_agent_recall_does_not_use_paths(service) -> None:
    first = await service.delegate(_request("first-query"))
    await service.complete(first.activation.activation_id, result={"summary": "done"})

    class RecallStub:
        async def find_reusable_session(self, repository, **kwargs):
            del repository
            assert kwargs["ignore_paths"] is True
            return None

    service.session_recall = RecallStub()
    await service.delegate(
        replace(
            _request("next-query"),
            runtime_context={"single_agent_mode": True},
        )
    )


async def test_explicit_session_bypasses_automatic_recall(service) -> None:
    first = await service.delegate(_request("inspect-runtime"))
    await service.complete(first.activation.activation_id, result={"summary": "done"})

    class RecallMustNotRun:
        async def find_reusable_session(self, repository, **kwargs):
            del repository, kwargs
            raise AssertionError("automatic recall must not run for explicit session_id")

    service.session_recall = RecallMustNotRun()
    continued = await service.delegate(
        replace(_request("inspect-more"), session_id=first.session.session_id)
    )

    assert continued.session.session_id == first.session.session_id
    assert continued.disposition is DelegateDisposition.RESTORED


async def test_agent_profile_is_a_fixed_choice(service) -> None:
    with pytest.raises(ValueError, match="Unknown agent profile"):
        await service.delegate(replace(_request("inspect-runtime"), profile="custom-agent"))


async def test_completed_explorer_session_can_be_promoted_to_worker(service) -> None:
    explorer_tools = get_profile("explorer").allow or frozenset()
    worker_tools = get_profile("worker").allow or frozenset()
    registered = _request("inspect-runtime").registered_tools | explorer_tools | worker_tools
    explored = await service.delegate(
        replace(
            _request("inspect-runtime"),
            profile="explorer",
            registered_tools=registered,
        )
    )
    await service.complete(explored.activation.activation_id, result={"summary": "root cause"})

    promoted = await service.delegate(
        replace(
            _request("implement-fix"),
            profile="worker",
            session_id=explored.session.session_id,
            registered_tools=registered,
        )
    )
    persisted = await service.repository.get_session(explored.session.session_id)

    assert promoted.disposition is DelegateDisposition.RESTORED
    assert promoted.session.session_id == explored.session.session_id
    assert promoted.session.profile == "worker"
    assert {"read_file", "exec_command", "apply_patch"} <= promoted.task.effective_tools
    assert persisted is not None
    assert persisted.profile == "worker"
    assert persisted.effective_tools == promoted.task.effective_tools


async def test_continuing_session_without_profile_preserves_explorer(service) -> None:
    explorer_tools = get_profile("explorer").allow or frozenset()
    registered = _request("inspect-runtime").registered_tools | explorer_tools
    explored = await service.delegate(
        replace(
            _request("inspect-runtime"),
            profile="explorer",
            registered_tools=registered,
        )
    )
    await service.complete(explored.activation.activation_id, result={"summary": "root cause"})

    continued = await service.delegate(
        replace(
            _request("clarify-root-cause"),
            profile=None,
            session_id=explored.session.session_id,
            registered_tools=registered,
        )
    )

    assert continued.session.profile == "explorer"
    assert "apply_patch" not in continued.task.effective_tools


async def test_planned_board_item_becomes_the_same_delegated_task(service) -> None:
    planned = await service.add_task_board_item(
        run_id="run-1",
        caller_session_id="root-session",
        parent_task_id="root-task",
        task_key="inspect-runtime",
        description="Inspect the runtime",
        acceptance_criteria="Identify the responsible code path",
    )

    delegated = await service.delegate(
        replace(
            _request("inspect-runtime"),
            acceptance_criteria="Identify the responsible code path",
        )
    )
    persisted = await service.repository.get_task(planned.task_id)

    assert delegated.task.task_id == planned.task_id
    assert persisted is not None and persisted.board_only is False
    assert persisted.acceptance_criteria == "Identify the responsible code path"


async def test_started_pending_board_item_can_still_be_delegated(service) -> None:
    planned = await service.add_task_board_item(
        run_id="run-1",
        caller_session_id="root-session",
        parent_task_id="root-task",
        task_key="implement-fix",
        description="Implement the fix",
        acceptance_criteria="Apply the smallest source change",
    )
    started = await service.update_task_board_item(
        run_id="run-1",
        caller_session_id="root-session",
        task_key="implement-fix",
        action="start",
    )

    delegated = await service.delegate(
        replace(
            _request("implement-fix"),
            acceptance_criteria="Apply the smallest source change",
        )
    )
    persisted = await service.repository.get_task(planned.task_id)

    assert started.board_status is TaskBoardStatus.WORKING
    assert delegated.task.task_id == planned.task_id
    assert delegated.activation.phase is ActivationPhase.STARTING
    assert persisted is not None and persisted.board_only is False
    assert persisted.board_status is TaskBoardStatus.WORKING


async def test_planned_board_item_accepts_refined_criteria_before_assignment(service) -> None:
    planned = await service.add_task_board_item(
        run_id="run-1",
        caller_session_id="root-session",
        parent_task_id="root-task",
        task_key="inspect-runtime",
        description="Inspect the runtime",
        acceptance_criteria="Identify the responsible code path",
    )

    delegated = await service.delegate(_request("inspect-runtime"))
    persisted = await service.repository.get_task(planned.task_id)

    assert delegated.task.task_id == planned.task_id
    assert persisted is not None and persisted.board_only is False
    assert persisted.acceptance_criteria == (
        "Return the complete inspect-runtime result with evidence"
    )


async def test_started_board_item_can_restore_a_prior_child_session(service) -> None:
    original = await service.delegate(_request("initial-inspection"))
    await service.complete(original.activation.activation_id, result={"summary": "done"})
    tools = frozenset(
        {"read_file", "exec_command", "apply_patch", "delegate_task", "interrupt_agent"}
    )
    await service.start_run(
        run_id="run-2",
        root_session_id="root-session-2",
        root_task_id="root-task-2",
        root_task_key="root",
        root_task="Continue the requested change",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
        root_runtime_session_key="root-session",
    )
    planned = await service.add_task_board_item(
        run_id="run-2",
        caller_session_id="root-session-2",
        parent_task_id="root-task-2",
        task_key="read-missing-source",
        description="Read the remaining source",
        acceptance_criteria="Return the missing function",
    )
    await service.update_task_board_item(
        run_id="run-2",
        caller_session_id="root-session-2",
        task_key="read-missing-source",
        action="start",
    )

    restored = await service.delegate(
        replace(
            _request("read-missing-source", parent_task_id="root-task-2"),
            run_id="run-2",
            parent_session_id="root-session-2",
            session_id=original.session.session_id,
            acceptance_criteria="Return the exact missing function with line numbers",
        )
    )
    persisted = await service.repository.get_task(planned.task_id)

    assert restored.disposition is DelegateDisposition.RESTORED
    assert restored.task.task_id == planned.task_id
    assert restored.session.session_id == original.session.session_id
    assert persisted is not None and persisted.board_only is False
    assert persisted.board_status is TaskBoardStatus.WORKING
    assert persisted.owner_session_id == original.session.session_id
    assert persisted.acceptance_criteria == (
        "Return the exact missing function with line numbers"
    )


async def test_started_task_still_rejects_changed_acceptance_criteria(service) -> None:
    await service.delegate(_request("inspect-runtime"))

    with pytest.raises(DelegationConflictError, match="acceptance criteria changed"):
        await service.delegate(
            replace(
                _request("inspect-runtime"),
                acceptance_criteria="Return a different result",
            )
        )


async def test_child_can_add_and_complete_dynamic_substeps_with_evidence(service) -> None:
    child = await service.delegate(_request("reproduce-paper"))
    substep = await service.add_task_board_item(
        run_id="run-1",
        caller_session_id=child.session.session_id,
        parent_task_id=child.task.task_id,
        task_key="search-paper",
        description="Find the paper",
        acceptance_criteria="Return the paper title and source URL",
    )

    await service.update_task_board_item(
        run_id="run-1",
        caller_session_id=child.session.session_id,
        task_key="search-paper",
        action="start",
    )
    completed = await service.update_task_board_item(
        run_id="run-1",
        caller_session_id=child.session.session_id,
        task_key="search-paper",
        action="complete",
        evidence="Paper DOI and repository URL verified",
    )

    assert completed.task_id == substep.task_id
    assert completed.board_status is TaskBoardStatus.COMPLETED
    assert completed.outcome is TaskOutcome.SUCCEEDED
    assert completed.evidence == ("Paper DOI and repository URL verified",)


async def test_completed_task_requires_explicit_reopen_before_another_attempt(service) -> None:
    original = await service.delegate(_request("inspect-runtime"))
    await service.complete(original.activation.activation_id, result={"summary": "done"})

    reused = await service.delegate(_request("inspect-runtime"))
    assert reused.disposition is DelegateDisposition.REUSED

    reopened = await service.update_task_board_item(
        run_id="run-1",
        caller_session_id="root-session",
        task_key="inspect-runtime",
        action="reopen",
        evidence="Acceptance condition changed",
    )
    retried = await service.delegate(_request("inspect-runtime"))

    assert reopened.board_status is TaskBoardStatus.PLANNED
    assert retried.disposition is DelegateDisposition.RETRIED
    assert retried.task.task_id == original.task.task_id
    assert retried.session.session_id == original.session.session_id


async def test_sibling_cannot_attach_to_or_reuse_another_parents_task(service) -> None:
    owned = await service.delegate(_request("owned-task"))
    sibling = await service.delegate(_request("sibling-parent"))
    sibling_request = replace(
        _request("owned-task", parent_task_id=sibling.task.task_id),
        parent_session_id=sibling.session.session_id,
        parent_activation_id=sibling.activation.activation_id,
    )

    with pytest.raises(DelegationConflictError, match="direct parent"):
        await service.delegate(sibling_request)

    await service.complete(owned.activation.activation_id, result={"summary": "private"})
    with pytest.raises(DelegationConflictError, match="direct parent"):
        await service.delegate(sibling_request)


async def test_completing_one_child_starts_the_oldest_queued_child(service) -> None:
    children = [await service.delegate(_request(f"task-{index}")) for index in range(9)]

    promoted = await service.complete(
        children[0].activation.activation_id,
        result={"summary": "done"},
    )

    assert promoted is not None
    assert promoted.activation_id == children[8].activation.activation_id
    assert promoted.phase is ActivationPhase.STARTING
    assert await service.repository.live_direct_child_count("root-session") == 8


async def test_ancestor_task_key_is_rejected(service) -> None:
    child = await service.delegate(_request("child"))

    with pytest.raises(DelegationCycleError):
        await service.delegate(
            replace(
                _request("root", parent_task_id=child.task.task_id),
                parent_session_id=child.session.session_id,
            )
        )


async def test_depth_three_agent_cannot_delegate(service) -> None:
    parent_session_id = "root-session"
    parent_task_id = "root-task"
    for depth in range(1, 4):
        outcome = await service.delegate(
            replace(
                _request(f"depth-{depth}", parent_task_id=parent_task_id),
                parent_session_id=parent_session_id,
            )
        )
        parent_session_id = outcome.session.session_id
        parent_task_id = outcome.task.task_id

    with pytest.raises(DelegationDepthError):
        await service.delegate(
            replace(
                _request("too-deep", parent_task_id=parent_task_id),
                parent_session_id=parent_session_id,
            )
        )


async def test_running_session_append_uses_fifo_without_new_activation(service) -> None:
    original = await service.delegate(_request("inspect-runtime"))
    await service.repository.set_activation_runtime_facts(
        original.activation.activation_id,
        phase=ActivationPhase.RUNNING,
        live_model_call=True,
    )

    continued = await service.delegate(
        replace(
            _request("inspect-more"),
            session_id=original.session.session_id,
        )
    )

    assert continued.disposition is DelegateDisposition.APPENDED
    assert continued.session.session_id == original.session.session_id
    assert continued.activation.activation_id == original.activation.activation_id
    pending = await service.repository.list_pending_messages(original.session.session_id)
    assert [message.kind for message in pending] == ["delegated_task"]
    assert pending[0].payload["task_id"] == continued.task.task_id


async def test_duplicate_pending_append_attaches_to_task_scoped_wait(service) -> None:
    original = await service.delegate(_request("inspect-runtime"))
    await service.repository.set_activation_runtime_facts(
        original.activation.activation_id,
        phase=ActivationPhase.RUNNING,
        live_model_call=True,
    )
    appended = await service.delegate(
        replace(_request("inspect-more"), session_id=original.session.session_id)
    )

    attached = await service.delegate(
        replace(_request("inspect-more"), session_id=original.session.session_id)
    )

    assert appended.disposition is DelegateDisposition.APPENDED
    assert attached.disposition is DelegateDisposition.ATTACHED
    assert attached.task.task_id == appended.task.task_id
    assert attached.activation.activation_id == original.activation.activation_id


async def test_cold_restore_stores_current_parent_authority_on_new_task(service) -> None:
    original = await service.delegate(
        replace(
            _request("inspect-runtime"),
            runtime_context={
                "principal_is_owner": True,
                "principal_host_execute": True,
                "run_mode": "full",
            },
        )
    )
    await service.complete(original.activation.activation_id, result={"summary": "done"})

    restored = await service.delegate(
        replace(
            _request("inspect-safe"),
            session_id=original.session.session_id,
            runtime_context={
                "principal_is_owner": False,
                "principal_host_execute": False,
                "run_mode": "safe",
            },
        )
    )

    assert restored.task.runtime_context == {
        "principal_is_owner": False,
        "principal_host_execute": False,
        "run_mode": "safe",
    }
    assert restored.session.runtime_context["run_mode"] == "full"


async def test_retry_recomputes_tools_from_current_parent_authority(service) -> None:
    original = await service.delegate(_request("retry-with-less-authority"))
    await service.finish_activation(
        original.activation.activation_id,
        outcome=TaskOutcome.FAILED,
        result={"error": "retry"},
    )
    reduced = frozenset({"read_file", "delegate_task", "interrupt_agent"})

    retried = await service.delegate(
        replace(
            _request("retry-with-less-authority"),
            inherited_tools=reduced,
        )
    )

    assert retried.disposition is DelegateDisposition.RETRIED
    assert retried.task.effective_tools == reduced
    assert "exec_command" in retried.session.effective_tools


async def test_same_agent_retry_hint_requires_idle_failed_session_and_capacity(
    service,
) -> None:
    request = _request("retry-hint")
    attempt = await service.delegate(request)

    assert (
        await service.can_retry_same_agent(
            task_id=attempt.task.task_id,
            session_id=attempt.session.session_id,
        )
        is False
    )

    for attempt_number in range(3):
        await service.finish_activation(
            attempt.activation.activation_id,
            outcome=TaskOutcome.FAILED,
            result={"error": "temporary failure"},
        )
        can_retry = await service.can_retry_same_agent(
            task_id=attempt.task.task_id,
            session_id=attempt.session.session_id,
        )
        assert can_retry is (attempt_number < 2)
        if can_retry:
            attempt = await service.delegate(request)


async def test_failed_task_rejects_same_session_retry_when_child_disallows_it(service) -> None:
    request = _request("capability-mismatch")
    attempt = await service.delegate(request)
    await service.finish_activation(
        attempt.activation.activation_id,
        outcome=TaskOutcome.FAILED,
        result={
            "status": "failed",
            "error": "This profile cannot execute commands.",
            "retry_same_agent": False,
        },
    )

    with pytest.raises(
        DelegationConflictError,
        match="retry_same_agent=false.*reroute",
    ):
        await service.delegate(request)


async def test_retryable_failed_task_does_not_block_new_work_and_reuses_owner(service) -> None:
    request = _request("implement-useid-mask-fix")
    original = await service.delegate(request)
    await service.finish_activation(
        original.activation.activation_id,
        outcome=TaskOutcome.FAILED,
        result={
            "status": "failed",
            "error": "response_incomplete",
            "retry_same_agent": True,
        },
    )

    different_task = await service.delegate(_request("apply-render-mask-fix"))

    assert different_task.task.task_key == "apply-render-mask-fix"
    assert different_task.session.session_id != original.session.session_id

    retried = await service.delegate(request)

    assert retried.disposition is DelegateDisposition.RETRIED
    assert retried.task.task_id == original.task.task_id
    assert retried.session.session_id == original.session.session_id


async def test_retry_limit_stops_repeated_execution_of_same_task_key(service) -> None:
    request = _request("bounded-retry")
    attempt = await service.delegate(request)

    for _ in range(2):
        await service.finish_activation(
            attempt.activation.activation_id,
            outcome=TaskOutcome.INTERRUPTED,
            result={"error": "provider timeout"},
        )
        attempt = await service.delegate(request)
        assert attempt.disposition is DelegateDisposition.RETRIED

    await service.finish_activation(
        attempt.activation.activation_id,
        outcome=TaskOutcome.INTERRUPTED,
        result={"error": "provider timeout"},
    )
    with pytest.raises(
        DelegationConflictError,
        match=("task execution attempt limit 3 reached.*do not retry the same task_key"),
    ):
        await service.delegate(request)

    rerouted = await service.delegate(_request("bounded-retry-replanned"))
    assert rerouted.disposition is DelegateDisposition.CREATED


async def test_retry_limit_cannot_be_evaded_by_renaming_same_work(service) -> None:
    request = _request("excel-analysis")
    attempt = await service.delegate(request)

    for _ in range(2):
        await service.finish_activation(
            attempt.activation.activation_id,
            outcome=TaskOutcome.FAILED,
            result={"error": "worker failed"},
        )
        attempt = await service.delegate(request)

    await service.finish_activation(
        attempt.activation.activation_id,
        outcome=TaskOutcome.FAILED,
        result={"error": "worker failed"},
    )

    for renamed_key in (
        "excel-analysis-retry",
        "excel_analysis_explorer",
        "excel-analysis-researcher",
        "excel-analysis-reviewer",
        "excel-analysis-final",
        "excel-analysis-retry-2",
    ):
        with pytest.raises(
            DelegationConflictError,
            match="semantic task family.*attempt limit 3 reached",
        ):
            await service.delegate(_request(renamed_key))

    smaller = await service.delegate(_request("department-budget-comparison"))
    assert smaller.disposition is DelegateDisposition.CREATED


async def test_retry_limit_also_fences_replacement_sessions(service) -> None:
    request = _request("bounded-replacement")
    attempt = await service.delegate(request)

    for _ in range(2):
        await service.finish_activation(
            attempt.activation.activation_id,
            outcome=TaskOutcome.FAILED,
            result={"error": "failed"},
        )
        attempt = await service.delegate(
            replace(
                request,
                replace_session_id=attempt.session.session_id,
            )
        )
        assert attempt.disposition is DelegateDisposition.REPLACED

    await service.finish_activation(
        attempt.activation.activation_id,
        outcome=TaskOutcome.FAILED,
        result={"error": "failed"},
    )
    with pytest.raises(DelegationConflictError, match="attempt limit 3 reached"):
        await service.delegate(
            replace(
                request,
                replace_session_id=attempt.session.session_id,
            )
        )


async def test_waiting_session_append_queues_next_round_without_resolving_dependency(
    service,
) -> None:
    original = await service.delegate(_request("inspect-runtime"))
    await service.repository.set_activation_runtime_facts(
        original.activation.activation_id,
        phase=ActivationPhase.RUNNING,
        external_wait_id="approval-1",
    )

    continued = await service.delegate(
        replace(
            _request("use-this-constraint"),
            session_id=original.session.session_id,
        )
    )

    assert continued.disposition is DelegateDisposition.APPENDED
    assert continued.activation.activation_id == original.activation.activation_id
    current = await service.repository.get_activation(original.activation.activation_id)
    assert current is not None
    assert current.external_wait_id == "approval-1"


async def test_foreground_completion_does_not_enqueue_parent_notification(service) -> None:
    delegated = await service.delegate(_request("foreground"))

    await service.complete(delegated.activation.activation_id, result={"summary": "done"})

    assert await service.repository.list_pending_messages("root-session") == []


async def test_background_completion_enqueues_one_parent_notification(service) -> None:
    delegated = await service.delegate(replace(_request("background"), background=True))

    await service.complete(delegated.activation.activation_id, result={"summary": "done"})

    messages = await service.repository.list_pending_messages("root-session")
    assert len(messages) == 1
    assert messages[0].kind == "child_result"
    assert messages[0].idempotency_key == (
        f"child-result:{delegated.task.task_id}:{delegated.activation.activation_id}"
    )


async def test_background_delivery_and_processing_finish_run_in_either_order(service) -> None:
    delegated = await service.delegate(replace(_request("background"), background=True))
    await service.complete(delegated.activation.activation_id, result={"summary": "done"})

    await service.repository.mark_child_result_processed(delegated.task.task_id)
    completed = await service.acknowledge_result_delivery(delegated.task.task_id)

    run = await service.repository.get_run("run-1")
    assert completed is True
    assert run is not None
    assert run.final_synthesis_completed is True
    assert run.lifecycle.value == "completed"


async def test_released_session_cold_restores_same_persistent_session(service) -> None:
    original = await service.delegate(_request("inspect-runtime"))
    await service.complete(original.activation.activation_id, result={"summary": "done"})

    restored = await service.delegate(
        replace(
            _request("inspect-follow-up"),
            session_id=original.session.session_id,
        )
    )

    assert restored.disposition is DelegateDisposition.RESTORED
    assert restored.session.session_id == original.session.session_id
    assert restored.activation.activation_id != original.activation.activation_id
    assert restored.activation.phase is ActivationPhase.STARTING


async def test_persistent_session_can_reattach_to_a_later_root_turn(service) -> None:
    original = await service.delegate(_request("inspect-runtime"))
    await service.complete(original.activation.activation_id, result={"summary": "done"})
    tools = frozenset(
        {"read_file", "exec_command", "apply_patch", "delegate_task", "interrupt_agent"}
    )
    await service.start_run(
        run_id="run-2",
        root_session_id="root-session-2",
        root_task_id="root-task-2",
        root_task_key="root",
        root_task="Continue the requested change",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
        root_runtime_session_key="root-session",
    )

    restored = await service.delegate(
        replace(
            _request("inspect-follow-up", parent_task_id="root-task-2"),
            run_id="run-2",
            parent_session_id="root-session-2",
            session_id=original.session.session_id,
        )
    )

    assert restored.disposition is DelegateDisposition.RESTORED
    assert restored.session.session_id == original.session.session_id
    assert restored.task.run_id == "run-2"
    attachment = await service.repository.get_session_attachment(
        run_id="run-2",
        session_id=original.session.session_id,
    )
    assert attachment is not None
    assert attachment.parent_session_id == "root-session-2"

    targets = await service.request_interrupt(
        caller_session_id="root-session-2",
        session_id=restored.session.session_id,
        activation_id=restored.activation.activation_id,
        reason="stop restored work",
    )
    assert [target.activation_id for target in targets] == [restored.activation.activation_id]


async def test_later_root_turn_can_discover_prior_unsynthesized_child_work(service) -> None:
    original = await service.delegate(_request("inspect-runtime"))
    await service.complete(
        original.activation.activation_id,
        result={
            "status": "completed",
            "summary": "Found the implementation owner",
            "deliverable": "The relevant method is Runtime.run.",
        },
    )
    tools = frozenset(
        {"read_file", "exec_command", "apply_patch", "delegate_task", "interrupt_agent"}
    )
    await service.start_run(
        run_id="run-2",
        root_session_id="root-session-2",
        root_task_id="root-task-2",
        root_task_key="root",
        root_task="Continue the requested change",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
        root_runtime_session_key="root-session",
    )

    recovered = await service.repository.list_prior_unsynthesized_child_work(
        root_runtime_session_key="root-session",
        exclude_run_id="run-2",
    )

    assert [(task.task_key, session_id) for task, session_id in recovered] == [
        ("inspect-runtime", original.session.session_id)
    ]
    assert recovered[0][0].result == {
        "status": "completed",
        "summary": "Found the implementation owner",
        "deliverable": "The relevant method is Runtime.run.",
    }


async def test_archiving_origin_run_keeps_reattached_session_available(service) -> None:
    original = await service.delegate(_request("inspect-runtime"))
    await service.complete(original.activation.activation_id, result={"summary": "done"})
    assert await service.mark_final_synthesis_completed("run-1") is True
    tools = frozenset(
        {"read_file", "exec_command", "apply_patch", "delegate_task", "interrupt_agent"}
    )
    await service.start_run(
        run_id="run-2",
        root_session_id="root-session-2",
        root_task_id="root-task-2",
        root_task_key="root",
        root_task="Continue the requested change",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
        root_runtime_session_key="root-session",
    )
    await service.delegate(
        replace(
            _request("inspect-follow-up", parent_task_id="root-task-2"),
            run_id="run-2",
            parent_session_id="root-session-2",
            session_id=original.session.session_id,
        )
    )

    assert await service.repository.archive_eligible_runs(cutoff=1_000) == ["run-1"]
    session = await service.repository.get_session(original.session.session_id)
    assert session is not None
    assert session.lifecycle is SessionLifecycle.ACTIVE


async def test_replacement_requires_idle_source_and_preserves_semantic_task(service) -> None:
    original = await service.delegate(_request("implement-change"))

    with pytest.raises(DelegationConflictError, match="still has a live activation"):
        await service.delegate(
            replace(
                _request("implement-change"),
                replace_session_id=original.session.session_id,
            )
        )

    await service.finish_activation(
        original.activation.activation_id,
        outcome=TaskOutcome.FAILED,
        result={"error": "stalled"},
    )
    replacement = await service.delegate(
        replace(
            _request("implement-change"),
            replace_session_id=original.session.session_id,
        )
    )

    assert replacement.disposition is DelegateDisposition.REPLACED
    assert replacement.task.task_id == original.task.task_id
    assert replacement.task.owner_session_id == replacement.session.session_id
    assert replacement.task.replaces_session_id == original.session.session_id
    assert replacement.task.retry_of_activation_id == original.activation.activation_id


async def test_interrupt_uses_activation_generation_fence_and_marks_subtree(service) -> None:
    parent = await service.delegate(_request("parent"))
    child = await service.delegate(
        replace(
            _request("child", parent_task_id=parent.task.task_id),
            parent_session_id=parent.session.session_id,
        )
    )

    with pytest.raises(InterruptFenceError):
        await service.request_interrupt(
            caller_session_id="root-session",
            session_id=parent.session.session_id,
            activation_id="old-generation",
            reason="loop",
        )

    targets = await service.request_interrupt(
        caller_session_id="root-session",
        session_id=parent.session.session_id,
        activation_id=parent.activation.activation_id,
        reason="loop",
    )

    assert [target.activation_id for target in targets] == [
        child.activation.activation_id,
        parent.activation.activation_id,
    ]
    assert all(target.phase is ActivationPhase.STOPPING for target in targets)


async def test_ownerless_stopping_activation_is_recoverable_and_cannot_revive(service) -> None:
    delegated = await service.delegate(_request("stop-before-start"))
    await service.request_interrupt(
        caller_session_id="root-session",
        session_id=delegated.session.session_id,
        activation_id=delegated.activation.activation_id,
        reason="cancel queued work",
    )

    orphaned = await service.repository.list_orphaned_activations()
    assert [item.activation_id for item in orphaned] == [delegated.activation.activation_id]
    updated = await service.repository.set_activation_runtime_facts(
        delegated.activation.activation_id,
        phase=ActivationPhase.RUNNING,
        live_model_call=True,
    )
    assert updated.phase is ActivationPhase.STOPPING
    persisted = await service.repository.get_activation(delegated.activation.activation_id)
    assert persisted is not None and persisted.phase is ActivationPhase.STOPPING


async def test_stopping_parent_cannot_delegate_new_descendant(service) -> None:
    parent = await service.delegate(_request("parent"))
    await service.request_interrupt(
        caller_session_id="root-session",
        session_id=parent.session.session_id,
        activation_id=parent.activation.activation_id,
        reason="stop subtree",
    )

    with pytest.raises(DelegationConflictError, match="stopping"):
        await service.delegate(
            replace(
                _request("late-child", parent_task_id=parent.task.task_id),
                parent_session_id=parent.session.session_id,
                parent_activation_id=parent.activation.activation_id,
            )
        )


async def test_nested_background_delegation_is_rejected_until_durable_wake_exists(service) -> None:
    parent = await service.delegate(_request("parent"))

    with pytest.raises(DelegationPermissionError, match="root agent"):
        await service.delegate(
            replace(
                _request("nested-background", parent_task_id=parent.task.task_id),
                parent_session_id=parent.session.session_id,
                parent_activation_id=parent.activation.activation_id,
                background=True,
            )
        )


async def test_background_retry_has_attempt_scoped_result_identity(service) -> None:
    first = await service.delegate(replace(_request("retry-background"), background=True))
    await service.finish_activation(
        first.activation.activation_id,
        outcome=TaskOutcome.FAILED,
        result={"error": "first"},
    )
    first_messages = await service.repository.list_pending_child_result_messages()
    assert len(first_messages) == 1
    first_idempotency_key = first_messages[0].idempotency_key
    await service.repository.acknowledge_child_result_message(
        first.task.task_id,
        activation_id=first.activation.activation_id,
    )
    await service.repository.mark_child_result_processed(
        first.task.task_id,
        activation_id=first.activation.activation_id,
    )

    retry = await service.delegate(replace(_request("retry-background"), background=True))
    await service.complete(retry.activation.activation_id, result={"summary": "second"})

    messages = await service.repository.list_unprocessed_child_result_messages()
    assert [message.payload["activation_id"] for message in messages] == [
        retry.activation.activation_id,
    ]
    assert messages[0].idempotency_key != first_idempotency_key


async def test_finished_interrupt_releases_activation_but_keeps_session(service) -> None:
    delegated = await service.delegate(_request("looping-task"))
    await service.request_interrupt(
        caller_session_id="root-session",
        session_id=delegated.session.session_id,
        activation_id=delegated.activation.activation_id,
        reason="loop detected",
    )

    await service.finish_activation(
        delegated.activation.activation_id,
        outcome=TaskOutcome.INTERRUPTED,
        result={"reason": "loop detected"},
    )

    activation = await service.repository.get_activation(delegated.activation.activation_id)
    session = await service.repository.get_session(delegated.session.session_id)
    task = await service.repository.get_task(delegated.task.task_id)
    assert activation is not None and activation.phase is ActivationPhase.RELEASED
    assert session is not None and session.lifecycle is SessionLifecycle.IDLE
    assert task is not None and task.outcome is TaskOutcome.INTERRUPTED


async def test_interrupt_caller_cannot_target_sibling_session(service) -> None:
    first = await service.delegate(_request("first"))
    second = await service.delegate(_request("second"))

    with pytest.raises(InterruptFenceError, match="outside caller subtree"):
        await service.request_interrupt(
            caller_session_id=first.session.session_id,
            session_id=second.session.session_id,
            activation_id=second.activation.activation_id,
            reason="not my child",
        )
