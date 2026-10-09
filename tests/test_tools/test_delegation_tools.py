from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from opensquilla.orchestration.executor import DelegateExecution
from opensquilla.orchestration.models import (
    ActivationPhase,
    AgentState,
    DelegatedTaskRecord,
    OrchestrationMode,
    TaskBoardStatus,
    TaskOutcome,
)
from opensquilla.orchestration.profiles import PRESET_PROFILES
from opensquilla.orchestration.service import DelegateDisposition
from opensquilla.tools.builtin import delegation
from opensquilla.tools.policy_runtime import detect_runtime_tool_surface_capabilities
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import (
    ToolContext,
    ToolError,
    current_tool_context,
)


class FakeExecutor:
    def __init__(self) -> None:
        self.requests = []
        self.interrupts = []
        self.progress_updates = []
        self.runs = []
        self.service = SimpleNamespace(
            repository=SimpleNamespace(
                get_run=self._get_run,
                delegated_work_state=self._delegated_work_state,
                run_ready_for_final_synthesis=self._run_ready_for_final_synthesis,
                list_task_board=self._list_task_board,
                list_prior_unsynthesized_child_work=(
                    self._list_prior_unsynthesized_child_work
                ),
                list_single_agent_results=self._list_single_agent_results,
                get_session=self._get_session,
                get_task=self._get_task,
            )
        )
        self.service.add_task_board_item = self._add_task_board_item
        self.service.update_task_board_item = self._update_task_board_item
        self.run_exists = False
        self.work_state = (0, 0)
        self.run_ready = True
        self.ready_observation = None
        self.foreground_result = {
            "status": "completed",
            "summary": "done",
            "error": None,
            "retry_same_agent": False,
        }
        self.retry_allowed = True
        self.agent_state_value = AgentState.IDLE
        self.board_tasks = [
            SimpleNamespace(
                task_id="root-task",
                task_key="root",
                parent_task_id=None,
                owner_session_id="root-session",
                board_status=TaskBoardStatus.WORKING,
                outcome=TaskOutcome.PENDING,
                description="Root task",
                acceptance_criteria=None,
                evidence=(),
                board_only=False,
            )
        ]
        self.prior_child_work = []
        self.completed_child_work = []
        self.sessions = {}

    async def _list_prior_unsynthesized_child_work(self, **kwargs):
        del kwargs
        return list(self.prior_child_work)

    async def _list_single_agent_results(self, **kwargs):
        del kwargs
        return list(self.completed_child_work)

    async def _list_task_board(self, run_id):
        del run_id
        return list(self.board_tasks)

    async def _get_task(self, task_id):
        return next((task for task in self.board_tasks if task.task_id == task_id), None)

    async def _get_session(self, session_id):
        return self.sessions.get(session_id)

    async def _add_task_board_item(self, **kwargs):
        task = SimpleNamespace(
            task_id=f"task-{len(self.board_tasks)}",
            task_key=kwargs["task_key"],
            parent_task_id=kwargs["parent_task_id"],
            owner_session_id=kwargs["caller_session_id"],
            board_status=TaskBoardStatus.PLANNED,
            outcome=TaskOutcome.PENDING,
            description=kwargs["description"],
            acceptance_criteria=kwargs["acceptance_criteria"],
            evidence=(),
            board_only=True,
        )
        self.board_tasks.append(task)
        return task

    async def _update_task_board_item(self, **kwargs):
        task = next(task for task in self.board_tasks if task.task_key == kwargs["task_key"])
        statuses = {
            "start": TaskBoardStatus.WORKING,
            "complete": TaskBoardStatus.COMPLETED,
            "block": TaskBoardStatus.BLOCKED,
            "reopen": TaskBoardStatus.PLANNED,
        }
        task.board_status = statuses[kwargs["action"]]
        if kwargs["action"] == "complete":
            task.outcome = TaskOutcome.SUCCEEDED
        elif kwargs["action"] == "block":
            task.outcome = TaskOutcome.FAILED
        elif kwargs["action"] == "reopen":
            task.outcome = TaskOutcome.PENDING
        if kwargs.get("evidence"):
            task.evidence = (*task.evidence, kwargs["evidence"])
        return task

    async def _get_run(self, run_id):
        del run_id
        return (
            SimpleNamespace(
                mode=OrchestrationMode.COMPLEX,
                root_session_id="root-session",
                root_task_id="root-task",
            )
            if self.run_exists
            else None
        )

    async def _delegated_work_state(self, run_id):
        del run_id
        return self.work_state

    async def _run_ready_for_final_synthesis(self, run_id, **kwargs):
        del run_id
        return self.run_ready or kwargs == self.ready_observation

    async def ensure_run(self, **kwargs):
        self.runs.append(kwargs)

    async def delegate(self, request):
        self.requests.append(request)
        planned = next(
            (task for task in self.board_tasks if task.task_key == request.task_key),
            None,
        )
        if planned is not None:
            planned.board_only = False
            planned.owner_session_id = "session-1"
            status = str(
                self.foreground_result.get("status")
                or ("failed" if self.foreground_result.get("error") else "completed")
            )
            if request.background:
                planned.board_status = TaskBoardStatus.PLANNED
                planned.outcome = TaskOutcome.PENDING
            elif status == "completed":
                planned.board_status = TaskBoardStatus.COMPLETED
                planned.outcome = TaskOutcome.SUCCEEDED
            else:
                planned.board_status = TaskBoardStatus.BLOCKED
                planned.outcome = TaskOutcome.FAILED
        return DelegateExecution(
            disposition=DelegateDisposition.CREATED,
            run_id=request.run_id,
            task_id="task-1",
            task_key=request.task_key,
            session_id="session-1",
            activation_id="activation-1",
            phase=ActivationPhase.STARTING,
            background=request.background,
            result=(None if request.background else dict(self.foreground_result)),
        )

    async def can_retry_same_agent(self, *, task_id: str, session_id: str) -> bool:
        del task_id, session_id
        return self.retry_allowed

    async def interrupt(
        self,
        *,
        caller_session_id: str,
        session_id: str,
        activation_id: str,
        reason: str,
    ):
        self.interrupts.append((caller_session_id, session_id, activation_id, reason))
        return [
            type(
                "Activation",
                (),
                {"activation_id": activation_id, "session_id": session_id},
            )()
        ]

    async def agent_state(self, *, run_id: str, session_id: str):
        del run_id, session_id
        return self.agent_state_value

    async def publish_progress(self, *, run_id: str, task_id: str, phase: str):
        self.progress_updates.append((run_id, task_id, phase))


@pytest.fixture(autouse=True)
def reset_executor():
    delegation.set_orchestration_executor(None)
    try:
        yield
    finally:
        delegation.set_orchestration_executor(None)


def _context() -> ToolContext:
    context = ToolContext(
        is_owner=True,
        session_key="agent:main:subagent:root",
        authorized_tool_names=frozenset(
            {"read_file", "apply_patch", "delegate_task", "interrupt_agent", "task_board"}
        ),
    )
    context.orchestration_run_id = "run-1"
    context.orchestration_session_id = "root-session"
    context.orchestration_task_id = "root-task"
    context.orchestration_worker_template_tools = frozenset(
        {"read_file", "apply_patch", "delegate_task", "interrupt_agent", "task_board"}
    )
    context.orchestration_complex_mode = True
    return context


def _plan_task(
    executor: FakeExecutor,
    *,
    task_key: str,
    task: str,
    acceptance_criteria: str,
    owner_session_id: str = "root-session",
    board_only: bool = True,
) -> None:
    executor.board_tasks.append(
        SimpleNamespace(
            task_id=f"task-{len(executor.board_tasks)}",
            task_key=task_key,
            parent_task_id="root-task",
            owner_session_id=owner_session_id,
            board_status=(
                TaskBoardStatus.PLANNED if board_only else TaskBoardStatus.COMPLETED
            ),
            outcome=(TaskOutcome.PENDING if board_only else TaskOutcome.SUCCEEDED),
            description=task,
            acceptance_criteria=acceptance_criteria,
            evidence=(),
            board_only=board_only,
        )
    )


def test_delegate_schema_defaults_to_foreground_and_supports_lifecycle_controls() -> None:
    registered = get_default_registry().get("delegate_task")

    assert registered is not None
    assert "Foreground is the default and blocks" in registered.spec.description
    assert "background=true" in registered.spec.description
    assert "calls emitted in one response run concurrently" in registered.spec.description
    assert "one bounded task with concrete acceptance criteria" in registered.spec.description
    assert "sufficiently similar same-profile child" in registered.spec.description
    assert "completed or overlapping work" in registered.spec.description
    assert "only across non-overlapping sources" in registered.spec.description
    assert "one worker session owns a requested code change" in registered.spec.description
    assert "delegate exactly one investigation" not in registered.spec.description
    assert "execution session" not in registered.spec.description
    assert "Reusing a completed task_key returns its stored result" in (registered.spec.description)
    assert "not a copy of whole files or raw search results" in registered.spec.description
    assert "Complex roots receive progress, not the explorer's technical deliverable" in (
        registered.spec.description
    )
    assert "smallest relevant existing check" not in registered.spec.description
    assert "do not repair the environment" not in registered.spec.description
    assert "localized coding task" not in registered.spec.description
    assert registered.spec.required == ["task", "task_key", "acceptance_criteria"]
    assert "One bounded outcome" in registered.spec.parameters["task"]["description"]
    assert "key findings and evidence" in registered.spec.parameters["task"]["description"]
    assert "unverified implementation or whole-file handoff" in (
        registered.spec.parameters["task"]["description"]
    )
    session_description = registered.spec.parameters["session_id"]["description"]
    assert "without repeating known context" in session_description
    acceptance_description = registered.spec.parameters["acceptance_criteria"]["description"]
    assert "Concrete observable result required for this task" in acceptance_description
    assert registered.spec.parameters["background"]["default"] is False
    assert registered.spec.parameters["agent"]["enum"] == list(PRESET_PROFILES)
    assert "default" not in registered.spec.parameters["agent"]
    agent_description = registered.spec.parameters["agent"]["description"]
    assert "inherit keeps all inherited worker capabilities" in agent_description
    assert "worker can read and edit files" in agent_description
    assert "explorer can only read local files and search code" in agent_description
    assert "default for requested changes" not in agent_description
    assert "not preparatory reading" not in agent_description
    assert "researcher can only read local files and use web research" in agent_description
    assert "cannot run local commands, tests, or edit files" in agent_description
    assert "reviewer can only read files and diffs" in agent_description
    assert agent_description.count("cannot run commands, tests, or edit files") == 2
    assert "Omit it to keep an existing session's profile" in agent_description
    assert "explicitly select worker" in agent_description
    assert {"session_id", "replace_session_id"} <= set(registered.spec.parameters)
    session_description = registered.spec.parameters["session_id"]["description"]
    assert "preserves its conversation and tool results" in session_description
    assert "agent=worker" in session_description
    assert "title" not in registered.spec.parameters
    assert registered.spec.execution_timeout_seconds == 0


def test_agent_model_surface_contains_the_three_orchestration_controls() -> None:
    registry = get_default_registry()

    assert registry.get("delegate_task") is not None
    assert registry.get("interrupt_agent") is not None
    assert registry.get("task_board") is not None
    for removed in (
        "agents_list",
        "subagents",
        "sessions_send",
        "sessions_spawn",
        "sessions_list",
        "sessions_history",
        "sessions_yield",
        "session_status",
    ):
        assert registry.get(removed) is None


def test_task_board_does_not_truncate_planned_work_items() -> None:
    registered = get_default_registry().get("task_board")

    assert registered is not None
    assert "maxLength" not in registered.spec.parameters["description"]
    assert "maxLength" not in registered.spec.parameters["acceptance_criteria"]
    assert "Never make whole files, full source, or every raw search result" in (
        registered.spec.description
    )
    assert "decision-critical findings" in (
        registered.spec.parameters["description"]["description"]
    )


@pytest.mark.asyncio
async def test_task_board_adds_and_completes_a_shared_subtask() -> None:
    executor = FakeExecutor()
    delegation.set_orchestration_executor(executor)
    token = current_tool_context.set(_context())
    try:
        raw = delegation.task_board.__wrapped__.__wrapped__
        added = json.loads(
            await raw(
                action="add",
                task_key="inspect-runtime",
                description="Inspect the runtime",
                acceptance_criteria="Locate the owner",
            )
        )
        completed = json.loads(
            await raw(
                action="complete",
                task_key="inspect-runtime",
                evidence="Owner located in orchestration runtime",
            )
        )
    finally:
        current_tool_context.reset(token)

    assert added["tasks"][1]["board_status"] == "planned"
    assert added["tasks"][1]["agent_state"] == "idle"
    assert added["tasks"][1]["outcome"] == "pending"
    assert completed["tasks"][1]["board_status"] == "completed"
    assert completed["tasks"][1]["agent_state"] == "idle"
    assert completed["tasks"][1]["outcome"] == "succeeded"
    assert "status" not in completed["tasks"][1]
    assert completed["tasks"][1]["evidence"] == ["Owner located in orchestration runtime"]
    assert executor.progress_updates == [
        ("run-1", "task-1", "add"),
        ("run-1", "task-1", "complete"),
    ]


@pytest.mark.asyncio
async def test_complex_root_can_freeze_a_new_task_on_first_delegation() -> None:
    executor = FakeExecutor()
    delegation.set_orchestration_executor(executor)
    token = current_tool_context.set(_context())
    try:
        raw = delegation.delegate_task.__wrapped__.__wrapped__
        await raw(
            task="Inspect the runtime",
            task_key="inspect-runtime",
            acceptance_criteria="Return the responsible file and smallest change.",
        )
    finally:
        current_tool_context.reset(token)

    assert len(executor.requests) == 1
    assert executor.requests[0].acceptance_criteria == (
        "Return the responsible file and smallest change."
    )


@pytest.mark.asyncio
async def test_complex_root_freezes_criteria_but_keeps_current_task_instruction() -> None:
    executor = FakeExecutor()
    frozen = "Return the root cause, target paths, and focused verification."
    _plan_task(
        executor,
        task_key="inspect-runtime",
        task="Inspect the runtime",
        acceptance_criteria=frozen,
    )
    delegation.set_orchestration_executor(executor)
    token = current_tool_context.set(_context())
    try:
        raw = delegation.delegate_task.__wrapped__.__wrapped__
        await raw(
            task="Inspect the runtime and include useful local commands.",
            task_key="inspect-runtime",
            acceptance_criteria="A reworded contract that must not replace the frozen fields.",
        )
    finally:
        current_tool_context.reset(token)

    assert len(executor.requests) == 1
    assert executor.requests[0].task == "Inspect the runtime and include useful local commands."
    assert executor.requests[0].acceptance_criteria == frozen


@pytest.mark.asyncio
async def test_delegate_omits_profile_so_existing_session_can_keep_it() -> None:
    executor = FakeExecutor()
    _plan_task(
        executor,
        task_key="implement-fix",
        task="Implement the smallest fix",
        acceptance_criteria="Apply the source change and run focused verification.",
    )
    executor.sessions["session-1"] = SimpleNamespace(profile="explorer")
    delegation.set_orchestration_executor(executor)
    token = current_tool_context.set(_context())
    try:
        raw = delegation.delegate_task.__wrapped__.__wrapped__
        await raw(
            task="Implement the smallest fix using the existing investigation context.",
            task_key="implement-fix",
            acceptance_criteria="Apply the source change and run focused verification.",
            session_id="session-1",
        )
    finally:
        current_tool_context.reset(token)

    assert executor.requests[0].profile is None


@pytest.mark.asyncio
async def test_delegate_can_explicitly_promote_existing_session_to_worker() -> None:
    executor = FakeExecutor()
    _plan_task(
        executor,
        task_key="implement-fix",
        task="Implement the smallest fix",
        acceptance_criteria="Apply the source change and run focused verification.",
    )
    executor.sessions["session-1"] = SimpleNamespace(profile="explorer")
    delegation.set_orchestration_executor(executor)
    token = current_tool_context.set(_context())
    try:
        raw = delegation.delegate_task.__wrapped__.__wrapped__
        await raw(
            task="Implement the smallest fix using the existing investigation context.",
            task_key="implement-fix",
            acceptance_criteria="Apply the source change and run focused verification.",
            agent="worker",
            session_id="session-1",
        )
    finally:
        current_tool_context.reset(token)

    assert executor.requests[0].profile == "worker"


@pytest.mark.asyncio
async def test_complex_root_can_add_discovered_task_after_first_delegation() -> None:
    executor = FakeExecutor()
    _plan_task(
        executor,
        task_key="inspect-runtime",
        task="Inspect the runtime",
        acceptance_criteria="Return the responsible file and smallest change.",
    )
    delegation.set_orchestration_executor(executor)
    token = current_tool_context.set(_context())
    try:
        delegate = delegation.delegate_task.__wrapped__.__wrapped__
        board = delegation.task_board.__wrapped__.__wrapped__
        await delegate(
            task="Inspect the runtime",
            task_key="inspect-runtime",
            acceptance_criteria="Return the responsible file and smallest change.",
        )
        await board(
            action="add",
            task_key="inspect-new-site",
            description="Inspect a newly discovered call site",
            acceptance_criteria="Return the call site and its relevant behavior.",
        )
        await delegate(
            task="Inspect the newly discovered call site",
            task_key="inspect-new-site",
            acceptance_criteria="Replace this with a broader criterion.",
        )
    finally:
        current_tool_context.reset(token)

    assert executor.requests[-1].task_key == "inspect-new-site"
    assert executor.requests[-1].acceptance_criteria == (
        "Return the call site and its relevant behavior."
    )


@pytest.mark.asyncio
async def test_complex_root_can_start_new_worker_stage_in_existing_session() -> None:
    executor = FakeExecutor()
    frozen = "Report the root cause and whether implementation can proceed."
    _plan_task(
        executor,
        task_key="investigate-fix",
        task="Investigate the cause",
        acceptance_criteria=frozen,
        owner_session_id="session-1",
        board_only=False,
    )
    executor.sessions["session-1"] = SimpleNamespace(profile="explorer")
    delegation.set_orchestration_executor(executor)
    token = current_tool_context.set(_context())
    try:
        raw = delegation.delegate_task.__wrapped__.__wrapped__
        await raw(
            task="Implement the fix using the investigation already in this session.",
            task_key="implement-fix",
            acceptance_criteria="Apply the source change and run focused verification.",
            agent="worker",
            session_id="session-1",
        )
    finally:
        current_tool_context.reset(token)

    assert len(executor.requests) == 1
    assert executor.requests[0].task_key == "implement-fix"
    assert executor.requests[0].acceptance_criteria == (
        "Apply the source change and run focused verification."
    )
    assert executor.requests[0].session_id == "session-1"
    assert executor.requests[0].profile == "worker"
    assert executor.board_tasks[1].acceptance_criteria == frozen


@pytest.mark.asyncio
async def test_complex_root_receives_prior_unsynthesized_child_results() -> None:
    executor = FakeExecutor()
    executor.prior_child_work = [
        (
            DelegatedTaskRecord(
                task_id="old-task",
                run_id="old-run",
                task_key="inspect-runtime",
                owner_session_id="old-child",
                description="Inspect the runtime",
                outcome=TaskOutcome.SUCCEEDED,
                board_status=TaskBoardStatus.COMPLETED,
                evidence=("Located Runtime.run",),
                result={
                    "status": "completed",
                    "summary": "Found the implementation owner",
                    "deliverable": "The relevant method is Runtime.run.",
                },
            ),
            "old-child",
        )
    ]
    delegation.set_orchestration_executor(executor)

    recovery = await delegation.ensure_complex_root_run(
        _context(),
        root_task="Continue the requested change",
    )

    assert "[Recovered delegated work from earlier unfinished turns]" in recovery
    assert '"task_key": "inspect-runtime"' in recovery
    assert '"session_id": "old-child"' in recovery
    assert '"summary": "Found the implementation owner"' in recovery
    assert '"deliverable": "The relevant method is Runtime.run."' in recovery


@pytest.mark.asyncio
async def test_complete_task_root_does_not_recover_prior_child_summaries() -> None:
    executor = FakeExecutor()
    executor.prior_child_work = [
        (
            DelegatedTaskRecord(
                task_id="old-task",
                run_id="old-run",
                task_key="figure-captions",
                owner_session_id="old-child",
                description="Write captions for three figures",
                acceptance_criteria="Return the captions and sources",
                outcome=TaskOutcome.SUCCEEDED,
                board_status=TaskBoardStatus.COMPLETED,
                evidence=("private working detail",),
                result={
                    "status": "completed",
                    "summary": "Three captions ready",
                    "deliverable": "The full report should stay out of the root context.",
                },
            ),
            "old-child",
        )
    ]
    delegation.set_orchestration_executor(executor)
    context = _context()
    context.orchestration_single_mode = True

    recovery = await delegation.ensure_complex_root_run(
        context,
        root_task="Continue the report",
    )

    assert recovery == ""


@pytest.mark.asyncio
async def test_complete_task_root_does_not_inject_completed_session_history() -> None:
    executor = FakeExecutor()
    executor.completed_child_work = [
        DelegatedTaskRecord(
            task_id="old-task",
            run_id="old-run",
            task_key="calc-17x19",
            owner_session_id="old-calculation-child",
            description="Calculate 17×19 and show one line of checking",
            outcome=TaskOutcome.SUCCEEDED,
            board_status=TaskBoardStatus.COMPLETED,
            result={
                "status": "completed",
                "summary": "Calculated 17×19=323.",
                "deliverable": "Full answer must remain outside the root context.",
            },
        )
    ]
    delegation.set_orchestration_executor(executor)
    context = _context()
    context.orchestration_single_mode = True

    reminder = await delegation.ensure_complex_root_run(
        context,
        root_task="Continue the previous calculation",
    )

    assert reminder == ""


@pytest.mark.asyncio
async def test_single_mode_does_not_replay_interrupted_prior_task_on_new_query() -> None:
    executor = FakeExecutor()
    executor.prior_child_work = [
        (
            DelegatedTaskRecord(
                task_id="old-image-task",
                run_id="previous-query",
                task_key="robot-cafe-png-illustration",
                owner_session_id="old-image-child",
                description="Draw the robot cafe image",
                outcome=TaskOutcome.INTERRUPTED,
                result={"status": "interrupted", "summary": "Image tool unavailable"},
            ),
            "old-image-child",
        )
    ]
    delegation.set_orchestration_executor(executor)
    context = _context()
    context.orchestration_single_mode = True

    recovery = await delegation.ensure_complex_root_run(
        context,
        root_task="Create a market research report",
    )

    assert recovery == ""


@pytest.mark.asyncio
async def test_recovery_keeps_long_deliverable_and_task_local_unresolved_questions() -> None:
    executor = FakeExecutor()
    deliverable = "exact implementation evidence\n" * 200
    executor.prior_child_work = [
        (
            DelegatedTaskRecord(
                task_id="old-task",
                run_id="old-run",
                task_key="locate-fix",
                owner_session_id="old-child",
                description="Locate the fix",
                outcome=TaskOutcome.SUCCEEDED,
                board_status=TaskBoardStatus.COMPLETED,
                result={
                    "status": "completed",
                    "deliverable": deliverable,
                    "unresolved": [],
                },
            ),
            "old-child",
        )
    ]
    delegation.set_orchestration_executor(executor)

    recovery = await delegation.ensure_complex_root_run(_context(), root_task="Continue")

    recovered = json.loads(recovery.splitlines()[-1])
    assert recovered[0]["deliverable"] == deliverable.strip()
    assert recovered[0]["unresolved"] == []
    assert "deliverable_available_in_child_session" not in recovery


@pytest.mark.asyncio
async def test_recovery_omits_empty_interrupted_attempts() -> None:
    executor = FakeExecutor()
    executor.prior_child_work = [
        (
            DelegatedTaskRecord(
                task_id="empty-pending-task",
                run_id="old-run",
                task_key="stale-pending-investigation",
                owner_session_id="pending-child",
                description="Continue the stale investigation",
                outcome=TaskOutcome.PENDING,
            ),
            "pending-child",
        ),
        (
            DelegatedTaskRecord(
                task_id="restart-task",
                run_id="old-run",
                task_key="stale-restart-investigation",
                owner_session_id="restart-child",
                description="Retry work interrupted by the diagnostic restart",
                outcome=TaskOutcome.INTERRUPTED,
                result={"status": "interrupted", "summary": "gateway_restarted"},
            ),
            "restart-child",
        ),
        (
            DelegatedTaskRecord(
                task_id="useful-task",
                run_id="old-run",
                task_key="located-root-cause",
                owner_session_id="useful-child",
                description="Locate the root cause",
                outcome=TaskOutcome.SUCCEEDED,
                result={
                    "status": "completed",
                    "summary": "Located the root cause and smallest edit",
                    "deliverable": "Change the condition in src/runtime.py.",
                    "unresolved": [],
                },
            ),
            "useful-child",
        ),
    ]
    delegation.set_orchestration_executor(executor)

    recovery = await delegation.ensure_complex_root_run(_context(), root_task="Continue")

    assert "located-root-cause" in recovery
    assert "stale-pending-investigation" not in recovery
    assert "stale-restart-investigation" not in recovery


def test_bound_orchestration_executor_is_detected_as_task_runtime() -> None:
    delegation.set_orchestration_executor(FakeExecutor())

    capabilities = detect_runtime_tool_surface_capabilities()

    assert capabilities.task_runtime is True


@pytest.mark.asyncio
async def test_complex_root_delegation_uses_frozen_worker_template() -> None:
    executor = FakeExecutor()
    _plan_task(
        executor,
        task_key="inspect-runtime",
        task="Inspect the runtime",
        acceptance_criteria="Return the responsible file, function, and evidence.",
    )
    delegation.set_orchestration_executor(executor)
    context = _context()
    token = current_tool_context.set(context)
    try:
        raw = delegation.delegate_task.__wrapped__.__wrapped__
        payload = json.loads(
            await raw(
                task="Inspect the runtime",
                task_key="inspect-runtime",
                acceptance_criteria="Return the responsible file, function, and evidence.",
            )
        )
    finally:
        current_tool_context.reset(token)

    assert payload == {
        "status": "completed",
        "task_key": "inspect-runtime",
        "board_status": "completed",
        "agent_state": "idle",
        "outcome": "succeeded",
        "session_id": "session-1",
        "activation_id": "activation-1",
        "background": False,
        "summary": "done",
        "error": None,
        "evidence": [],
        "acceptance_criteria": "Return the responsible file, function, and evidence.",
        "follow_up": (
            "Report this step as completed with its short summary and current status. "
            "Move to the next planned task; if none remains, summarize. Reuse this "
            "session when its context helps."
        ),
    }
    assert executor.requests[0].inherited_tools == (context.orchestration_worker_template_tools)
    assert executor.requests[0].acceptance_criteria == (
        "Return the responsible file, function, and evidence."
    )


@pytest.mark.asyncio
async def test_foreground_result_returns_child_deliverable_to_parent() -> None:
    executor = FakeExecutor()
    _plan_task(
        executor,
        task_key="inspect-runtime",
        task="Inspect the runtime",
        acceptance_criteria="Return the exact failing branch and remaining question.",
    )
    executor.foreground_result["deliverable"] = (
        "JodaUtils.java: the empty interval path should return no matches."
    )
    executor.foreground_result["unresolved"] = [
        "Which compatibility branch owns the legacy input?"
    ]
    delegation.set_orchestration_executor(executor)
    token = current_tool_context.set(_context())
    try:
        raw = delegation.delegate_task.__wrapped__.__wrapped__
        payload = json.loads(
            await raw(
                task="Inspect the runtime",
                task_key="inspect-runtime",
                acceptance_criteria="Return the exact failing branch and remaining question.",
            )
        )
    finally:
        current_tool_context.reset(token)

    assert payload["summary"] == "done"
    assert payload["deliverable"] == (
        "JodaUtils.java: the empty interval path should return no matches."
    )
    assert payload["unresolved"] == [
        "Which compatibility branch owns the legacy input?"
    ]
    assert "evidence_sufficient" not in payload
    assert payload["follow_up"] == (
        "Report this step as incomplete with its listed unresolved items. Continue "
        'session_id="session-1" only for those items with a new task_key; do not '
        "add unrelated work or create a replacement agent."
    )
    assert "next_action" not in payload


@pytest.mark.asyncio
async def test_single_mode_passes_selected_task_without_full_parent_result() -> None:
    executor = FakeExecutor()
    executor.board_tasks[0].description = "Write a report about the attached figures."
    executor.foreground_result.update(
        summary="Report completed.",
        deliverable="The complete report with all three sections.",
    )
    delegation.set_orchestration_executor(executor)
    context = _context()
    context.orchestration_single_mode = True
    token = current_tool_context.set(context)
    try:
        raw = delegation.delegate_task.__wrapped__.__wrapped__
        payload = json.loads(
            await raw(
                task="Write captions for all three figures and check their source notes",
                task_key="figure-captions",
                acceptance_criteria="Return three accurate captions with source notes.",
            )
        )
    finally:
        current_tool_context.reset(token)

    assert executor.requests[0].task == (
        "Write captions for all three figures and check their source notes"
    )
    assert executor.requests[0].acceptance_criteria == (
        "Return three accurate captions with source notes."
    )
    assert executor.requests[0].profile == "inherit"
    assert payload["task_id"] == "task-1"
    assert payload["summary"] == "Report completed."
    assert "deliverable" not in payload
    assert "follow_up" not in payload
    assert "retry_same_agent" not in payload


@pytest.mark.asyncio
async def test_complex_root_explorer_result_keeps_detail_in_child_session() -> None:
    executor = FakeExecutor()
    _plan_task(
        executor,
        task_key="investigate",
        task="Investigate the failure",
        acceptance_criteria="Report the cause and whether execution can proceed.",
    )
    executor.foreground_result.update(
        summary="Investigation complete; execution can proceed.",
        deliverable="Full source content and detailed technical notes stay in this session.",
        unresolved=[],
    )
    delegation.set_orchestration_executor(executor)
    token = current_tool_context.set(_context())
    try:
        raw = delegation.delegate_task.__wrapped__.__wrapped__
        payload = json.loads(
            await raw(
                task="Investigate the failure",
                task_key="investigate",
                acceptance_criteria="Report the cause and whether execution can proceed.",
                agent="explorer",
            )
        )
    finally:
        current_tool_context.reset(token)

    assert payload["status"] == "completed"
    assert payload["summary"] == "Investigation complete; execution can proceed."
    assert "deliverable" not in payload
    assert "evidence" not in payload
    assert "acceptance_criteria" not in payload
    assert payload["session_id"] == "session-1"
    assert "agent=worker" in payload["follow_up"]


@pytest.mark.asyncio
async def test_foreground_result_accepts_completed_assignment_with_empty_unresolved() -> None:
    executor = FakeExecutor()
    _plan_task(
        executor,
        task_key="inspect-runtime",
        task="Inspect the runtime",
        acceptance_criteria="Return the exact responsible method.",
    )
    executor.foreground_result["unresolved"] = []
    delegation.set_orchestration_executor(executor)
    token = current_tool_context.set(_context())
    try:
        raw = delegation.delegate_task.__wrapped__.__wrapped__
        payload = json.loads(
            await raw(
                task="Inspect the runtime",
                task_key="inspect-runtime",
                acceptance_criteria="Return the exact responsible method.",
            )
        )
    finally:
        current_tool_context.reset(token)

    assert payload["unresolved"] == []
    assert "evidence_sufficient" not in payload
    assert payload["follow_up"].startswith("Report this step as completed")
    assert "next planned task" in payload["follow_up"]
    assert "Compare" not in payload["follow_up"]
    assert "next_action" not in payload


@pytest.mark.asyncio
async def test_foreground_explorer_completion_closes_only_its_assigned_task() -> None:
    executor = FakeExecutor()
    _plan_task(
        executor,
        task_key="locate-root-cause",
        task="Locate the root cause",
        acceptance_criteria="Return the root cause, smallest edit, and expected behavior.",
    )
    executor.foreground_result["unresolved"] = []
    delegation.set_orchestration_executor(executor)
    token = current_tool_context.set(_context())
    try:
        raw = delegation.delegate_task.__wrapped__.__wrapped__
        payload = json.loads(
            await raw(
                task="Locate the root cause",
                task_key="locate-root-cause",
                acceptance_criteria="Return the root cause, smallest edit, and expected behavior.",
                agent="explorer",
            )
        )
    finally:
        current_tool_context.reset(token)

    assert payload["follow_up"].startswith("Report this step as completed")
    assert 'delegate_task with session_id="session-1" and agent=worker' in (
        payload["follow_up"]
    )
    assert "Compare this result" not in payload["follow_up"]


@pytest.mark.asyncio
async def test_complex_root_keeps_parent_selected_agent_and_task() -> None:
    executor = FakeExecutor()
    _plan_task(
        executor,
        task_key="compatibility-question",
        task="Check one concrete unanswered compatibility question",
        acceptance_criteria="Answer the named compatibility question with evidence.",
    )
    executor.prior_child_work = [
        (
            DelegatedTaskRecord(
                task_id="old-task",
                run_id="old-run",
                task_key="locate-fix",
                owner_session_id="old-child",
                description="Locate the fix",
                outcome=TaskOutcome.SUCCEEDED,
                board_status=TaskBoardStatus.COMPLETED,
                result={"evidence_sufficient": True},
            ),
            "old-child",
        )
    ]
    delegation.set_orchestration_executor(executor)
    token = current_tool_context.set(_context())
    try:
        raw = delegation.delegate_task.__wrapped__.__wrapped__
        payload = json.loads(
            await raw(
                task="Check one concrete unanswered compatibility question",
                task_key="compatibility-question",
                acceptance_criteria="Answer the named compatibility question with evidence.",
                agent="explorer",
            )
        )
    finally:
        current_tool_context.reset(token)

    assert payload["status"] == "completed"
    assert executor.requests[0].profile == "explorer"
    assert executor.requests[0].task == (
        "Check one concrete unanswered compatibility question"
    )
    assert executor.requests[0].task_key == "compatibility-question"


@pytest.mark.asyncio
async def test_complex_root_can_continue_relevant_session_at_parent_discretion() -> None:
    executor = FakeExecutor()
    _plan_task(
        executor,
        task_key="prior-schema-work",
        task="Inspect the schema version",
        acceptance_criteria="Return the schema-version answer and source reference.",
        owner_session_id="old-child",
        board_only=False,
    )
    executor.sessions["old-child"] = SimpleNamespace(profile="researcher")
    delegation.set_orchestration_executor(executor)
    token = current_tool_context.set(_context())
    try:
        raw = delegation.delegate_task.__wrapped__.__wrapped__
        payload = json.loads(
            await raw(
                task="Answer the remaining schema-version question only",
                task_key="schema-version-question",
                acceptance_criteria="Return the schema-version answer and source reference.",
                agent="inherit",
                session_id="old-child",
            )
        )
    finally:
        current_tool_context.reset(token)

    assert payload["status"] == "completed"
    assert executor.requests[0].session_id == "old-child"
    assert executor.requests[0].task_key == "schema-version-question"


@pytest.mark.asyncio
async def test_complex_root_preserves_parent_implementation_request() -> None:
    executor = FakeExecutor()
    _plan_task(
        executor,
        task_key="implement-fix",
        task="Implement and verify the minimal fix",
        acceptance_criteria="Return the patch and focused test result.",
    )
    executor.prior_child_work = [
        (
            DelegatedTaskRecord(
                task_id="old-task",
                run_id="old-run",
                task_key="locate-fix",
                owner_session_id="old-child",
                description="Locate the fix",
                outcome=TaskOutcome.SUCCEEDED,
                board_status=TaskBoardStatus.COMPLETED,
                result={"evidence_sufficient": True},
            ),
            "old-child",
        )
    ]
    delegation.set_orchestration_executor(executor)
    token = current_tool_context.set(_context())
    try:
        raw = delegation.delegate_task.__wrapped__.__wrapped__
        await raw(
            task="Implement and verify the minimal fix",
            task_key="implement-fix",
            acceptance_criteria="Return the patch and focused test result.",
            agent="worker",
        )
    finally:
        current_tool_context.reset(token)

    assert executor.requests[0].task == "Implement and verify the minimal fix"
    assert executor.requests[0].task_key == "implement-fix"


@pytest.mark.asyncio
async def test_complex_root_run_is_created_before_delegation() -> None:
    executor = FakeExecutor()
    delegation.set_orchestration_executor(executor)
    context = _context()

    await delegation.ensure_complex_root_run(
        context,
        root_task="The public lookup result must contain no matches.",
    )

    assert len(executor.runs) == 1
    assert executor.runs[0]["run_id"] == "run-1"
    assert executor.runs[0]["root_task"] == ("The public lookup result must contain no matches.")


@pytest.mark.asyncio
async def test_complex_root_synthesis_requires_terminal_delegated_work() -> None:
    executor = FakeExecutor()
    delegation.set_orchestration_executor(executor)
    context = _context()
    executor.run_exists = True

    assert await delegation.complex_root_synthesis_state(context) == ("no_delegated_work")
    executor.work_state = (2, 1)
    assert await delegation.complex_root_synthesis_state(context) == ("delegated_work_pending")
    executor.work_state = (2, 0)
    executor.run_ready = False
    assert await delegation.complex_root_synthesis_state(context) == ("delegated_result_pending")
    executor.run_ready = True
    assert await delegation.complex_root_synthesis_state(context) == "ready"


@pytest.mark.asyncio
async def test_complex_result_notification_counts_its_own_payload_as_observed() -> None:
    executor = FakeExecutor()
    delegation.set_orchestration_executor(executor)
    context = _context()
    executor.run_exists = True
    executor.work_state = (1, 0)
    executor.run_ready = False
    executor.ready_observation = {
        "observing_task_id": "child-task",
        "observing_activation_id": "activation-7",
    }
    context.task_id = "orchestration-result:child-task:activation:activation-7:delivery:2"

    assert await delegation.complex_root_synthesis_state(context) == "ready"


@pytest.mark.asyncio
async def test_background_must_be_explicit() -> None:
    executor = FakeExecutor()
    _plan_task(
        executor,
        task_key="inspect-runtime",
        task="Inspect the runtime",
        acceptance_criteria="Return the runtime owner and evidence.",
    )
    executor.agent_state_value = AgentState.WORKING
    delegation.set_orchestration_executor(executor)
    token = current_tool_context.set(_context())
    try:
        raw = delegation.delegate_task.__wrapped__.__wrapped__
        payload = json.loads(
            await raw(
                task="Inspect the runtime",
                task_key="inspect-runtime",
                acceptance_criteria="Return the runtime owner and evidence.",
                background=True,
            )
        )
    finally:
        current_tool_context.reset(token)

    assert payload == {
        "status": "working",
        "task_key": "inspect-runtime",
        "board_status": "planned",
        "agent_state": "working",
        "outcome": "pending",
        "session_id": "session-1",
        "activation_id": "activation-1",
        "background": True,
        "summary": "",
        "error": None,
        "evidence": [],
        "acceptance_criteria": "Return the runtime owner and evidence.",
    }


def test_delegate_tool_tells_parent_how_to_follow_up_completed_work() -> None:
    spec = get_default_registry().get("delegate_task").spec

    assert "unresolved" in spec.description
    assert "one bounded task" in spec.description
    assert "Reuse session_id for missing results or retryable failures" in spec.description
    assert "runtime may reuse a sufficiently similar same-profile child" in spec.description
    assert "evidence_sufficient" not in spec.description


@pytest.mark.asyncio
async def test_failed_foreground_result_tells_parent_to_reuse_same_session() -> None:
    executor = FakeExecutor()
    _plan_task(
        executor,
        task_key="inspect-runtime",
        task="Inspect the runtime",
        acceptance_criteria="Return the runtime owner and evidence.",
    )
    executor.foreground_result = {
        "status": "failed",
        "summary": "The provider request failed.",
        "error": "temporary network error",
        "retry_same_agent": True,
    }
    delegation.set_orchestration_executor(executor)
    token = current_tool_context.set(_context())
    try:
        raw = delegation.delegate_task.__wrapped__.__wrapped__
        payload = json.loads(
            await raw(
                task="Inspect the runtime",
                task_key="inspect-runtime",
                acceptance_criteria="Return the runtime owner and evidence.",
            )
        )
    finally:
        current_tool_context.reset(token)

    assert payload == {
        "status": "failed",
        "task_key": "inspect-runtime",
        "board_status": "blocked",
        "agent_state": "idle",
        "outcome": "failed",
        "session_id": "session-1",
        "activation_id": "activation-1",
        "background": False,
        "retry_same_agent": True,
        "summary": "The provider request failed.",
        "error": "temporary network error",
        "evidence": [],
        "acceptance_criteria": "Return the runtime owner and evidence.",
        "follow_up": (
            'If this task still needs work, you may retry it with delegate_task using '
            'task_key="inspect-runtime" and session_id="session-1", asking only for the missing '
            'acceptance criterion. Otherwise reroute with the same task_key and '
            'replace_session_id="session-1", or stop if the result is no longer needed.'
        ),
    }


@pytest.mark.asyncio
async def test_unstructured_runtime_failure_defaults_to_same_session_recovery() -> None:
    executor = FakeExecutor()
    _plan_task(
        executor,
        task_key="inspect-runtime",
        task="Inspect the runtime",
        acceptance_criteria="Return the runtime owner and evidence.",
    )
    executor.foreground_result = {
        "error": "router exploded",
        "error_type": "RuntimeError",
    }
    delegation.set_orchestration_executor(executor)
    token = current_tool_context.set(_context())
    try:
        raw = delegation.delegate_task.__wrapped__.__wrapped__
        payload = json.loads(
            await raw(
                task="Inspect the runtime",
                task_key="inspect-runtime",
                acceptance_criteria="Return the runtime owner and evidence.",
            )
        )
    finally:
        current_tool_context.reset(token)

    assert payload == {
        "status": "failed",
        "task_key": "inspect-runtime",
        "board_status": "blocked",
        "agent_state": "idle",
        "outcome": "failed",
        "session_id": "session-1",
        "activation_id": "activation-1",
        "background": False,
        "retry_same_agent": True,
        "summary": "router exploded",
        "error": "router exploded",
        "evidence": [],
        "acceptance_criteria": "Return the runtime owner and evidence.",
        "follow_up": (
            'If this task still needs work, you may retry it with delegate_task using '
            'task_key="inspect-runtime" and session_id="session-1", asking only for the missing '
            'acceptance criterion. Otherwise reroute with the same task_key and '
            'replace_session_id="session-1", or stop if the result is no longer needed.'
        ),
    }


@pytest.mark.asyncio
async def test_child_delegation_reuses_existing_run_identity() -> None:
    executor = FakeExecutor()
    delegation.set_orchestration_executor(executor)
    context = _context()
    context.subagent_depth = 1
    context.orchestration_session_id = "child-session"
    context.orchestration_task_id = "child-task"
    token = current_tool_context.set(context)
    try:
        raw = delegation.delegate_task.__wrapped__.__wrapped__
        await raw(
            task="Inspect a nested scope",
            task_key="nested-inspection",
            acceptance_criteria="Return the nested owner and evidence.",
        )
    finally:
        current_tool_context.reset(token)

    assert executor.runs == []
    assert executor.requests[0].parent_session_id == "child-session"
    assert executor.requests[0].parent_task_id == "child-task"


@pytest.mark.asyncio
async def test_interrupt_requires_current_orchestration_context() -> None:
    executor = FakeExecutor()
    delegation.set_orchestration_executor(executor)
    token = current_tool_context.set(ToolContext(is_owner=True))
    try:
        raw = delegation.interrupt_agent.__wrapped__.__wrapped__
        with pytest.raises(ToolError, match="orchestration context"):
            await raw(
                session_id="session-1",
                activation_id="activation-1",
                reason="loop detected",
            )
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_interrupt_is_scoped_to_current_orchestration_session() -> None:
    executor = FakeExecutor()
    executor.agent_state_value = AgentState.WORKING
    delegation.set_orchestration_executor(executor)
    token = current_tool_context.set(_context())
    try:
        raw = delegation.interrupt_agent.__wrapped__.__wrapped__
        payload = json.loads(
            await raw(
                session_id="session-1",
                activation_id="activation-1",
                reason="loop detected",
            )
        )
    finally:
        current_tool_context.reset(token)

    assert executor.interrupts == [("root-session", "session-1", "activation-1", "loop detected")]
    assert payload["status"] == "working"
