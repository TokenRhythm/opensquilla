from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from opensquilla.tools.builtin import filesystem
from opensquilla.tools.types import (
    CallerKind,
    RetryableToolInputError,
    ToolContext,
    current_tool_context,
)


def _original_async(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__  # type: ignore[attr-defined]
    return fn


@pytest.fixture
def workspace_context(
    tmp_path: Path,
) -> Iterator[tuple[Path, ToolContext, list[dict[str, Any]]]]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    events: list[dict[str, Any]] = []
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        workspace_dir=str(workspace),
        session_key="agent:main:test",
        on_runtime_event=events.append,
    )
    token = current_tool_context.set(ctx)
    try:
        yield workspace, ctx, events
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_write_file_records_changed_semantic_receipt(
    workspace_context: tuple[Path, ToolContext, list[dict[str, Any]]],
) -> None:
    workspace, ctx, events = workspace_context
    target = workspace / "src" / "app.py"
    write_file = _original_async(filesystem.write_file)

    result = await write_file(str(target), "print('hello')\n")

    assert "Written" in result
    receipt = ctx.workspace_mutation_receipts[-1]
    assert receipt["changed"] is True
    assert receipt["relative_path"] == "src/app.py"
    assert receipt["workspace_epoch"] == 1
    assert any(
        event.get("name") == "workspace.semantic_mutation_receipt" for event in events
    )


@pytest.mark.asyncio
async def test_write_file_records_noop_semantic_receipt(
    workspace_context: tuple[Path, ToolContext, list[dict[str, Any]]],
) -> None:
    workspace, ctx, _events = workspace_context
    target = workspace / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("same\n", encoding="utf-8")
    write_file = _original_async(filesystem.write_file)

    await write_file(str(target), "same\n")

    receipt = ctx.workspace_mutation_receipts[-1]
    assert receipt["changed"] is False
    assert receipt["relative_path"] == "src/app.py"
    assert receipt["workspace_epoch"] == 0


@pytest.mark.asyncio
async def test_edit_file_rejects_noop_edit(
    workspace_context: tuple[Path, ToolContext, list[dict[str, Any]]],
) -> None:
    workspace, ctx, _events = workspace_context
    target = workspace / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("alpha\n", encoding="utf-8")
    edit_file = _original_async(filesystem.edit_file)

    await filesystem.read_file(str(target))
    # An edit whose old_text == new_text changes nothing; it must be rejected so the
    # model retries with the intended change instead of recording a phantom success.
    with pytest.raises(RetryableToolInputError):
        await edit_file(str(target), old_text="alpha\n", new_text="alpha\n")

    assert target.read_text(encoding="utf-8") == "alpha\n"
    assert ctx.workspace_epoch == 0


@pytest.mark.asyncio
async def test_edit_file_records_changed_semantic_receipt(
    workspace_context: tuple[Path, ToolContext, list[dict[str, Any]]],
) -> None:
    workspace, ctx, _events = workspace_context
    target = workspace / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("alpha\n", encoding="utf-8")
    edit_file = _original_async(filesystem.edit_file)

    await filesystem.read_file(str(target))
    await edit_file(str(target), old_text="alpha\n", new_text="beta\n")

    receipt = ctx.workspace_mutation_receipts[-1]
    assert receipt["changed"] is True
    assert receipt["operation"] == "edit_file"
    assert receipt["relative_path"] == "src/app.py"
    assert receipt["workspace_epoch"] == 1
