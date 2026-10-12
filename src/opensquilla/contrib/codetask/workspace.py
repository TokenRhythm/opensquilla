"""Host working tree: clone the target repo and prepare a task branch.

v1 is host-only and always clones fresh into a disposable run directory.
``--in-place`` is intentionally out of scope (codex review #5): mutating a
user's real checkout needs worktree/LFS/submodule handling deferred to v2.

SECURITY: this is NOT an OS isolation boundary. Running an agent that may
install dependencies and execute repo code on the host is trusted-host
execution. Only point code-task at repositories you trust.
"""

from __future__ import annotations

import locale
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from opensquilla.contrib.codetask.config import (
    BUILD_ARTIFACT_EXCLUDES,
    GIT_USER_EMAIL,
    GIT_USER_NAME,
    TASK_BRANCH_PREFIX,
    build_workspace_dir,
    repo_dir,
    run_dir,
)

logger = logging.getLogger(__name__)

CLONE_TIMEOUT = 1800  # seconds; large repos


class WorkspaceError(RuntimeError):
    """Clone/branch/setup failed."""


@dataclass
class PreparedRepo:
    path: Path
    base_ref: str
    base_commit: str
    branch: str


def _git(args: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def is_dirty(path: Path) -> bool:
    """True if the given checkout has uncommitted changes."""
    r = _git(["status", "--porcelain"], path)
    return r.returncode == 0 and bool(r.stdout.strip())


def prepare_scratch_repo(run_id: str, *, slug: str = "task") -> PreparedRepo:
    """Prepare an empty git repo for "write code from scratch" tasks.

    The agent adds files; ``collect_change`` diffs them against an empty base
    commit. Used by ``--verification-mode scratch`` (green-only).
    """
    dest = repo_dir(run_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if _git(["init", "-q"], dest).returncode != 0:
        raise WorkspaceError("git init failed for scratch repo")
    _git(["config", "user.email", GIT_USER_EMAIL], dest)
    _git(["config", "user.name", GIT_USER_NAME], dest)
    _write_repo_excludes(dest)
    # Empty initial commit so `git diff <base>` and count_commits work normally.
    _git(["commit", "-q", "--allow-empty", "-m", "scratch base"], dest)
    head = _git(["rev-parse", "HEAD"], dest)
    base_commit = head.stdout.strip() if head.returncode == 0 else ""
    branch = f"{TASK_BRANCH_PREFIX}{slug}"
    if _git(["checkout", "-b", branch], dest).returncode != 0:
        branch = f"{TASK_BRANCH_PREFIX}{slug}-{run_id.split('-')[-1]}"
        _git(["checkout", "-b", branch], dest)
    logger.info("Prepared scratch repo @ %s on branch %s", base_commit[:12], branch)
    return PreparedRepo(
        path=dest,
        base_ref="(scratch)",
        base_commit=base_commit,
        branch=branch,
    )


def ensure_build_workspace(slug: str) -> Path:
    """A DURABLE empty git repo for a from-scratch app build, under the code-task
    workspace dir. The build flow clones it into the run dir and persists the
    verified app back here, so a follow-up edit can ``--repo <path>`` at it."""
    dest = build_workspace_dir() / slug
    dest.mkdir(parents=True, exist_ok=True)
    if not (dest / ".git").exists():
        if _git(["init", "-q"], dest).returncode != 0:
            raise WorkspaceError("git init failed for build workspace")
        _git(["config", "user.email", GIT_USER_EMAIL], dest)
        _git(["config", "user.name", GIT_USER_NAME], dest)
        _write_repo_excludes(dest)
        _git(["commit", "-q", "--allow-empty", "-m", "app workspace base"], dest)
    return dest


def prepare_repo(
    run_id: str,
    repo: str,
    *,
    base_ref: str | None = None,
    shallow: bool = False,
    slug: str = "task",
) -> PreparedRepo:
    """Clone ``repo`` (URL or local path) into the run dir and branch off.

    Local-path sources are cloned too (never mutated in place), so a failed
    or messy run can be discarded by deleting the run dir.
    """
    dest = repo_dir(run_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)

    clone_cmd = ["git", "clone"]
    if shallow:
        clone_cmd += ["--depth", "1"]
    clone_cmd += [_normalize_source(repo), str(dest)]
    try:
        result = subprocess.run(
            clone_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CLONE_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise WorkspaceError("git is not installed.") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(f"git clone timed out after {CLONE_TIMEOUT}s.") from exc
    if result.returncode != 0:
        raise WorkspaceError(f"git clone failed: {(result.stderr or '').strip()[-400:]}")

    # Check out the requested base.
    if base_ref:
        r = _git(["checkout", base_ref], dest)
        if r.returncode != 0:
            hint = ""
            if _git(["rev-parse", "--verify", "HEAD"], dest).returncode != 0:
                hint = (
                    " (the repository is empty/unborn and has no commit, so a "
                    "--base ref cannot be checked out; omit --base to scaffold "
                    "from scratch)"
                )
            raise WorkspaceError(
                f"git checkout {base_ref} failed: {r.stderr.strip()}{hint}"
            )

    head = _git(["rev-parse", "HEAD"], dest)
    base_commit = head.stdout.strip() if head.returncode == 0 else ""
    resolved_ref = base_ref or _current_branch(dest) or base_commit

    # Local commit identity (does not touch the user's global git config).
    _git(["config", "user.email", GIT_USER_EMAIL], dest)
    _git(["config", "user.name", GIT_USER_NAME], dest)

    # Keep build/cache artifacts (pyc, egg-info, node_modules...) out of the
    # collected change without creating a tracked .gitignore.
    _write_repo_excludes(dest)

    branch = f"{TASK_BRANCH_PREFIX}{slug}"
    r = _git(["checkout", "-b", branch], dest)
    if r.returncode != 0:
        # Branch name collision: fall back to a run-suffixed name.
        branch = f"{TASK_BRANCH_PREFIX}{slug}-{run_id.split('-')[-1]}"
        r = _git(["checkout", "-b", branch], dest)
        if r.returncode != 0:
            raise WorkspaceError(f"could not create task branch: {r.stderr.strip()}")

    logger.info("Prepared %s @ %s on branch %s", repo, base_commit[:12], branch)
    return PreparedRepo(path=dest, base_ref=resolved_ref, base_commit=base_commit, branch=branch)


def _write_repo_excludes(dest: Path) -> None:
    """Append build-artifact patterns to the clone's .git/info/exclude.

    Repo-local and untracked, so the agent's dependency install / test runs
    cannot leak pyc/egg-info/node_modules into the diff, and no .gitignore
    file appears in the change.
    """
    exclude_file = dest / ".git" / "info" / "exclude"
    try:
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        # The existing exclude file is git/user controlled with no UTF-8
        # guarantee; we only need to know whether it ends in a newline, so
        # mojibake from a locale fallback is fine.
        if exclude_file.exists():
            try:
                existing = exclude_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                existing = exclude_file.read_text(
                    encoding=locale.getpreferredencoding(False) or "utf-8",
                    errors="replace",
                )
        else:
            existing = ""
        block = "\n# opensquilla code-task build-artifact excludes\n" + "\n".join(
            BUILD_ARTIFACT_EXCLUDES
        )
        with exclude_file.open("a", encoding="utf-8") as fh:
            if not existing.endswith("\n") and existing:
                fh.write("\n")
            fh.write(block + "\n")
    except OSError as exc:
        logger.warning("could not write .git/info/exclude: %s", exc)


def _normalize_source(repo: str) -> str:
    """Expand a local path source; pass URLs through untouched."""
    if "://" in repo or repo.startswith("git@"):
        return repo
    p = Path(repo).expanduser()
    if p.exists():
        return str(p.resolve())
    return repo


def _current_branch(path: Path) -> str:
    r = _git(["rev-parse", "--abbrev-ref", "HEAD"], path)
    return r.stdout.strip() if r.returncode == 0 else ""


def _empty_tree(repo: Path) -> str:
    """Git's empty tree object id, derived at runtime.

    Diffing the index against it yields every tracked file as an addition.
    Computed via ``git hash-object`` (not hardcoded) so it is correct for both
    SHA-1 and SHA-256 repositories. Uses ``--stdin`` with empty input so it
    works on Windows too (``/dev/null`` does not exist there).
    """
    r = subprocess.run(
        ["git", "hash-object", "-t", "tree", "--stdin"],
        cwd=str(repo),
        input="",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if r.returncode != 0:
        raise WorkspaceError(f"could not compute empty tree: {(r.stderr or '').strip()}")
    return r.stdout.strip()


def _diff_or_raise(args: list[str], repo: Path) -> str:
    """Run a ``git diff`` variant, raising on failure instead of swallowing it.

    These diff commands never use ``--exit-code``/``--quiet``/``--check``, so a
    non-zero return code is always a real error (e.g. a bad base ref). The old
    code ignored it and silently returned an empty patch.
    """
    r = _git(args, repo)
    if r.returncode != 0:
        raise WorkspaceError(
            f"git {' '.join(args)} failed: {(r.stderr or '').strip()[-300:]}"
        )
    return cast(str, r.stdout)


def collect_change(repo: Path, base_commit: str) -> tuple[int, str, str]:
    """Return (files_changed, diffstat, full_patch) of work vs base_commit.

    The agent commits on the task branch; uncommitted work is also captured
    so a non-committing agent still yields a diff.

    When ``base_commit`` is "" (an unborn/empty source repo — build mode
    scaffolding a brand-new app), the whole generated tree IS the change, so
    the diff is taken against Git's empty tree using the staged index
    (``--cached <empty_tree>``). That stays correct even when the agent commits
    its scaffold mid-run: a plain ``git diff ""`` errors, and a bare
    ``git diff --cached`` would compare against the agent's new HEAD and miss
    the committed files once a HEAD exists.
    """
    add = _git(["add", "-A"], repo)
    if add.returncode != 0:
        raise WorkspaceError(f"git add -A failed: {(add.stderr or '').strip()}")

    if base_commit:
        stat_args = ["diff", "--stat", base_commit]
        patch_args = ["diff", "--binary", "--no-color", base_commit]
        names_args = ["diff", "--name-only", base_commit]
    else:
        empty = _empty_tree(repo)
        stat_args = ["diff", "--cached", "--stat", empty]
        patch_args = ["diff", "--cached", "--binary", "--no-color", empty]
        names_args = ["diff", "--cached", "--name-only", empty]

    diffstat = _diff_or_raise(stat_args, repo).strip()
    patch = _diff_or_raise(patch_args, repo)
    names = _diff_or_raise(names_args, repo).strip()
    files_changed = len([n for n in names.splitlines() if n.strip()])
    return files_changed, diffstat, patch


def persist_to_source(
    source_repo: Path, base_commit: str, patch: str, message: str
) -> tuple[bool, str]:
    """Apply a VERIFIED build-mode change back onto a stable LOCAL source repo.

    Lets follow-up edits iterate on the same app. Guarded: the source must be a
    clean git repo whose HEAD still equals ``base_commit`` (no drift since the
    run started). Returns ``(ok, new_commit_or_reason)``.
    """
    if not (source_repo / ".git").is_dir():
        return False, "source is not a git repo"
    if is_dirty(source_repo):
        return False, "source repo has uncommitted changes"
    # NB: `git rev-parse HEAD` on an unborn (commit-less) repo echoes the literal
    # "HEAD" on stdout and exits non-zero — so check the return code and treat an
    # unborn HEAD as "" to match an empty base_commit (build-from-scratch).
    head_proc = _git(["rev-parse", "--verify", "HEAD"], source_repo)
    head = head_proc.stdout.strip() if head_proc.returncode == 0 else ""
    if head != base_commit:
        return False, f"source HEAD moved ({head[:12]} != base {base_commit[:12]})"
    if not patch.strip():
        return False, "no change to persist"
    proc = subprocess.run(
        ["git", "apply", "--whitespace=nowarn"],
        cwd=str(source_repo),
        input=patch,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if proc.returncode != 0:
        return False, f"git apply failed: {(proc.stderr or '').strip()[-300:]}"
    _git(["config", "user.email", GIT_USER_EMAIL], source_repo)
    _git(["config", "user.name", GIT_USER_NAME], source_repo)
    _git(["add", "-A"], source_repo)
    c = _git(["commit", "-m", message], source_repo)
    if c.returncode != 0:
        return False, f"commit failed: {(c.stderr or '').strip()[-200:]}"
    return True, _git(["rev-parse", "HEAD"], source_repo).stdout.strip()


def count_commits(repo: Path, base_commit: str) -> int:
    if base_commit:
        r = _git(["rev-list", "--count", f"{base_commit}..HEAD"], repo)
    else:
        # Unborn/empty base: count whatever the agent committed. If it never
        # committed, HEAD is still unborn -> 0.
        if _git(["rev-parse", "--verify", "HEAD"], repo).returncode != 0:
            return 0
        r = _git(["rev-list", "--count", "HEAD"], repo)
    try:
        return int(r.stdout.strip())
    except (ValueError, AttributeError):
        return 0


def cleanup(run_id: str, keep: bool = True) -> None:
    """Remove the cloned repo (artifacts under run dir are kept by default)."""
    if keep:
        return
    target = run_dir(run_id)
    if target.exists():
        shutil.rmtree(target)
