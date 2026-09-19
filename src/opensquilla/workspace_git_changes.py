"""Read-only Git working-tree inspection for project workspaces.

The owner-facing surfaces (the Web UI Git panel and any later workbench
consumer) need *structured* working-tree state, while the agent-facing
``git_status`` / ``git_diff`` tools return text for a model to read.  Both
layers must still agree on how Git is executed, so this module never spawns Git
itself: it reuses :func:`opensquilla.git_runtime.run_git`, which owns safe
absolute-path resolution, the non-interactive environment, and timeouts.

Parsing is split from execution on purpose.  ``parse_porcelain_status`` is a
pure function over the porcelain v2 byte stream, so the record shapes that are
easy to get wrong (renames carry the original path in a second NUL field,
unmerged entries carry four modes and three hashes) are covered by offline unit
tests instead of requiring a live Git repository.

For a file that is not tracked yet, ``git diff`` prints nothing, so the review
surface would silently show an empty diff for exactly the files an agent most
often creates.  ``read_workspace_diff`` therefore falls back to
``git diff --no-index`` against the platform null device, which produces the
ordinary "new file" diff Git itself would print for a staged addition.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from opensquilla.git_runtime import (
    GitRunResult,
    GitRunState,
    harden_read_only_git_args,
    run_git,
)

STATUS_TIMEOUT_SECONDS = 10.0
DIFF_TIMEOUT_SECONDS = 15.0
WRITE_TIMEOUT_SECONDS = 20.0
# A push is the only call here that can wait on a network and a credential
# helper, so it gets its own budget.
PUSH_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_ENTRIES = 500
DEFAULT_MAX_DIFF_BYTES = 512 * 1024

_MAX_REPO_PATH_CHARS = 4096

ChangeType = Literal[
    "added",
    "modified",
    "deleted",
    "renamed",
    "copied",
    "typeChanged",
    "unmerged",
    "untracked",
    "unknown",
]
AvailabilityReason = Literal[
    "git_unavailable",
    "not_repository",
    "timed_out",
    "failed",
]
# Reasons a write is refused before it could change anything. Each one is a
# different thing for the operator to do next, so they are separate codes
# rather than one "failed".
PreconditionCode = Literal[
    "untracked_path",
    "nothing_staged",
    "no_upstream",
    "commit_published",
    "no_parent",
]

_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:")
_SEPARATOR_RE = re.compile(r"[\\/]")
# `git diff` (and `--no-index`) announce binary content instead of hunks.
_BINARY_DIFF_RE = re.compile(r"^(?:Binary files .* differ|GIT binary patch)$", re.MULTILINE)

# Porcelain v2 status codes, most specific first. A rename that also modified
# content reports both, and the review list should say "renamed".
_CHANGE_PRIORITY: tuple[tuple[str, ChangeType], ...] = (
    ("R", "renamed"),
    ("C", "copied"),
    ("A", "added"),
    ("D", "deleted"),
    ("T", "typeChanged"),
    ("M", "modified"),
)


class WorkspacePathError(ValueError):
    """A caller-supplied path is not a repository-relative path."""


class WorkspaceGitPreconditionError(RuntimeError):
    """A write was refused before it could remove or rewrite anything.

    Distinct from :class:`WorkspaceGitUnavailableError`, which means Git ran and
    would not do it. These are the cases the caller can act on, so they carry a
    code of their own instead of collapsing into a generic failure.
    """

    def __init__(self, code: PreconditionCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class WorkspaceGitUnavailableError(RuntimeError):
    """Git could not produce the requested result.

    ``result`` carries Git's own output when the command ran and failed, so a
    caller can report what Git said instead of a generic failure.
    """

    def __init__(
        self,
        reason: AvailabilityReason,
        *,
        result: GitRunResult | None = None,
    ) -> None:
        self.reason = reason
        self.result = result
        super().__init__(reason)


class WorkspaceStatusParseError(RuntimeError):
    """Porcelain v2 output did not match the documented record shapes."""


@dataclass(frozen=True)
class WorkspaceChangeEntry:
    """One changed path, with staged/unstaged state kept separate.

    ``added_lines`` / ``removed_lines`` are ``None`` when the count is genuinely
    unknown (a binary file, or a path Git reports no line stats for). They are
    never reported as ``0`` for those cases, because a confident zero is a
    different claim from "not countable".
    """

    path: str
    previous_path: str | None
    change_type: ChangeType
    staged: bool
    unstaged: bool
    added_lines: int | None = None
    removed_lines: int | None = None


@dataclass(frozen=True)
class WorkspaceChanges:
    """Working-tree state for one project workspace."""

    available: bool
    availability_reason: AvailabilityReason | None
    branch: str | None
    detached: bool
    upstream: str | None
    ahead: int
    behind: int
    total_count: int
    truncated: bool
    added_lines: int
    removed_lines: int
    entries: tuple[WorkspaceChangeEntry, ...]


@dataclass(frozen=True)
class WorkspaceDiff:
    """One file's unified diff, already bounded for transport."""

    path: str
    staged: bool
    text: str
    truncated: bool
    binary: bool


@dataclass(frozen=True)
class _StatusHeader:
    branch: str | None = None
    detached: bool = False
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0


def _write_availability_reason(result: GitRunResult) -> AvailabilityReason:
    return _availability_reason(result.state) or "failed"


def stage_paths(
    workspace_path: str,
    paths: Sequence[str],
    *,
    staged: bool = True,
    timeout: float = WRITE_TIMEOUT_SECONDS,
    environment: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Stage or unstage *paths* in the workspace index.

    This is the first operation in this module that writes, so it is worth
    being explicit about the two things that make it safe to expose:

    * Only the **index** is touched. ``git restore --staged`` and ``git add``
      never rewrite worktree content, so staging a path by mistake costs the
      user nothing but a second call with the opposite ``staged`` value.
    * The read-only hardening is deliberately *not* applied here. It exists to
      stop a repository from executing helpers during a read
      (``--no-optional-locks``, ``core.fsmonitor=false``); a write is the one
      case that legitimately takes the index lock, so reusing it would break
      the operation it is meant to protect.

    The returned tuple is the caller's paths as they were applied, so the
    confirmation the operator sees is the same string set Git acted on.
    """

    repo_paths = tuple(normalize_repo_path(path) for path in paths)
    if not repo_paths:
        raise WorkspacePathError("at least one path is required")
    args = (
        ("add", "--", *repo_paths)
        if staged
        else ("restore", "--staged", "--", *repo_paths)
    )
    result = run_git(
        args,
        cwd=workspace_path,
        timeout=timeout,
        environment=environment,
    )
    if result.state is not GitRunState.OK:
        raise WorkspaceGitUnavailableError(
            _write_availability_reason(result),
            result=result,
        )
    return repo_paths


def _require_tracked(
    workspace_path: str,
    repo_paths: Sequence[str],
    timeout: float,
    environment: Mapping[str, str] | None,
) -> None:
    """Refuse a discard for a path Git does not track yet.

    Restoring an untracked path would have to *delete* the file instead, and a
    file the agent just created is exactly the one a reviewer must not lose to a
    mis-click. The panel does not offer the action for those rows; this keeps the
    API from offering it either.
    """

    for repo_path in repo_paths:
        result = run_git(
            ("ls-files", "--error-unmatch", "--", repo_path),
            cwd=workspace_path,
            timeout=timeout,
            environment=environment,
        )
        if result.state is GitRunState.OK:
            continue
        if result.returncode == 1:
            raise WorkspaceGitPreconditionError(
                "untracked_path",
                f"{repo_path} is not tracked, so there is nothing to restore it from.",
            )
        raise WorkspaceGitUnavailableError(
            _availability_reason(result.state) or "failed",
            result=result,
        )


def discard_paths(
    workspace_path: str,
    paths: Sequence[str],
    *,
    timeout: float = WRITE_TIMEOUT_SECONDS,
    environment: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Restore the *worktree* of tracked paths from the index.

    This discards uncommitted edits, so it is the one operation here that can
    lose work. It restores from the index rather than from ``HEAD``: a path that
    also has staged content keeps that staged content, which is what "discard
    these edits" means when the same file is staged and edited.
    """

    repo_paths = tuple(normalize_repo_path(path) for path in paths)
    if not repo_paths:
        raise WorkspacePathError("at least one path is required")
    _require_tracked(workspace_path, repo_paths, timeout, environment)
    result = run_git(
        ("restore", "--worktree", "--", *repo_paths),
        cwd=workspace_path,
        timeout=timeout,
        environment=environment,
    )
    if result.state is not GitRunState.OK:
        raise WorkspaceGitUnavailableError(
            _write_availability_reason(result),
            result=result,
        )
    return repo_paths


def commit_index(
    workspace_path: str,
    message: str,
    *,
    timeout: float = WRITE_TIMEOUT_SECONDS,
    environment: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Commit the index and return ``(sha, subject)``.

    Only the index is committed; nothing is staged implicitly, because a commit
    that silently includes files the operator did not choose is the mistake this
    whole surface exists to prevent. An empty index is refused up front with its
    own code rather than surfacing Git's "nothing to commit" prose.

    Git's own identity and signing configuration are left in force: an unknown
    author or a signing prompt must fail loudly here rather than be worked
    around with a synthetic identity.
    """

    subject = message.strip()
    if not subject:
        raise WorkspacePathError("a commit message is required")
    # `--quiet` reports the answer as an exit status: 0 means the index holds no
    # changes, 1 means it does. Anything else is a real Git failure, so the two
    # are separated rather than both being treated as "no changes".
    staged = run_git(
        ("diff", "--cached", "--quiet"),
        cwd=workspace_path,
        timeout=timeout,
        environment=environment,
    )
    if staged.state is GitRunState.OK:
        raise WorkspaceGitPreconditionError(
            "nothing_staged",
            "Nothing is staged, so there is nothing to commit.",
        )
    if staged.returncode != 1:
        raise WorkspaceGitUnavailableError(
            _write_availability_reason(staged),
            result=staged,
        )
    result = run_git(
        ("commit", "-m", message),
        cwd=workspace_path,
        timeout=timeout,
        environment=environment,
    )
    if result.state is not GitRunState.OK:
        raise WorkspaceGitUnavailableError(
            _write_availability_reason(result),
            result=result,
        )
    head = run_git(
        ("rev-parse", "HEAD"),
        cwd=workspace_path,
        timeout=timeout,
        environment=environment,
    )
    sha = head.stdout_text.strip() if head.state is GitRunState.OK else ""
    return sha, subject.splitlines()[0]


def push_current_branch(
    workspace_path: str,
    *,
    upstream: str | None,
    timeout: float = PUSH_TIMEOUT_SECONDS,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Push the current branch to its upstream, never forcing.

    ``upstream`` comes from the status read the caller already made, so a branch
    without one is refused here instead of guessing a remote and publishing a
    branch the operator did not name. Credentials are the environment's problem:
    the child gets no terminal, and Git's own refusal is reported verbatim.
    """

    if not upstream:
        raise WorkspaceGitPreconditionError(
            "no_upstream",
            "This branch has no upstream, so there is nothing to push it to.",
        )
    result = run_git(
        ("push",),
        cwd=workspace_path,
        timeout=timeout,
        environment=environment,
        allow_user_interaction=True,
    )
    if result.state is not GitRunState.OK:
        raise WorkspaceGitUnavailableError(
            _write_availability_reason(result),
            result=result,
        )
    return result.stdout_text.strip() or result.stderr_text.strip()


def undo_last_commit(
    workspace_path: str,
    *,
    upstream: str | None,
    ahead: int,
    timeout: float = WRITE_TIMEOUT_SECONDS,
    environment: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Move the branch tip back one commit and return the undone ``(sha, subject)``.

    A ``--soft`` reset, so the commit's content stays staged and nothing is
    lost: this is "undo the commit", not "undo the work".

    Two refusals, both about not rewriting something the operator cannot get
    back:

    * a tip already on the upstream is published history. Undoing it locally
      would need a force push to reconcile, so it is refused instead.
    * the root commit has no parent to reset to.
    """

    if upstream and ahead <= 0:
        raise WorkspaceGitPreconditionError(
            "commit_published",
            f"{upstream} already has this commit, so undoing it locally would "
            "rewrite published history.",
        )
    parent = run_git(
        ("rev-parse", "--verify", "--quiet", "HEAD~1"),
        cwd=workspace_path,
        timeout=timeout,
        environment=environment,
    )
    if parent.state is not GitRunState.OK:
        raise WorkspaceGitPreconditionError(
            "no_parent",
            "The first commit has no parent, so there is nothing to reset to.",
        )
    head = run_git(
        ("log", "-1", "--pretty=%H%x00%s"),
        cwd=workspace_path,
        timeout=timeout,
        environment=environment,
    )
    if head.state is not GitRunState.OK:
        raise WorkspaceGitUnavailableError(
            _write_availability_reason(head),
            result=head,
        )
    sha, _, subject = head.stdout_text.strip().partition("\x00")
    result = run_git(
        ("reset", "--soft", "HEAD~1"),
        cwd=workspace_path,
        timeout=timeout,
        environment=environment,
    )
    if result.state is not GitRunState.OK:
        raise WorkspaceGitUnavailableError(
            _write_availability_reason(result),
            result=result,
        )
    return sha, subject


def normalize_repo_path(value: object) -> str:
    """Validate a repository-relative path supplied by a caller.

    The value is returned unchanged rather than re-written: callers round-trip
    the exact strings ``read_workspace_changes`` reported, so normalizing
    separators here could turn a valid path into one that matches no file.
    """

    if not isinstance(value, str):
        raise WorkspacePathError("path must be a string")
    if len(value) > _MAX_REPO_PATH_CHARS:
        raise WorkspacePathError("path is too long")
    if "\x00" in value:
        raise WorkspacePathError("path must not contain NUL")
    candidate = value.strip()
    if not candidate:
        raise WorkspacePathError("path must not be empty")
    if candidate.startswith(("/", "\\")) or _WINDOWS_ABSOLUTE.match(candidate):
        raise WorkspacePathError("path must be relative to the workspace")
    segments = [segment for segment in _SEPARATOR_RE.split(candidate) if segment not in ("", ".")]
    if not segments:
        raise WorkspacePathError("path must not be empty")
    if any(segment == ".." for segment in segments):
        raise WorkspacePathError("path must stay inside the workspace")
    return candidate


def _change_type(xy: str) -> ChangeType:
    for code, change_type in _CHANGE_PRIORITY:
        if code in xy:
            return change_type
    return "unknown"


def _tracked_entry(path: str, previous_path: str | None, xy: str) -> WorkspaceChangeEntry:
    return WorkspaceChangeEntry(
        path=path,
        previous_path=previous_path,
        change_type=_change_type(xy),
        staged=xy[:1] not in ("", "."),
        unstaged=xy[1:2] not in ("", "."),
    )


def _apply_header(header: _StatusHeader, record: str) -> _StatusHeader:
    key, _, value = record.partition(" ")
    if key == "branch.head":
        # Git reports a detached HEAD with the literal placeholder "(detached)".
        if value == "(detached)":
            return _StatusHeader(
                branch=header.branch,
                detached=True,
                upstream=header.upstream,
                ahead=header.ahead,
                behind=header.behind,
            )
        return _StatusHeader(
            branch=value,
            detached=header.detached,
            upstream=header.upstream,
            ahead=header.ahead,
            behind=header.behind,
        )
    if key == "branch.upstream":
        return _StatusHeader(
            branch=header.branch,
            detached=header.detached,
            upstream=value,
            ahead=header.ahead,
            behind=header.behind,
        )
    if key == "branch.ab":
        ahead, behind = header.ahead, header.behind
        for token in value.split():
            match = re.fullmatch(r"([+-])(\d+)", token)
            if match is None:
                continue
            count = int(match.group(2))
            if match.group(1) == "+":
                ahead = count
            else:
                behind = count
        return _StatusHeader(
            branch=header.branch,
            detached=header.detached,
            upstream=header.upstream,
            ahead=ahead,
            behind=behind,
        )
    # branch.oid and any future header field do not change the projection.
    return header


def parse_porcelain_status(output: str) -> tuple[_StatusHeader, list[WorkspaceChangeEntry]]:
    """Parse ``git status --porcelain=v2 --branch -z`` output.

    Every record is NUL-terminated, and a rename/copy record is followed by one
    extra NUL field holding the original path, so the scan is index-based rather
    than a plain comprehension.
    """

    header = _StatusHeader()
    entries: list[WorkspaceChangeEntry] = []
    tokens = output.split("\x00")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        if token.startswith("# "):
            header = _apply_header(header, token[2:])
            continue
        record = token[0]
        if record == "?":
            entries.append(
                WorkspaceChangeEntry(
                    path=token[2:],
                    previous_path=None,
                    change_type="untracked",
                    staged=False,
                    unstaged=True,
                )
            )
            continue
        if record == "!":
            # Ignored files are not part of a change review.
            continue
        fields = token.split(" ")
        if record == "1" and len(fields) >= 9:
            entries.append(_tracked_entry(" ".join(fields[8:]), None, fields[1]))
            continue
        if record == "2" and len(fields) >= 10:
            previous = tokens[index] if index < len(tokens) else ""
            index += 1
            entries.append(
                _tracked_entry(" ".join(fields[9:]), previous or None, fields[1])
            )
            continue
        if record == "u" and len(fields) >= 11:
            entries.append(
                WorkspaceChangeEntry(
                    path=" ".join(fields[10:]),
                    previous_path=None,
                    change_type="unmerged",
                    staged=True,
                    unstaged=True,
                )
            )
            continue
        raise WorkspaceStatusParseError(
            f"unrecognized porcelain v2 record: {token[:64]!r}"
        )
    return header, entries


def _numstat_value(raw: str) -> int | None:
    """`git diff --numstat` prints `-` when a file has no countable lines."""

    return int(raw) if raw.isdigit() else None


def parse_numstat(output: str) -> dict[str, tuple[int | None, int | None]]:
    """Parse ``git diff --numstat HEAD -z`` into ``path -> (added, removed)``.

    With ``-z`` a rename/copy record leaves its path field empty and carries the
    original and new path in the following two NUL fields, so the scan is
    index-based. Both paths of a rename are registered, because callers looking
    up counts may hold either spelling.
    """

    counts: dict[str, tuple[int | None, int | None]] = {}
    tokens = output.split("\x00")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        added_raw, separator, remainder = token.partition("\t")
        if not separator:
            continue
        removed_raw, separator, path = remainder.partition("\t")
        if not separator:
            # Not a numstat record (a stray token); skip rather than invent one.
            continue
        value = (_numstat_value(added_raw), _numstat_value(removed_raw))
        if path:
            counts[path] = value
            continue
        previous = tokens[index] if index < len(tokens) else ""
        current = tokens[index + 1] if index + 1 < len(tokens) else ""
        index += 2
        for candidate in (current, previous):
            if candidate:
                counts[candidate] = value
    return counts


def _apply_line_counts(
    entries: list[WorkspaceChangeEntry],
    counts: dict[str, tuple[int | None, int | None]],
) -> list[WorkspaceChangeEntry]:
    return [
        replace(
            entry,
            added_lines=counts[entry.path][0] if entry.path in counts else None,
            removed_lines=counts[entry.path][1] if entry.path in counts else None,
        )
        for entry in entries
    ]


def _total(values: tuple[int | None, ...]) -> int:
    return sum(value for value in values if value is not None)


def _availability_reason(result_state: GitRunState) -> AvailabilityReason | None:
    if result_state is GitRunState.UNAVAILABLE:
        return "git_unavailable"
    if result_state is GitRunState.NOT_REPOSITORY:
        return "not_repository"
    if result_state is GitRunState.TIMED_OUT:
        return "timed_out"
    if result_state is GitRunState.FAILED:
        return "failed"
    return None


def read_workspace_changes(
    workspace_path: str,
    *,
    timeout: float = STATUS_TIMEOUT_SECONDS,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    environment: Mapping[str, str] | None = None,
) -> WorkspaceChanges:
    """Return the working-tree state of *workspace_path*.

    A missing Git, a timeout, or a directory that is not a repository is
    reported as ``available=False`` with a reason instead of raising: the panel
    needs to distinguish "nothing changed" from "cannot tell".
    """

    result = run_git(
        harden_read_only_git_args(
            (
                "status",
                "--porcelain=v2",
                "--branch",
                "--untracked-files=all",
                "-z",
            )
        ),
        cwd=workspace_path,
        timeout=timeout,
        environment=environment,
    )
    reason = _availability_reason(result.state)
    if reason is not None:
        return WorkspaceChanges(
            available=False,
            availability_reason=reason,
            branch=None,
            detached=False,
            upstream=None,
            ahead=0,
            behind=0,
            total_count=0,
            truncated=False,
            added_lines=0,
            removed_lines=0,
            entries=(),
        )
    header, entries = parse_porcelain_status(result.stdout_text)
    entries = _apply_line_counts(entries, _read_line_counts(workspace_path, timeout, environment))
    kept = entries[: max(0, max_entries)]
    return WorkspaceChanges(
        available=True,
        availability_reason=None,
        branch=header.branch,
        detached=header.detached,
        upstream=header.upstream,
        ahead=header.ahead,
        behind=header.behind,
        total_count=len(entries),
        truncated=len(entries) > len(kept),
        added_lines=_total(tuple(entry.added_lines for entry in entries)),
        removed_lines=_total(tuple(entry.removed_lines for entry in entries)),
        entries=tuple(kept),
    )


def _read_line_counts(
    workspace_path: str,
    timeout: float,
    environment: Mapping[str, str] | None,
) -> dict[str, tuple[int | None, int | None]]:
    """Per-file line counts against HEAD, or an empty map when unavailable.

    A missing count is reported as unknown rather than failing the whole read:
    the change list itself is still correct and useful without stats.
    """

    result = run_git(
        harden_read_only_git_args(("diff", "--numstat", "HEAD", "-z")),
        cwd=workspace_path,
        timeout=timeout,
        environment=environment,
    )
    if result.state is not GitRunState.OK:
        return {}
    return parse_numstat(result.stdout_text)


def _null_device() -> str:
    return "NUL" if os.name == "nt" else "/dev/null"


def is_untracked_path(
    workspace_path: str,
    path: str,
    *,
    timeout: float = DIFF_TIMEOUT_SECONDS,
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Return whether *path* has no index entry yet.

    ``git diff`` says nothing about such a file, so the caller must pick the
    ``--no-index`` comparison instead. ``ls-files --error-unmatch`` reports an
    unmatched path as exit status 1, which is the untracked answer rather than a
    Git failure.
    """

    repo_path = normalize_repo_path(path)
    result = run_git(
        harden_read_only_git_args(
            ("ls-files", "--error-unmatch", "--", repo_path)
        ),
        cwd=workspace_path,
        timeout=timeout,
        environment=environment,
    )
    if result.state is GitRunState.OK:
        return False
    if result.returncode == 1:
        return True
    raise WorkspaceGitUnavailableError(_availability_reason(result.state) or "failed")


def read_workspace_diff(
    workspace_path: str,
    path: str,
    *,
    staged: bool = False,
    untracked: bool = False,
    timeout: float = DIFF_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_DIFF_BYTES,
    environment: Mapping[str, str] | None = None,
) -> WorkspaceDiff:
    """Return one file's unified diff.

    ``untracked`` selects the ``--no-index`` comparison against the null device,
    because a not-yet-tracked file has no index or HEAD entry to diff against.
    """

    repo_path = normalize_repo_path(path)
    if untracked:
        # `--no-ext-diff` / `--no-textconv` come from the shared read-only
        # hardening, so only the comparison mode is selected here.
        args: tuple[str, ...] = (
            "diff",
            "--no-color",
            "--no-index",
            "--",
            _null_device(),
            repo_path,
        )
    else:
        args = (
            "diff",
            "--no-color",
            "--unified=3",
            *(("--cached",) if staged else ()),
            "--",
            repo_path,
        )
    result = run_git(
        harden_read_only_git_args(args),
        cwd=workspace_path,
        timeout=timeout,
        environment=environment,
    )
    # `--no-index` reports "differences found" as exit status 1, which is the
    # success path for an untracked file rather than a Git failure.
    succeeded = result.state is GitRunState.OK or (
        untracked and result.returncode == 1
    )
    if not succeeded:
        raise WorkspaceGitUnavailableError(
            _availability_reason(result.state) or "failed",
            result=result,
        )
    text = result.stdout_text
    binary = _BINARY_DIFF_RE.search(text) is not None
    truncated = len(text) > max_bytes
    return WorkspaceDiff(
        path=repo_path,
        staged=staged and not untracked,
        text=text[:max_bytes] if truncated else text,
        truncated=truncated,
        binary=binary,
    )


def read_staged_index_diff(
    workspace_path: str,
    *,
    timeout: float = DIFF_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_DIFF_BYTES,
    environment: Mapping[str, str] | None = None,
) -> WorkspaceDiff:
    """Return the whole staged patch, refusing an empty index.

    ``commit_index`` refuses to commit an empty index; this read refuses to
    describe one, because a message generated for an empty patch would be
    about nothing. The two share the same precondition code so a caller sees
    one reason, not two.

    Unlike a per-file read, the result names no path: it covers whatever the
    index holds, which is exactly what a commit message has to describe.
    """

    # Hardened like every other read on this surface: the emptiness probe is
    # still a read of a repository whose own configuration is untrusted, and
    # an unhardened `git diff` runs a repo-configured helper (``core.fsmonitor``)
    # and may write the index back. The write path deliberately skips this; a
    # read must not inherit that exemption.
    staged = run_git(
        harden_read_only_git_args(("diff", "--cached", "--quiet")),
        cwd=workspace_path,
        timeout=timeout,
        environment=environment,
    )
    if staged.state is GitRunState.OK:
        raise WorkspaceGitPreconditionError(
            "nothing_staged",
            "Nothing is staged, so there is nothing to describe.",
        )
    if staged.returncode != 1:
        raise WorkspaceGitUnavailableError(
            _write_availability_reason(staged),
            result=staged,
        )
    result = run_git(
        harden_read_only_git_args(
            ("diff", "--cached", "--no-color", "--unified=3")
        ),
        cwd=workspace_path,
        timeout=timeout,
        environment=environment,
    )
    if result.state is not GitRunState.OK:
        raise WorkspaceGitUnavailableError(
            _write_availability_reason(result),
            result=result,
        )
    text = result.stdout_text
    binary = _BINARY_DIFF_RE.search(text) is not None
    truncated = len(text) > max_bytes
    return WorkspaceDiff(
        path="",
        staged=True,
        text=text[:max_bytes] if truncated else text,
        truncated=truncated,
        binary=binary,
    )


__all__ = [
    "AvailabilityReason",
    "ChangeType",
    "DEFAULT_MAX_DIFF_BYTES",
    "DEFAULT_MAX_ENTRIES",
    "DIFF_TIMEOUT_SECONDS",
    "STATUS_TIMEOUT_SECONDS",
    "WorkspaceChangeEntry",
    "WorkspaceChanges",
    "WorkspaceDiff",
    "WorkspaceGitUnavailableError",
    "WorkspacePathError",
    "WorkspaceStatusParseError",
    "is_untracked_path",
    "normalize_repo_path",
    "parse_numstat",
    "parse_porcelain_status",
    "read_staged_index_diff",
    "read_workspace_changes",
    "read_workspace_diff",
    "stage_paths",
    "commit_index",
    "discard_paths",
    "push_current_branch",
    "undo_last_commit",
    "PreconditionCode",
    "PUSH_TIMEOUT_SECONDS",
    "WorkspaceGitPreconditionError",
    "WRITE_TIMEOUT_SECONDS",
]
