"""Read-only access to a single child's durable user-facing answer."""

from __future__ import annotations

from typing import Any

from opensquilla.gateway.rpc import (
    RpcContext,
    RpcHandlerError,
    RpcUnavailableError,
    get_dispatcher,
)
from opensquilla.orchestration.repository import OrchestrationRepository

_repository: OrchestrationRepository | None = None
_d = get_dispatcher()


def bind_delegated_result_repository(repository: OrchestrationRepository | None) -> None:
    global _repository
    _repository = repository


def unbind_delegated_result_repository(repository: OrchestrationRepository) -> None:
    global _repository
    if _repository is repository:
        _repository = None


def _result_payload(task: Any) -> dict[str, Any]:
    result = task.result if isinstance(task.result, dict) else {}
    return {
        "taskId": task.task_id,
        "sessionId": task.owner_session_id,
        "status": task.outcome.value,
        "summary": str(result.get("summary") or ""),
        "deliverable": str(result.get("deliverable") or ""),
        "error": str(result.get("error") or "") if result.get("error") else None,
    }


@_d.method("delegated_results.get", scope="operator.read")
async def get_delegated_result(params: dict | None, _ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise RpcHandlerError("INVALID_REQUEST", "sessionKey and taskId are required")
    session_key = params.get("sessionKey")
    task_id = params.get("taskId")
    if not isinstance(session_key, str) or not session_key.strip():
        raise RpcHandlerError("INVALID_REQUEST", "sessionKey is required")
    if not isinstance(task_id, str) or not task_id.strip():
        raise RpcHandlerError("INVALID_REQUEST", "taskId is required")
    repository = _repository
    if repository is None:
        raise RpcUnavailableError("Delegated results are not available")
    task = await repository.get_task(task_id)
    run = await repository.get_run(task.run_id) if task is not None else None
    root = await repository.get_session(run.root_session_id) if run is not None else None
    root_task = await repository.get_task(run.root_task_id) if run is not None else None
    if (
        task is None
        or run is None
        or root is None
        or root_task is None
        or root.runtime_session_key != session_key.strip()
        or task.parent_task_id != run.root_task_id
        or not root_task.runtime_context.get("single_agent_mode")
    ):
        raise RpcHandlerError("NOT_FOUND", "Delegated result not found")
    return _result_payload(task)


@_d.method("delegated_results.list", scope="operator.read")
async def list_delegated_results(params: dict | None, _ctx: RpcContext) -> dict[str, Any]:
    session_key = params.get("sessionKey") if isinstance(params, dict) else None
    if not isinstance(session_key, str) or not session_key.strip():
        raise RpcHandlerError("INVALID_REQUEST", "sessionKey is required")
    repository = _repository
    if repository is None:
        raise RpcUnavailableError("Delegated results are not available")
    tasks = await repository.list_single_agent_results(
        root_runtime_session_key=session_key.strip()
    )
    return {"results": [_result_payload(task) for task in tasks]}
