from __future__ import annotations

import asyncio

from opensquilla.tools.policy_helpers import ToolPolicy, apply_tool_policy
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import ToolContext

LOW_ENTROPY_REPO_CODING_TOOLS = {
    "read_source",
    "edit_source",
    "read_file",
    "grep_search",
    "glob_search",
    "list_dir",
    "git_status",
    "git_diff",
    "retrieve_tool_result",
    "exec_command",
    "process",
}
HIGH_ENTROPY_EDIT_AND_EXEC_TOOLS = {"execute_code", "write_file", "edit_file"}
STRICT_SOURCE_EDIT_TOOLS = {
    "read_source",
    "edit_source",
    "grep_search",
    "glob_search",
    "git_status",
    "git_diff",
    "retrieve_tool_result",
    "exec_command",
    "process",
}
SOURCE_EDIT_V2_TOOLS = {
    "read_source",
    "edit_source",
    "source_symbols",
    "grep_search",
    "glob_search",
    "git_status",
    "git_diff",
    "retrieve_tool_result",
    "exec_command",
    "process",
}
BALANCED_SOURCE_EDIT_TOOLS = {
    "read_source",
    "edit_source",
    "create_source",
    "write_scratch",
    "source_symbols",
    "read_file",
    "grep_search",
    "glob_search",
    "list_dir",
    "git_status",
    "git_diff",
    "retrieve_tool_result",
    "exec_command",
    "process",
}
PATCH_FALLBACK_SOURCE_EDIT_TOOLS = BALANCED_SOURCE_EDIT_TOOLS | {"apply_patch"}
SCAFFOLD_EDIT_TOOLS = {
    "exec_command",
    "process",
    "read_file",
    "edit_file",
    "write_file",
    "glob_search",
    "grep_search",
    "list_dir",
    "git_status",
    "git_diff",
    "retrieve_tool_result",
}
SCAFFOLD_PATCH_TOOLS = SCAFFOLD_EDIT_TOOLS | {"apply_patch"}
STRICT_SOURCE_EDIT_FORBIDDEN_TOOLS = {
    "read_file",
    "list_dir",
    "write_file",
    "edit_file",
    "apply_patch",
    "execute_code",
    "background_process",
    "git_log",
}
SCAFFOLD_FORBIDDEN_TOOLS = {
    "background_process",
    "execute_code",
    "git_log",
    "read_source",
    "edit_source",
    "source_symbols",
}


def _tool_names(ctx: ToolContext) -> set[str]:
    return {tool.name for tool in get_default_registry().to_tool_definitions(ctx)}


def _tool_descriptions(ctx: ToolContext) -> dict[str, str]:
    return {
        tool.name: tool.description
        for tool in get_default_registry().to_tool_definitions(ctx)
    }


def test_source_edit_tools_are_hidden_by_default() -> None:
    names = _tool_names(ToolContext(is_owner=True))

    assert "read_source" not in names
    assert "edit_source" not in names
    assert "create_source" not in names
    assert "write_scratch" not in names
    assert "source_symbols" not in names


def test_source_edit_tools_are_visible_when_surfaced() -> None:
    names = _tool_names(
        ToolContext(
            is_owner=True,
            surfaced_tools={
                "read_source",
                "edit_source",
                "create_source",
                "write_scratch",
                "source_symbols",
            },
        )
    )

    assert "read_source" in names
    assert "edit_source" in names
    assert "create_source" in names
    assert "write_scratch" in names
    assert "source_symbols" in names


def test_source_edit_tools_are_visible_when_explicitly_allowed() -> None:
    names = _tool_names(
        ToolContext(
            is_owner=True,
            allowed_tools={"read_source", "edit_source", "create_source", "write_scratch"},
        )
    )

    assert names == {"read_source", "edit_source", "create_source", "write_scratch"}


def test_repo_coding_source_edit_profile_exposes_low_entropy_surface() -> None:
    registry = get_default_registry()
    updated = apply_tool_policy(
        ToolContext(is_owner=True),
        available_tools=registry.list_names(),
        agent_policy=ToolPolicy(profile="repo_coding_source_edit"),
    )
    names = _tool_names(updated)

    assert names == LOW_ENTROPY_REPO_CODING_TOOLS
    assert HIGH_ENTROPY_EDIT_AND_EXEC_TOOLS.isdisjoint(names)


def test_repo_coding_source_edit_strict_profile_exposes_exact_strict_surface() -> None:
    registry = get_default_registry()
    updated = apply_tool_policy(
        ToolContext(is_owner=True),
        available_tools=registry.list_names(),
        agent_policy=ToolPolicy(profile="repo_coding_source_edit_strict"),
    )
    names = _tool_names(updated)

    assert names == STRICT_SOURCE_EDIT_TOOLS
    assert STRICT_SOURCE_EDIT_FORBIDDEN_TOOLS.isdisjoint(names)


def test_repo_coding_source_edit_v2_profile_exposes_exact_v2_surface() -> None:
    registry = get_default_registry()
    updated = apply_tool_policy(
        ToolContext(is_owner=True),
        available_tools=registry.list_names(),
        agent_policy=ToolPolicy(profile="repo_coding_source_edit_v2"),
    )
    names = _tool_names(updated)

    assert names == SOURCE_EDIT_V2_TOOLS
    assert STRICT_SOURCE_EDIT_FORBIDDEN_TOOLS.isdisjoint(names)


def test_repo_coding_source_edit_balanced_profile_exposes_balanced_surface() -> None:
    registry = get_default_registry()
    updated = apply_tool_policy(
        ToolContext(is_owner=True),
        available_tools=registry.list_names(),
        agent_policy=ToolPolicy(profile="repo_coding_source_edit_balanced"),
    )
    names = _tool_names(updated)

    assert names == BALANCED_SOURCE_EDIT_TOOLS
    assert {"write_file", "edit_file", "apply_patch", "execute_code"}.isdisjoint(names)


def test_repo_coding_source_edit_patch_fallback_profile_adds_only_apply_patch() -> None:
    registry = get_default_registry()
    updated = apply_tool_policy(
        ToolContext(is_owner=True),
        available_tools=registry.list_names(),
        agent_policy=ToolPolicy(profile="repo_coding_source_edit_patch_fallback"),
    )
    names = _tool_names(updated)

    assert names == PATCH_FALLBACK_SOURCE_EDIT_TOOLS
    assert {"write_file", "edit_file", "execute_code"}.isdisjoint(names)


def test_repo_coding_scaffold_edit_profile_exposes_exact_scaffold_surface() -> None:
    registry = get_default_registry()
    updated = apply_tool_policy(
        ToolContext(is_owner=True),
        available_tools=registry.list_names(),
        agent_policy=ToolPolicy(profile="repo_coding_scaffold_edit"),
    )
    names = _tool_names(updated)

    assert names == SCAFFOLD_EDIT_TOOLS
    assert SCAFFOLD_FORBIDDEN_TOOLS.isdisjoint(names)
    assert "apply_patch" not in names


def test_repo_coding_scaffold_patch_profile_adds_only_apply_patch() -> None:
    registry = get_default_registry()
    updated = apply_tool_policy(
        ToolContext(is_owner=True),
        available_tools=registry.list_names(),
        agent_policy=ToolPolicy(profile="repo_coding_scaffold_patch"),
    )
    names = _tool_names(updated)

    assert names == SCAFFOLD_PATCH_TOOLS
    assert SCAFFOLD_FORBIDDEN_TOOLS.isdisjoint(names)


def test_scaffold_edit_descriptions_do_not_reference_hidden_tools() -> None:
    registry = get_default_registry()
    updated = apply_tool_policy(
        ToolContext(is_owner=True),
        available_tools=registry.list_names(),
        agent_policy=ToolPolicy(profile="repo_coding_scaffold_edit"),
    )
    descriptions = "\n".join(_tool_descriptions(updated).values())

    for hidden_tool in SCAFFOLD_FORBIDDEN_TOOLS | {"apply_patch", "read_spreadsheet"}:
        assert hidden_tool not in descriptions


def test_scaffold_patch_descriptions_keep_apply_patch_only_when_visible() -> None:
    registry = get_default_registry()
    updated = apply_tool_policy(
        ToolContext(is_owner=True),
        available_tools=registry.list_names(),
        agent_policy=ToolPolicy(profile="repo_coding_scaffold_patch"),
    )
    descriptions = "\n".join(_tool_descriptions(updated).values())

    assert "apply_patch" in descriptions
    for hidden_tool in SCAFFOLD_FORBIDDEN_TOOLS | {"read_spreadsheet"}:
        assert hidden_tool not in descriptions


def test_list_tools_description_rendering_uses_visible_surface() -> None:
    listed_tools = asyncio.run(
        get_default_registry().list_tools(caller_kind="channel", is_owner=False)
    )
    read_file = next(tool for tool in listed_tools if tool["name"] == "read_file")

    assert "read_spreadsheet" not in str(read_file["description"])


def test_exec_command_description_keeps_source_edit_contract_when_visible() -> None:
    descriptions = _tool_descriptions(
        ToolContext(
            is_owner=True,
            surfaced_tools={"read_source", "edit_source", "source_symbols"},
        )
    )

    assert "read_source" in descriptions["exec_command"]
    assert "edit_source" in descriptions["exec_command"]
