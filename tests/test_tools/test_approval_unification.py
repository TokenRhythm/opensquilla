from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, reset_runtime
from opensquilla.tools.builtin import code_exec, shell
from opensquilla.tools.builtin import patch as patch_tool
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


def _original_async(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__  # type: ignore[attr-defined]
    return fn


@pytest.fixture(autouse=True)
def _reset_state():
    reset_approval_queue()
    reset_runtime()
    yield
    reset_runtime()
    reset_approval_queue()


@pytest.mark.asyncio
async def test_shell_warnlist_uses_sandbox_gate_without_exec_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class _Runtime:
        effective = SimpleNamespace(sandbox_enabled=True)

    async def _fake_gate_action(**kwargs):
        calls.append(("gate", kwargs))
        policy = SimpleNamespace()
        request = SimpleNamespace(cwd="/tmp", action_kind="shell.exec", policy=policy)
        return object(), policy, request

    async def _fake_run_under_backend(request, *, runtime=None):
        calls.append(("backend", request))
        return SimpleNamespace(
            returncode=0,
            stdout="sandboxed\n",
            stderr="",
            backend_notes=(),
        )

    monkeypatch.setattr(shell, "get_runtime", lambda: _Runtime())
    monkeypatch.setattr(shell, "gate_action", _fake_gate_action)
    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(
            allowed=True,
            needs_approval=True,
            reason="command requires approval",
        ),
    )

    token = current_tool_context.set(
        ToolContext(is_owner=True, caller_kind=CallerKind.CLI, session_key="s1")
    )
    try:
        result = await shell.exec_command("rm x")
    finally:
        current_tool_context.reset(token)

    assert "sandboxed" in result
    assert get_approval_queue().list_pending("exec") == []
    assert [name for name, _ in calls] == ["gate", "backend"]
    hints = calls[0][1]["hints"]  # type: ignore[index]
    assert hints.high_impact is True


@pytest.mark.asyncio
async def test_apply_patch_workspace_escape_requires_explicit_elevation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("old\n", encoding="utf-8")
    configure_runtime(
        SandboxSettings(
            run_mode="standard",
            backend="noop",
            allow_legacy_mode=True,
            exclude_slash_tmp=True,
            exclude_tmpdir_env_var=True,
        ),
        workspace=workspace,
    )
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(workspace),
            session_key="s1",
        )
    )
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            f"""*** Begin Patch
*** Update File: {outside.as_posix()}
@@@ -1,1 +1,1 @@@
-old
+new
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result)
    assert payload["status"] == "elevation_required"
    assert payload["reason"] == "outside_writable_roots"
    assert payload["paths"] == [str(outside.resolve())]
    assert get_approval_queue().list_pending("exec") == []
    assert outside.read_text(encoding="utf-8") == "old\n"


@pytest.mark.asyncio
async def test_apply_patch_approved_absolute_escape_uses_shared_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("old\n", encoding="utf-8")
    configure_runtime(
        SandboxSettings(
            run_mode="standard",
            backend="noop",
            allow_legacy_mode=True,
            exclude_slash_tmp=True,
            exclude_tmpdir_env_var=True,
        ),
        workspace=workspace,
    )
    actions = []

    def allow(action, **_kwargs):
        actions.append(action)
        return SimpleNamespace(allowed=True)

    monkeypatch.setattr(patch_tool, "gate_elevated_action", allow)
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(workspace),
            session_key="s1",
        )
    )
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            f"""*** Begin Patch
*** Update File: {outside.as_posix()}
@@ -1,1 +1,1 @@
-old
+new
*** End Patch""",
            sandbox_permissions="require_escalated",
            justification="Modify the exact user-requested file outside the workspace.",
        )
    finally:
        current_tool_context.reset(token)

    assert result.startswith("Applied patch: 1 file(s) modified")
    assert outside.read_text(encoding="utf-8") == "new\n"
    assert len(actions) == 1
    assert actions[0].tool_name == "apply_patch"
    assert actions[0].target_paths == ((str(outside.resolve()), "write"),)


@pytest.mark.asyncio
async def test_destructive_code_exec_without_runtime_does_not_create_exec_approval() -> None:
    token = current_tool_context.set(
        ToolContext(is_owner=True, caller_kind=CallerKind.CLI, session_key="s1")
    )
    try:
        result = await code_exec.execute_code("import os\nos.remove('target.txt')")
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result)
    assert payload["status"] == "denied"
    assert payload["reason"] == "runtime_unconfigured"
    assert get_approval_queue().list_pending("exec") == []
