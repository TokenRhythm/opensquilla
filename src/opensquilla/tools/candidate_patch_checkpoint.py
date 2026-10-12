"""Content snapshots for reversible candidate-patch trial edits."""

from __future__ import annotations

import hashlib
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


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
    paths = _git_dirty_paths(root)
    return CandidatePatchCheckpoint(
        workspace=root,
        label=label,
        created_at=time.time(),
        head=_git_head(root),
        files={path: _snapshot_path(root, path) for path in paths},
    )


def restore_candidate_patch_checkpoint(checkpoint: CandidatePatchCheckpoint) -> dict[str, object]:
    """Restore the workspace to the checkpoint's dirty-file state."""

    root = checkpoint.workspace.expanduser().resolve()
    current_paths = set(_git_dirty_paths(root))
    checkpoint_paths = set(checkpoint.files)
    touched_paths = sorted(current_paths | checkpoint_paths)
    restored: list[str] = []
    removed: list[str] = []

    for relative_path in touched_paths:
        snapshot = checkpoint.files.get(relative_path)
        if snapshot is not None:
            target = root / relative_path
            if snapshot.exists and snapshot.content is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(snapshot.content)
                restored.append(relative_path)
            else:
                _remove_file_if_present(target)
                removed.append(relative_path)
            continue

        head_content = _git_show_head_path(root, relative_path)
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
        "label": checkpoint.label,
        "path_count": len(touched_paths),
        "restored_paths": restored,
        "removed_paths": removed,
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


def _git_dirty_paths(root: Path) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return []
    if completed.returncode != 0:
        return []
    return _parse_git_status_z(completed.stdout.decode("utf-8", errors="replace"))


def _parse_git_status_z(output: str) -> list[str]:
    paths: list[str] = []
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
            paths.append(relative_path.replace("\\", "/"))
    return sorted(set(paths))


def _git_head(root: Path) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _git_show_head_path(root: Path, relative_path: str) -> bytes | None:
    completed = subprocess.run(
        ["git", "show", "--end-of-options", f"HEAD:{relative_path}"],
        cwd=root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout


def _remove_file_if_present(path: Path) -> None:
    if path.exists() and path.is_file():
        path.unlink()
