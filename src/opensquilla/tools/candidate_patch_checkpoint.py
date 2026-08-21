"""Content snapshots for reversible candidate-patch trial edits."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

from opensquilla.git_runtime import GitRunResult, GitRunState, run_git


@dataclass(frozen=True)
class CandidatePatchFileSnapshot:
    """A workspace-relative file snapshot."""

    relative_path: str
    exists: bool
    content: bytes | None
    sha256: str | None


@dataclass(frozen=True)
class CandidatePatchCheckpoint:
    """Snapshot of the dirty workspace state before trying a candidate patch."""

    workspace: Path
    label: str | None
    created_at: float
    head: str | None
    files: dict[str, CandidatePatchFileSnapshot]
    git_state: GitRunState = GitRunState.OK

    @property
    def changed_paths(self) -> list[str]:
        return sorted(self.files)


def create_candidate_patch_checkpoint(
    workspace: str | Path,
    *,
    label: str | None = None,
) -> CandidatePatchCheckpoint:
    """Capture current dirty files so a later candidate can be reverted.

    The checkpoint stores file content directly and only uses git for read-only
    status/blob queries. It intentionally avoids destructive git commands.
    """

    root = Path(workspace).expanduser().resolve()
    git_state, statuses = _git_dirty_statuses(root)
    paths = sorted(statuses)
    return CandidatePatchCheckpoint(
        workspace=root,
        label=label,
        created_at=time.time(),
        head=_git_head(root) if git_state is GitRunState.OK else None,
        files={path: _snapshot_path(root, path) for path in paths},
        git_state=git_state,
    )


def restore_candidate_patch_checkpoint(checkpoint: CandidatePatchCheckpoint) -> dict[str, object]:
    """Restore the workspace to the checkpoint's dirty-file state."""

    root = checkpoint.workspace.expanduser().resolve()
    if checkpoint.git_state is not GitRunState.OK:
        return _checkpoint_restore_skipped(checkpoint, checkpoint.git_state)
    current_git_state, current_statuses = _git_dirty_statuses(root)
    if current_git_state is not GitRunState.OK:
        return _checkpoint_restore_skipped(checkpoint, current_git_state)
    current_paths = set(current_statuses)
    checkpoint_paths = set(checkpoint.files)
    touched_paths = sorted(current_paths | checkpoint_paths)
    restore_plan: list[tuple[str, bytes | None, bool]] = []

    # Resolve every Git-dependent restore input before changing files. If Git
    # becomes unavailable, the checkpoint remains untouched and the caller gets
    # an explicit skipped result rather than a misleading partial "restored".
    for relative_path in touched_paths:
        snapshot = checkpoint.files.get(relative_path)
        if snapshot is not None:
            restore_plan.append(
                (
                    relative_path,
                    snapshot.content if snapshot.exists else None,
                    False,
                )
            )
            continue
        if current_statuses.get(relative_path, "").startswith("??"):
            restore_plan.append((relative_path, None, False))
            continue
        head_state, present_at_head = _git_path_present_at_head(root, relative_path)
        if head_state is not GitRunState.OK:
            return _checkpoint_restore_skipped(checkpoint, head_state)
        if not present_at_head:
            # A staged-new path is authoritatively absent from HEAD. Removing
            # the candidate file preserves the checkpoint's pre-candidate
            # absence without conflating that verdict with a Git failure.
            restore_plan.append(
                (
                    relative_path,
                    None,
                    "A" in current_statuses.get(relative_path, ""),
                )
            )
            continue
        head_result = _git_show_head_path_result(root, relative_path)
        if not head_result.ok:
            return _checkpoint_restore_skipped(checkpoint, head_result.state)
        restore_plan.append((relative_path, head_result.stdout, False))

    staged_new_paths = [
        relative_path
        for relative_path, _head_content, unstage_new in restore_plan
        if unstage_new
    ]
    if staged_new_paths:
        unstage_result = run_git(
            ("rm", "--cached", "-f", "--", *staged_new_paths),
            cwd=root,
            timeout=2.0,
        )
        if not unstage_result.ok:
            return _checkpoint_restore_skipped(checkpoint, unstage_result.state)

    restored: list[str] = []
    removed: list[str] = []

    for relative_path, head_content, _unstage_new in restore_plan:
        target = root / relative_path
        if head_content is None:
            _remove_file_if_present(target)
            removed.append(relative_path)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(head_content)
            restored.append(relative_path)

    return {
        "status": "restored",
        "git_state": GitRunState.OK.value,
        "label": checkpoint.label,
        "path_count": len(touched_paths),
        "restored_paths": restored,
        "removed_paths": removed,
    }


def _checkpoint_restore_skipped(
    checkpoint: CandidatePatchCheckpoint,
    state: GitRunState,
) -> dict[str, object]:
    return {
        "status": "skipped",
        "reason": "git_observation_unavailable",
        "git_state": state.value,
        "label": checkpoint.label,
        "path_count": None,
        "restored_paths": [],
        "removed_paths": [],
    }


def _snapshot_path(root: Path, relative_path: str) -> CandidatePatchFileSnapshot:
    target = root / relative_path
    if not target.exists() or not target.is_file():
        return CandidatePatchFileSnapshot(
            relative_path=relative_path,
            exists=False,
            content=None,
            sha256=None,
        )
    content = target.read_bytes()
    return CandidatePatchFileSnapshot(
        relative_path=relative_path,
        exists=True,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _git_dirty_statuses(root: Path) -> tuple[GitRunState, dict[str, str]]:
    completed = run_git(
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        cwd=root,
        timeout=2.0,
    )
    if not completed.ok:
        return completed.state, {}
    return (
        GitRunState.OK,
        _parse_git_status_map_z(completed.stdout.decode("utf-8", errors="replace")),
    )


def _git_dirty_paths(root: Path) -> list[str]:
    _state, statuses = _git_dirty_statuses(root)
    return sorted(statuses)


def _parse_git_status_z(output: str) -> list[str]:
    return sorted(_parse_git_status_map_z(output))


def _parse_git_status_map_z(output: str) -> dict[str, str]:
    paths: dict[str, str] = {}
    entries = output.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2]
        relative_path = entry[3:] if len(entry) > 3 else ""
        if status[:1] in {"R", "C"} and index < len(entries):
            relative_path = entries[index] or relative_path
            index += 1
        if relative_path:
            paths[relative_path.replace("\\", "/")] = status
    return paths


def _git_head(root: Path) -> str | None:
    completed = run_git(("rev-parse", "HEAD"), cwd=root, timeout=2.0)
    if not completed.ok:
        return None
    return completed.stdout_text.strip() or None


def _git_show_head_path(root: Path, relative_path: str) -> bytes | None:
    completed = _git_show_head_path_result(root, relative_path)
    if not completed.ok:
        return None
    return completed.stdout


def _git_show_head_path_result(root: Path, relative_path: str) -> GitRunResult:
    return run_git(
        ("show", "--end-of-options", f"HEAD:{relative_path}"),
        cwd=root,
        timeout=2.0,
    )


def _git_path_present_at_head(
    root: Path,
    relative_path: str,
) -> tuple[GitRunState, bool]:
    result = run_git(
        ("ls-tree", "--name-only", "HEAD", "--", relative_path),
        cwd=root,
        timeout=2.0,
    )
    if not result.ok:
        return result.state, False
    return GitRunState.OK, bool(result.stdout.strip())


def _remove_file_if_present(path: Path) -> None:
    if path.exists() and path.is_file():
        path.unlink()
