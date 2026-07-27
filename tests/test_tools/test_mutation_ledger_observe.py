from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, reset_runtime
from opensquilla.tools.builtin.code_exec import execute_code
from opensquilla.tools.builtin.shell import exec_command
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


def _init_git_workspace(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)


def _write_source_with_shell_redirection_command() -> str:
    if os.name == "nt":
        return "New-Item -ItemType Directory -Force -Path src | Out-Null; 'print(1)' > src/app.py"
    return "mkdir -p src && printf 'print(1)\\n' > src/app.py"


def _write_utf8_repro_command() -> str:
    return (
        'python -c "from pathlib import Path; '
        "Path('debug_case.php').write_text('temporary repro\\n', encoding='utf-8')\""
    )


@pytest.fixture
def mutation_context(tmp_path: Path):
    reset_runtime()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _init_git_workspace(workspace)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    configure_runtime(
        SandboxSettings(
            sandbox=False,
            security_grading=False,
            allow_legacy_mode=True,
        ),
        workspace=workspace,
    )
    events: list[dict] = []
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        workspace_dir=str(workspace),
        scratch_dir=str(scratch),
        session_key="agent:main:test",
        on_runtime_event=events.append,
    )
    token = current_tool_context.set(ctx)
    try:
        yield workspace, scratch, ctx, events
    finally:
        current_tool_context.reset(token)
        reset_runtime()


@pytest.mark.asyncio
async def test_exec_command_observes_workspace_mutation_without_model_visible_write(
    mutation_context,
) -> None:
    workspace, _scratch, ctx, events = mutation_context
    result = await exec_command(
        _write_source_with_shell_redirection_command(),
        workdir=str(workspace),
    )

    assert result.startswith("exit_code=0")
    assert ctx.workspace_file_writes == []
    assert ctx.workspace_mutation_records
    assert ctx.workspace_mutation_records[0]["tool"] == "exec_command"
    assert "shell_source_mutation_suspected" not in ctx.workspace_mutation_records[0]
    assert ctx.workspace_mutation_records[0]["paths"] == [
        {
            "relative_path": "src/app.py",
            "status": "??",
            "classification": "source",
        }
    ]
    mutation_events = [
        event for event in events if event.get("name") == "workspace_mutation_observed"
    ]
    assert len(mutation_events) == 1
    event = mutation_events[0]
    assert event["feature"] == "mutation_ledger"
    assert event["tool"] == "exec_command"
    assert event["operation"] == "observe_only"
    assert event["path_count"] == 1
    assert event["paths"] == [
        {
            "relative_path": "src/app.py",
            "status": "??",
            "classification": "source",
        }
    ]
    assert "command_hash" in event
    assert "command" not in event


@pytest.mark.asyncio
async def test_exec_command_observes_suspected_shell_source_mutation(
    mutation_context,
) -> None:
    workspace, _scratch, ctx, events = mutation_context
    ctx.allowed_tools = {
        "read_source",
        "edit_source",
        "source_symbols",
        "grep_search",
        "glob_search",
        "git_status",
        "git_diff",
        "retrieve_tool_result",
        "exec_command",
    }
    result = await exec_command(
        _write_source_with_shell_redirection_command(),
        workdir=str(workspace),
    )

    assert result.startswith("exit_code=0")
    assert ctx.workspace_mutation_records[0]["shell_source_mutation_suspected"] is True
    assert ctx.workspace_mutation_records[0]["shell_source_mutation_targets"] == ["src/app.py"]
    suspected_events = [
        event for event in events if event.get("name") == "shell.source_mutation_suspected"
    ]
    assert len(suspected_events) == 1
    event = suspected_events[0]
    assert event["feature"] == "shell_source_mutation"
    assert event["tool"] == "exec_command"
    assert event["shell_source_mutation_reason"] == "redirection_or_tee"
    assert event["shell_source_mutation_targets"] == ["src/app.py"]
    assert "command_hash" in event
    assert "command" not in event


@pytest.mark.asyncio
async def test_exec_command_read_only_does_not_emit_mutation_event(
    mutation_context,
) -> None:
    workspace, _scratch, _ctx, events = mutation_context
    result = await exec_command("git status --short", workdir=str(workspace))

    assert result.startswith("exit_code=0")
    assert not [event for event in events if event.get("name") == "workspace_mutation_observed"]


@pytest.mark.asyncio
async def test_exec_command_python_read_does_not_emit_source_mutation_signal(
    mutation_context,
) -> None:
    workspace, _scratch, _ctx, events = mutation_context
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")
    result = await exec_command(
        "python -c \"open('src/app.py').read()\"",
        workdir=str(workspace),
    )

    assert result.startswith("exit_code=0")
    assert not [event for event in events if event.get("name") == "shell.source_mutation_suspected"]


@pytest.mark.asyncio
async def test_execute_code_observes_workspace_mutation_classification(
    mutation_context,
) -> None:
    workspace, _scratch, ctx, events = mutation_context
    result = await execute_code(
        "from pathlib import Path\n"
        "Path('tests/test_demo.py').parent.mkdir(exist_ok=True)\n"
        "Path('tests/test_demo.py').write_text('def test_demo(): pass\\n')\n",
        timeout=5,
    )

    payload = json.loads(result)
    assert payload["exit_code"] == 0
    assert ctx.workspace_file_writes == []
    assert ctx.workspace_mutation_records
    assert ctx.workspace_mutation_records[0]["paths"] == [
        {
            "relative_path": "tests/test_demo.py",
            "status": "??",
            "classification": "test-like",
        }
    ]
    mutation_events = [
        event for event in events if event.get("name") == "workspace_mutation_observed"
    ]
    assert len(mutation_events) == 1
    event = mutation_events[0]
    assert event["tool"] == "execute_code"
    assert event["paths"] == [
        {
            "relative_path": "tests/test_demo.py",
            "status": "??",
            "classification": "test-like",
        }
    ]
    assert "code_hash" in event
    assert "code" not in event


@pytest.mark.asyncio
async def test_full_host_exec_command_allows_and_observes_root_repro_files(
    mutation_context,
) -> None:
    workspace, _scratch, ctx, events = mutation_context
    result = await exec_command(
        _write_utf8_repro_command(),
        workdir=str(workspace),
    )

    assert result.startswith("exit_code=0")
    assert (workspace / "debug_case.php").read_text(encoding="utf-8") == "temporary repro\n"
    assert ctx.workspace_mutation_records[0]["paths"] == [
        {
            "relative_path": "debug_case.php",
            "status": "??",
            "classification": "scratch",
        }
    ]
    assert [event for event in events if event.get("name") == "workspace_mutation_observed"]
