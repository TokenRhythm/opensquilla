from __future__ import annotations

import asyncio
import threading
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
async def test_write_file_repeated_stop_waits_for_commit_and_receipt(
    monkeypatch: pytest.MonkeyPatch,
    workspace_context: tuple[Path, ToolContext, list[dict[str, Any]]],
) -> None:
    workspace, ctx, _events = workspace_context
    target = workspace / "src" / "stopped.py"
    worker_started = threading.Event()
    release_worker = threading.Event()
    real_write_text = Path.write_text

    def gated_write_text(
        path: Path,
        data: str,
        *args: Any,
        **kwargs: Any,
    ) -> int:
        if path == target:
            worker_started.set()
            assert release_worker.wait(timeout=2.0)
        return real_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", gated_write_text)
    write_file = _original_async(filesystem.write_file)
    task = asyncio.create_task(write_file(str(target), "settled = True\n"))
    assert await asyncio.to_thread(worker_started.wait, 0.5)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert not target.exists()

    release_worker.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.5)

    assert target.read_text(encoding="utf-8") == "settled = True\n"
    assert len(ctx.workspace_mutation_receipts) == 1
    assert ctx.workspace_mutation_receipts[0]["changed"] is True
    assert ctx.workspace_epoch == 1
    assert len(ctx.workspace_file_writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    ["write_file", "write_scratch", "create_source", "edit_file", "edit_source"],
)
async def test_executor_mutation_tools_settle_before_repeated_stop(
    tool_name: str,
    monkeypatch: pytest.MonkeyPatch,
    workspace_context: tuple[Path, ToolContext, list[dict[str, Any]]],
) -> None:
    workspace, ctx, _events = workspace_context
    worker_started = threading.Event()
    release_worker = threading.Event()
    real_run_executor_mutation = filesystem._run_executor_mutation

    async def gated_run_executor_mutation(
        worker: Callable[[], Any],
        *,
        settle: Callable[[BaseException | None], None],
    ) -> Any:
        def gated_worker() -> Any:
            worker_started.set()
            assert release_worker.wait(timeout=2.0)
            return worker()

        return await real_run_executor_mutation(gated_worker, settle=settle)

    monkeypatch.setattr(filesystem, "_run_executor_mutation", gated_run_executor_mutation)

    target = workspace / "src" / f"{tool_name}.py"
    expected = "after = True\n"
    if tool_name == "write_file":
        operation = _original_async(filesystem.write_file)(str(target), expected)
    elif tool_name == "write_scratch":
        scratch = workspace / ".opensquilla-scratch"
        scratch.mkdir()
        ctx.scratch_dir = str(scratch)
        target = scratch / "repro.py"
        operation = _original_async(filesystem.write_scratch)("repro.py", expected)
    elif tool_name == "create_source":
        operation = _original_async(filesystem.create_source)(str(target), expected)
    elif tool_name == "edit_file":
        target.parent.mkdir()
        target.write_text("before = False\n", encoding="utf-8")
        await filesystem.read_file(str(target))
        operation = _original_async(filesystem.edit_file)(
            str(target),
            old_text="before = False\n",
            new_text=expected,
        )
    else:
        target.parent.mkdir()
        target.write_text("before = False\n", encoding="utf-8")
        revision = filesystem.source_revision_for_path(target)
        operation = _original_async(filesystem.edit_source)(
            str(target),
            expected_revision=revision,
            edits=[{"start_line": 1, "end_line": 1, "replacement": expected}],
        )

    task = asyncio.create_task(operation)
    assert await asyncio.to_thread(worker_started.wait, 0.5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release_worker.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.5)

    assert target.read_text(encoding="utf-8") == expected
    if tool_name == "write_scratch":
        assert len(ctx.scratch_file_writes) == 1
        assert ctx.workspace_mutation_receipts == []
    else:
        assert len(ctx.workspace_mutation_receipts) == 1
        assert ctx.workspace_mutation_receipts[0]["changed"] is True
        assert len(ctx.workspace_file_writes) == 1


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
