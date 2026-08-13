"""Regression test: configured sandbox-off execution is Full Host Access."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from opensquilla.application.approval_queue import get_approval_queue, reset_approval_queue
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, reset_runtime
from opensquilla.tools.builtin import code_exec
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


@pytest.mark.asyncio
async def test_destructive_code_exec_runs_without_approval_when_sandbox_disabled(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "target.txt").write_text("keep me\n", encoding="utf-8")

    configure_runtime(
        SandboxSettings(sandbox=False, security_grading=False),
        workspace=workspace,
    )
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(workspace),
            run_mode="standard",
            session_key="s1",
        )
    )
    try:
        result = await code_exec.execute_code("import os\nos.remove('target.txt')")
    finally:
        current_tool_context.reset(token)
        reset_runtime()

    try:
        payload = json.loads(result)
        assert payload["exit_code"] == 0
        assert not (workspace / "target.txt").exists()
        assert get_approval_queue().list_pending("exec") == []
    finally:
        reset_approval_queue()


@pytest.mark.skipif(os.name != "posix", reason="process group behavior is POSIX-specific")
@pytest.mark.asyncio
async def test_code_exec_caller_cancel_stops_child_process_tree(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "code-child-ran"
    child = (
        "import pathlib, time; "
        f"time.sleep(0.6); pathlib.Path({str(marker)!r}).write_text('ran')"
    )
    code = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "time.sleep(30)"
    )

    configure_runtime(
        SandboxSettings(sandbox=False, security_grading=False),
        workspace=workspace,
    )
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(workspace),
            run_mode="standard",
            session_key="s1",
            task_id="task-code-exec",
        )
    )
    running = asyncio.create_task(code_exec.execute_code(code, timeout=30.0))
    try:
        await asyncio.sleep(0.1)
        running.cancel()
        cancelled = await asyncio.gather(running, return_exceptions=True)
        assert isinstance(cancelled[0], asyncio.CancelledError)
        await asyncio.sleep(0.8)
        assert not marker.exists()
    finally:
        current_tool_context.reset(token)
        reset_runtime()
