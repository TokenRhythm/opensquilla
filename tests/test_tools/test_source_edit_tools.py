from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest

from opensquilla.tools.builtin import filesystem
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import (
    CallerKind,
    RetryableToolInputError,
    ToolContext,
    ToolError,
    current_tool_context,
)


def _original_async(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__  # type: ignore[attr-defined]
    return fn


@pytest.fixture
def workspace_context(tmp_path: Path) -> Iterator[tuple[Path, ToolContext]]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        workspace_dir=str(workspace),
        session_key="agent:main:test",
    )
    token = current_tool_context.set(ctx)
    try:
        yield workspace, ctx
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_read_source_returns_revision_receipt_and_plain_lines(
    workspace_context: tuple[Path, ToolContext],
) -> None:
    workspace, _ctx = workspace_context
    target = workspace / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = await filesystem.read_source("src/app.py", start_line=2, end_line=3)

    payload = json.loads(result)
    assert payload["status"] == "success"
    assert payload["path"] == "src/app.py"
    assert payload["revision"].startswith("file_")
    assert payload["range"] == [2, 3]
    assert payload["total_lines"] == 3
    assert payload["lines"] == [
        {"line": 2, "text": "two"},
        {"line": 3, "text": "three"},
    ]


@pytest.mark.asyncio
async def test_read_source_records_workspace_read(
    workspace_context: tuple[Path, ToolContext],
) -> None:
    workspace, ctx = workspace_context
    target = workspace / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")

    await filesystem.read_source(str(target), start_line=1, end_line=2)

    assert ctx.workspace_file_reads[-1] == {
        "path": str(target),
        "relative_path": "src/app.py",
        "name": "app.py",
        "suffix": ".py",
        "operation": "read_source",
        "offset": 1,
        "limit": 2,
        "complete": False,
    }


@pytest.mark.asyncio
async def test_read_source_defaults_end_line_and_returns_total_lines(
    workspace_context: tuple[Path, ToolContext],
) -> None:
    workspace, ctx = workspace_context
    target = workspace / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = await filesystem.read_source("src/app.py", start_line=2)

    payload = json.loads(result)
    assert payload["status"] == "success"
    assert payload["path"] == "src/app.py"
    assert payload["range"] == [2, 3]
    assert payload["total_lines"] == 3
    assert payload["lines"] == [
        {"line": 2, "text": "two"},
        {"line": 3, "text": "three"},
    ]
    assert ctx.workspace_file_reads[-1]["offset"] == 2
    assert ctx.workspace_file_reads[-1]["limit"] == 2


@pytest.mark.asyncio
async def test_read_source_defaults_start_line_to_first_line(
    workspace_context: tuple[Path, ToolContext],
) -> None:
    workspace, ctx = workspace_context
    target = workspace / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = await filesystem.read_source("src/app.py")

    payload = json.loads(result)
    assert payload["status"] == "success"
    assert payload["range"] == [1, 3]
    assert payload["total_lines"] == 3
    assert payload["lines"][0] == {"line": 1, "text": "one"}
    assert ctx.workspace_file_reads[-1]["offset"] == 1
    assert ctx.workspace_file_reads[-1]["limit"] == 3


@pytest.mark.asyncio
async def test_read_source_rejects_binary_file(
    workspace_context: tuple[Path, ToolContext],
) -> None:
    workspace, _ctx = workspace_context
    target = workspace / "src" / "blob.txt"
    target.parent.mkdir()
    target.write_bytes(b"abc\x00def")

    with pytest.raises(ToolError, match="NUL"):
        await filesystem.read_source(str(target), start_line=1, end_line=1)


@pytest.mark.asyncio
async def test_glob_search_returns_workspace_relative_paths(
    workspace_context: tuple[Path, ToolContext],
) -> None:
    workspace, _ctx = workspace_context
    target = workspace / "pkg" / "module.py"
    target.parent.mkdir()
    target.write_text("VALUE = 1\n", encoding="utf-8")

    result = await filesystem.glob_search("**/*.py")

    assert "pkg/module.py" in result
    assert str(workspace) not in result


@pytest.mark.asyncio
async def test_grep_search_returns_workspace_relative_paths(
    workspace_context: tuple[Path, ToolContext],
) -> None:
    workspace, _ctx = workspace_context
    target = workspace / "pkg" / "module.py"
    target.parent.mkdir()
    target.write_text("VALUE = 1\n", encoding="utf-8")

    result = await filesystem.grep_search("VALUE")

    assert "pkg/module.py:1: VALUE = 1" in result
    assert str(workspace) not in result


def test_read_source_is_not_visible_unless_surfaced_or_allowed() -> None:
    registry = get_default_registry()
    default_names = {tool.name for tool in registry.to_tool_definitions(ToolContext(is_owner=True))}
    surfaced_names = {
        tool.name
        for tool in registry.to_tool_definitions(
            ToolContext(is_owner=True, surfaced_tools={"read_source"})
        )
    }
    allowed_names = {
        tool.name
        for tool in registry.to_tool_definitions(
            ToolContext(is_owner=True, allowed_tools={"read_source"})
        )
    }

    assert "read_source" not in default_names
    assert "read_source" in surfaced_names
    assert allowed_names == {"read_source"}


def test_read_source_schema_requires_only_path() -> None:
    registry = get_default_registry()
    tools = registry.to_tool_definitions(ToolContext(is_owner=True, surfaced_tools={"read_source"}))
    read_source = next(tool for tool in tools if tool.name == "read_source")

    assert read_source.input_schema.required == ["path"]


def test_scoped_source_write_tools_are_hidden_unless_surfaced() -> None:
    registry = get_default_registry()
    default_names = {tool.name for tool in registry.to_tool_definitions(ToolContext(is_owner=True))}
    surfaced_names = {
        tool.name
        for tool in registry.to_tool_definitions(
            ToolContext(is_owner=True, surfaced_tools={"create_source", "write_scratch"})
        )
    }

    assert "create_source" not in default_names
    assert "write_scratch" not in default_names
    assert {"create_source", "write_scratch"}.issubset(surfaced_names)


@pytest.mark.asyncio
async def test_create_source_creates_new_workspace_file_with_mutation_receipt(
    workspace_context: tuple[Path, ToolContext],
) -> None:
    workspace, ctx = workspace_context
    create_source = _original_async(filesystem.create_source)

    result = await create_source("src/new_module.py", "VALUE = 1\n")

    payload = json.loads(result)
    target = workspace / "src" / "new_module.py"
    assert payload["status"] == "created"
    assert payload["path"] == "src/new_module.py"
    assert payload["changed"] is True
    assert payload["after_revision"].startswith("file_")
    assert payload["workspace_epoch"] == 1
    assert "+VALUE = 1" in payload["diff_summary"]
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"
    receipt = ctx.workspace_mutation_receipts[-1]
    assert receipt["tool"] == "create_source"
    assert receipt["operation"] == "create_source"
    assert receipt["relative_path"] == "src/new_module.py"
    assert receipt["changed"] is True
    assert receipt["workspace_epoch"] == 1


@pytest.mark.asyncio
async def test_create_source_rejects_existing_file(
    workspace_context: tuple[Path, ToolContext],
) -> None:
    workspace, _ctx = workspace_context
    create_source = _original_async(filesystem.create_source)
    target = workspace / "src" / "existing.py"
    target.parent.mkdir()
    target.write_text("old\n", encoding="utf-8")

    with pytest.raises(RetryableToolInputError, match="already exists"):
        await create_source("src/existing.py", "new\n")

    assert target.read_text(encoding="utf-8") == "old\n"


@pytest.mark.asyncio
async def test_create_source_rejects_scratch_path(
    workspace_context: tuple[Path, ToolContext],
) -> None:
    workspace, ctx = workspace_context
    create_source = _original_async(filesystem.create_source)
    scratch = workspace / ".opensquilla-scratch"
    scratch.mkdir()
    ctx.scratch_dir = str(scratch)

    with pytest.raises(ToolError, match="scratch"):
        await create_source(".opensquilla-scratch/repro.py", "print('x')\n")


@pytest.mark.asyncio
async def test_edit_source_rejects_scratch_path(
    workspace_context: tuple[Path, ToolContext],
) -> None:
    workspace, ctx = workspace_context
    edit_source = _original_async(filesystem.edit_source)
    scratch = workspace / ".opensquilla-scratch"
    scratch.mkdir()
    target = scratch / "repro.py"
    target.write_text("before\n", encoding="utf-8")
    ctx.scratch_dir = str(scratch)

    with pytest.raises(ToolError, match="edit_source refused a scratch path"):
        await edit_source(
            ".opensquilla-scratch/repro.py",
            "unused-revision",
            [{"start_line": 1, "end_line": 1, "replacement": "after\n"}],
        )

    assert target.read_text(encoding="utf-8") == "before\n"


@pytest.mark.asyncio
async def test_write_scratch_writes_only_configured_scratch_dir(
    workspace_context: tuple[Path, ToolContext],
) -> None:
    workspace, ctx = workspace_context
    write_scratch = _original_async(filesystem.write_scratch)
    scratch = workspace / ".opensquilla-scratch"
    scratch.mkdir()
    ctx.scratch_dir = str(scratch)

    result = await write_scratch("repro.py", "print('x')\n")

    payload = json.loads(result)
    target = scratch / "repro.py"
    assert payload["status"] == "written"
    assert payload["path"] == "repro.py"
    assert payload["scratch"] is True
    assert payload["changed"] is True
    assert payload["bytes"] == len(b"print('x')\n")
    assert payload["sha256"]
    assert target.read_text(encoding="utf-8") == "print('x')\n"
    assert ctx.scratch_file_writes[-1]["relative_path"] == "repro.py"
    assert ctx.workspace_mutation_receipts == []


@pytest.mark.parametrize("scratch_relation", ["workspace_ancestor", "same_root"])
@pytest.mark.asyncio
async def test_write_scratch_rejects_workspace_target_for_overlapping_roots(
    workspace_context: tuple[Path, ToolContext],
    scratch_relation: str,
) -> None:
    workspace, ctx = workspace_context
    write_scratch = _original_async(filesystem.write_scratch)
    ctx.scratch_dir = str(
        workspace.parent if scratch_relation == "workspace_ancestor" else workspace
    )

    target = workspace / "src.py"
    with pytest.raises(ToolError, match="workspace target"):
        await write_scratch(str(target), "changed\n")

    assert not target.exists()


@pytest.mark.asyncio
async def test_write_scratch_requires_scratch_dir(
    workspace_context: tuple[Path, ToolContext],
) -> None:
    write_scratch = _original_async(filesystem.write_scratch)

    with pytest.raises(ToolError, match="scratch_dir"):
        await write_scratch("repro.py", "print('x')\n")


@pytest.mark.asyncio
async def test_write_scratch_rejects_paths_outside_scratch(
    workspace_context: tuple[Path, ToolContext],
) -> None:
    workspace, ctx = workspace_context
    write_scratch = _original_async(filesystem.write_scratch)
    scratch = workspace / ".opensquilla-scratch"
    scratch.mkdir()
    ctx.scratch_dir = str(scratch)

    with pytest.raises(ToolError, match="scratch"):
        await write_scratch("../src/app.py", "print('x')\n")


@pytest.mark.asyncio
async def test_source_symbols_returns_workspace_relative_symbol_receipts(
    workspace_context: tuple[Path, ToolContext],
) -> None:
    workspace, _ctx = workspace_context
    target = workspace / "pkg" / "module.py"
    target.parent.mkdir()
    target.write_text(
        "class Widget:\n"
        "    def render(self):\n"
        "        return 1\n\n"
        "def helper(value):\n"
        "    return value\n",
        encoding="utf-8",
    )

    result = await filesystem.source_symbols(query="helper", path="pkg")

    payload = json.loads(result)
    assert payload["status"] == "success"
    assert payload["results"] == [
        {
            "path": "pkg/module.py",
            "line": 5,
            "kind": "function",
            "name": "helper",
            "preview": "def helper(value):",
        }
    ]


@pytest.mark.asyncio
async def test_source_symbols_has_no_required_arguments(
    workspace_context: tuple[Path, ToolContext],
) -> None:
    workspace, _ctx = workspace_context
    target = workspace / "src" / "app.go"
    target.parent.mkdir()
    target.write_text("package main\n\nfunc Run() {}\n", encoding="utf-8")

    result = await filesystem.source_symbols()

    payload = json.loads(result)
    assert payload["status"] == "success"
    assert payload["results"][0]["path"] == "src/app.go"
    assert payload["results"][0]["name"] == "Run"


@pytest.mark.asyncio
async def test_edit_source_applies_revision_gated_line_edit(
    workspace_context: tuple[Path, ToolContext],
) -> None:
    workspace, ctx = workspace_context
    edit_source = _original_async(filesystem.edit_source)
    target = workspace / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    read_payload = json.loads(await filesystem.read_source("src/app.py", start_line=1, end_line=3))

    result = await edit_source(
        "src/app.py",
        expected_revision=read_payload["revision"],
        edits=[{"start_line": 2, "end_line": 2, "replacement": "BETA\n"}],
    )

    payload = json.loads(result)
    assert payload["status"] == "applied"
    assert payload["path"] == "src/app.py"
    assert payload["changed"] is True
    assert payload["before_revision"] == read_payload["revision"]
    assert payload["after_revision"].startswith("file_")
    assert payload["after_revision"] != read_payload["revision"]
    assert payload["workspace_epoch"] == 1
    assert "-beta" in payload["diff_summary"]
    assert "+BETA" in payload["diff_summary"]
    assert target.read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"
    receipt = ctx.workspace_mutation_receipts[-1]
    assert receipt["operation"] == "edit_source"
    assert receipt["changed"] is True
    assert receipt["workspace_epoch"] == 1


@pytest.mark.asyncio
async def test_edit_source_noop_does_not_increment_workspace_epoch(
    workspace_context: tuple[Path, ToolContext],
) -> None:
    workspace, ctx = workspace_context
    edit_source = _original_async(filesystem.edit_source)
    target = workspace / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    read_payload = json.loads(await filesystem.read_source("src/app.py", start_line=1, end_line=2))

    result = await edit_source(
        "src/app.py",
        expected_revision=read_payload["revision"],
        edits=[{"start_line": 2, "end_line": 2, "replacement": "beta\n"}],
    )

    payload = json.loads(result)
    assert payload["changed"] is False
    assert payload["workspace_epoch"] == 0
    assert ctx.workspace_epoch == 0
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\n"


@pytest.mark.asyncio
async def test_edit_source_revision_conflict_leaves_file_unchanged(
    workspace_context: tuple[Path, ToolContext],
) -> None:
    workspace, _ctx = workspace_context
    edit_source = _original_async(filesystem.edit_source)
    target = workspace / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    read_payload = json.loads(await filesystem.read_source("src/app.py", start_line=1, end_line=2))
    target.write_text("alpha\nchanged\n", encoding="utf-8")

    with pytest.raises(RetryableToolInputError, match="revision_conflict"):
        await edit_source(
            "src/app.py",
            expected_revision=read_payload["revision"],
            edits=[{"start_line": 2, "end_line": 2, "replacement": "BETA\n"}],
        )

    assert target.read_text(encoding="utf-8") == "alpha\nchanged\n"


@pytest.mark.asyncio
async def test_edit_source_invalid_line_edit_leaves_file_unchanged(
    workspace_context: tuple[Path, ToolContext],
) -> None:
    workspace, _ctx = workspace_context
    edit_source = _original_async(filesystem.edit_source)
    target = workspace / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    read_payload = json.loads(await filesystem.read_source("src/app.py", start_line=1, end_line=2))

    with pytest.raises(RetryableToolInputError, match="line range"):
        await edit_source(
            "src/app.py",
            expected_revision=read_payload["revision"],
            edits=[{"start_line": 3, "end_line": 3, "replacement": "gamma\n"}],
        )

    assert target.read_text(encoding="utf-8") == "alpha\nbeta\n"


@pytest.mark.asyncio
async def test_edit_source_overlapping_ranges_leave_file_unchanged(
    workspace_context: tuple[Path, ToolContext],
) -> None:
    workspace, _ctx = workspace_context
    edit_source = _original_async(filesystem.edit_source)
    target = workspace / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    read_payload = json.loads(await filesystem.read_source("src/app.py", start_line=1, end_line=3))

    with pytest.raises(RetryableToolInputError, match="overlap"):
        await edit_source(
            "src/app.py",
            expected_revision=read_payload["revision"],
            edits=[
                {"start_line": 1, "end_line": 2, "replacement": "x\n"},
                {"start_line": 2, "end_line": 3, "replacement": "y\n"},
            ],
        )

    assert target.read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"
