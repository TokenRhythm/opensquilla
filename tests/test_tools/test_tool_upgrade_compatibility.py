from __future__ import annotations

import inspect
from dataclasses import fields

from opensquilla.tools import ToolContext, ToolRegistry, tool
from opensquilla.tools.builtin.shell import background_process, exec_command


def test_tool_decorator_preserves_legacy_owner_only_position() -> None:
    registry = ToolRegistry()

    @tool("legacy_tool", "Legacy positional decorator call.", {}, [], True, registry=registry)
    async def legacy_tool() -> str:
        return "ok"

    registered = registry.get("legacy_tool")
    assert registered is not None
    assert registered.spec.owner_only is True
    assert registered.spec.runtime_only_arguments == frozenset()


def test_tool_runtime_only_arguments_is_keyword_only() -> None:
    parameters = inspect.signature(tool).parameters

    assert list(parameters)[:5] == [
        "name",
        "description",
        "params",
        "required",
        "owner_only",
    ]
    assert parameters["runtime_only_arguments"].kind is inspect.Parameter.KEYWORD_ONLY


def test_shell_tools_preserve_legacy_approval_id_positions() -> None:
    exec_bound = inspect.signature(exec_command).bind(
        "printf compat-ok",
        None,
        5.0,
        None,
        None,
        "legacy-exec-approval",
    )
    background_bound = inspect.signature(background_process).bind(
        "printf compat-ok",
        None,
        5.0,
        "legacy-background-approval",
    )

    assert exec_bound.arguments["approval_id"] == "legacy-exec-approval"
    assert background_bound.arguments["approval_id"] == "legacy-background-approval"
    for function in (exec_command, background_process):
        parameters = inspect.signature(function).parameters
        for name in ("sandbox_permissions", "justification", "prefix_rule"):
            assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY


def test_tool_context_appends_new_runtime_fields_after_legacy_fields() -> None:
    field_names = [item.name for item in fields(ToolContext)]

    legacy_runtime_tail = [
        "sandbox_file_system_profile",
        "on_sandbox_auto_review",
        "session_epoch",
        "workspace_id",
        "execution_id",
        "sandbox_session_manager",
        "sandbox_gateway_config",
        "tool_description_overrides",
        "tool_description_overrides_source",
        "endgame_git_freeze_instrumentation_exempt",
        "scratch_verify_mirror_active",
    ]
    legacy_tail_start = field_names.index(legacy_runtime_tail[0])
    assert legacy_tail_start == 64
    assert (
        field_names[legacy_tail_start : legacy_tail_start + len(legacy_runtime_tail)]
        == legacy_runtime_tail
    )
    assert field_names[legacy_tail_start + len(legacy_runtime_tail) :] == [
        "sandbox_policy",
        "channel_admin_verified",
        "collaboration_mode",
        "collaboration_revision",
        "active_plan_revision_id",
        "plan_run_id",
        "plan_storage",
        "plan_event_emitter",
        "user_input_provider",
        "plan_revision",
        "plan_run",
        "goal_run",
        "goal_context",
        "goal_service",
        "artifact_context",
        "artifact_session",
        "desktop_artifact_bridge",
        "artifact_event_emitter",
        "generated_artifact_adopter",
        "exclusive_tools",
        "artifact_mutation_attempt_controller",
        "turn_cleanup_callbacks",
        "tool_result_retrieval_available",
        "parent_session_key",
        "parent_task_id",
        "artifact_candidate_loop_controller",
        "artifact_preview_service",
        "tool_result_media",
    ]
