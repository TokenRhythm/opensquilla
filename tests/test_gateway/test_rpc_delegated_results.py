from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.gateway.orchestration_runtime import build_orchestration_runtime
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.orchestration.models import OrchestrationMode
from opensquilla.orchestration.repository import OrchestrationRepository
from opensquilla.orchestration.service import DelegateRequest


@pytest.mark.asyncio
async def test_single_agent_result_rpc_reads_only_own_durable_child_result(tmp_path) -> None:
    repository = await OrchestrationRepository.open(tmp_path / "orchestration.db")
    runtime = build_orchestration_runtime(
        repository=repository,
        session_manager=SimpleNamespace(),
        task_runtime=SimpleNamespace(),
        config=SimpleNamespace(llm=SimpleNamespace(model="test-model")),
        registry=SimpleNamespace(),
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
            root_runtime_session_key="agent:main:webchat:owner",
            root_runtime_context={"single_agent_mode": True},
        )
        child = await runtime.service.delegate(
            DelegateRequest(
                run_id="single-run",
                parent_session_id="single-root",
                parent_task_id="single-task",
                task_key="whole-report",
                task="Write the report",
                acceptance_criteria="Complete the request",
                inherited_tools=tools,
                registered_tools=tools,
                runtime_context={"single_agent_mode": True},
            )
        )
        await runtime.service.complete(
            child.activation.activation_id,
            result={"summary": "done", "deliverable": "The complete report."},
        )
        second_child = await runtime.service.delegate(
            DelegateRequest(
                run_id="single-run",
                parent_session_id="single-root",
                parent_task_id="single-task",
                task_key="database-migration",
                task="Migrate the database schema",
                acceptance_criteria="Return the applied migration and verification result",
                inherited_tools=tools,
                registered_tools=tools,
                runtime_context={"single_agent_mode": True},
            )
        )
        await runtime.service.complete(
            second_child.activation.activation_id,
            result={"summary": "migration done", "deliverable": "Migration verified."},
        )

        permitted = await get_dispatcher().dispatch(
            "result-owner",
            "delegated_results.get",
            {
                "sessionKey": "agent:main:webchat:owner",
                "taskId": child.task.task_id,
            },
            RpcContext(conn_id="result-owner"),
        )
        assert permitted.ok, permitted.error
        assert permitted.payload["deliverable"] == "The complete report."
        assert permitted.payload["taskId"] == child.task.task_id

        listed = await get_dispatcher().dispatch(
            "result-list-owner",
            "delegated_results.list",
            {"sessionKey": "agent:main:webchat:owner"},
            RpcContext(conn_id="result-list-owner"),
        )
        assert listed.ok, listed.error
        assert [item["taskId"] for item in listed.payload["results"]] == [
            child.task.task_id,
            second_child.task.task_id,
        ]
        assert listed.payload["results"][0]["deliverable"] == "The complete report."
        assert listed.payload["results"][1]["deliverable"] == "Migration verified."

        denied = await get_dispatcher().dispatch(
            "result-other",
            "delegated_results.get",
            {
                "sessionKey": "agent:main:webchat:other",
                "taskId": child.task.task_id,
            },
            RpcContext(conn_id="result-other"),
        )
        assert not denied.ok
        assert "The complete report." not in str(denied.error)
    finally:
        await runtime.close()

    closed = await get_dispatcher().dispatch(
        "result-after-close",
        "delegated_results.get",
        {
            "sessionKey": "agent:main:webchat:owner",
            "taskId": child.task.task_id,
        },
        RpcContext(conn_id="result-after-close"),
    )
    assert not closed.ok
    assert closed.error is not None
    assert closed.error.code == "UNAVAILABLE"
