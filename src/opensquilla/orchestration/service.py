"""Application service for durable delegated-task ownership and scheduling."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

import structlog

from opensquilla.orchestration.models import (
    ActivationPhase,
    AgentActivationRecord,
    AgentSessionRecord,
    AgentState,
    DelegatedTaskRecord,
    OrchestrationMode,
    OrchestrationRunRecord,
    SessionLifecycle,
    TaskBoardStatus,
    TaskOutcome,
)
from opensquilla.orchestration.profiles import get_profile, resolve_profile_tools
from opensquilla.orchestration.repository import (
    ConcurrentActivationError,
    OrchestrationRepository,
)
from opensquilla.orchestration.session_recall import SessionRecallEngine
from opensquilla.orchestration.state import derive_agent_state

DEFAULT_MAX_DIRECT_CHILDREN = 8
DEFAULT_MAX_DELEGATION_DEPTH = 3
DEFAULT_MAX_TASK_ATTEMPTS = 3

_RECOVERY_TASK_KEY_MARKERS = frozenset(
    {
        "agent",
        "attempt",
        "continue",
        "continuation",
        "explorer",
        "final",
        "recover",
        "recovery",
        "replacement",
        "researcher",
        "resume",
        "retrieval",
        "retrieve",
        "retried",
        "retry",
        "reviewer",
        "worker",
    }
)
_RECOVERY_TASK_KEY_COUNTER = re.compile(r"(?:attempt|retry|try|v)\d+")
log = structlog.get_logger(__name__)


def _semantic_task_family(task_key: str) -> str:
    """Collapse role/recovery suffixes that must not reset an attempt budget."""

    tokens = re.findall(r"[a-z0-9]+", task_key.casefold())
    while len(tokens) >= 2 and tokens[0] in _RECOVERY_TASK_KEY_MARKERS and tokens[1].isdigit():
        del tokens[:2]
    while tokens and (
        tokens[0] in _RECOVERY_TASK_KEY_MARKERS
        or _RECOVERY_TASK_KEY_COUNTER.fullmatch(tokens[0]) is not None
    ):
        tokens.pop(0)
    while len(tokens) >= 2 and tokens[-1].isdigit() and tokens[-2] in _RECOVERY_TASK_KEY_MARKERS:
        del tokens[-2:]
    while tokens and (
        tokens[-1] in _RECOVERY_TASK_KEY_MARKERS
        or _RECOVERY_TASK_KEY_COUNTER.fullmatch(tokens[-1]) is not None
    ):
        tokens.pop()
    return "-".join(tokens) or task_key.casefold().strip()


class DelegateDisposition(StrEnum):
    CREATED = "created"
    ATTACHED = "attached"
    REUSED = "reused"
    APPENDED = "appended"
    RESTORED = "restored"
    RETRIED = "retried"
    REPLACED = "replaced"


class DelegationCycleError(ValueError):
    pass


class DelegationDepthError(ValueError):
    pass


class DelegationPermissionError(ValueError):
    pass


class DelegationConflictError(ValueError):
    pass


class InterruptFenceError(ValueError):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class DelegateRequest:
    run_id: str
    parent_session_id: str
    parent_task_id: str
    parent_activation_id: str | None = None
    task_key: str
    task: str
    inherited_tools: frozenset[str]
    registered_tools: frozenset[str]
    acceptance_criteria: str = ""
    profile: str | None = None
    background: bool = False
    session_id: str | None = None
    replace_session_id: str | None = None
    runtime_context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class DelegateOutcome:
    disposition: DelegateDisposition
    task: DelegatedTaskRecord
    session: AgentSessionRecord
    activation: AgentActivationRecord
    result: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivationSettlement:
    promoted: AgentActivationRecord | None
    outcome: TaskOutcome
    result: dict[str, Any]
    interrupted_task_ids: tuple[str, ...] = ()


class OrchestrationService:
    def __init__(
        self,
        repository: OrchestrationRepository,
        *,
        max_direct_children: int = DEFAULT_MAX_DIRECT_CHILDREN,
        max_delegation_depth: int = DEFAULT_MAX_DELEGATION_DEPTH,
        max_task_attempts: int = DEFAULT_MAX_TASK_ATTEMPTS,
        session_recall: SessionRecallEngine | None = None,
    ) -> None:
        if max_direct_children < 1:
            raise ValueError("max_direct_children must be positive")
        if max_delegation_depth < 1:
            raise ValueError("max_delegation_depth must be positive")
        if max_task_attempts < 1:
            raise ValueError("max_task_attempts must be positive")
        self.repository = repository
        self.max_direct_children = max_direct_children
        self.max_delegation_depth = max_delegation_depth
        self.max_task_attempts = max_task_attempts
        self.session_recall = session_recall
        self._run_locks: dict[str, asyncio.Lock] = {}

    def _run_lock(self, run_id: str) -> asyncio.Lock:
        lock = self._run_locks.get(run_id)
        if lock is None:
            lock = asyncio.Lock()
            self._run_locks[run_id] = lock
        return lock

    def run_serialization_lock(self, run_id: str) -> asyncio.Lock:
        """Serialize result admission with delegate/retry state transitions."""

        return self._run_lock(run_id)

    async def ensure_run(
        self,
        *,
        run_id: str,
        root_session_id: str,
        root_task_id: str,
        root_task_key: str,
        root_task: str,
        mode: OrchestrationMode,
        worker_template_tools: frozenset[str],
        registered_tools: frozenset[str],
        root_runtime_session_key: str | None = None,
        root_runtime_context: dict[str, Any] | None = None,
    ) -> OrchestrationRunRecord:
        async with self._run_lock(run_id):
            existing = await self.repository.get_run(run_id)
            if existing is not None:
                if (
                    existing.root_session_id != root_session_id
                    or existing.root_task_id != root_task_id
                    or existing.mode is not mode
                    or existing.worker_template_tools != worker_template_tools
                ):
                    raise ValueError("orchestration run identity or configuration changed")
                unknown_worker_tools = worker_template_tools - registered_tools
                if unknown_worker_tools:
                    raise ValueError(
                        "worker template contains unregistered tools: "
                        + ", ".join(sorted(unknown_worker_tools))
                    )
                await self.repository.repair_run_bundle(
                    run=existing,
                    root_session=AgentSessionRecord(
                        session_id=root_session_id,
                        run_id=run_id,
                        profile="inherit",
                        runtime_session_key=root_runtime_session_key or root_session_id,
                        depth=0,
                        effective_tools=worker_template_tools,
                        runtime_context=dict(root_runtime_context or {}),
                    ),
                    root_task=DelegatedTaskRecord(
                        task_id=root_task_id,
                        run_id=run_id,
                        task_key=root_task_key,
                        owner_session_id=root_session_id,
                        description=root_task,
                        effective_tools=worker_template_tools,
                        runtime_context=dict(root_runtime_context or {}),
                    ),
                )
                return existing
            return await self.start_run(
                run_id=run_id,
                root_session_id=root_session_id,
                root_task_id=root_task_id,
                root_task_key=root_task_key,
                root_task=root_task,
                mode=mode,
                worker_template_tools=worker_template_tools,
                registered_tools=registered_tools,
                root_runtime_session_key=root_runtime_session_key,
                root_runtime_context=root_runtime_context,
            )

    async def start_run(
        self,
        *,
        run_id: str,
        root_session_id: str,
        root_task_id: str,
        root_task_key: str,
        root_task: str,
        mode: OrchestrationMode,
        worker_template_tools: frozenset[str],
        registered_tools: frozenset[str],
        root_runtime_session_key: str | None = None,
        root_runtime_context: dict[str, Any] | None = None,
    ) -> OrchestrationRunRecord:
        if not root_task.strip():
            raise ValueError("root task must not be empty")
        unknown_worker_tools = worker_template_tools - registered_tools
        if unknown_worker_tools:
            raise ValueError(
                "worker template contains unregistered tools: "
                + ", ".join(sorted(unknown_worker_tools))
            )
        run = OrchestrationRunRecord(
            run_id=run_id,
            root_session_id=root_session_id,
            root_task_id=root_task_id,
            mode=mode,
            worker_template_tools=worker_template_tools,
        )
        root_session = AgentSessionRecord(
            session_id=root_session_id,
            run_id=run_id,
            profile="inherit",
            runtime_session_key=root_runtime_session_key or root_session_id,
            depth=0,
            effective_tools=worker_template_tools,
            runtime_context=dict(root_runtime_context or {}),
        )
        root_task_record = DelegatedTaskRecord(
            task_id=root_task_id,
            run_id=run_id,
            task_key=root_task_key,
            owner_session_id=root_session_id,
            description=root_task,
            board_status=TaskBoardStatus.WORKING,
            effective_tools=worker_template_tools,
            runtime_context=dict(root_runtime_context or {}),
        )
        await self.repository.create_run_bundle(
            run=run,
            root_session=root_session,
            root_task=root_task_record,
        )
        return run

    async def delegate(self, request: DelegateRequest) -> DelegateOutcome:
        if not request.task.strip():
            raise ValueError("delegated task must not be empty")
        if not request.task_key.strip():
            raise ValueError("task_key must not be empty")
        if not request.acceptance_criteria.strip():
            raise ValueError("acceptance criteria must not be empty")
        if request.session_id is not None and request.replace_session_id is not None:
            raise ValueError("session_id and replace_session_id are mutually exclusive")
        if request.profile is not None:
            get_profile(request.profile)
        task_key = request.task_key.strip()
        acceptance_criteria = request.acceptance_criteria.strip()
        if task_key != request.task_key or acceptance_criteria != request.acceptance_criteria:
            request = replace(
                request,
                task_key=task_key,
                acceptance_criteria=acceptance_criteria,
            )

        async with self._run_lock(request.run_id):
            parent_session = await self.repository.get_session(request.parent_session_id)
            parent_attachment = await self.repository.get_session_attachment(
                run_id=request.run_id,
                session_id=request.parent_session_id,
            )
            if parent_session is None or parent_attachment is None:
                raise ValueError("parent session does not belong to orchestration run")
            if request.parent_activation_id is not None:
                parent_activation = await self.repository.current_activation_for_session(
                    request.parent_session_id
                )
                if (
                    parent_activation is None
                    or parent_activation.activation_id != request.parent_activation_id
                ):
                    raise DelegationConflictError(
                        "parent activation generation does not match the current session"
                    )
                if parent_activation.phase in {
                    ActivationPhase.STOPPING,
                    ActivationPhase.RELEASED,
                }:
                    raise DelegationConflictError("parent activation is stopping")
            if parent_attachment.depth >= self.max_delegation_depth:
                raise DelegationDepthError(
                    f"maximum delegation depth {self.max_delegation_depth} exceeded"
                )
            if not get_profile(parent_session.profile).may_delegate:
                raise DelegationPermissionError(
                    f"agent profile cannot delegate: {parent_session.profile}"
                )
            if request.background and parent_attachment.depth > 0:
                raise DelegationPermissionError(
                    "background delegation is currently supported only from the root agent"
                )

            if parent_attachment.depth == 0 and parent_session.runtime_context.get(
                "single_agent_mode"
            ):
                run_tasks = await self.repository.list_task_board(request.run_id)
                if any(
                    task.parent_task_id == request.parent_task_id
                    and not task.board_only
                    and task.outcome is not TaskOutcome.PENDING
                    for task in run_tasks
                ):
                    raise DelegationConflictError(
                        "Complete Task Mode is one-shot within a user query; "
                        "wait for a new user query before delegating again"
                    )

            ancestor_keys = await self.repository.ancestor_task_keys(request.parent_task_id)
            if request.task_key in ancestor_keys:
                raise DelegationCycleError(
                    f"delegated task repeats ancestor key: {request.task_key}"
                )

            if request.replace_session_id is not None:
                return await self._replace_session(request, parent_session)

            existing = await self.repository.find_task_by_key(
                request.run_id,
                request.task_key,
            )
            if existing is not None:
                if (
                    not existing.board_only
                    and existing.acceptance_criteria != request.acceptance_criteria
                ):
                    raise DelegationConflictError(
                        "task acceptance criteria changed; reuse the stored criteria or "
                        "create a genuinely different task key"
                    )
                if existing.board_only:
                    if existing.parent_task_id != request.parent_task_id:
                        raise DelegationConflictError(
                            "planned task belongs under a different parent task"
                        )
                    if existing.owner_session_id != request.parent_session_id:
                        raise DelegationConflictError(
                            "planned task belongs to another agent session"
                        )
                    if (
                        existing.outcome is not TaskOutcome.PENDING
                        or existing.board_status
                        not in {TaskBoardStatus.PLANNED, TaskBoardStatus.WORKING}
                    ):
                        raise DelegationConflictError(
                            "task-board item is closed; reopen it before delegation"
                        )
                    if request.session_id is not None:
                        return await self._continue_session(
                            request,
                            parent_session,
                            planned_task=existing,
                        )
                    recalled_request = await self._automatic_recall_request(
                        request,
                        parent_session=parent_session,
                    )
                    if recalled_request is not None:
                        return await self._continue_session(
                            recalled_request,
                            parent_session,
                            planned_task=existing,
                        )
                    return await self._assign_board_task(
                        request,
                        parent_session=parent_session,
                        parent_depth=parent_attachment.depth,
                        task=existing,
                    )
                session = await self.repository.get_session(existing.owner_session_id)
                session_attachment = await self.repository.get_session_attachment(
                    run_id=request.run_id,
                    session_id=existing.owner_session_id,
                )
                activation = await self.repository.latest_activation_for_task(existing.task_id)
                if session is None or session_attachment is None:
                    raise RuntimeError("existing delegated task is missing its attached session")
                if session_attachment.parent_session_id != request.parent_session_id:
                    raise DelegationConflictError("existing task belongs to another direct parent")
                if activation is None and existing.outcome is TaskOutcome.PENDING:
                    activation = await self.repository.current_activation_for_session(
                        existing.owner_session_id
                    )
                if activation is None and existing.retry_of_activation_id is not None:
                    activation = await self.repository.get_activation(
                        existing.retry_of_activation_id
                    )
                if activation is None:
                    raise RuntimeError("existing delegated task has no execution attempt")
                if existing.outcome is TaskOutcome.SUCCEEDED:
                    return DelegateOutcome(
                        disposition=DelegateDisposition.REUSED,
                        task=existing,
                        session=session,
                        activation=activation,
                        result=existing.result,
                    )
                if existing.outcome is TaskOutcome.PENDING:
                    if (
                        request.session_id is not None
                        and request.session_id != existing.owner_session_id
                    ):
                        raise DelegationConflictError(
                            "task key is already owned by another agent session"
                        )
                    if not request.background and existing.background:
                        existing = await self.repository.promote_pending_task_to_foreground(
                            existing.task_id
                        )
                    return DelegateOutcome(
                        disposition=DelegateDisposition.ATTACHED,
                        task=existing,
                        session=session,
                        activation=activation,
                    )

                if (
                    isinstance(existing.result, dict)
                    and existing.result.get("retry_same_agent") is False
                ):
                    raise DelegationConflictError(
                        "failed task returned retry_same_agent=false; do not restore its "
                        "session; reroute bounded remaining work to a suitable profile"
                    )
                retry_session_id = request.session_id or existing.owner_session_id
                if retry_session_id != existing.owner_session_id:
                    raise DelegationConflictError(
                        "failed task can only be retried in its owning session or replaced"
                    )
                return await self._retry_in_session(
                    request,
                    session=session,
                    task=existing,
                    previous_activation=activation,
                )

            await self._require_semantic_task_family_capacity(
                run_id=request.run_id,
                task_key=request.task_key,
            )

            if request.session_id is not None:
                return await self._continue_session(request, parent_session)

            recalled_request = await self._automatic_recall_request(
                request,
                parent_session=parent_session,
            )
            if recalled_request is not None:
                return await self._continue_session(recalled_request, parent_session)

            profile = get_profile(request.profile or "inherit")
            effective_tools = resolve_profile_tools(
                inherited=request.inherited_tools,
                registered=request.registered_tools,
                preset=profile,
            )
            session = AgentSessionRecord(
                session_id=self.repository.allocate_id("agent_session"),
                run_id=request.run_id,
                profile=profile.name,
                runtime_session_key=self._new_runtime_session_key(parent_session),
                parent_session_id=request.parent_session_id,
                depth=parent_attachment.depth + 1,
                effective_tools=effective_tools,
                runtime_context=dict(request.runtime_context or parent_session.runtime_context),
            )
            task = DelegatedTaskRecord(
                task_id=self.repository.allocate_id("delegated_task"),
                run_id=request.run_id,
                task_key=request.task_key,
                owner_session_id=session.session_id,
                description=request.task,
                acceptance_criteria=request.acceptance_criteria,
                parent_task_id=request.parent_task_id,
                background=request.background,
                effective_tools=effective_tools,
                runtime_context=dict(request.runtime_context),
            )
            activation = AgentActivationRecord(
                activation_id=self.repository.allocate_id("agent_activation"),
                session_id=session.session_id,
                task_id=task.task_id,
                phase=ActivationPhase.STARTING,
                route_required=True,
            )
            try:
                activation = await self.repository.create_delegation_bundle(
                    session=session,
                    task=task,
                    activation=activation,
                    max_direct_children=self.max_direct_children,
                    parent_activation_id=request.parent_activation_id,
                    require_live_parent=parent_attachment.depth > 0,
                )
            except ConcurrentActivationError as exc:
                raise DelegationConflictError(str(exc)) from exc
            return DelegateOutcome(
                disposition=DelegateDisposition.CREATED,
                task=task,
                session=session,
                activation=activation,
            )

    async def _assign_board_task(
        self,
        request: DelegateRequest,
        *,
        parent_session: AgentSessionRecord,
        parent_depth: int,
        task: DelegatedTaskRecord,
    ) -> DelegateOutcome:
        profile = get_profile(request.profile or "inherit")
        effective_tools = resolve_profile_tools(
            inherited=request.inherited_tools,
            registered=request.registered_tools,
            preset=profile,
        )
        session = AgentSessionRecord(
            session_id=self.repository.allocate_id("agent_session"),
            run_id=request.run_id,
            profile=profile.name,
            runtime_session_key=self._new_runtime_session_key(parent_session),
            parent_session_id=request.parent_session_id,
            depth=parent_depth + 1,
            effective_tools=effective_tools,
            runtime_context=dict(request.runtime_context or parent_session.runtime_context),
        )
        activation = AgentActivationRecord(
            activation_id=self.repository.allocate_id("agent_activation"),
            session_id=session.session_id,
            task_id=task.task_id,
            phase=ActivationPhase.STARTING,
            route_required=True,
        )
        try:
            assigned, activation = await self.repository.activate_board_task_bundle(
                session=session,
                task_id=task.task_id,
                description=request.task,
                acceptance_criteria=request.acceptance_criteria,
                background=request.background,
                effective_tools=effective_tools,
                runtime_context=dict(request.runtime_context),
                activation=activation,
                max_direct_children=self.max_direct_children,
                parent_activation_id=request.parent_activation_id,
                require_live_parent=parent_depth > 0,
            )
        except (ConcurrentActivationError, ValueError) as exc:
            raise DelegationConflictError(str(exc)) from exc
        return DelegateOutcome(
            disposition=DelegateDisposition.CREATED,
            task=assigned,
            session=session,
            activation=activation,
        )

    async def add_task_board_item(
        self,
        *,
        run_id: str,
        caller_session_id: str,
        parent_task_id: str,
        task_key: str,
        description: str,
        acceptance_criteria: str | None = None,
    ) -> DelegatedTaskRecord:
        task_key = task_key.strip()
        if not task_key or not description.strip() or not str(acceptance_criteria or "").strip():
            raise ValueError(
                "task_key, description, and acceptance criteria must not be empty"
            )
        async with self._run_lock(run_id):
            parent = await self.repository.get_task(parent_task_id)
            if parent is None or parent.run_id != run_id:
                raise ValueError("parent task does not belong to orchestration run")
            if parent.owner_session_id != caller_session_id:
                raise DelegationPermissionError("agent may add tasks only below its own task")
            existing = await self.repository.find_task_by_key(run_id, task_key)
            if existing is not None:
                if (
                    existing.parent_task_id == parent_task_id
                    and existing.description == description.strip()
                    and existing.acceptance_criteria == str(acceptance_criteria).strip()
                ):
                    return existing
                raise ValueError("task key already exists with a different assignment")
            task = DelegatedTaskRecord(
                task_id=self.repository.allocate_id("delegated_task"),
                run_id=run_id,
                task_key=task_key,
                owner_session_id=caller_session_id,
                description=description.strip(),
                parent_task_id=parent_task_id,
                acceptance_criteria=str(acceptance_criteria).strip(),
                board_only=True,
            )
            return await self.repository.create_task(task)

    async def update_task_board_item(
        self,
        *,
        run_id: str,
        caller_session_id: str,
        task_key: str,
        action: str,
        evidence: str | None = None,
    ) -> DelegatedTaskRecord:
        async with self._run_lock(run_id):
            target = await self.repository.find_task_by_key(run_id, task_key.strip())
            if target is None:
                raise ValueError(f"task not found: {task_key}")
            parent = (
                await self.repository.get_task(target.parent_task_id)
                if target.parent_task_id is not None
                else None
            )
            if target.owner_session_id != caller_session_id and (
                parent is None or parent.owner_session_id != caller_session_id
            ):
                raise DelegationPermissionError(
                    "agent may update only its own task or a direct child task"
                )
            transitions = {
                "start": TaskBoardStatus.WORKING,
                "complete": TaskBoardStatus.COMPLETED,
                "block": TaskBoardStatus.BLOCKED,
                "reopen": TaskBoardStatus.PLANNED,
            }
            try:
                status = transitions[action]
            except KeyError as exc:
                raise ValueError(f"unsupported task-board action: {action}") from exc
            if action == "start" and target.board_status not in {
                TaskBoardStatus.PLANNED,
                TaskBoardStatus.WORKING,
            }:
                raise ValueError("closed task must be reopened before it can start")
            if action == "reopen" and target.board_status not in {
                TaskBoardStatus.COMPLETED,
                TaskBoardStatus.BLOCKED,
            }:
                raise ValueError("only completed or blocked tasks can be reopened")
            return await self.repository.update_task_board_item(
                target.task_id,
                status=status,
                evidence=(evidence.strip() if evidence else None),
                reopen=action == "reopen",
            )

    async def _continue_session(
        self,
        request: DelegateRequest,
        parent_session: AgentSessionRecord,
        *,
        planned_task: DelegatedTaskRecord | None = None,
    ) -> DelegateOutcome:
        assert request.session_id is not None
        session = await self.repository.get_session(request.session_id)
        if session is None:
            raise DelegationConflictError("continued session does not exist")
        parent_attachment = await self.repository.get_session_attachment(
            run_id=request.run_id,
            session_id=parent_session.session_id,
        )
        if parent_attachment is None:
            raise DelegationConflictError("parent session is not attached to this run")
        attachment = await self.repository.get_session_attachment(
            run_id=request.run_id,
            session_id=session.session_id,
        )
        if attachment is not None and attachment.parent_session_id != parent_session.session_id:
            raise DelegationConflictError(
                "continued session is attached under a different parent in this run"
            )
        if attachment is None:
            origin_parent = (
                await self.repository.get_session(session.parent_session_id)
                if session.parent_session_id is not None
                else None
            )
            origin_parent_key = (
                origin_parent.runtime_session_key or origin_parent.session_id
                if origin_parent is not None
                else None
            )
            current_parent_key = parent_session.runtime_session_key or parent_session.session_id
            if origin_parent_key != current_parent_key:
                raise DelegationConflictError(
                    "continued session belongs to a different persistent parent"
                )
        profile = get_profile(request.profile or session.profile)
        effective_tools = resolve_profile_tools(
            inherited=request.inherited_tools,
            registered=request.registered_tools,
            preset=profile,
        )
        session = replace(
            session,
            profile=profile.name,
            effective_tools=effective_tools,
        )

        task = DelegatedTaskRecord(
            task_id=(
                planned_task.task_id
                if planned_task is not None
                else self.repository.allocate_id("delegated_task")
            ),
            run_id=request.run_id,
            task_key=request.task_key,
            owner_session_id=session.session_id,
            description=request.task,
            acceptance_criteria=request.acceptance_criteria,
            parent_task_id=request.parent_task_id,
            background=request.background,
            effective_tools=effective_tools,
            runtime_context=dict(request.runtime_context),
        )
        candidate = AgentActivationRecord(
            activation_id=self.repository.allocate_id("agent_activation"),
            session_id=session.session_id,
            task_id=task.task_id,
            phase=ActivationPhase.STARTING,
        )
        try:
            current, appended = await self.repository.continue_session_bundle(
                session=session,
                task=task,
                activation=candidate,
                parent_session_id=parent_session.session_id,
                depth=parent_attachment.depth + 1,
                max_direct_children=self.max_direct_children,
                parent_activation_id=request.parent_activation_id,
                require_live_parent=parent_attachment.depth > 0,
                planned_task_id=(planned_task.task_id if planned_task is not None else None),
            )
        except RuntimeError as exc:
            raise DelegationConflictError(str(exc)) from exc
        if appended:
            return DelegateOutcome(
                disposition=DelegateDisposition.APPENDED,
                task=task,
                session=session,
                activation=current,
            )
        return DelegateOutcome(
            disposition=DelegateDisposition.RESTORED,
            task=task,
            session=session,
            activation=current,
        )

    async def _retry_in_session(
        self,
        request: DelegateRequest,
        *,
        session: AgentSessionRecord,
        task: DelegatedTaskRecord,
        previous_activation: AgentActivationRecord,
    ) -> DelegateOutcome:
        current = await self.repository.current_activation_for_session(session.session_id)
        if current is not None:
            raise DelegationConflictError("retry session still has a live activation")
        await self._require_task_attempt_capacity(task.task_id)
        parent_attachment = await self.repository.get_session_attachment(
            run_id=request.run_id,
            session_id=request.parent_session_id,
        )
        if parent_attachment is None:
            raise DelegationConflictError("retry parent is not attached to this run")
        activation = AgentActivationRecord(
            activation_id=self.repository.allocate_id("agent_activation"),
            session_id=session.session_id,
            task_id=task.task_id,
            phase=ActivationPhase.STARTING,
        )
        effective_tools = resolve_profile_tools(
            inherited=request.inherited_tools,
            registered=request.registered_tools,
            preset=get_profile(session.profile),
        )
        retried, activation = await self.repository.retry_task_bundle(
            task_id=task.task_id,
            session=session,
            session_is_new=False,
            description=request.task,
            parent_task_id=request.parent_task_id,
            retry_of_activation_id=previous_activation.activation_id,
            replaces_session_id=None,
            background=request.background,
            effective_tools=effective_tools,
            runtime_context=dict(request.runtime_context),
            activation=activation,
            run_id=request.run_id,
            parent_session_id=request.parent_session_id,
            depth=parent_attachment.depth + 1,
            max_direct_children=self.max_direct_children,
            parent_activation_id=request.parent_activation_id,
            require_live_parent=parent_attachment.depth > 0,
        )
        return DelegateOutcome(
            disposition=DelegateDisposition.RETRIED,
            task=retried,
            session=session,
            activation=activation,
        )

    async def _replace_session(
        self,
        request: DelegateRequest,
        parent_session: AgentSessionRecord,
    ) -> DelegateOutcome:
        assert request.replace_session_id is not None
        source = await self.repository.get_session(request.replace_session_id)
        source_attachment = await self.repository.get_session_attachment(
            run_id=request.run_id,
            session_id=request.replace_session_id,
        )
        if (
            source is None
            or source_attachment is None
            or (source_attachment.parent_session_id != parent_session.session_id)
        ):
            raise DelegationConflictError(
                "replacement source is not a direct child in this orchestration run"
            )
        if await self.repository.current_activation_for_session(source.session_id) is not None:
            raise DelegationConflictError("replacement source still has a live activation")
        task = await self.repository.find_task_by_key(request.run_id, request.task_key)
        if task is None or task.owner_session_id != source.session_id:
            raise DelegationConflictError(
                "replacement must retain a task owned by the source session"
            )
        if task.acceptance_criteria != request.acceptance_criteria:
            raise DelegationConflictError(
                "task acceptance criteria changed; reuse the stored criteria"
            )
        if task.outcome not in {
            TaskOutcome.FAILED,
            TaskOutcome.INTERRUPTED,
            TaskOutcome.TIMED_OUT,
        }:
            raise DelegationConflictError("only an unsuccessful terminal task can be replaced")
        await self._require_task_attempt_capacity(task.task_id)
        previous_activation = await self.repository.latest_activation_for_task(task.task_id)
        if previous_activation is None and task.retry_of_activation_id is not None:
            previous_activation = await self.repository.get_activation(task.retry_of_activation_id)
        if previous_activation is None:
            raise RuntimeError("replacement task has no prior activation")

        profile = get_profile(request.profile or source.profile)
        effective_tools = resolve_profile_tools(
            inherited=request.inherited_tools,
            registered=request.registered_tools,
            preset=profile,
        )
        replacement = AgentSessionRecord(
            session_id=self.repository.allocate_id("agent_session"),
            run_id=request.run_id,
            profile=profile.name,
            runtime_session_key=self._new_runtime_session_key(parent_session),
            parent_session_id=parent_session.session_id,
            depth=source_attachment.depth,
            effective_tools=effective_tools,
            runtime_context=dict(request.runtime_context or parent_session.runtime_context),
            checkpoint={
                "replaces_session_id": source.session_id,
                "source_checkpoint": source.checkpoint,
                "handoff": request.task,
            },
        )
        activation = AgentActivationRecord(
            activation_id=self.repository.allocate_id("agent_activation"),
            session_id=replacement.session_id,
            task_id=task.task_id,
            phase=ActivationPhase.STARTING,
            route_required=True,
        )
        retried, activation = await self.repository.retry_task_bundle(
            task_id=task.task_id,
            session=replacement,
            session_is_new=True,
            description=request.task,
            parent_task_id=request.parent_task_id,
            retry_of_activation_id=previous_activation.activation_id,
            replaces_session_id=source.session_id,
            background=request.background,
            effective_tools=effective_tools,
            runtime_context=dict(request.runtime_context),
            activation=activation,
            run_id=request.run_id,
            parent_session_id=parent_session.session_id,
            depth=source_attachment.depth,
            max_direct_children=self.max_direct_children,
            parent_activation_id=request.parent_activation_id,
            require_live_parent=source_attachment.depth > 1,
        )
        return DelegateOutcome(
            disposition=DelegateDisposition.REPLACED,
            task=retried,
            session=replacement,
            activation=activation,
        )

    async def _require_task_attempt_capacity(self, task_id: str) -> None:
        attempts = await self.repository.activation_count_for_task(task_id)
        if attempts >= self.max_task_attempts:
            raise DelegationConflictError(
                f"task execution attempt limit {self.max_task_attempts} reached; "
                "do not retry the same task_key; reroute with a new, "
                "non-overlapping task_key or synthesize the available results"
            )

    async def _automatic_recall_request(
        self,
        request: DelegateRequest,
        *,
        parent_session: AgentSessionRecord,
    ) -> DelegateRequest | None:
        """Select a same-profile persistent child when task similarity is sufficient."""

        if self.session_recall is None:
            return None
        profile = request.profile or "inherit"
        match = await self.session_recall.find_reusable_session(
            self.repository,
            parent_runtime_session_key=(
                parent_session.runtime_session_key or parent_session.session_id
            ),
            profile=profile,
            task=request.task,
            acceptance_criteria=request.acceptance_criteria,
            ignore_paths=bool(request.runtime_context.get("single_agent_mode")),
        )
        if match is None:
            return None
        log.info(
            "subagent_session_recalled",
            run_id=request.run_id,
            task_key=request.task_key,
            profile=profile,
            session_id=match.session_id,
            matched_task_id=match.task_id,
            score=round(match.score, 4),
        )
        return replace(request, profile=profile, session_id=match.session_id)

    async def can_retry_same_agent(self, *, task_id: str, session_id: str) -> bool:
        """Return whether another activation may reuse this persistent child."""

        task = await self.repository.get_task(task_id)
        session = await self.repository.get_session(session_id)
        if (
            task is None
            or session is None
            or task.owner_session_id != session.session_id
            or session.lifecycle is SessionLifecycle.ARCHIVED
            or task.outcome
            not in {
                TaskOutcome.FAILED,
                TaskOutcome.INTERRUPTED,
                TaskOutcome.TIMED_OUT,
            }
        ):
            return False
        if await self.repository.current_activation_for_session(session.session_id) is not None:
            return False
        attempts = await self.repository.activation_count_for_task(task.task_id)
        return attempts < self.max_task_attempts

    async def _require_semantic_task_family_capacity(
        self,
        *,
        run_id: str,
        task_key: str,
    ) -> None:
        family = _semantic_task_family(task_key)
        counts = await self.repository.activation_counts_by_task_key(run_id)
        attempts = sum(
            count for existing_key, count in counts if _semantic_task_family(existing_key) == family
        )
        if attempts >= self.max_task_attempts:
            raise DelegationConflictError(
                f"semantic task family {family!r} execution attempt limit "
                f"{self.max_task_attempts} reached; changing task_key, profile, or "
                "session does not reset it; delegate materially smaller "
                "non-overlapping work or synthesize the available results"
            )

    def _new_runtime_session_key(self, parent_session: AgentSessionRecord) -> str:
        parent_key = parent_session.runtime_session_key or parent_session.session_id
        agent_id = "main"
        if parent_key.startswith("agent:"):
            parts = parent_key.split(":")
            if len(parts) >= 2 and parts[1]:
                agent_id = parts[1]
        suffix = self.repository.allocate_id("child").replace("_", "-")[-32:]
        return f"agent:{agent_id}:subagent:{suffix}"

    async def complete(
        self,
        activation_id: str,
        *,
        result: dict[str, Any],
    ) -> AgentActivationRecord | None:
        return await self.finish_activation(
            activation_id,
            outcome=TaskOutcome.SUCCEEDED,
            result=result,
        )

    async def finish_activation(
        self,
        activation_id: str,
        *,
        outcome: TaskOutcome,
        result: dict[str, Any],
        continue_session: bool = True,
    ) -> AgentActivationRecord | None:
        settlement = await self.settle_activation(
            activation_id,
            outcome=outcome,
            result=result,
            continue_session=continue_session,
        )
        return settlement.promoted

    async def settle_activation(
        self,
        activation_id: str,
        *,
        outcome: TaskOutcome,
        result: dict[str, Any],
        continue_session: bool = True,
    ) -> ActivationSettlement:
        if outcome is TaskOutcome.PENDING:
            raise ValueError("terminal task outcome is required")
        activation = await self.repository.get_activation(activation_id)
        if activation is None:
            raise ValueError(f"activation not found: {activation_id}")
        session = await self.repository.get_session(activation.session_id)
        if session is None:
            raise RuntimeError("activation session is missing")
        task = await self.repository.get_task(activation.task_id)
        if task is None:
            raise RuntimeError("activation task is missing")

        async with self._run_lock(task.run_id):
            current = await self.repository.get_activation(activation_id)
            if current is None:
                raise ValueError(f"activation not found: {activation_id}")
            if current.phase is ActivationPhase.STOPPING:
                continue_session = False
                if outcome is TaskOutcome.SUCCEEDED:
                    outcome = TaskOutcome.INTERRUPTED
                    result = {"reason": current.terminal_reason or "activation interrupted"}
            promoted, interrupted_task_ids = await self.repository.finish_activation_bundle(
                activation_id,
                outcome=outcome,
                result=result,
                next_activation_id=self.repository.allocate_id("agent_activation"),
                continue_session=continue_session,
            )
            return ActivationSettlement(
                promoted=promoted,
                outcome=outcome,
                result=result,
                interrupted_task_ids=interrupted_task_ids,
            )

    async def acknowledge_result_delivery(
        self,
        task_id: str,
        *,
        activation_id: str | None = None,
        owner: str | None = None,
    ) -> bool:
        task = await self.repository.get_task(task_id)
        if task is None:
            raise ValueError(f"task not found: {task_id}")
        async with self._run_lock(task.run_id):
            await self.repository.acknowledge_child_result_message(
                task_id,
                activation_id=activation_id,
                owner=owner,
            )
            run = await self.repository.get_run(task.run_id)
            if (
                run is not None
                and not run.final_synthesis_completed
                and await self.repository.run_ready_for_final_synthesis(task.run_id)
            ):
                await self.repository.mark_final_synthesis_completed(task.run_id)
            return await self.repository.complete_run_if_eligible(task.run_id)

    async def observe_child_result(
        self,
        task_id: str,
        *,
        activation_id: str,
    ) -> None:
        """Record a result returned directly by foreground reuse as observed."""

        task = await self.repository.get_task(task_id)
        if task is None:
            raise ValueError(f"task not found: {task_id}")
        async with self._run_lock(task.run_id):
            await self.repository.observe_child_result_message(
                task_id,
                activation_id=activation_id,
            )

    async def mark_final_synthesis_completed(self, run_id: str) -> bool:
        async with self._run_lock(run_id):
            run = await self.repository.get_run(run_id)
            if run is None:
                raise ValueError(f"orchestration run not found: {run_id}")
            if run.final_synthesis_completed:
                return False
            await self.repository.mark_final_synthesis_completed(run_id)
            return await self.repository.complete_run_if_eligible(run_id)

    async def terminate_run_without_synthesis(
        self,
        run_id: str,
        *,
        root_outcome: TaskOutcome,
        result: dict[str, Any],
    ) -> bool:
        async with self._run_lock(run_id):
            return await self.repository.terminate_run_without_synthesis(
                run_id,
                root_outcome=root_outcome,
                result=result,
            )

    async def agent_state(self, *, run_id: str, session_id: str) -> AgentState:
        session = await self.repository.get_session(session_id)
        attachment = await self.repository.get_session_attachment(
            run_id=run_id,
            session_id=session_id,
        )
        if session is None or attachment is None:
            raise ValueError("agent session is not attached to orchestration run")
        activation = await self.repository.current_activation_for_session(session_id)
        if activation is not None:
            activation_task = await self.repository.get_task(activation.task_id)
            if activation_task is None or activation_task.run_id != run_id:
                activation = None
        return derive_agent_state(
            session,
            activation,
            inbox_count=len(await self.repository.list_pending_messages(session_id)),
            live_descendants=await self.repository.live_descendant_count(
                session_id,
                run_id=run_id,
            ),
        )

    async def request_interrupt(
        self,
        *,
        caller_session_id: str,
        session_id: str,
        activation_id: str,
        reason: str,
    ) -> list[AgentActivationRecord]:
        if not reason.strip():
            raise ValueError("interrupt reason must not be empty")
        session = await self.repository.get_session(session_id)
        if session is None:
            raise InterruptFenceError("interrupt target session does not exist")
        caller = await self.repository.get_session(caller_session_id)
        current = await self.repository.current_activation_for_session(session_id)
        if current is None or current.activation_id != activation_id:
            raise InterruptFenceError(
                "activation generation does not match the current session activation"
            )
        task = await self.repository.get_task(current.task_id)
        if task is None:
            raise InterruptFenceError("interrupt target task does not exist")
        caller_attachment = await self.repository.get_session_attachment(
            run_id=task.run_id,
            session_id=caller_session_id,
        )
        target_attachment = await self.repository.get_session_attachment(
            run_id=task.run_id,
            session_id=session_id,
        )
        if caller is None or caller_attachment is None or target_attachment is None:
            raise InterruptFenceError("interrupt caller does not belong to target run")
        if not await self.repository.session_is_in_subtree(
            ancestor_session_id=caller_session_id,
            descendant_session_id=session_id,
            run_id=task.run_id,
        ):
            raise InterruptFenceError("interrupt target is outside caller subtree")
        async with self._run_lock(task.run_id):
            try:
                return await self.repository.mark_subtree_stopping(
                    session_id,
                    run_id=task.run_id,
                    expected_activation_id=activation_id,
                    reason=reason,
                )
            except ConcurrentActivationError as exc:
                raise InterruptFenceError(
                    "activation generation does not match the current session activation"
                ) from exc


__all__ = [
    "ActivationSettlement",
    "DEFAULT_MAX_DELEGATION_DEPTH",
    "DEFAULT_MAX_DIRECT_CHILDREN",
    "DelegateDisposition",
    "DelegateOutcome",
    "DelegateRequest",
    "DelegationConflictError",
    "DelegationCycleError",
    "DelegationDepthError",
    "DelegationPermissionError",
    "InterruptFenceError",
    "OrchestrationService",
]
