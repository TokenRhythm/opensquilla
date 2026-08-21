from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.engine.patch_evidence_ledger import PatchEvidenceLedger
from opensquilla.git_runtime import GitRunState
from opensquilla.tools.types import ToolContext, current_tool_context
from opensquilla.tools.write_tracking import record_workspace_file_read


def test_patch_evidence_ledger_writes_observe_only_snapshot(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("print('old')\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/app.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    source.write_text("print('new')\n", encoding="utf-8")
    ledger_path = tmp_path / "ledger.json"
    ledger = PatchEvidenceLedger(
        path=str(ledger_path),
        workspace_dir=str(tmp_path),
        session_key="session-1",
        agent_id="main",
    )
    ledger.record_tool_result(
        iteration=2,
        tool_name="exec_command",
        arguments={"command": "pytest tests/test_app.py"},
        result_text="FAILED tests/test_app.py::test_app - expected old actual new",
        is_error=True,
        duration_ms=123,
        failure_anchors=["FAILED tests/test_app.py::test_app - expected old actual new"],
        focused_verification=True,
    )
    ledger.write_final(
        read_records=[
            {"relative_path": "src/app.py", "operation": "read_file", "offset": 1, "limit": 20}
        ],
        write_records=[{"relative_path": "src/app.py", "operation": "apply_patch_update"}],
        scratch_records=[],
        final_status="ok",
        iterations=3,
        provider_call_count=4,
    )

    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert payload["summary"]["read_file_count"] == 1
    assert payload["summary"]["changed_file_count"] == 1
    assert payload["summary"]["verification_command_count"] == 1
    assert payload["summary"]["failure_anchor_count"] == 1
    assert payload["diff_paths"] == ["src/app.py"]
    assert payload["verification_commands"][0]["command"] == "pytest tests/test_app.py"


def test_patch_evidence_ledger_redacts_secret_like_text(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.json"
    secret = "sk-or-v1-abcdefghijklmnopqrstuvwxyz"
    ledger = PatchEvidenceLedger(
        path=str(ledger_path),
        workspace_dir=str(tmp_path),
        session_key="session-1",
        agent_id="main",
    )
    ledger.record_tool_result(
        iteration=1,
        tool_name="exec_command",
        arguments={"command": f"env | grep OPENROUTER_API_KEY={secret}"},
        result_text=f"env.OPENROUTER_API_KEY={secret}",
        is_error=True,
        duration_ms=1,
        failure_anchors=[f"env.OPENROUTER_API_KEY={secret}"],
        focused_verification=True,
    )
    ledger.write_final(
        read_records=[],
        write_records=[],
        scratch_records=[],
        final_status="ok",
        iterations=1,
        provider_call_count=1,
    )

    text = ledger_path.read_text(encoding="utf-8")
    assert secret not in text
    assert "env.OPENROUTER_API_KEY=[REDACTED]" in text


def test_patch_evidence_ledger_records_unknown_git_without_claiming_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path = tmp_path / "ledger.json"
    calls: list[tuple[str, ...]] = []

    def unavailable(args, **_kwargs):
        calls.append(tuple(args))
        return SimpleNamespace(ok=False, state=GitRunState.NOT_REPOSITORY)

    monkeypatch.setattr(
        "opensquilla.engine.patch_evidence_ledger.run_git",
        unavailable,
    )
    ledger = PatchEvidenceLedger(
        path=str(ledger_path),
        workspace_dir=str(tmp_path),
        session_key="session-1",
        agent_id="main",
    )

    ledger.write_final(
        read_records=[],
        write_records=[{"relative_path": "src/app.py", "operation": "edit"}],
        scratch_records=[],
        final_status="ok",
        iterations=1,
        provider_call_count=1,
    )

    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert calls == [("status", "--porcelain=v1", "--untracked-files=all")]
    assert payload["git_state"] == "not_repository"
    assert payload["git_diff_observed"] is False
    # Keep the v1 list/int shape for older ledger consumers; the additive
    # observed flag is the authoritative distinction from a clean workspace.
    assert payload["diff_paths"] == []
    assert payload["summary"]["diff_path_count"] == 0


def test_record_workspace_file_read_tracks_workspace_relative_path(tmp_path: Path) -> None:
    path = tmp_path / "pkg" / "module.py"
    path.parent.mkdir()
    path.write_text("x = 1\n", encoding="utf-8")
    ctx = ToolContext(workspace_dir=str(tmp_path))
    token = current_tool_context.set(ctx)
    try:
        record_workspace_file_read(path, operation="read_file", offset=2, limit=5)
    finally:
        current_tool_context.reset(token)

    assert ctx.workspace_file_reads == [
        {
            "path": str(path.resolve(strict=False)),
            "relative_path": "pkg/module.py",
            "name": "module.py",
            "suffix": ".py",
            "operation": "read_file",
            "offset": 2,
            "limit": 5,
        }
    ]
