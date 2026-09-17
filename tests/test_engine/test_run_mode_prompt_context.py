from __future__ import annotations

from opensquilla.engine.runtime import TurnRunner
from opensquilla.tools.types import CallerKind, ToolContext


def test_full_host_access_tool_context_is_visible_to_model_prompt() -> None:
    ctx = ToolContext(
        caller_kind=CallerKind.WEB,
        run_mode="full",
        workspace_dir="/workspace/.opensquilla/workspace",
    )

    extra = TurnRunner._extra_context_for_tool_context(ctx)

    execution_context = extra["Execution Context"]
    assert "Run mode: Full Host Access" in execution_context
    assert "Execution target: host" in execution_context
    assert "Sandbox: disabled for tool execution" in execution_context
    assert (
        "Writes outside the workspace do not require OpenSquilla sandbox approval"
        in execution_context
    )
    assert (
        "Do not use sandbox_permissions=require_escalated in Full Host Access"
        in execution_context
    )


def test_legacy_trusted_prompt_uses_safe_mode_guidance() -> None:
    ctx = ToolContext(
        caller_kind=CallerKind.WEB,
        run_mode="trusted",
        workspace_dir="/workspace/.opensquilla/workspace",
    )

    extra = TurnRunner._extra_context_for_tool_context(ctx)

    execution_context = extra["Execution Context"]
    assert "Run mode: Safe" in execution_context
    assert "Default execution target: sandbox" in execution_context
    assert "writes stay within declared writable roots" in execution_context
    install_guidance = (
        "Do not refuse a user-requested installation merely because the default path "
        "starts sandboxed"
    )
    assert install_guidance in execution_context
