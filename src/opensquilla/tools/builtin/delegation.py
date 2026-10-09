"""The complete model-facing delegated-agent control surface."""

from __future__ import annotations

import json
from typing import Any

from opensquilla.engine.subagent_delegation import (
    completed_task_follow_up,
    failed_task_follow_up,
    is_complex_root,
)
from opensquilla.orchestration.executor import OrchestrationExecutor
from opensquilla.orchestration.models import DelegatedTaskRecord, OrchestrationMode
from opensquilla.orchestration.profiles import PRESET_PROFILES
from opensquilla.orchestration.service import DelegateRequest
from opensquilla.tools.registry import ToolRegistry, get_default_registry, tool
from opensquilla.tools.run_mode import current_run_mode, full_host_access_for_context
from opensquilla.tools.types import (
    PlanAccess,
    ToolContext,
    ToolError,
    current_tool_context,
)

_executor: OrchestrationExecutor | None = None
_registry: ToolRegistry | None = None

_AGENT_PROFILE_PARAMETER_DESCRIPTION = (
    "Optionally choose one fixed child capability profile: "
    + "; ".join(
        f"{profile.name} {profile.capability_summary}" for profile in PRESET_PROFILES.values()
    )
    + ". Omit it to keep an existing session's profile, or to use inherit for a new session. "
    "With session_id, explicitly select worker when the same child should execute from its "
    "retained investigation context. Presets filter inherited capabilities and never widen "
    "current authority."
)

def set_orchestration_executor(
    executor: OrchestrationExecutor | None,
    *,
    registry: ToolRegistry | None = None,
) -> None:
    """Bind the process-lifetime executor after gateway services are ready."""

    global _executor, _registry
    _executor = executor
    _registry = registry


def orchestration_runtime_available() -> bool:
    """Return whether delegated-agent execution is wired for this process."""

    return _executor is not None


def _require_executor() -> OrchestrationExecutor:
    if _executor is None:
        raise ToolError("Agent orchestration runtime is not available")
    return _executor


def _require_context() -> ToolContext:
    context = current_tool_context.get()
    if (
        context is None
        or not context.orchestration_run_id
        or not context.orchestration_session_id
        or not context.orchestration_task_id
    ):
        raise ToolError("Current turn has no delegated-agent orchestration context")
    return context


def _registered_tool_names() -> frozenset[str]:
    registry = _registry or get_default_registry()
    return frozenset(registry.list_names())


def _inherited_tool_names(context: ToolContext) -> frozenset[str]:
    if (
        context.orchestration_complex_mode
        and context.subagent_depth == 0
        and context.orchestration_worker_template_tools is not None
    ):
        return context.orchestration_worker_template_tools
    if context.authorized_tool_names is not None:
        return context.authorized_tool_names
    if context.allowed_tools is not None:
        return frozenset(context.allowed_tools)
    return _registered_tool_names()


def _runtime_context(context: ToolContext) -> dict[str, Any]:
    sandbox_context: dict[str, Any] | None = None
    serializer = getattr(context.sandbox_run_context, "to_origin_payload", None)
    if callable(serializer):
        raw = serializer()
        if isinstance(raw, dict):
            sandbox_context = dict(raw)
    elif isinstance(context.sandbox_run_context, dict):
        sandbox_context = dict(context.sandbox_run_context)
    return {
        "principal_is_owner": bool(context.is_owner),
        "principal_host_execute": full_host_access_for_context(context),
        "elevated": context.elevated,
        "run_mode": current_run_mode() or context.run_mode,
        "sandbox_run_context": sandbox_context,
        "sandbox_mounts": [dict(item) for item in context.sandbox_mounts if isinstance(item, dict)],
        "active_model": context.active_model,
        "active_provider": context.active_provider,
        "workspace_id": context.workspace_id,
        "single_agent_mode": bool(context.orchestration_single_mode),
    }


def _is_complex_root(context: ToolContext) -> bool:
    return bool(
        context.orchestration_complex_mode
        and int(context.subagent_depth or 0) == 0
    )


async def _resolve_frozen_root_delegation(
    *,
    context: ToolContext,
    executor: OrchestrationExecutor,
    task_key: str,
    task: str,
    acceptance_criteria: str,
) -> tuple[str, str]:
    """Return the frozen task and result contract for a Complex root delegation."""

    if not _is_complex_root(context):
        return task.strip(), acceptance_criteria.strip()
    tasks = await executor.service.repository.list_task_board(
        str(context.orchestration_run_id)
    )
    direct = [
        item
        for item in tasks
        if item.parent_task_id == str(context.orchestration_task_id)
    ]
    target = next((item for item in direct if item.task_key == task_key.strip()), None)
    if target is not None:
        return (
            task.strip(),
            str(target.acceptance_criteria or "").strip(),
        )

    return task.strip(), acceptance_criteria.strip()


async def ensure_complex_root_run(
    context: ToolContext | None,
    *,
    root_task: str | None = None,
) -> str:
    """Persist Complex Task Mode before the root can produce model output."""

    if (
        context is None
        or not context.orchestration_complex_mode
        or int(context.subagent_depth or 0) != 0
        or _executor is None
    ):
        return ""
    inherited_tools = _inherited_tool_names(context)
    await _executor.ensure_run(
        run_id=str(context.orchestration_run_id),
        root_session_id=str(context.orchestration_session_id),
        root_task_id=str(context.orchestration_task_id),
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=context.orchestration_worker_template_tools or inherited_tools,
        registered_tools=_registered_tool_names(),
        root_runtime_session_key=context.session_key,
        root_runtime_context=_runtime_context(context),
        root_task=root_task,
    )
    if context.orchestration_single_mode:
        # Each user query is one-shot. Earlier child results remain available to
        # runtime recall, but are not unfinished work for the new query.
        return ""
    root_runtime_session_key = str(context.session_key or "").strip()
    if not root_runtime_session_key:
        return ""
    prior = await _executor.service.repository.list_prior_unsynthesized_child_work(
        root_runtime_session_key=root_runtime_session_key,
        exclude_run_id=str(context.orchestration_run_id),
    )
    if not prior:
        return ""

    recovered: list[dict[str, Any]] = []
    for task, child_session_id in prior:
        result = task.result if isinstance(task.result, dict) else {}
        deliverable = result.get("deliverable")
        unresolved = result.get("unresolved")
        error = result.get("error") or result.get("reason")
        summary = str(result.get("summary") or "")
        if task.outcome.value == "pending" and not result:
            continue
        if (
            task.outcome.value == "interrupted"
            and summary == "gateway_restarted"
            and not task.evidence
            and not error
            and not (isinstance(deliverable, str) and deliverable.strip())
            and not (isinstance(unresolved, list) and unresolved)
        ):
            continue
        status = "completed" if task.outcome.value == "succeeded" else task.outcome.value
        item: dict[str, Any] = {
            "task_key": task.task_key,
            "task": task.description,
            "status": status,
            "session_id": child_session_id,
            "summary": summary,
        }
        item["evidence"] = list(task.evidence)
        if task.acceptance_criteria:
            item["acceptance_criteria"] = task.acceptance_criteria
        if isinstance(deliverable, str) and deliverable.strip():
            item["deliverable"] = deliverable.strip()
        if isinstance(unresolved, list):
            item["unresolved"] = [
                question.strip()
                for question in unresolved
                if isinstance(question, str) and question.strip()
            ]
        if error:
            item["error"] = str(error)
        recovered.append(item)

    if not recovered:
        return ""
    return "\n".join(
        (
            "[Recovered delegated work from earlier unfinished turns]",
            "These records are persisted orchestration state, not a new user request.",
            json.dumps(recovered, ensure_ascii=False),
        )
    )


async def complex_root_synthesis_state(context: ToolContext | None) -> str:
    """Return whether a Complex root has terminal delegated work to synthesize."""

    if context is None or int(context.subagent_depth or 0) != 0 or _executor is None:
        return "not_complex_root"
    run_id = str(context.orchestration_run_id or "")
    run = await _executor.service.repository.get_run(run_id)
    if (
        run is None
        or run.mode is not OrchestrationMode.COMPLEX
        or run.root_session_id != str(context.orchestration_session_id or "")
        or run.root_task_id != str(context.orchestration_task_id or "")
    ):
        return "not_complex_root"
    total, pending = await _executor.service.repository.delegated_work_state(run_id)
    if total == 0:
        return "no_delegated_work"
    if pending:
        return "delegated_work_pending"
    observing_task_id: str | None = None
    observing_activation_id: str | None = None
    runtime_task_id = str(context.task_id or "")
    result_prefix = "orchestration-result:"
    if runtime_task_id.startswith(result_prefix):
        result_identity = runtime_task_id.removeprefix(result_prefix).split(":delivery:", 1)[0]
        child_task_id, separator, activation_id = result_identity.partition(":activation:")
        if separator and child_task_id and activation_id:
            observing_task_id = child_task_id
            observing_activation_id = activation_id
    if not await _executor.service.repository.run_ready_for_final_synthesis(
        run_id,
        observing_task_id=observing_task_id,
        observing_activation_id=observing_activation_id,
    ):
        return "delegated_result_pending"
    return "ready"


@tool(
    name="delegate_task",
    description=(
        "Delegate one bounded task with concrete acceptance criteria to a persistent child agent. "
        "In Complex mode, acceptance_criteria freezes this task's result fields at dispatch; "
        "task_board add is optional for advance planning. "
        "Foreground is the default and blocks until the child returns; set background=true "
        "to return immediately and post the terminal result to the parent queue. Results "
        "include status, summary, unresolved, session_id, and follow_up. Complex roots receive "
        "progress, not the explorer's technical deliverable; those details stay in the child "
        "session. Choose a fixed agent profile whose listed capabilities cover the task. "
        "In Complex mode, one worker session owns a requested code change in one repository, "
        "including its investigation and checks. Dispatch together only across "
        "non-overlapping sources; "
        "do not repeat completed or overlapping work. "
        "Reuse session_id for missing results or retryable failures outside Complete Task "
        "Mode; that mode allows reuse only on a new user query. If session_id is omitted, "
        "the runtime may reuse a sufficiently similar same-profile child from this parent. "
        "Multiple independent delegate_task calls emitted in one response run concurrently, "
        "including foreground calls. Reusing a completed task_key returns its stored result "
        "without executing the task again. Request the result needed to satisfy the assigned "
        "criteria, not a copy of whole files or raw search results."
    ),
    params={
        "task": {
            "type": "string",
            "description": (
                "One bounded outcome for the selected child. Specify the needed result and "
                "acceptance criteria; for an investigation, ask for key findings and evidence "
                "rather than an unverified implementation or whole-file handoff."
            ),
            "minLength": 1,
        },
        "task_key": {
            "type": "string",
            "description": (
                "Stable semantic key unique within this run. Reuse it only to attach, reuse, "
                "or retry the same work within its execution-attempt budget. Adding retry "
                "or agent-role suffixes does not reset the semantic work-family budget."
            ),
            "minLength": 1,
        },
        "acceptance_criteria": {
            "type": "string",
            "description": "Concrete observable result required for this task.",
            "minLength": 1,
        },
        "background": {
            "type": "boolean",
            "description": "Run asynchronously and notify this parent later.",
            "default": False,
        },
        "agent": {
            "type": "string",
            "enum": list(PRESET_PROFILES),
            "description": _AGENT_PROFILE_PARAMETER_DESCRIPTION,
        },
        "session_id": {
            "type": "string",
            "description": (
                "Existing child session to append or cold-restore; it preserves its conversation "
                "and tool results. Omit agent to retain its profile, or set agent=worker when "
                "its existing context is useful for implementation. Send the new request without "
                "repeating known context."
            ),
        },
        "replace_session_id": {
            "type": "string",
            "description": "Idle unsuccessful child session to replace using a bounded handoff.",
        },
    },
    required=["task", "task_key", "acceptance_criteria"],
    plan_access=PlanAccess.CONTROL,
    execution_timeout_seconds=0,
    cancellation_policy="must_settle",
)
async def delegate_task(
    task: str,
    task_key: str,
    acceptance_criteria: str,
    background: bool = False,
    agent: str | None = None,
    session_id: str | None = None,
    replace_session_id: str | None = None,
) -> str:
    context = _require_context()
    executor = _require_executor()
    inherited_tools = _inherited_tool_names(context)
    registered_tools = _registered_tool_names()
    try:
        if int(context.subagent_depth or 0) == 0:
            await executor.ensure_run(
                run_id=str(context.orchestration_run_id),
                root_session_id=str(context.orchestration_session_id),
                root_task_id=str(context.orchestration_task_id),
                mode=(
                    OrchestrationMode.COMPLEX
                    if context.orchestration_complex_mode
                    else OrchestrationMode.ORDINARY
                ),
                worker_template_tools=(
                    context.orchestration_worker_template_tools or inherited_tools
                ),
                registered_tools=registered_tools,
                root_runtime_session_key=context.session_key,
                root_runtime_context=_runtime_context(context),
                root_task=None,
            )
        task, acceptance_criteria = await _resolve_frozen_root_delegation(
            context=context,
            executor=executor,
            task_key=task_key,
            task=task,
            acceptance_criteria=acceptance_criteria,
        )
        result = await executor.delegate(
            DelegateRequest(
                run_id=str(context.orchestration_run_id),
                parent_session_id=str(context.orchestration_session_id),
                parent_task_id=str(context.orchestration_task_id),
                parent_activation_id=context.orchestration_activation_id,
                task_key=task_key,
                task=task,
                acceptance_criteria=acceptance_criteria,
                inherited_tools=inherited_tools,
                registered_tools=registered_tools,
                profile="inherit" if context.orchestration_single_mode else agent,
                background=background,
                session_id=session_id,
                replace_session_id=replace_session_id,
                runtime_context=_runtime_context(context),
            )
        )
    except (RuntimeError, ValueError) as exc:
        raise ToolError(str(exc)) from exc
    child_result = dict(result.result or {})
    if result.background and result.result is None:
        status = "working"
    else:
        status = str(child_result.get("status") or "").strip().casefold()
        if status not in {"completed", "failed", "interrupted"}:
            status = "failed" if child_result.get("error") else "completed"
    summary = str(
        child_result.get("summary") or child_result.get("error") or child_result.get("reason") or ""
    )
    error = child_result.get("error") or child_result.get("reason")
    requested_retry = child_result.get("retry_same_agent")
    retry_same_agent = (
        requested_retry
        if isinstance(requested_retry, bool)
        else status in {"failed", "interrupted"}
    )
    if status in {"failed", "interrupted"} and retry_same_agent:
        retry_same_agent = await executor.can_retry_same_agent(
            task_id=result.task_id,
            session_id=result.session_id,
        )
    else:
        retry_same_agent = False
    persisted_task = await executor.service.repository.get_task(result.task_id)
    if persisted_task is not None:
        board_status = persisted_task.board_status.value
        outcome = persisted_task.outcome.value
    elif status == "completed":
        board_status = "completed"
        outcome = "succeeded"
    elif status in {"failed", "interrupted"}:
        board_status = "blocked"
        outcome = status
    else:
        board_status = "planned"
        outcome = "pending"
    agent_state = (
        await executor.agent_state(
            run_id=str(context.orchestration_run_id),
            session_id=result.session_id,
        )
    ).value
    parent_result = {
        "status": status,
        "task_key": result.task_key,
        "board_status": board_status,
        "agent_state": agent_state,
        "outcome": outcome,
        "session_id": result.session_id,
        "activation_id": result.activation_id,
        "background": result.background,
        "summary": summary,
        "error": error,
        "evidence": list(persisted_task.evidence) if persisted_task is not None else [],
        "acceptance_criteria": (
            persisted_task.acceptance_criteria
            if persisted_task is not None and persisted_task.acceptance_criteria
            else acceptance_criteria.strip()
        ),
    }
    if context.orchestration_single_mode:
        parent_result["task_id"] = result.task_id
        parent_result.pop("evidence", None)
        parent_result.pop("acceptance_criteria", None)
    unresolved = child_result.get("unresolved")
    normalized_unresolved: list[str] | None = None
    if isinstance(unresolved, list):
        normalized_unresolved = [
            question.strip()
            for question in unresolved
            if isinstance(question, str) and question.strip()
        ]
        parent_result["unresolved"] = normalized_unresolved
    child_session = await executor.service.repository.get_session(result.session_id)
    child_profile = child_session.profile if child_session is not None else agent or "inherit"
    compact_explorer_result = is_complex_root(context) and child_profile == "explorer"
    if compact_explorer_result:
        parent_result.pop("evidence", None)
        parent_result.pop("acceptance_criteria", None)
    if context.orchestration_single_mode:
        pass
    elif status in {"failed", "interrupted"}:
        parent_result["retry_same_agent"] = retry_same_agent
        parent_result["follow_up"] = failed_task_follow_up(
            task_key=result.task_key,
            session_id=result.session_id,
            retry_same_agent=retry_same_agent,
        )
    elif status == "completed":
        parent_result["follow_up"] = completed_task_follow_up(
            session_id=result.session_id,
            unresolved=normalized_unresolved,
            profile=child_profile,
        )
    deliverable = child_result.get("deliverable")
    if (
        isinstance(deliverable, str)
        and deliverable.strip()
        and not compact_explorer_result
        and not context.orchestration_single_mode
    ):
        parent_result["deliverable"] = deliverable.strip()
    return json.dumps(
        parent_result,
        ensure_ascii=False,
    )


@tool(
    name="task_board",
    description=(
        "Read or update the shared hierarchical task list. Use add for a subtask under "
        "your current task with observable acceptance criteria, start when work begins, "
        "complete with concise evidence, block with the blocker, and reopen before repeating "
        "closed work. A Complex root may add tasks before dispatching, but delegate_task "
        "also records each new task and its result fields. Children may add substeps. "
        "Never make whole files, full source, or every raw search result a planned outcome."
    ),
    params={
        "action": {
            "type": "string",
            "enum": ["list", "add", "start", "complete", "block", "reopen"],
        },
        "task_key": {
            "type": "string",
            "description": "Stable task key. Defaults to the current assigned task.",
        },
        "description": {
            "type": "string",
            "description": (
                "Required concise outcome only when adding a subtask. Ask for "
                "decision-critical findings, paths, line references, and short key snippets, not "
                "whole files or full source."
            ),
        },
        "acceptance_criteria": {
            "type": "string",
            "description": "Required short observable completion condition when adding a task.",
        },
        "evidence": {
            "type": "string",
            "description": "Short result evidence or blocker for complete/block.",
        },
    },
    required=["action"],
    plan_access=PlanAccess.CONTROL,
)
async def task_board(
    action: str,
    task_key: str | None = None,
    description: str | None = None,
    acceptance_criteria: str | None = None,
    evidence: str | None = None,
) -> str:
    context = _require_context()
    executor = _require_executor()
    run_id = str(context.orchestration_run_id)
    session_id = str(context.orchestration_session_id)
    current_task_id = str(context.orchestration_task_id)
    updated_task: DelegatedTaskRecord | None = None
    try:
        if int(context.subagent_depth or 0) == 0:
            inherited_tools = _inherited_tool_names(context)
            await executor.ensure_run(
                run_id=run_id,
                root_session_id=session_id,
                root_task_id=current_task_id,
                mode=(
                    OrchestrationMode.COMPLEX
                    if context.orchestration_complex_mode
                    else OrchestrationMode.ORDINARY
                ),
                worker_template_tools=(
                    context.orchestration_worker_template_tools or inherited_tools
                ),
                registered_tools=_registered_tool_names(),
                root_runtime_session_key=context.session_key,
                root_runtime_context=_runtime_context(context),
                root_task=None,
            )
        if action == "list":
            tasks = await executor.service.repository.list_task_board(run_id)
        elif action == "add":
            if not task_key or not description or not acceptance_criteria:
                raise ValueError(
                    "task_key, description, and acceptance_criteria are required for add"
                )
            updated_task = await executor.service.add_task_board_item(
                run_id=run_id,
                caller_session_id=session_id,
                parent_task_id=current_task_id,
                task_key=task_key,
                description=description,
                acceptance_criteria=acceptance_criteria,
            )
            tasks = await executor.service.repository.list_task_board(run_id)
        else:
            if not task_key:
                current = await executor.service.repository.get_task(current_task_id)
                if current is None:
                    raise ValueError("current orchestration task does not exist")
                task_key = current.task_key
            updated_task = await executor.service.update_task_board_item(
                run_id=run_id,
                caller_session_id=session_id,
                task_key=task_key,
                action=action,
                evidence=evidence,
            )
            tasks = await executor.service.repository.list_task_board(run_id)
    except (RuntimeError, ValueError) as exc:
        raise ToolError(str(exc)) from exc

    if updated_task is not None:
        await executor.publish_progress(
            run_id=run_id,
            task_id=updated_task.task_id,
            phase=action,
        )

    keys_by_id = {task.task_id: task.task_key for task in tasks}
    projected_tasks: list[dict[str, Any]] = []
    for task in tasks:
        agent_state = await executor.agent_state(
            run_id=run_id,
            session_id=task.owner_session_id,
        )
        projected_tasks.append(
            {
                "task_key": task.task_key,
                "parent_task_key": (
                    keys_by_id.get(task.parent_task_id)
                    if task.parent_task_id is not None
                    else None
                ),
                "board_status": task.board_status.value,
                "agent_state": agent_state.value,
                "outcome": task.outcome.value,
                "description": task.description,
                "acceptance_criteria": task.acceptance_criteria,
                "evidence": list(task.evidence),
            }
        )
    return json.dumps(
        {"tasks": projected_tasks},
        ensure_ascii=False,
    )


@tool(
    name="interrupt_agent",
    description=(
        "Stop the current activation of a delegated child and its unfinished descendants. "
        "Both IDs are required as a generation fence, so an old request cannot stop a newer "
        "cold-restored activation. The persistent session and context are retained."
    ),
    params={
        "session_id": {
            "type": "string",
            "description": "Persistent child session identifier.",
        },
        "activation_id": {
            "type": "string",
            "description": "Current activation identifier returned by delegate_task.",
        },
        "reason": {
            "type": "string",
            "description": "Concrete reason for interruption.",
            "minLength": 1,
        },
    },
    required=["session_id", "activation_id", "reason"],
    plan_access=PlanAccess.CONTROL,
    cancellation_policy="must_settle",
)
async def interrupt_agent(
    session_id: str,
    activation_id: str,
    reason: str,
) -> str:
    context = _require_context()
    executor = _require_executor()
    try:
        targets = await executor.interrupt(
            caller_session_id=str(context.orchestration_session_id),
            session_id=session_id,
            activation_id=activation_id,
            reason=reason,
        )
        state = await executor.agent_state(
            run_id=str(context.orchestration_run_id),
            session_id=session_id,
        )
    except (RuntimeError, ValueError) as exc:
        raise ToolError(str(exc)) from exc
    return json.dumps(
        {
            "session_id": session_id,
            "activation_id": activation_id,
            "status": state.value,
            "target_activation_ids": [target.activation_id for target in targets],
        },
        ensure_ascii=False,
    )


__all__ = [
    "complex_root_synthesis_state",
    "ensure_complex_root_run",
    "delegate_task",
    "interrupt_agent",
    "task_board",
    "orchestration_runtime_available",
    "set_orchestration_executor",
]
