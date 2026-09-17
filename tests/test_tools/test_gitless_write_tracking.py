from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.git_runtime import GitRunState
from opensquilla.tools import write_policy
from opensquilla.tools.types import ToolContext, current_tool_context
from opensquilla.tools.write_tracking import (
    WorkspaceMutationSnapshot,
    enforce_workspace_write_deny_effects,
    snapshot_current_workspace_mutations,
    workspace_write_deny_effect_preflight,
    workspace_write_progress_note,
)


def _context(workspace: Path, **kwargs: object) -> ToolContext:
    return ToolContext(workspace_dir=str(workspace), **kwargs)


def test_snapshot_is_lazy_without_a_ledger_or_effect_consumer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _context(tmp_path)
    token = current_tool_context.set(ctx)
    monkeypatch.setattr(
        "opensquilla.tools.write_tracking.run_git",
        lambda *_args, **_kwargs: pytest.fail("unused mutation snapshots must not launch Git"),
    )
    try:
        snapshot = snapshot_current_workspace_mutations()
    finally:
        current_tool_context.reset(token)

    assert snapshot == {}
    assert snapshot.observed is False
    assert snapshot.authoritative is False
    assert snapshot.skip_reason == "no_consumer"


def test_revert_preflight_blocks_when_git_snapshot_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_WORKSPACE_WRITE_DENY_EFFECT", "revert")
    ctx = _context(tmp_path)
    ctx.workspace_write_deny_globs = ["tests/**"]
    token = current_tool_context.set(ctx)
    monkeypatch.setattr(
        "opensquilla.tools.write_tracking.run_git",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=False,
            state=GitRunState.UNAVAILABLE,
            stdout=b"",
        ),
    )
    try:
        before = snapshot_current_workspace_mutations()
        raw = workspace_write_deny_effect_preflight(
            tool_name="exec_command",
            before=before,
        )
    finally:
        current_tool_context.reset(token)

    assert raw is not None
    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["code"] == "WORKSPACE_WRITE_PROTECTION_UNAVAILABLE"
    assert payload["git_state"] == "unavailable"


def test_warn_mode_reports_unavailable_protection_only_once_per_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_WORKSPACE_WRITE_DENY_EFFECT", "warn")
    events: list[dict[str, object]] = []
    ctx = _context(tmp_path, execution_id="turn-1", on_runtime_event=events.append)
    ctx.workspace_write_deny_globs = ["tests/**"]
    token = current_tool_context.set(ctx)
    unavailable = WorkspaceMutationSnapshot(git_state=GitRunState.UNAVAILABLE)
    monkeypatch.setattr(
        "opensquilla.tools.write_tracking.snapshot_workspace_mutations",
        lambda _workspace: unavailable,
    )
    try:
        first = enforce_workspace_write_deny_effects(
            tool_name="exec_command",
            before=unavailable,
            output="first",
        )
        second = enforce_workspace_write_deny_effects(
            tool_name="execute_code",
            before=unavailable,
            output="second",
        )
    finally:
        current_tool_context.reset(token)

    assert first.startswith("[workspace write protection unavailable]")
    assert second == "second"
    records = [
        record
        for record in ctx.workspace_mutation_records
        if record.get("operation") == "effect_enforcement_unavailable"
    ]
    assert len(records) == 1
    assert [event["name"] for event in events] == ["effect_enforcement_unavailable"]


def test_tracked_only_git_unavailable_remains_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "tests" / "test_new.py"
    target.parent.mkdir()
    target.write_text("x\n", encoding="utf-8")
    ctx = _context(tmp_path)
    ctx.workspace_write_deny_globs = ["tests/**"]
    monkeypatch.setenv("OPENSQUILLA_WORKSPACE_WRITE_DENY_TRACKED_ONLY", "on")
    monkeypatch.setattr(
        write_policy,
        "run_git",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=False,
            state=GitRunState.UNAVAILABLE,
            returncode=None,
            stdout=b"",
        ),
    )

    match = write_policy.match_workspace_write_deny(
        target,
        original_path="tests/test_new.py",
        workspace=tmp_path,
        ctx=ctx,
    )

    assert match is not None


def test_workspace_progress_note_omits_hidden_or_unavailable_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _context(tmp_path)
    ctx.workspace_file_writes.append({"relative_path": "src/app.py"})
    ctx.denied_tools.add("git_diff")
    token = current_tool_context.set(ctx)
    monkeypatch.setattr(
        "opensquilla.tools.write_tracking.probe_git_repository",
        lambda *_args, **_kwargs: pytest.fail("hidden git_diff must not probe Git"),
    )
    try:
        note = workspace_write_progress_note()
    finally:
        current_tool_context.reset(token)

    assert "inspect git_diff" not in note
    assert "focused verification" in note


def test_workspace_progress_note_omits_git_outside_a_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _context(tmp_path)
    ctx.workspace_file_writes.append({"relative_path": "src/app.py"})
    token = current_tool_context.set(ctx)
    monkeypatch.setattr(
        "opensquilla.tools.write_tracking.probe_git_repository",
        lambda *_args, **_kwargs: GitRunState.NOT_REPOSITORY,
    )
    try:
        note = workspace_write_progress_note()
    finally:
        current_tool_context.reset(token)

    assert "inspect git_diff" not in note
    assert "focused verification" in note
