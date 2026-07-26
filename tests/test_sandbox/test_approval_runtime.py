from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.application.approval_queue import ApprovalQueue
from opensquilla.engine.types import ToolCall
from opensquilla.sandbox.approval_runtime import (
    ApprovalAction,
    SandboxOverride,
    SuspendedToolRequest,
    select_sandbox_override,
)
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.elevation import ElevationAction, gate_elevated_action
from opensquilla.sandbox.integration import configure_runtime, reset_runtime
from opensquilla.sandbox.permissions import FileSystemPermissionProfile
from opensquilla.tool_boundary import ToolContinuation
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import (
    CallerKind,
    ToolContext,
    ToolSpec,
    current_tool_context,
)


def test_patch_action_redacts_full_patch_from_audit(tmp_path: Path) -> None:
    patch = "*** Begin Patch\n*** Update File: a.py\n@@\n-old\n+new\n*** End Patch"
    action = ApprovalAction.apply_patch(
        call_id="call-1",
        cwd=tmp_path,
        files=(tmp_path / "a.py",),
        patch=patch,
        justification="Apply the user's requested one-file change.",
    )

    assert "patch" not in action.audit_payload()["payload"]
    assert action.audit_payload()["payload"]["patch_length"] == len(patch)


def test_suspended_request_resumes_the_same_tool_call_object() -> None:
    call = ToolCall(
        tool_use_id="call-1",
        tool_name="write_file",
        arguments={"path": "/tmp/x", "content": "x"},
    )
    suspended = SuspendedToolRequest(
        tool_call=call,
        action=ApprovalAction.filesystem(
            call_id="call-1",
            tool_name="write_file",
            cwd=Path("/tmp"),
            paths=((Path("/tmp/x"), "write"),),
            payload={"content": "x"},
            justification="Write the exact requested file.",
        ),
        metadata={"session_key": "s1"},
    )

    resumed = suspended.approve("approval-1")

    assert resumed is call
    assert resumed.arguments == {"path": "/tmp/x", "content": "x"}
    assert resumed.continuation is not None
    assert resumed.continuation.approval_id == "approval-1"
    assert resumed.continuation.tool_use_id == "call-1"
    assert resumed.continuation.session_key == "s1"
    assert suspended.state == "approved"


def test_escalation_is_forbidden_when_profile_has_denied_reads(tmp_path: Path) -> None:
    profile = FileSystemPermissionProfile.workspace(
        workspace=tmp_path,
        denied_read_roots=(tmp_path / "secret",),
    )

    assert (
        select_sandbox_override("require_escalated", profile)
        is SandboxOverride.NO_OVERRIDE
    )
    assert (
        select_sandbox_override(
            "require_escalated",
            FileSystemPermissionProfile.workspace(workspace=tmp_path),
        )
        is SandboxOverride.DANGER_FULL_ACCESS
    )


def test_runtime_approval_id_is_not_exposed_in_provider_tool_schema() -> None:
    registry = ToolRegistry()

    async def handler(command: str, approval_id: str | None = None) -> str:
        del command, approval_id
        return "ok"

    registry.register(
        ToolSpec(
            name="exec_command",
            description="Execute one command.",
            parameters={
                "command": {"type": "string"},
                "approval_id": {"type": "string"},
            },
            required=["command"],
            runtime_only_arguments=frozenset({"approval_id"}),
        ),
        handler,
    )

    definition = registry.to_tool_definitions(
        ToolContext(is_owner=True, caller_kind=CallerKind.AGENT)
    )[0]

    assert "approval_id" not in definition.input_schema.properties


@pytest.mark.asyncio
async def test_unmarked_plugin_approval_id_remains_model_visible_and_callable() -> None:
    captured: list[str] = []

    async def handler(approval_id: str) -> str:
        captured.append(approval_id)
        return "ok"

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="plugin_business_approval",
            description="Plugin-owned business approval id.",
            parameters={"approval_id": {"type": "string"}},
            required=["approval_id"],
        ),
        handler,
    )
    context = ToolContext(is_owner=True, caller_kind=CallerKind.AGENT)

    definition = registry.to_tool_definitions(context)[0]
    result = await build_tool_handler(registry, context)(
        ToolCall(
            tool_use_id="plugin-call",
            tool_name="plugin_business_approval",
            arguments={"approval_id": "business-123"},
        )
    )

    assert "approval_id" in definition.input_schema.properties
    assert result.content == "ok"
    assert captured == ["business-123"]


def test_gate_resolves_configured_denied_reads_when_profile_is_omitted(
    tmp_path: Path,
) -> None:
    queue = ApprovalQueue(db_path=":memory:")
    configure_runtime(
        SandboxSettings(
            run_mode="trusted",
            backend="noop",
            allow_legacy_mode=True,
            denied_read_roots=[str(tmp_path / "secret")],
        ),
        approval_queue=queue,
        workspace=tmp_path,
    )
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.AGENT,
            session_key="s1",
            workspace_dir=str(tmp_path),
            run_mode="trusted",
        )
    )
    action = ElevationAction(
        tool_name="exec_command",
        action_kind="shell.exec",
        argv=("sh", "-lc", "cat secret/token"),
        cwd=str(tmp_path),
        sandbox_permissions="require_escalated",
        justification="Read the exact requested file outside the sandbox.",
    )
    try:
        result = gate_elevated_action(
            action,
            approval_id=None,
            session_key="s1",
            queue=queue,
        )
        pending = queue.list_pending()
    finally:
        current_tool_context.reset(token)
        reset_runtime()
        queue.close()

    assert result.status == "elevation_forbidden_denied_reads"
    assert result.requested is False
    assert pending == []

def test_elevation_gate_does_not_queue_override_with_denied_reads(tmp_path: Path) -> None:
    queue = ApprovalQueue(db_path=":memory:")
    profile = FileSystemPermissionProfile.workspace(
        workspace=tmp_path,
        denied_read_roots=(tmp_path / "secret",),
    )
    action = ElevationAction(
        tool_name="exec_command",
        action_kind="shell.exec",
        argv=("sh", "-lc", "touch /tmp/probe"),
        cwd=str(tmp_path),
        sandbox_permissions="require_escalated",
        justification="Create the exact requested probe.",
    )
    try:
        result = gate_elevated_action(
            action,
            approval_id=None,
            session_key="s1",
            queue=queue,
            file_system_profile=profile,
        )
    finally:
        queue.close()

    assert result.status == "elevation_forbidden_denied_reads"
    assert result.allowed is False
    assert result.requested is False


@pytest.mark.asyncio
async def test_dispatch_injects_continuation_after_model_schema_validation() -> None:
    captured: list[tuple[str, str]] = []

    async def handler(command: str, approval_id: str) -> str:
        captured.append((command, approval_id))
        return "executed"

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="exec_command",
            description="Execute one command.",
            parameters={"command": {"type": "string"}},
            required=["command"],
            runtime_only_arguments=frozenset({"approval_id"}),
        ),
        handler,
    )
    dispatch = build_tool_handler(
        registry,
        ToolContext(is_owner=True, caller_kind=CallerKind.CLI, session_key="s1"),
    )
    call = ToolCall(
        tool_use_id="call-1",
        tool_name="exec_command",
        arguments={"command": "touch /tmp/probe"},
        continuation=ToolContinuation(
            approval_id="approval-1",
            tool_use_id="call-1",
            session_key="s1",
        ),
    )

    result = await dispatch(call)

    assert result.content == "executed"
    assert captured == [("touch /tmp/probe", "approval-1")]
    assert call.arguments == {"command": "touch /tmp/probe"}


@pytest.mark.asyncio
async def test_dispatch_rejects_continuation_from_another_call_or_session() -> None:
    called = False

    async def handler(command: str, approval_id: str) -> str:
        nonlocal called
        del command, approval_id
        called = True
        return "executed"

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="exec_command",
            description="Execute one command.",
            parameters={"command": {"type": "string"}},
            required=["command"],
            runtime_only_arguments=frozenset({"approval_id"}),
        ),
        handler,
    )
    dispatch = build_tool_handler(
        registry,
        ToolContext(is_owner=True, caller_kind=CallerKind.CLI, session_key="session-b"),
    )
    call = ToolCall(
        tool_use_id="call-b",
        tool_name="exec_command",
        arguments={"command": "touch /tmp/probe"},
        continuation=ToolContinuation(
            approval_id="approval-1",
            tool_use_id="call-a",
            session_key="session-a",
        ),
    )

    result = await dispatch(call)

    assert result.is_error is True
    assert "approval_continuation_mismatch" in result.content
    assert called is False
