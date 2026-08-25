from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.engine.agent import Agent
from opensquilla.engine.runtime_state_capsule import (
    build_runtime_state_capsule,
    runtime_state_capsule_message,
)
from opensquilla.engine.turn_runner.agent_bootstrap_stage import (
    _runtime_state_capsule_mode_from_env,
)
from opensquilla.engine.types import AgentConfig
from opensquilla.git_runtime import GitRunState
from opensquilla.provider.types import Message
from opensquilla.tools.mutation_receipts import (
    fingerprint_file,
    record_semantic_mutation_receipt,
)
from opensquilla.tools.types import ToolContext


def _init_git_workspace(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)


def _commit_file(workspace: Path, relative_path: str, text: str) -> Path:
    target = workspace / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", relative_path], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=workspace, check=True)
    return target


def test_runtime_state_capsule_reports_source_diff_and_last_mutation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _init_git_workspace(workspace)
    source = _commit_file(workspace, "src/app.py", "print('base')\n")
    ctx = ToolContext(workspace_dir=str(workspace), workspace_epoch=0)

    before = fingerprint_file(source)
    source.write_text("print('changed')\n", encoding="utf-8")
    after = fingerprint_file(source)
    record_semantic_mutation_receipt(
        tool_name="edit_source",
        path=source,
        operation="edit_source",
        before=before,
        after=after,
        partial=False,
        ctx=ctx,
    )

    capsule = build_runtime_state_capsule(workspace=workspace, tool_context=ctx)

    assert capsule["schema"] == "runtime_state_capsule_v1"
    assert capsule["workspace"]["epoch"] == 1
    assert capsule["workspace"]["source_diff"] is True
    assert capsule["workspace"]["source_paths"] == ["src/app.py"]
    assert capsule["workspace"]["scratch_only"] is False
    assert capsule["last_mutation"] == {
        "tool": "edit_source",
        "operation": "edit_source",
        "path": "src/app.py",
        "classification": "source",
        "changed": True,
        "partial": False,
        "workspace_epoch": 1,
    }


def test_runtime_state_capsule_message_is_stable_json(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _init_git_workspace(workspace)
    _commit_file(workspace, "src/app.py", "print('base')\n")
    ctx = ToolContext(workspace_dir=str(workspace), workspace_epoch=0)

    message = runtime_state_capsule_message(
        build_runtime_state_capsule(workspace=workspace, tool_context=ctx)
    )

    prefix = "Runtime state capsule:\n"
    assert message.startswith(prefix)
    payload = json.loads(message.removeprefix(prefix))
    assert payload["schema"] == "runtime_state_capsule_v1"
    assert payload["workspace"]["source_diff"] is False


def test_runtime_state_capsule_marks_git_unknown_without_false_blocking_fact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = ToolContext(workspace_dir=str(tmp_path), workspace_epoch=1)
    ctx.workspace_mutation_receipts.append(
        {
            "relative_path": "src/app.py",
            "classification": "source",
            "changed": True,
            "workspace_epoch": 1,
        }
    )
    monkeypatch.setattr(
        "opensquilla.engine.runtime_state_capsule.run_git",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=False,
            state=GitRunState.UNAVAILABLE,
            stdout=b"",
        ),
    )

    capsule = build_runtime_state_capsule(workspace=tmp_path, tool_context=ctx)

    assert capsule["workspace"]["git_state"] == "unavailable"
    assert capsule["workspace"]["diff_observed"] is False
    assert capsule["workspace"]["diff"] is False
    assert capsule["workspace"]["diff_paths"] == []
    assert capsule["finalization"]["source_diff_present"] is False
    assert capsule["finalization"]["blocking_facts"] == []


def test_agent_runtime_state_capsule_injection_is_opt_in(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _init_git_workspace(workspace)
    source = _commit_file(workspace, "src/app.py", "print('base')\n")
    ctx = ToolContext(workspace_dir=str(workspace), workspace_epoch=0)

    before = fingerprint_file(source)
    source.write_text("print('changed')\n", encoding="utf-8")
    after = fingerprint_file(source)
    record_semantic_mutation_receipt(
        tool_name="edit_source",
        path=source,
        operation="edit_source",
        before=before,
        after=after,
        partial=False,
        ctx=ctx,
    )

    agent = Agent(
        provider=None,  # type: ignore[arg-type]
        config=AgentConfig(
            workspace_dir=str(workspace),
            runtime_state_capsule_mode="inject",
        ),
        tool_context=ctx,
    )
    request_messages, _sanitize = agent._provider_request_messages_with_sanitize(
        [Message(role="user", content="fix this")],
        request_context_message=None,
        request_context_insert_index=1,
        runtime_context_message=Agent._runtime_context_message("runtime"),
        runtime_context_insert_index=1,
    )

    assert any(
        isinstance(message.content, str)
        and message.content.startswith("Runtime state capsule:\n")
        for message in request_messages
    )

    off_agent = Agent(
        provider=None,  # type: ignore[arg-type]
        config=AgentConfig(workspace_dir=str(workspace), runtime_state_capsule_mode="off"),
        tool_context=ctx,
    )
    off_messages, _sanitize = off_agent._provider_request_messages_with_sanitize(
        [Message(role="user", content="fix this")],
        request_context_message=None,
        request_context_insert_index=1,
        runtime_context_message=Agent._runtime_context_message("runtime"),
        runtime_context_insert_index=1,
    )

    assert not any(
        isinstance(message.content, str)
        and message.content.startswith("Runtime state capsule:\n")
        for message in off_messages
    )


def test_runtime_state_capsule_mode_env_parser(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_RUNTIME_STATE_CAPSULE_MODE", "inject")

    assert _runtime_state_capsule_mode_from_env("off") == "inject"

    monkeypatch.setenv("OPENSQUILLA_RUNTIME_STATE_CAPSULE_MODE", "bad")
    assert _runtime_state_capsule_mode_from_env("inject") == "off"

    monkeypatch.delenv("OPENSQUILLA_RUNTIME_STATE_CAPSULE_MODE")
    assert _runtime_state_capsule_mode_from_env("log") == "log"
