"""Gateway adapters that execute durable orchestration activations on TaskRuntime."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

import structlog

from opensquilla.engine.subagent_delegation import (
    SINGLE_SUBAGENT_EXECUTION_PROMPT,
    SUBAGENT_EXECUTION_PROMPT,
    completed_task_follow_up,
    failed_task_follow_up,
)
from opensquilla.gateway.orchestration_routing import ChildTreeRoute
from opensquilla.gateway.project_workspace_runtime import AcceptedRunModeOverride
from opensquilla.gateway.routing import build_subagent_route_envelope
from opensquilla.orchestration.executor import ActivationExecutionResult
from opensquilla.orchestration.models import (
    AgentActivationRecord,
    AgentSessionRecord,
    AgentState,
    DelegatedTaskRecord,
    OrchestrationMode,
    TaskOutcome,
)
from opensquilla.orchestration.repository import OrchestrationRepository
from opensquilla.orchestration.session_recall import SessionRecallEngine
from opensquilla.run_mode import RunMode, normalize_run_mode
from opensquilla.sandbox.run_context import (
    RUN_CONTEXT_ORIGIN_KEY,
    RunContext,
    run_context_for_subagent,
    run_context_from_origin_payload,
)
from opensquilla.session.keys import parse_agent_id
from opensquilla.session.models import AgentTaskStatus

RetrySameAgentCheck = Callable[..., Awaitable[bool]]
AgentStateCheck = Callable[..., Awaitable[AgentState]]
log = structlog.get_logger(__name__)


def _task_prompt(
    task: DelegatedTaskRecord,
    *,
    root_task: str | None = None,
) -> str:
    # The delegating parent owns task decomposition. Repeating the full root
    # request here can reintroduce superseded phases and broaden a bounded
    # child assignment, so the child receives only its task and criteria.
    del root_task
    sections = [
        SINGLE_SUBAGENT_EXECUTION_PROMPT
        if task.runtime_context.get("single_agent_mode")
        else SUBAGENT_EXECUTION_PROMPT
    ]
    sections.append(
        f"Assigned task key: {task.task_key}\n"
        f"Assigned task (the only work to execute):\n{task.description}"
    )
    if task.acceptance_criteria:
        sections.append(
            "Acceptance criteria (return immediately once all are satisfied):\n"
            f"{task.acceptance_criteria}"
        )
    return "\n\n".join(sections)


def _continuation_prompt(task: DelegatedTaskRecord) -> str:
    """Append only the missing delta when a durable child session already exists."""

    if task.retry_of_activation_id is not None:
        return (
            "Continue the previous task in this existing conversation. "
            "Reuse its completed work and tool results. Finish only the remaining work; "
            "do not rerun successful steps. Return the required structured result."
        )
    sections = [
        "Continue in this existing conversation with only this follow-up. Reuse prior context "
        "and completed work; do not repeat successful steps.\n"
        f"Follow-up request:\n{task.description}"
    ]
    if task.acceptance_criteria:
        sections.append(
            "Acceptance criteria for this follow-up:\n"
            f"{task.acceptance_criteria}"
        )
    return "\n\n".join(sections)


def _status_outcome(status: object) -> TaskOutcome:
    value = getattr(status, "value", status)
    if value == AgentTaskStatus.SUCCEEDED.value:
        return TaskOutcome.SUCCEEDED
    if value == AgentTaskStatus.CANCELLED.value:
        return TaskOutcome.INTERRUPTED
    if value == AgentTaskStatus.TIMEOUT.value:
        return TaskOutcome.TIMED_OUT
    return TaskOutcome.FAILED


def _runtime_failure_retry_same_agent(terminal: object) -> bool:
    """Honor the engine's persisted retry decision when one is available."""

    details = getattr(terminal, "details", None)
    if isinstance(details, dict):
        turn_outcome = details.get("turn_outcome")
        if isinstance(turn_outcome, dict):
            retryable = turn_outcome.get("retryable")
            if isinstance(retryable, bool):
                return retryable
    return True


def _invalid_child_report(summary: str) -> tuple[TaskOutcome, dict[str, Any]]:
    return (
        TaskOutcome.FAILED,
        {
            "status": "failed",
            "summary": summary,
            "error": "Child returned an invalid structured completion report.",
            "error_type": "InvalidSubagentResult",
            "terminal_reason": "invalid_subagent_result",
            "retry_same_agent": True,
        },
    )


def _unresolved_questions(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return [
        question.strip()
        for question in value
        if isinstance(question, str) and question.strip()
    ]


def _completed_child_report(
    summary: str,
    *,
    deliverable: str | None = None,
    unresolved: list[str] | None = None,
) -> tuple[TaskOutcome, dict[str, Any]]:
    result: dict[str, Any] = {
        "status": "completed",
        "summary": summary,
        "error": None,
        "retry_same_agent": False,
    }
    if deliverable:
        result["deliverable"] = deliverable
    if unresolved is not None:
        result["unresolved"] = unresolved
    return (
        TaskOutcome.SUCCEEDED,
        result,
    )


def _decode_child_report(text: str) -> object:
    """Decode an exact report or a final report preceded by harmless prose."""

    candidate = text
    try:
        return json.loads(candidate)
    except (TypeError, ValueError):
        pass

    stripped = candidate.rstrip()
    if stripped.endswith("```"):
        closing_start = len(stripped) - 3
        opening_ends: list[int] = []
        offset = 0
        for line in stripped[:closing_start].splitlines(keepends=True):
            if line.strip().casefold() in {"```", "```json"}:
                opening_ends.append(offset + len(line))
            offset += len(line)
        for body_start in reversed(opening_ends):
            try:
                return json.loads(stripped[body_start:closing_start].strip())
            except (TypeError, ValueError):
                continue

    decoder = json.JSONDecoder()
    for index in reversed([i for i, char in enumerate(candidate) if char == "{"]):
        try:
            payload, end = decoder.raw_decode(candidate, index)
        except (TypeError, ValueError):
            continue
        if not candidate[end:].strip():
            return payload
    raise ValueError("child report does not end with a JSON object")


def _parse_structured_child_report(content: str) -> tuple[TaskOutcome, dict[str, Any]]:
    """Turn the child's final JSON into the small parent-facing result contract."""

    text = content.strip()
    if not text:
        return _invalid_child_report(text)
    try:
        payload = _decode_child_report(text)
    except (TypeError, ValueError):
        return _invalid_child_report("Child report did not end with a structured JSON object.")
    if not isinstance(payload, dict):
        return _invalid_child_report("Child report must be a structured JSON object.")
    if set(payload) == {"result"} and isinstance(payload["result"], dict):
        payload = payload["result"]

    status = str(payload.get("status") or "").strip().casefold()
    summary = str(payload.get("summary") or "").strip()
    if status not in {"completed", "failed"} or not summary:
        return _invalid_child_report(
            "Child report omitted a valid status or non-empty summary."
        )
    if status == "completed":
        deliverable = payload.get("deliverable")
        unresolved_value = payload.get("unresolved")
        if (
            not isinstance(deliverable, str)
            or not deliverable.strip()
            or not isinstance(unresolved_value, list)
            or payload.get("error", ...) is not None
            or payload.get("retry_same_agent", ...) is not False
        ):
            return _invalid_child_report(
                "Child report omitted required completion fields."
            )
        unresolved = _unresolved_questions(unresolved_value)
        if unresolved:
            incomplete_result: dict[str, Any] = {
                "status": "failed",
                "summary": summary,
                "error": (
                    "Unable to complete: not all assigned acceptance criteria were satisfied."
                ),
                "error_type": "UnsatisfiedSubagentCriteria",
                "retry_same_agent": False,
                "unresolved": unresolved,
            }
            if isinstance(deliverable, str) and deliverable.strip():
                incomplete_result["deliverable"] = deliverable.strip()
            return TaskOutcome.FAILED, incomplete_result
        return _completed_child_report(
            summary,
            deliverable=deliverable.strip(),
            unresolved=unresolved,
        )

    error = str(payload.get("error") or summary).strip()
    retry_same_agent = payload.get("retry_same_agent")
    if not isinstance(retry_same_agent, bool):
        return _invalid_child_report(summary)
    result: dict[str, Any] = {
        "status": "failed",
        "summary": summary,
        "error": error,
        "retry_same_agent": retry_same_agent,
    }
    deliverable = payload.get("deliverable")
    if isinstance(deliverable, str) and deliverable.strip():
        result["deliverable"] = deliverable.strip()
    unresolved = _unresolved_questions(payload.get("unresolved"))
    if unresolved is not None:
        result["unresolved"] = unresolved
    return (
        TaskOutcome.FAILED,
        result,
    )


def _parse_child_report(
    content: str,
    *,
    single_mode: bool = False,
) -> tuple[TaskOutcome, dict[str, Any]]:
    outcome, report = _parse_structured_child_report(content)
    if not single_mode or report.get("error_type") != "InvalidSubagentResult":
        return outcome, report

    answer = content.strip()
    if not answer:
        return outcome, report
    try:
        payload = _decode_child_report(answer)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict) and str(payload.get("status") or "").strip().casefold() in {
        "completed",
        "failed",
    }:
        # A malformed orchestration report is still invalid. Only an ordinary
        # final answer (including task-specific JSON) takes the one-shot path.
        return outcome, report
    return _completed_child_report(
        "Child returned a final answer.",
        deliverable=answer,
        unresolved=[],
    )


def _activation_authority(context: dict[str, Any]) -> tuple[RunContext, bool, bool]:
    """Derive fail-closed execution authority for one activation."""

    owner = bool(context.get("principal_is_owner"))
    host_execute = owner and bool(context.get("principal_host_execute"))
    try:
        run_mode = normalize_run_mode(context.get("run_mode"))
    except ValueError:
        run_mode = RunMode.SAFE
    if run_mode is RunMode.FULL and not host_execute:
        run_mode = RunMode.SAFE
    hydrated = run_context_from_origin_payload(
        context.get("sandbox_run_context"),
        source="orchestration_parent_activation",
        preserve_materialized_user_grants=True,
    )
    if hydrated is None:
        hydrated = RunContext(run_mode=run_mode, source="orchestration_parent_activation")
    return (
        replace(run_context_for_subagent(hydrated), run_mode=run_mode),
        owner,
        host_execute and run_mode is RunMode.FULL,
    )


class TaskRuntimeActivationRunner:
    """Keep durable agent identity while borrowing TaskRuntime execution slots."""

    def __init__(
        self,
        *,
        repository: OrchestrationRepository,
        session_manager: Any,
        task_runtime: Any,
        config: Any,
        session_recall: SessionRecallEngine | None = None,
    ) -> None:
        self.repository = repository
        self.session_manager = session_manager
        self.task_runtime = task_runtime
        self.config = config
        self.session_recall = session_recall
        self._runtime_tasks: dict[str, tuple[str, str]] = {}

    @staticmethod
    def _runtime_task_id(activation_id: str) -> str:
        return f"orchestration-activation:{activation_id}"

    async def run(
        self,
        *,
        activation: AgentActivationRecord,
        session: AgentSessionRecord,
        task: DelegatedTaskRecord,
        route: Any,
    ) -> ActivationExecutionResult:
        runtime_key = session.runtime_session_key or session.session_id
        attachment = await self.repository.get_session_attachment(
            run_id=task.run_id,
            session_id=session.session_id,
        )
        parent_session_id = (
            attachment.parent_session_id if attachment is not None else session.parent_session_id
        )
        depth = attachment.depth if attachment is not None else session.depth
        parent = (
            await self.repository.get_session(parent_session_id)
            if parent_session_id is not None
            else None
        )
        parent_key = (
            parent.runtime_session_key or parent.session_id if parent is not None else runtime_key
        )
        parent_node = await self.session_manager.get_session(parent_key)
        context = dict(task.runtime_context)
        run_context, principal_is_owner, principal_host_execute = _activation_authority(context)
        selected = route if isinstance(route, ChildTreeRoute) else None
        baseline_model = str(context.get("active_model") or "").strip()
        if not baseline_model:
            baseline_model = str(
                getattr(getattr(self.config, "llm", None), "model", "") or ""
            ).strip()
        model = selected.model if selected is not None else baseline_model or None
        baseline_provider = str(context.get("active_provider") or "").strip() or None
        provider = selected.provider if selected is not None else baseline_provider

        activation_origin = {
            "kind": "orchestration",
            "run_id": task.run_id,
            "task_id": task.task_id,
            "task_key": task.task_key,
            "profile": session.profile,
            RUN_CONTEXT_ORIGIN_KEY: run_context.to_origin_payload(),
        }
        if selected is not None:
            activation_origin["routing"] = {
                "model": selected.model,
                "provider": selected.provider,
                "tier": selected.tier,
                "source": selected.source,
                "confidence": selected.confidence,
                "thinking_level": selected.thinking_level,
            }
        create_kwargs: dict[str, Any] = {
            "model": model,
            "model_provider": provider,
            "spawn_depth": depth,
            "parent_session_key": parent_key,
            "spawned_by": parent_key,
            "origin": activation_origin,
        }
        workspace_id = context.get("workspace_id")
        current_workspace_id = (
            workspace_id if isinstance(workspace_id, str) and workspace_id else None
        )
        create_kwargs["workspace_id"] = current_workspace_id
        node, _created = await self.session_manager.get_or_create(
            runtime_key,
            agent_id=parse_agent_id(runtime_key),
            **create_kwargs,
        )
        if not _created:
            existing_origin = getattr(node, "origin", None)
            origin = dict(existing_origin) if isinstance(existing_origin, dict) else {}
            origin.update(activation_origin)
            update_fields: dict[str, Any] = {
                "origin": origin,
                "workspace_id": current_workspace_id,
            }
            node = await self.session_manager.update(
                runtime_key,
                expected_session_id=str(node.session_id),
                expected_session_epoch=int(getattr(node, "epoch", 0) or 0),
                **update_fields,
            )

        run = await self.repository.get_run(task.run_id)
        if run is None:
            raise RuntimeError(f"orchestration run not found: {task.run_id}")
        envelope = build_subagent_route_envelope(
            session_key=runtime_key,
            parent_session_key=parent_key,
            agent_id=parse_agent_id(runtime_key),
            session_id=str(node.session_id),
            session_epoch=int(getattr(node, "epoch", 0) or 0),
            parent_session_id=(str(parent_node.session_id) if parent_node is not None else None),
            parent_session_epoch=(
                int(getattr(parent_node, "epoch", 0) or 0) if parent_node is not None else None
            ),
            run_id=task.run_id,
            parent_task_id=task.parent_task_id,
            spawn_depth=depth,
            origin="delegate_task",
            principal_is_owner=principal_is_owner,
            principal_host_execute=principal_host_execute,
            elevated=(
                "full"
                if run_context.run_mode is RunMode.FULL and principal_host_execute
                else str(context["elevated"])
                if principal_is_owner and context.get("elevated") == "on"
                else None
            ),
            run_mode=run_context.run_mode,
            sandbox_run_context=run_context,
            sandbox_mounts=run_context.to_origin_payload()["mounts"],
            tools=sorted(task.effective_tools),
            tools_are_final=True,
        )
        route_metadata: dict[str, Any] = {
            "orchestration_run_id": task.run_id,
            "orchestration_session_id": session.session_id,
            "orchestration_task_id": task.task_id,
            "orchestration_activation_id": activation.activation_id,
            "orchestration_worker_template_tools": sorted(run.worker_template_tools),
            "complex_task_mode": run.mode is OrchestrationMode.COMPLEX,
            "single_agent_mode": bool(task.runtime_context.get("single_agent_mode")),
            "baseline_model": baseline_model or None,
            "baseline_provider": baseline_provider,
            "routing_applied": selected is not None,
        }
        if selected is not None:
            route_metadata.update(
                {
                    "routed_model": selected.model,
                    "routed_provider": selected.provider,
                    "routed_tier": selected.tier,
                    "routing_source": selected.source,
                    "routing_confidence": selected.confidence,
                    "thinking_level": selected.thinking_level,
                }
            )
        envelope = replace(
            envelope,
            metadata={**envelope.metadata, **route_metadata},
        )
        prompt = _task_prompt(task) if _created else _continuation_prompt(task)
        persisted = await self.session_manager.append_message(
            runtime_key,
            role="user",
            content=prompt,
            expected_session_id=str(node.session_id),
            expected_session_epoch=int(getattr(node, "epoch", 0) or 0),
        )
        runtime_task_id = self._runtime_task_id(activation.activation_id)
        self._runtime_tasks[activation.activation_id] = (runtime_task_id, runtime_key)
        try:
            handle = await self.task_runtime.enqueue(
                envelope,
                prompt,
                mode="followup",
                run_kind="subagent",
                task_id=runtime_task_id,
                persisted_user_message_id=getattr(persisted, "message_id", None),
                accepted_run_mode_override=AcceptedRunModeOverride(
                    run_mode=run_context.run_mode,
                    run_mode_source="parent_activation",
                    source="orchestration_parent_activation",
                ),
            )
            terminal = await self.task_runtime.wait(handle.task_id)
        finally:
            self._runtime_tasks.pop(activation.activation_id, None)

        outcome = _status_outcome(terminal.status)
        if outcome is TaskOutcome.SUCCEEDED:
            transcript = await self.session_manager.get_transcript(
                runtime_key,
                expected_session_id=str(node.session_id),
                expected_session_epoch=int(getattr(node, "epoch", 0) or 0),
            )
            summary = next(
                (
                    str(getattr(entry, "content", "") or "")
                    for entry in reversed(transcript)
                    if getattr(entry, "role", None) == "assistant"
                ),
                "",
            )
            report_outcome, report = _parse_child_report(
                summary,
                single_mode=bool(task.runtime_context.get("single_agent_mode")),
            )
            if self.session_recall is not None:
                try:
                    report["recall_index"] = await self.session_recall.build_task_index(
                        task=task,
                        result=report,
                        transcript=transcript,
                        start_message_id=getattr(persisted, "message_id", None),
                    )
                except Exception as exc:  # noqa: BLE001 - recall must not fail the child task
                    log.warning(
                        "subagent_session_recall_index_failed",
                        task_id=task.task_id,
                        error=str(exc),
                    )
            report.update(
                {
                    "runtime_task_id": handle.task_id,
                    "session_key": runtime_key,
                }
            )
            return ActivationExecutionResult(outcome=report_outcome, result=report)
        error = str(getattr(terminal, "error_message", "") or "child task failed")
        return ActivationExecutionResult(
            outcome=outcome,
            result={
                "status": ("interrupted" if outcome is TaskOutcome.INTERRUPTED else "failed"),
                "summary": error,
                "error": error,
                "error_type": str(getattr(terminal, "error_class", "") or "AgentTaskError"),
                "terminal_reason": getattr(terminal, "terminal_reason", None),
                "retry_same_agent": _runtime_failure_retry_same_agent(terminal),
                "runtime_task_id": handle.task_id,
                "session_key": runtime_key,
            },
        )

    async def interrupt(
        self,
        *,
        activation: AgentActivationRecord,
        reason: str,
    ) -> None:
        session = await self.repository.get_session(activation.session_id)
        if session is None:
            return
        runtime_task_id, runtime_key = self._runtime_tasks.get(
            activation.activation_id,
            (
                self._runtime_task_id(activation.activation_id),
                session.runtime_session_key or session.session_id,
            ),
        )
        await self.task_runtime.cancel_exact(
            task_id=runtime_task_id,
            session_key=runtime_key,
            source="orchestration_interrupt",
            reason=reason,
        )


class TaskRuntimeParentNotifier:
    """Enqueue a parent follow-up after durable background-result delivery."""

    def __init__(
        self,
        *,
        repository: OrchestrationRepository,
        task_runtime: Any,
        serialization_lock: Any = None,
        retry_same_agent_check: RetrySameAgentCheck | None = None,
        agent_state_check: AgentStateCheck | None = None,
    ) -> None:
        self.repository = repository
        self.task_runtime = task_runtime
        self.serialization_lock = serialization_lock
        self.retry_same_agent_check = retry_same_agent_check
        self.agent_state_check = agent_state_check

    @staticmethod
    def _notification_prefix(task_id: str, activation_id: str) -> str:
        return f"orchestration-result:{task_id}:activation:{activation_id}"

    async def delivery_state(
        self,
        *,
        task_id: str,
        activation_id: str,
    ) -> tuple[str, int]:
        prefix = self._notification_prefix(task_id, activation_id)
        attempt = 1
        while True:
            notification_task_id = prefix if attempt == 1 else f"{prefix}:delivery:{attempt}"
            try:
                existing = await self.task_runtime.status(notification_task_id)
            except KeyError:
                return "missing", attempt
            status = getattr(existing, "status", None)
            status_value = getattr(status, "value", status)
            if status_value == AgentTaskStatus.SUCCEEDED.value:
                return "succeeded", attempt
            if status_value in {
                AgentTaskStatus.QUEUED.value,
                AgentTaskStatus.RUNNING.value,
            }:
                return "active", attempt
            attempt += 1

    async def __call__(
        self,
        *,
        session: AgentSessionRecord,
        task: DelegatedTaskRecord,
        activation_id: str,
        outcome: TaskOutcome,
        result: dict[str, Any],
    ) -> bool:
        guard = (
            self.serialization_lock(task.run_id)
            if callable(self.serialization_lock)
            else contextlib.nullcontext()
        )
        async with guard:
            return await self._send_if_current(
                session=session,
                task=task,
                activation_id=activation_id,
                outcome=outcome,
                result=result,
            )

    async def _send_if_current(
        self,
        *,
        session: AgentSessionRecord,
        task: DelegatedTaskRecord,
        activation_id: str,
        outcome: TaskOutcome,
        result: dict[str, Any],
    ) -> bool:
        if callable(self.serialization_lock):
            if not await self.repository.child_result_needs_delivery(
                task.task_id,
                activation_id=activation_id,
            ):
                return True
        attachment = await self.repository.get_session_attachment(
            run_id=task.run_id,
            session_id=session.session_id,
        )
        parent_session_id = (
            attachment.parent_session_id if attachment is not None else session.parent_session_id
        )
        if parent_session_id is None:
            return True
        parent = await self.repository.get_session(parent_session_id)
        if parent is None:
            return True
        parent_key = parent.runtime_session_key or parent.session_id
        run = await self.repository.get_run(task.run_id)
        single_mode = bool(task.runtime_context.get("single_agent_mode"))
        raw_result = dict(result)
        failed = outcome in {
            TaskOutcome.FAILED,
            TaskOutcome.INTERRUPTED,
            TaskOutcome.TIMED_OUT,
        }
        requested_retry = raw_result.get("retry_same_agent")
        requested_same_agent_retry = (
            requested_retry if isinstance(requested_retry, bool) else failed
        )
        retry_same_agent = requested_same_agent_retry
        if requested_same_agent_retry and self.retry_same_agent_check is not None:
            retry_same_agent = await self.retry_same_agent_check(
                task_id=task.task_id,
                session_id=session.session_id,
            )
        summary = str(
            raw_result.get("summary") or raw_result.get("error") or raw_result.get("reason") or ""
        )
        persisted_task = await self.repository.get_task(task.task_id)
        agent_state = AgentState.IDLE
        if self.agent_state_check is not None:
            agent_state = await self.agent_state_check(
                run_id=task.run_id,
                session_id=session.session_id,
            )
        parent_result = {
            "status": (
                "completed"
                if not failed
                else "interrupted"
                if outcome is TaskOutcome.INTERRUPTED
                else "failed"
            ),
            "summary": summary,
            "error": (raw_result.get("error") or raw_result.get("reason") if failed else None),
            "board_status": (
                persisted_task.board_status.value if persisted_task is not None else "blocked"
            ),
            "agent_state": agent_state.value,
            "outcome": outcome.value,
            "evidence": list(persisted_task.evidence) if persisted_task is not None else [],
        }
        if persisted_task is not None and persisted_task.acceptance_criteria:
            parent_result["acceptance_criteria"] = persisted_task.acceptance_criteria
        compact_explorer_result = bool(
            run is not None
            and run.mode is OrchestrationMode.COMPLEX
            and parent.depth == 0
            and session.profile == "explorer"
        )
        if compact_explorer_result:
            parent_result.pop("evidence", None)
            parent_result.pop("acceptance_criteria", None)
        if single_mode:
            parent_result["task_id"] = task.task_id
            parent_result.pop("evidence", None)
            parent_result.pop("acceptance_criteria", None)
        unresolved = _unresolved_questions(raw_result.get("unresolved"))
        if unresolved is not None:
            parent_result["unresolved"] = unresolved
        if single_mode:
            pass
        elif failed:
            parent_result["retry_same_agent"] = retry_same_agent
            parent_result["follow_up"] = failed_task_follow_up(
                task_key=task.task_key,
                session_id=session.session_id,
                retry_same_agent=retry_same_agent,
            )
        else:
            parent_result["follow_up"] = completed_task_follow_up(
                session_id=session.session_id,
                unresolved=unresolved,
                profile=session.profile,
            )
        deliverable = raw_result.get("deliverable")
        if (
            isinstance(deliverable, str)
            and deliverable.strip()
            and not compact_explorer_result
            and not single_mode
        ):
            parent_result["deliverable"] = deliverable.strip()
        message = (
            (
                "A background delegated task reached a terminal state. Report its short "
                "status; the full answer is displayed separately. Do not delegate again "
                "for this user request. Finish after all in-flight child tasks return.\n"
                if single_mode
                else "A background delegated task reached a terminal state. Report its status "
                "and follow the next planned task; do not repeat the delegated work.\n"
            )
            + json.dumps(
                {
                    "task_id": task.task_id,
                    "activation_id": activation_id,
                    "task_key": task.task_key,
                    "session_id": session.session_id,
                    "outcome": outcome.value,
                    "result": parent_result,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        notification_prefix = self._notification_prefix(task.task_id, activation_id)
        delivery_state, delivery_attempt = await self.delivery_state(
            task_id=task.task_id,
            activation_id=activation_id,
        )
        if delivery_state == "succeeded":
            return True
        if delivery_state == "active":
            return False
        notification_task_id = (
            notification_prefix
            if delivery_attempt == 1
            else f"{notification_prefix}:delivery:{delivery_attempt}"
        )
        metadata = {
            "orchestration_run_id": task.run_id,
            "orchestration_session_id": parent.session_id,
            "orchestration_task_id": run.root_task_id if run is not None else task.task_id,
            "orchestration_worker_template_tools": (
                sorted(run.worker_template_tools) if run is not None else []
            ),
            "complex_task_mode": bool(run is not None and run.mode is OrchestrationMode.COMPLEX),
            "single_agent_mode": single_mode,
        }
        await self.task_runtime.send(
            parent_key,
            message,
            provenance={
                "kind": "orchestration_child_result",
                "task_id": task.task_id,
                "activation_id": activation_id,
                "session_id": session.session_id,
            },
            metadata=metadata,
            task_id=notification_task_id,
        )
        return False

    async def cancel_pending_delivery(
        self,
        *,
        session: AgentSessionRecord,
        task: DelegatedTaskRecord,
        activation_id: str,
    ) -> None:
        """Cancel a queued notification after foreground reuse observed its result."""

        parent_session_id = session.parent_session_id
        attachment = await self.repository.get_session_attachment(
            run_id=task.run_id,
            session_id=session.session_id,
        )
        if attachment is not None:
            parent_session_id = attachment.parent_session_id
        if parent_session_id is None:
            return
        parent = await self.repository.get_session(parent_session_id)
        if parent is None:
            return
        state, attempt = await self.delivery_state(
            task_id=task.task_id,
            activation_id=activation_id,
        )
        if state != "active":
            return
        prefix = self._notification_prefix(task.task_id, activation_id)
        notification_task_id = prefix if attempt == 1 else f"{prefix}:delivery:{attempt}"
        await self.task_runtime.cancel_exact(
            task_id=notification_task_id,
            session_key=parent.runtime_session_key or parent.session_id,
            source="orchestration_foreground_observation",
            reason="result observed through foreground reuse",
        )


__all__ = ["TaskRuntimeActivationRunner", "TaskRuntimeParentNotifier"]
