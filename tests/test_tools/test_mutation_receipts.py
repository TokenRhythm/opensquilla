from __future__ import annotations

import subprocess
from pathlib import Path

from opensquilla.tools.mutation_receipts import (
    fingerprint_file,
    record_semantic_mutation_receipt,
)
from opensquilla.tools.types import ToolContext, current_tool_context


def _run_git(workspace: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
    )


def _init_git_workspace(workspace: Path, relative_path: str = "src/app.py") -> Path:
    target = workspace / relative_path
    target.parent.mkdir(parents=True)
    target.write_text("before\n", encoding="utf-8")
    _run_git(workspace, "init")
    _run_git(workspace, "config", "user.email", "test@example.com")
    _run_git(workspace, "config", "user.name", "Test User")
    _run_git(workspace, "add", ".")
    _run_git(workspace, "commit", "-m", "init")
    return target


def test_changed_source_mutation_receipt_increments_workspace_epoch(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('before')\n")
    before = fingerprint_file(target)
    target.write_text("print('after')\n")
    after = fingerprint_file(target)

    events: list[dict] = []
    ctx = ToolContext(
        workspace_dir=str(workspace),
        on_runtime_event=events.append,
        session_key="agent:main:test",
    )
    token = current_tool_context.set(ctx)
    try:
        receipt = record_semantic_mutation_receipt(
            tool_name="edit_file",
            path=target,
            operation="edit_file",
            before=before,
            after=after,
            partial=False,
        )
    finally:
        current_tool_context.reset(token)

    assert receipt is not None
    assert receipt["changed"] is True
    assert receipt["workspace_epoch"] == 1
    assert receipt["relative_path"] == "src/app.py"
    assert receipt["classification"] == "source"
    assert ctx.workspace_epoch == 1
    assert ctx.workspace_mutation_receipts == [receipt]
    assert events[0]["name"] == "workspace.semantic_mutation_receipt"


def test_changed_source_mutation_receipt_captures_candidate(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    target = _init_git_workspace(workspace)
    before = fingerprint_file(target)
    target.write_text("after\n", encoding="utf-8")
    after = fingerprint_file(target)

    ctx = ToolContext(workspace_dir=str(workspace))
    token = current_tool_context.set(ctx)
    try:
        receipt = record_semantic_mutation_receipt(
            tool_name="edit_source",
            path=target,
            operation="edit_source",
            before=before,
            after=after,
            partial=False,
        )
    finally:
        current_tool_context.reset(token)

    assert receipt is not None
    assert receipt["receipt_id"].startswith("mut-")
    assert len(ctx.source_diff_candidates) == 1
    assert ctx.source_diff_candidates[0]["paths"] == ["src/app.py"]
    assert ctx.source_diff_candidates[0]["receipt_id"] == receipt["receipt_id"]


def test_hidden_configured_scratch_receipt_is_not_source(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    target = _init_git_workspace(workspace, ".artifacts/repro.py")
    before = fingerprint_file(target)
    target.write_text("after\n", encoding="utf-8")
    after = fingerprint_file(target)

    ctx = ToolContext(
        workspace_dir=str(workspace),
        scratch_dir=str(workspace / ".artifacts"),
    )
    token = current_tool_context.set(ctx)
    try:
        receipt = record_semantic_mutation_receipt(
            tool_name="edit_file",
            path=target,
            operation="edit_file",
            before=before,
            after=after,
            partial=False,
        )
    finally:
        current_tool_context.reset(token)

    assert receipt is not None
    assert receipt["classification"] == "scratch"
    assert ctx.source_diff_candidates == []


def test_noop_mutation_receipt_does_not_increment_workspace_epoch(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('same')\n")
    before = fingerprint_file(target)
    after = fingerprint_file(target)

    ctx = ToolContext(workspace_dir=str(workspace))
    token = current_tool_context.set(ctx)
    try:
        receipt = record_semantic_mutation_receipt(
            tool_name="write_file",
            path=target,
            operation="write_file",
            before=before,
            after=after,
            partial=False,
        )
    finally:
        current_tool_context.reset(token)

    assert receipt is not None
    assert receipt["changed"] is False
    assert receipt["workspace_epoch"] == 0
    assert ctx.workspace_epoch == 0
    assert ctx.source_diff_candidates == []


def test_test_like_mutation_receipt_does_not_capture_candidate(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    target = _init_git_workspace(workspace, "tests/test_app.py")
    before = fingerprint_file(target)
    target.write_text("after\n", encoding="utf-8")
    after = fingerprint_file(target)

    ctx = ToolContext(workspace_dir=str(workspace))
    token = current_tool_context.set(ctx)
    try:
        receipt = record_semantic_mutation_receipt(
            tool_name="edit_source",
            path=target,
            operation="edit_source",
            before=before,
            after=after,
            partial=False,
        )
    finally:
        current_tool_context.reset(token)

    assert receipt is not None
    assert receipt["classification"] == "test-like"
    assert ctx.source_diff_candidates == []


def test_mutation_receipt_returns_none_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.py"
    workspace.mkdir()
    outside.write_text("print('outside')\n")
    before = fingerprint_file(outside)
    after = fingerprint_file(outside)

    ctx = ToolContext(workspace_dir=str(workspace))
    token = current_tool_context.set(ctx)
    try:
        receipt = record_semantic_mutation_receipt(
            tool_name="write_file",
            path=outside,
            operation="write_file",
            before=before,
            after=after,
            partial=False,
        )
    finally:
        current_tool_context.reset(token)

    assert receipt is None
    assert ctx.workspace_mutation_receipts == []
    assert ctx.workspace_epoch == 0


def test_mutation_receipt_swallows_runtime_event_exceptions(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('before')\n")
    before = fingerprint_file(target)
    target.write_text("print('after')\n")
    after = fingerprint_file(target)

    def raise_on_event(_event: dict) -> None:
        raise RuntimeError("boom")

    ctx = ToolContext(workspace_dir=str(workspace), on_runtime_event=raise_on_event)
    token = current_tool_context.set(ctx)
    try:
        receipt = record_semantic_mutation_receipt(
            tool_name="edit_file",
            path=target,
            operation="edit_file",
            before=before,
            after=after,
            partial=False,
        )
    finally:
        current_tool_context.reset(token)

    assert receipt is not None
    assert receipt["changed"] is True
    assert ctx.workspace_epoch == 1
    assert ctx.workspace_mutation_receipts == [receipt]
