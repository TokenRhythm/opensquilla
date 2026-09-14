from __future__ import annotations

import inspect
from dataclasses import fields

from opensquilla.tools import ToolContext, ToolRegistry, tool
from opensquilla.tools.builtin.shell import background_process, exec_command


def test_builtin_registration_names_resolve_to_packaged_modules() -> None:
    from importlib.util import find_spec

    from opensquilla.tools import builtin

    assert "submit_tool" not in builtin.__all__
    assert all(find_spec(f"{builtin.__name__}.{name}") is not None for name in builtin.__all__)


def test_tool_decorator_preserves_legacy_owner_only_position() -> None:
    registry = ToolRegistry()

    @tool("legacy_tool", "Legacy positional decorator call.", {}, [], True, registry=registry)
    async def legacy_tool() -> str:
        return "ok"

    registered = registry.get("legacy_tool")
    assert registered is not None
    assert registered.spec.owner_only is True
    assert registered.spec.runtime_only_arguments == frozenset()


def test_tool_decorator_preserves_legacy_exposure_position_and_keyword() -> None:
    registry = ToolRegistry()

    @tool("legacy_positional", "Legacy positional access.", {}, [], False, False, registry=registry)
    async def legacy_positional() -> str:
        return "ok"

    @tool(
        "legacy_keyword",
        "Legacy keyword access.",
        registry=registry,
        exposed_by_default=False,
    )
    async def legacy_keyword() -> str:
        return "ok"

    for name in ("legacy_positional", "legacy_keyword"):
        registered = registry.get(name)
        assert registered is not None
        assert registered.spec.default_access == "deny"
        assert registered.spec.exposed_by_default is False


def test_tool_spec_preserves_legacy_exposure_keyword() -> None:
    from opensquilla.tools.types import ToolSpec

    spec = ToolSpec(
        name="legacy_spec",
        description="Legacy direct ToolSpec construction.",
        parameters={},
        exposed_by_default=False,
    )

    assert spec.default_access == "deny"
    assert spec.exposed_by_default is False


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
        "generated_artifact_adopter",
        "turn_cleanup_callbacks",
        "tool_result_retrieval_available",
        "parent_session_key",
        "parent_task_id",
        "tool_result_media",
        "session_id",
        "authorized_tool_names",
        "disclosed_tool_names",
        "tool_search_index",
        "tool_search_namespaces",
        "image_analysis_target",
        "desktop_browser",
        "artifact_source_paths",
        "workspace_preview_opener",
        "workspace_preview_scopes",
        "tool_result_store_max_bytes",
        "tool_result_store_disk_budget_bytes",
        "tool_result_store_retention_seconds",
    ]


def test_tool_context_preserves_complete_legacy_positional_constructor() -> None:
    defaults = ToolContext()
    # Preserve the complete constructor published before output-spool fields.
    legacy_fields = fields(ToolContext)[:105]
    assert legacy_fields[-1].name == "workspace_preview_scopes"
    legacy_values = [getattr(defaults, item.name) for item in legacy_fields]
    source_paths = {}
    preview_scopes = [{"path": "preview", "scope": "workspace"}]
    legacy_values[-3] = source_paths
    legacy_values[-1] = preview_scopes

    context = ToolContext(*legacy_values)

    assert context.artifact_source_paths is source_paths
    assert context.workspace_preview_scopes is preview_scopes
    assert context.tool_result_store_max_bytes == 8 * 1024 * 1024
    assert context.tool_result_store_disk_budget_bytes == 256 * 1024 * 1024
    assert context.tool_result_store_retention_seconds == 7 * 24 * 60 * 60
