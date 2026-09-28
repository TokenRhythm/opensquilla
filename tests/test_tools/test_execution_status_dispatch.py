from __future__ import annotations

import asyncio
import json

import pytest

from opensquilla.engine.types import ToolCall
from opensquilla.result_budget import ToolResultBudgetPolicy
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolContext, ToolSpec


def _registry(name: str, result: str) -> ToolRegistry:
    registry = ToolRegistry()

    async def handler() -> str:
        return result

    registry.register(ToolSpec(name=name, description=name, parameters={}), handler)
    return registry


@pytest.mark.asyncio
async def test_dispatch_propagates_cancellation_to_outer_timeout() -> None:
    registry = ToolRegistry()
    cancelled = asyncio.Event()

    async def handler() -> str:
        try:
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "late"

    registry.register(
        ToolSpec(name="slow_tool", description="slow_tool", parameters={}), handler
    )
    handler = build_tool_handler(registry)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            handler(ToolCall("call_slow_timeout", "slow_tool", {})),
            timeout=0.01,
        )

    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_exec_command_nonzero_exit_gets_trusted_execution_status() -> None:
    handler = build_tool_handler(_registry("exec_command", "exit_code=2\nfailed"))

    result = await handler(ToolCall("call_exec_1", "exec_command", {}))

    assert result.is_error is True
    assert result.execution_status == {
        "version": 1,
        "status": "error",
        "exit_code": 2,
        "timed_out": False,
        "truncated": False,
        "reason": "nonzero_exit",
        "source": "adapter",
        "preservation_class": "diagnostic",
    }


@pytest.mark.asyncio
async def test_exec_command_runtime_error_is_reported_as_tool_failure() -> None:
    content = "[error] shell could not be started"
    handler = build_tool_handler(_registry("exec_command", content))

    result = await handler(ToolCall("call_exec_spawn_failed", "exec_command", {}))

    assert result.content == content
    assert result.is_error is True
    assert result.execution_status is not None
    assert result.execution_status["status"] == "error"
    assert result.execution_status["exit_code"] is None
    assert result.execution_status["reason"] == "runtime_error"
    assert result.execution_status["preservation_class"] == "diagnostic"


@pytest.mark.asyncio
async def test_exec_command_error_text_in_stdout_does_not_override_success() -> None:
    content = "exit_code=0\n[error] synthetic error message used as test data\n"
    handler = build_tool_handler(_registry("exec_command", content))

    result = await handler(ToolCall("call_exec_printed_error", "exec_command", {}))

    assert result.content == content
    assert result.is_error is False
    assert result.execution_status is not None
    assert result.execution_status["status"] == "success"
    assert result.execution_status["exit_code"] == 0
    assert result.execution_status["reason"] is None


@pytest.mark.asyncio
async def test_unified_exec_running_receipt_gets_background_status() -> None:
    content = json.dumps({
        "status": "ok",
        "execution_id": "exec-1",
        "session": {"session_id": "exec-1", "status": "running", "returncode": None},
    })
    handler = build_tool_handler(_registry("exec_command", content))

    result = await handler(ToolCall("call_exec_running", "exec_command", {}))

    assert result.is_error is False
    assert result.execution_status is not None
    assert result.execution_status["status"] == "unknown"
    assert result.execution_status["reason"] == "background_running"


@pytest.mark.asyncio
async def test_unified_exec_completed_receipt_gets_exit_status() -> None:
    content = json.dumps({
        "status": "ok",
        "execution_id": "exec-2",
        "exited": True,
        "session": {"session_id": "exec-2", "status": "done", "returncode": 0},
    })
    handler = build_tool_handler(_registry("exec_command", content))

    result = await handler(ToolCall("call_exec_done", "exec_command", {}))

    assert result.is_error is False
    assert result.execution_status is not None
    assert result.execution_status["status"] == "success"
    assert result.execution_status["exit_code"] == 0


@pytest.mark.parametrize("tool_name", ["exec_command", "background_process"])
async def test_runtime_unavailable_is_a_trusted_tool_error(tool_name: str) -> None:
    payload = {
        "status": "failed",
        "code": "RUNTIME_UNAVAILABLE",
        "componentId": "node",
        "retryable": False,
    }
    handler = build_tool_handler(_registry(tool_name, json.dumps(payload)))

    result = await handler(ToolCall("call_runtime_missing", tool_name, {}))

    assert result.is_error is True
    assert result.execution_status is not None
    assert result.execution_status["reason"] == "runtime_unavailable"
    assert result.execution_status["status"] == "error"


@pytest.mark.asyncio
async def test_execute_code_timeout_gets_trusted_execution_status() -> None:
    handler = build_tool_handler(
        _registry(
            "execute_code",
            json.dumps(
                {
                    "exit_code": 124,
                    "stdout": "",
                    "stderr": "timed out",
                    "timed_out": True,
                }
            ),
        )
    )

    result = await handler(ToolCall("call_code_1", "execute_code", {}))

    assert result.is_error is True
    assert result.execution_status["status"] == "timeout"
    assert result.execution_status["timed_out"] is True
    assert result.execution_status["reason"] == "tool_timeout"


@pytest.mark.asyncio
async def test_unmapped_json_is_not_trusted_as_execution_status() -> None:
    handler = build_tool_handler(_registry("unknown_tool", json.dumps({"exit_code": 1})))

    result = await handler(ToolCall("call_unknown_1", "unknown_tool", {}))

    assert result.is_error is False
    assert result.execution_status is None


@pytest.mark.asyncio
async def test_execute_code_json_without_exit_code_is_not_trusted() -> None:
    handler = build_tool_handler(_registry("execute_code", json.dumps({"ok": False})))

    result = await handler(ToolCall("call_code_untrusted", "execute_code", {}))

    assert result.is_error is False
    assert result.execution_status is None


@pytest.mark.asyncio
async def test_background_process_running_is_unknown_non_error() -> None:
    handler = build_tool_handler(
        _registry("background_process", "session_id=abc123\ncommand: sleep 1\nstatus: running")
    )

    result = await handler(ToolCall("call_bg_running", "background_process", {}))

    assert result.is_error is False
    assert result.execution_status is not None
    assert result.execution_status["status"] == "unknown"
    assert result.execution_status["reason"] == "background_running"
    assert result.execution_status["preservation_class"] == "ephemeral"


@pytest.mark.asyncio
async def test_background_process_terminal_nonzero_is_error() -> None:
    handler = build_tool_handler(
        _registry(
            "process",
            json.dumps(
                {
                    "status": "ok",
                    "action": "poll",
                    "session": {
                        "status": "done",
                        "returncode": 7,
                        "timed_out": False,
                        "killed": False,
                    },
                }
            ),
        )
    )

    result = await handler(ToolCall("call_bg_failed", "process", {}))

    assert result.is_error is True
    assert result.execution_status is not None
    assert result.execution_status["status"] == "error"
    assert result.execution_status["exit_code"] == 7
    assert result.execution_status["reason"] == "nonzero_exit"


@pytest.mark.parametrize("wait_mode", ["any", "all"])
@pytest.mark.parametrize(
    "second,status,reason,is_error",
    [
        ({"status": "done", "returncode": 0}, "success", None, False),
        ({"status": "running", "returncode": None}, "unknown", "background_running", False),
        ({"status": "done", "returncode": 7}, "error", "nonzero_exit", True),
        ({"status": "timed_out", "returncode": -15}, "timeout", "tool_timeout", True),
        ({"status": "killed", "returncode": -9}, "cancelled", "killed", True),
    ],
)
async def test_multi_execution_wait_has_trusted_aggregate_status(
    wait_mode, second, status, reason, is_error,
) -> None:
    content = json.dumps({
        "status": "ok", "action": "wait", "wait_mode": wait_mode,
        "exited": wait_mode == "any" or second["status"] != "running",
        "sessions": [
            {"session_id": "first", "status": "done", "returncode": 0},
            {"session_id": "second", **second},
        ],
    })
    handler = build_tool_handler(_registry("process", content))

    result = await handler(ToolCall("call_multi_wait", "process", {}))

    assert result.content == content
    assert result.is_error is is_error
    assert result.execution_status is not None
    assert result.execution_status["status"] == status
    assert result.execution_status["reason"] == reason
    assert result.execution_status["timed_out"] is (status == "timeout")


@pytest.mark.parametrize("wait_mode", ["any", "all"])
async def test_multi_execution_wait_reports_failure_while_another_process_runs(wait_mode) -> None:
    content = json.dumps({
        "status": "ok", "action": "wait", "wait_mode": wait_mode,
        "exited": wait_mode == "any",
        "sessions": [
            {"session_id": "running", "status": "running", "returncode": None},
            {"session_id": "failed", "status": "done", "returncode": 8},
        ],
    })
    handler = build_tool_handler(_registry("process", content))

    result = await handler(ToolCall("call_multi_failure", "process", {}))

    assert result.is_error is True
    assert result.execution_status is not None
    assert result.execution_status["status"] == "error"
    assert result.execution_status["exit_code"] == 8


@pytest.mark.parametrize("tool_name", ["exec_command", "background_process"])
async def test_started_pty_initialization_failure_is_a_trusted_tool_error(tool_name: str) -> None:
    content = json.dumps({
        "status": "capability_error",
        "reason": "pty_started_but_handle_initialization_failed",
        "io_mode_requested": "pty",
        "fallback_reason": "synthetic resize failure after spawn",
    })
    handler = build_tool_handler(_registry(tool_name, content))

    result = await handler(ToolCall("call_pty_initialization_failed", tool_name, {}))

    assert result.content == content
    assert result.is_error is True
    assert result.execution_status is not None
    assert result.execution_status["status"] == "error"
    assert result.execution_status["reason"] == "pty_started_but_handle_initialization_failed"
    assert result.execution_status["exit_code"] is None
    assert result.execution_status["preservation_class"] == "diagnostic"


@pytest.mark.asyncio
async def test_approval_denial_preserves_approval_denied_reason() -> None:
    handler = build_tool_handler(
        _registry(
            "exec_command",
            json.dumps({"status": "approval_denied", "message": "operator rejected"}),
        )
    )

    result = await handler(ToolCall("call_approval_denied", "exec_command", {}))

    assert result.is_error is True
    assert result.terminates_turn is False
    assert result.execution_status is not None
    assert result.execution_status["status"] == "error"
    assert result.execution_status["reason"] == "approval_denied"


@pytest.mark.asyncio
async def test_budget_truncation_marks_status_truncated_without_changing_failure() -> None:
    handler = build_tool_handler(
        _registry("exec_command", "exit_code=1\n" + ("x" * 2000)),
        ToolContext(
            tool_result_budget_policy=ToolResultBudgetPolicy(
                max_single_tool_result_chars=160,
                max_tool_result_chars_per_turn=160,
            )
        ),
    )

    result = await handler(ToolCall("call_exec_2", "exec_command", {}))

    assert result.is_error is True
    assert result.execution_status["status"] == "error"
    assert result.execution_status["truncated"] is True
    assert result.execution_status["preservation_class"] == "retain_summary"
