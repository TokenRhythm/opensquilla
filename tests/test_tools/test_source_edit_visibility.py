from __future__ import annotations

import asyncio

from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import ToolContext


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
