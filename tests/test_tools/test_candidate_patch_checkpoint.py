from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.git_runtime import GitRunState
from opensquilla.tools.candidate_patch_checkpoint import (
    _git_show_head_path,
    create_candidate_patch_checkpoint,
    restore_candidate_patch_checkpoint,
)


def test_git_show_head_path_disambiguates_revision_like_paths(
    monkeypatch,
    tmp_path: Path,
) -> None:
    recorded: list[str] = []

    def fake_run(args, **kwargs):
        del kwargs
        recorded.extend(args)
        return SimpleNamespace(ok=True, stdout=b"content")

    monkeypatch.setattr(
        "opensquilla.tools.candidate_patch_checkpoint.run_git",
        fake_run,
    )

    assert _git_show_head_path(tmp_path, "--help") == b"content"
    assert recorded == ["show", "--end-of-options", "HEAD:--help"]


def _init_git_workspace(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)


def _commit_file(workspace: Path, relative_path: str, text: str) -> Path:
    target = workspace / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", relative_path], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=workspace, check=True)
    return target


def test_candidate_patch_checkpoint_restores_tracked_and_untracked_files(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _init_git_workspace(workspace)
    source = _commit_file(workspace, "src/app.py", "print('base')\n")

    checkpoint = create_candidate_patch_checkpoint(workspace, label="before-candidate")

    source.write_text("print('candidate')\n", encoding="utf-8")
    scratch = workspace / "scratch.py"
    scratch.write_text("print('debug')\n", encoding="utf-8")

    restore_candidate_patch_checkpoint(checkpoint)

    assert source.read_text(encoding="utf-8") == "print('base')\n"
    assert not scratch.exists()


def test_candidate_patch_checkpoint_preserves_preexisting_dirty_state(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _init_git_workspace(workspace)
    source = _commit_file(workspace, "src/app.py", "print('base')\n")

    source.write_text("print('accepted')\n", encoding="utf-8")
    checkpoint = create_candidate_patch_checkpoint(workspace, label="accepted")

    source.write_text("print('failed-candidate')\n", encoding="utf-8")

    restore_candidate_patch_checkpoint(checkpoint)

    assert source.read_text(encoding="utf-8") == "print('accepted')\n"


def test_candidate_checkpoint_unknown_git_skips_restore_without_claiming_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "opensquilla.tools.candidate_patch_checkpoint.run_git",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=False,
            state=GitRunState.UNAVAILABLE,
            stdout=b"",
        ),
    )
    checkpoint = create_candidate_patch_checkpoint(tmp_path, label="unknown")
    candidate = tmp_path / "candidate.py"
    candidate.write_text("keep me\n", encoding="utf-8")

    result = restore_candidate_patch_checkpoint(checkpoint)

    assert checkpoint.git_state is GitRunState.UNAVAILABLE
    assert result["status"] == "skipped"
    assert result["git_state"] == "unavailable"
    assert candidate.read_text(encoding="utf-8") == "keep me\n"


def test_candidate_checkpoint_removes_staged_new_candidate(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _init_git_workspace(workspace)
    _commit_file(workspace, "src/app.py", "print('base')\n")
    checkpoint = create_candidate_patch_checkpoint(workspace, label="clean")
    candidate = workspace / "candidate.py"
    candidate.write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "add", "candidate.py"], cwd=workspace, check=True)

    result = restore_candidate_patch_checkpoint(checkpoint)

    assert result["status"] == "restored"
    assert not candidate.exists()
    status = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""
