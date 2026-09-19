"""Offline parser coverage and real-Git integration for workspace inspection.

The parser fixtures are the exact NUL-separated byte streams Git 2.53 produced
for a real repository, so a change to the porcelain v2 assumptions fails here
instead of in the UI.  The integration tests drive a real repository because the
two untracked-file behaviours they pin (an empty ``git diff``, and the
``--no-index`` fallback against the platform null device) are properties of Git
itself, not of this module.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import pytest

from opensquilla import git_runtime, workspace_git_changes
from opensquilla.git_runtime import GitRunState
from opensquilla.workspace_git_changes import (
    WorkspaceGitPreconditionError,
    WorkspaceGitUnavailableError,
    WorkspacePathError,
    WorkspaceStatusParseError,
    commit_index,
    discard_paths,
    is_untracked_path,
    normalize_repo_path,
    parse_numstat,
    parse_porcelain_status,
    push_current_branch,
    read_workspace_changes,
    read_workspace_diff,
    stage_paths,
    undo_last_commit,
)

# Captured from `git status --porcelain=v2 --branch --untracked-files=all -z`
# after staging a modification (a.txt), a deletion (b.txt) and a rename
# ("c c.txt" -> "e e.txt"), plus one untracked file with a space in its name.
STATUS_WITH_RENAME = (
    "# branch.oid ca8238aacefd33f6888648691a11400f7508f1f8\x00"
    "# branch.head main\x00"
    "# branch.upstream origin/main\x00"
    "# branch.ab +0 -0\x00"
    "1 M. N... 100644 100644 100644 5626abf 0a93aab a.txt\x00"
    "1 D. N... 100644 000000 000000 f719efd 0000000 b.txt\x00"
    "2 R. N... 100644 100644 100644 2bdf67a 2bdf67a R100 e e.txt\x00c c.txt\x00"
    "? d d.txt\x00"
)

# Captured from a repository left in an unresolved merge conflict.
STATUS_UNMERGED = (
    "# branch.oid 4eef2e5664e7de9ca011bd955ce2bcd982883ac2\x00"
    "# branch.head main\x00"
    "u UU N... 100644 100644 100644 100644 df967b9 ba2906d 2299c37 f.txt\x00"
)


@pytest.fixture
def git_environment(tmp_path: Path) -> dict[str, str]:
    """Isolate Git configuration so output does not depend on the machine.

    A developer's ``diff.algorithm`` or ``core.quotepath`` must not change these
    assertions, so the system config is disabled and a synthetic global config
    only pins the initial branch name.
    """

    capability = git_runtime.resolve_git_capability(force_refresh=True)
    if not capability.available:
        pytest.skip(f"Git capability is unavailable: {capability.reason}")
    global_config = tmp_path / "isolated-gitconfig"
    global_config.write_text("[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    environment = dict(os.environ)
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = str(global_config)
    return environment


def _git(
    args: tuple[str, ...],
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> str:
    result = git_runtime.run_git(args, cwd=cwd, timeout=10.0, environment=environment)
    assert result.state is GitRunState.OK, result.stderr_text
    return result.stdout_text


def _init_repository(repository: Path, environment: Mapping[str, str]) -> None:
    repository.mkdir(parents=True, exist_ok=True)
    _git(("-c", "init.templateDir=", "init", "-q"), cwd=repository, environment=environment)
    _git(("config", "user.email", "tests@example.invalid"), cwd=repository, environment=environment)
    _git(("config", "user.name", "OpenSquilla Tests"), cwd=repository, environment=environment)
    _git(("config", "commit.gpgsign", "false"), cwd=repository, environment=environment)


def _commit_all(repository: Path, environment: Mapping[str, str], message: str = "commit") -> None:
    _git(("add", "--all"), cwd=repository, environment=environment)
    # `--allow-empty` keeps the helper usable for tests that only need a clean
    # baseline repository rather than a first commit.
    _git(("commit", "-q", "--allow-empty", "-m", message), cwd=repository, environment=environment)


def test_parse_status_projects_staged_and_unstaged_state() -> None:
    header, entries = parse_porcelain_status(STATUS_WITH_RENAME)

    assert header.branch == "main"
    assert header.detached is False
    assert header.upstream == "origin/main"
    assert (header.ahead, header.behind) == (0, 0)
    assert [
        (entry.path, entry.previous_path, entry.change_type, entry.staged, entry.unstaged)
        for entry in entries
    ] == [
        ("a.txt", None, "modified", True, False),
        ("b.txt", None, "deleted", True, False),
        # The rename record keeps the original path in the following NUL field
        # while the path itself still contains a space.
        ("e e.txt", "c c.txt", "renamed", True, False),
        ("d d.txt", None, "untracked", False, True),
    ]


def test_parse_status_reports_unmerged_entries() -> None:
    _, entries = parse_porcelain_status(STATUS_UNMERGED)

    assert len(entries) == 1
    entry = entries[0]
    assert (entry.path, entry.change_type) == ("f.txt", "unmerged")
    assert (entry.staged, entry.unstaged) == (True, True)


def test_parse_status_reads_detached_head_and_divergence() -> None:
    header, entries = parse_porcelain_status(
        "# branch.oid abc\x00"
        "# branch.head (detached)\x00"
        "# branch.upstream origin/main\x00"
        "# branch.ab +3 -2\x00"
    )

    assert header.detached is True
    assert header.branch is None
    assert (header.ahead, header.behind) == (3, 2)
    assert entries == []


def test_parse_status_skips_ignored_records() -> None:
    _, entries = parse_porcelain_status("! build/\x00? keep.txt\x00")

    assert [entry.path for entry in entries] == ["keep.txt"]


def test_parse_status_rejects_unknown_records() -> None:
    # An unrecognized record must fail loudly instead of silently shortening
    # the review list.
    with pytest.raises(WorkspaceStatusParseError):
        parse_porcelain_status("x unexpected record\x00")


# Captured from `git diff --numstat HEAD -z` after modifying a file, turning
# another into binary, and staging a rename.
NUMSTAT_WITH_RENAME = "2\t1\ta.ts\x00-\t-\tblob.bin\x000\t0\t\x00rename-me.ts\x00renamed.ts\x00"


def test_parse_numstat_reads_counts_and_keeps_binary_unknown() -> None:
    counts = parse_numstat(NUMSTAT_WITH_RENAME)

    assert counts["a.ts"] == (2, 1)
    # `-` means "no countable lines"; it must not become 0.
    assert counts["blob.bin"] == (None, None)
    # A rename record leaves its path field empty and carries both paths in the
    # following NUL fields, so either spelling resolves.
    assert counts["renamed.ts"] == (0, 0)
    assert counts["rename-me.ts"] == (0, 0)


def test_parse_numstat_ignores_stray_tokens() -> None:
    assert parse_numstat("\x00not-a-record\x00\x00") == {}


def test_read_workspace_changes_reports_counts_and_keeps_unknowns_unknown(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    (repository / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repository, git_environment)
    (repository / "tracked.txt").write_text("two\n", encoding="utf-8")
    (repository / "untracked.txt").write_text("new\n", encoding="utf-8")

    changes = read_workspace_changes(str(repository), environment=git_environment)

    counted = {entry.path: (entry.added_lines, entry.removed_lines) for entry in changes.entries}
    assert counted == {
        "tracked.txt": (1, 1),
        "untracked.txt": (None, None),
    }
    # Totals sum only what is known, so one unknown file cannot fake a zero.
    assert (changes.added_lines, changes.removed_lines) == (1, 1)


@pytest.mark.parametrize(
    "value",
    (
        "",
        "   ",
        "/etc/passwd",
        "\\\\server\\share\\file",
        "C:/Windows/system32",
        "..",
        "../../etc/passwd",
        "src/../../etc/passwd",
        "src/..",
        "nul\x00name",
    ),
)
def test_normalize_repo_path_rejects_escaping_values(value: str) -> None:
    with pytest.raises(WorkspacePathError):
        normalize_repo_path(value)


def test_normalize_repo_path_rejects_non_strings_and_long_values() -> None:
    with pytest.raises(WorkspacePathError):
        normalize_repo_path(None)
    with pytest.raises(WorkspacePathError):
        normalize_repo_path("src/" + "a" * 5000)


def test_normalize_repo_path_returns_the_caller_string_unchanged() -> None:
    # Callers round-trip the exact path reported by the status projection, so
    # normalizing separators here would break the follow-up diff request.
    assert normalize_repo_path("src/pkg/a b.txt") == "src/pkg/a b.txt"
    assert normalize_repo_path("src\\pkg\\a.txt") == "src\\pkg\\a.txt"
    assert normalize_repo_path("a/./b.txt") == "a/./b.txt"


def test_read_workspace_changes_projects_a_real_repository(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    (repository / "tracked.txt").write_text("one\n", encoding="utf-8")
    (repository / "renamed-source.txt").write_text("stable\n", encoding="utf-8")
    _commit_all(repository, git_environment)

    (repository / "tracked.txt").write_text("two\n", encoding="utf-8")
    (repository / "untracked.txt").write_text("new\n", encoding="utf-8")
    _git(
        ("mv", "renamed-source.txt", "renamed-target.txt"),
        cwd=repository,
        environment=git_environment,
    )

    changes = read_workspace_changes(
        str(repository),
        environment=git_environment,
    )

    assert changes.available is True
    assert changes.availability_reason is None
    assert changes.branch == "main"
    assert changes.detached is False
    assert changes.truncated is False
    assert changes.total_count == len(changes.entries) == 3
    projected = {
        entry.path: (entry.change_type, entry.staged, entry.unstaged, entry.previous_path)
        for entry in changes.entries
    }
    assert projected == {
        "tracked.txt": ("modified", False, True, None),
        "renamed-target.txt": ("renamed", True, False, "renamed-source.txt"),
        "untracked.txt": ("untracked", False, True, None),
    }


def test_read_workspace_changes_marks_truncation(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    _commit_all(repository, git_environment)
    for index in range(4):
        (repository / f"file-{index}.txt").write_text("x\n", encoding="utf-8")

    changes = read_workspace_changes(
        str(repository),
        max_entries=2,
        environment=git_environment,
    )

    assert changes.total_count == 4
    assert len(changes.entries) == 2
    assert changes.truncated is True


def test_read_workspace_changes_reports_a_non_repository(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    changes = read_workspace_changes(str(plain), environment=git_environment)

    assert changes.available is False
    assert changes.availability_reason == "not_repository"
    assert changes.entries == ()
    assert changes.total_count == 0


def test_read_workspace_diff_shows_unstaged_and_staged_halves(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    (repository / "file.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repository, git_environment)

    (repository / "file.txt").write_text("two\n", encoding="utf-8")
    _git(("add", "file.txt"), cwd=repository, environment=git_environment)
    (repository / "file.txt").write_text("three\n", encoding="utf-8")

    staged = read_workspace_diff(
        str(repository),
        "file.txt",
        staged=True,
        environment=git_environment,
    )
    unstaged = read_workspace_diff(
        str(repository),
        "file.txt",
        environment=git_environment,
    )

    assert staged.staged is True
    assert "-one" in staged.text
    assert "+two" in staged.text
    assert "+three" not in staged.text
    assert unstaged.staged is False
    assert "-two" in unstaged.text
    assert "+three" in unstaged.text
    assert (staged.truncated, unstaged.truncated) == (False, False)
    assert (staged.binary, unstaged.binary) == (False, False)


def test_read_workspace_diff_renders_untracked_files_as_new(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    _commit_all(repository, git_environment)
    (repository / "brand new.txt").write_text("hello untracked\n", encoding="utf-8")

    # `git diff` prints nothing for a path with no index entry, so the untracked
    # branch must be selected explicitly.
    assert is_untracked_path(
        str(repository),
        "brand new.txt",
        environment=git_environment,
    ) is True
    diff = read_workspace_diff(
        str(repository),
        "brand new.txt",
        untracked=True,
        environment=git_environment,
    )

    assert "new file mode" in diff.text
    assert "+hello untracked" in diff.text
    assert diff.staged is False
    assert diff.binary is False


def test_is_untracked_path_is_false_for_a_tracked_file(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    (repository / "file.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repository, git_environment)

    assert is_untracked_path(
        str(repository),
        "file.txt",
        environment=git_environment,
    ) is False


def test_read_workspace_diff_flags_binary_content(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    (repository / "blob.bin").write_bytes(b"\x00\x01\x02binary\x00")
    _commit_all(repository, git_environment)
    (repository / "blob.bin").write_bytes(b"\x00\x01\x03changed\x00\xff")

    diff = read_workspace_diff(
        str(repository),
        "blob.bin",
        environment=git_environment,
    )

    assert diff.binary is True


def test_read_workspace_diff_bounds_oversized_output(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    (repository / "file.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repository, git_environment)
    (repository / "file.txt").write_text("two\n" * 200, encoding="utf-8")

    diff = read_workspace_diff(
        str(repository),
        "file.txt",
        max_bytes=64,
        environment=git_environment,
    )

    assert diff.truncated is True
    assert len(diff.text) == 64


def test_read_workspace_diff_raises_outside_a_repository(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(WorkspaceGitUnavailableError) as raised:
        read_workspace_diff(str(plain), "file.txt", environment=git_environment)

    assert raised.value.reason == "not_repository"


def test_read_workspace_diff_raises_for_an_escaping_path(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)

    with pytest.raises(WorkspacePathError):
        read_workspace_diff(str(repository), "../../etc/passwd")


def test_read_only_reads_apply_the_shared_git_hardening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every Git invocation must disable repository-controlled helpers.

    A repository can configure ``core.fsmonitor``, ``diff.external`` or a
    textconv driver that would otherwise make these reads execute code the
    repository chose, so the argv is asserted directly instead of inferred from
    output.  The stub keeps this a hermetic unit test.
    """

    captured: list[tuple[str, ...]] = []

    def _capture(args: tuple[str, ...], **_kwargs: object) -> git_runtime.GitRunResult:
        captured.append(tuple(args))
        return git_runtime.GitRunResult(
            state=GitRunState.OK,
            returncode=0,
            stdout=b"",
            stderr=b"",
            capability=git_runtime.GitCapability(
                state=git_runtime.GitCapabilityState.AVAILABLE,
                executable=Path("git"),
                source="test",
            ),
        )

    monkeypatch.setattr(workspace_git_changes, "run_git", _capture)

    read_workspace_changes(".")           # status + numstat
    read_workspace_diff(".", "file.txt")
    is_untracked_path(".", "file.txt")
    stage_paths(".", ("file.txt",), staged=True)
    stage_paths(".", ("file.txt",), staged=False)

    def find(marker: str) -> tuple[str, ...]:
        return next(args for args in captured if marker in args)

    # Every read disables repository-controlled helpers...
    for args in captured:
        if args[0] in {"add", "restore"}:
            continue
        assert args[:3] == ("--no-optional-locks", "-c", "core.fsmonitor=false"), args

    # ...and both diff-shaped reads go further still.
    for marker in ("--numstat", "--unified=3"):
        args = find(marker)
        assert args[3:5] == ("diff", "--no-ext-diff"), args
        assert "--no-textconv" in args
    assert "ls-files" in find("ls-files")
    assert "status" in find("status")

    # A write is the one invocation that legitimately takes the index lock, so
    # the read-only flags must not be smuggled into its argv.
    stage_args = next(args for args in captured if args[0] == "add")
    unstage_args = next(args for args in captured if args[0] == "restore")
    assert stage_args == ("add", "--", "file.txt"), stage_args
    assert unstage_args == ("restore", "--staged", "--", "file.txt"), unstage_args


def test_stage_paths_stages_an_untracked_file_and_leaves_it_on_disk(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    _commit_all(repository, git_environment)
    (repository / "new.txt").write_text("fresh\n", encoding="utf-8")

    applied = stage_paths(str(repository), ("new.txt",), staged=True, environment=git_environment)

    assert applied == ("new.txt",)
    assert (repository / "new.txt").read_text(encoding="utf-8") == "fresh\n"
    changes = read_workspace_changes(str(repository), environment=git_environment)
    assert [(entry.path, entry.staged) for entry in changes.entries] == [("new.txt", True)]


def test_unstage_restores_the_index_only_and_keeps_the_edit(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    """The safety property that makes this method publishable.

    A reviewer who unstages a file must still have the edit afterwards; only
    the index may move.  This asserts the worktree bytes, not just the status
    line, because a status-only assertion would still pass if the content had
    been reverted.
    """

    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    (repository / "tracked.txt").write_text("original\n", encoding="utf-8")
    _commit_all(repository, git_environment)
    (repository / "tracked.txt").write_text("edited\n", encoding="utf-8")
    stage_paths(str(repository), ("tracked.txt",), staged=True, environment=git_environment)

    stage_paths(str(repository), ("tracked.txt",), staged=False, environment=git_environment)

    assert (repository / "tracked.txt").read_text(encoding="utf-8") == "edited\n"
    changes = read_workspace_changes(str(repository), environment=git_environment)
    assert [(entry.path, entry.staged, entry.unstaged) for entry in changes.entries] == [
        ("tracked.txt", False, True)
    ]


def test_stage_paths_is_idempotent(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    (repository / "tracked.txt").write_text("original\n", encoding="utf-8")
    _commit_all(repository, git_environment)
    (repository / "tracked.txt").write_text("edited\n", encoding="utf-8")

    first = stage_paths(str(repository), ("tracked.txt",), environment=git_environment)
    after_first = read_workspace_changes(str(repository), environment=git_environment)
    second = stage_paths(str(repository), ("tracked.txt",), environment=git_environment)
    after_second = read_workspace_changes(str(repository), environment=git_environment)

    assert first == second == ("tracked.txt",)
    assert [entry.staged for entry in after_first.entries] == [True]
    assert [entry.staged for entry in after_second.entries] == [True]


def test_stage_paths_rejects_escaping_paths_before_running_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unexpected(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Git must not run for a path outside the workspace")

    monkeypatch.setattr(workspace_git_changes, "run_git", _unexpected)

    with pytest.raises(WorkspacePathError):
        stage_paths(".", ("../outside.txt",))


def test_stage_paths_requires_at_least_one_path() -> None:
    with pytest.raises(WorkspacePathError):
        stage_paths(".", ())


def test_discard_restores_the_worktree_from_the_index_not_from_head(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    """Discarding edits must not also throw away staged content.

    The same file is staged *and* edited, which is the case where restoring from
    HEAD instead of the index would silently delete the staged version.
    """

    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    (repository / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _commit_all(repository, git_environment)
    (repository / "tracked.txt").write_text("staged\n", encoding="utf-8")
    stage_paths(str(repository), ("tracked.txt",), environment=git_environment)
    (repository / "tracked.txt").write_text("unstaged\n", encoding="utf-8")

    discarded = discard_paths(
        str(repository), ("tracked.txt",), environment=git_environment
    )

    assert discarded == ("tracked.txt",)
    # The edit is gone; the staged content is what remains.
    assert (repository / "tracked.txt").read_text(encoding="utf-8") == "staged\n"
    changes = read_workspace_changes(str(repository), environment=git_environment)
    assert [(entry.path, entry.staged, entry.unstaged) for entry in changes.entries] == [
        ("tracked.txt", True, False)
    ]


def test_discard_refuses_an_untracked_path_without_deleting_it(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    """A file the agent just created is the one a mis-click must not lose."""

    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    _commit_all(repository, git_environment)
    (repository / "fresh.txt").write_text("agent work\n", encoding="utf-8")

    with pytest.raises(WorkspaceGitPreconditionError) as raised:
        discard_paths(str(repository), ("fresh.txt",), environment=git_environment)

    assert raised.value.code == "untracked_path"
    assert (repository / "fresh.txt").read_text(encoding="utf-8") == "agent work\n"


def test_discard_recreates_a_deleted_tracked_file(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    (repository / "tracked.txt").write_text("kept\n", encoding="utf-8")
    _commit_all(repository, git_environment)
    (repository / "tracked.txt").unlink()

    discard_paths(str(repository), ("tracked.txt",), environment=git_environment)

    assert (repository / "tracked.txt").read_text(encoding="utf-8") == "kept\n"


def test_commit_refuses_an_empty_index(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    """Refused with its own reason, not with Git's prose."""

    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    _commit_all(repository, git_environment)

    with pytest.raises(WorkspaceGitPreconditionError) as raised:
        commit_index(str(repository), "nothing here", environment=git_environment)

    assert raised.value.code == "nothing_staged"


def test_commit_commits_only_the_index(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    (repository / "staged.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repository, git_environment)
    (repository / "staged.txt").write_text("two\n", encoding="utf-8")
    (repository / "loose.txt").write_text("not staged\n", encoding="utf-8")
    stage_paths(str(repository), ("staged.txt",), environment=git_environment)

    sha, subject = commit_index(
        str(repository),
        "tighten the thing\n\nlonger body",
        environment=git_environment,
    )

    assert len(sha) == 40
    assert subject == "tighten the thing"
    # The unstaged file is still uncommitted, i.e. nothing was swept in.
    changes = read_workspace_changes(str(repository), environment=git_environment)
    assert [(entry.path, entry.change_type) for entry in changes.entries] == [
        ("loose.txt", "untracked")
    ]
    logged = _git(("log", "-1", "--pretty=%s"), cwd=repository, environment=git_environment)
    assert logged.strip() == "tighten the thing"


def test_push_refuses_a_branch_without_an_upstream(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    _commit_all(repository, git_environment)

    with pytest.raises(WorkspaceGitPreconditionError) as raised:
        push_current_branch(
            str(repository), upstream=None, environment=git_environment
        )

    assert raised.value.code == "no_upstream"


def test_push_publishes_to_the_tracked_upstream(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    """A local bare repository stands in for the remote, so this stays offline."""

    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(
        ("-c", "init.templateDir=", "init", "--bare", "-q"),
        cwd=origin,
        environment=git_environment,
    )
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    (repository / "file.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repository, git_environment)
    _git(("remote", "add", "origin", str(origin)), cwd=repository, environment=git_environment)
    _git(("push", "-u", "origin", "main"), cwd=repository, environment=git_environment)
    (repository / "file.txt").write_text("two\n", encoding="utf-8")
    stage_paths(str(repository), ("file.txt",), environment=git_environment)
    commit_index(str(repository), "second", environment=git_environment)

    output = push_current_branch(
        str(repository), upstream="origin/main", environment=git_environment
    )

    assert "main" in output
    # The remote now has the commit, so the local branch is no longer ahead.
    changes = read_workspace_changes(str(repository), environment=git_environment)
    assert (changes.ahead, changes.behind) == (0, 0)


def test_undo_last_commit_keeps_the_content_staged(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    """A soft reset: the commit goes, the work stays, in the index."""

    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    (repository / "tracked.txt").write_text("first\n", encoding="utf-8")
    _commit_all(repository, git_environment, "first")
    first = _git(("rev-parse", "HEAD"), cwd=repository, environment=git_environment).strip()
    (repository / "tracked.txt").write_text("second\n", encoding="utf-8")
    _commit_all(repository, git_environment, "second commit")

    sha, subject = undo_last_commit(
        str(repository), upstream=None, ahead=0, environment=git_environment
    )

    assert subject == "second commit"
    assert len(sha) == 40
    assert _git(("rev-parse", "HEAD"), cwd=repository, environment=git_environment).strip() == first
    # The undone work is staged, not lost, and the worktree still has it.
    assert (repository / "tracked.txt").read_text(encoding="utf-8") == "second\n"
    changes = read_workspace_changes(str(repository), environment=git_environment)
    assert [(entry.path, entry.staged, entry.unstaged) for entry in changes.entries] == [
        ("tracked.txt", True, False)
    ]


def test_undo_refuses_a_commit_the_upstream_already_has(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    """Rewriting published history needs a force push, so it is refused."""

    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    (repository / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repository, git_environment)

    with pytest.raises(WorkspaceGitPreconditionError) as raised:
        undo_last_commit(
            str(repository),
            upstream="origin/main",
            ahead=0,
            environment=git_environment,
        )

    assert raised.value.code == "commit_published"


def test_undo_refuses_the_root_commit(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    _commit_all(repository, git_environment)

    with pytest.raises(WorkspaceGitPreconditionError) as raised:
        undo_last_commit(
            str(repository), upstream=None, ahead=0, environment=git_environment
        )

    assert raised.value.code == "no_parent"


def test_stage_paths_reports_gits_own_output_on_failure(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    """An index-only operation that does not apply must say why.

    Unstaging a path with no index entry is the realistic failure (the caller's
    view is stale), and the message has to come from Git rather than a generic
    "failed", or the panel cannot explain what happened.
    """

    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    _commit_all(repository, git_environment)
    (repository / "untracked.txt").write_text("never staged\n", encoding="utf-8")

    with pytest.raises(WorkspaceGitUnavailableError) as raised:
        stage_paths(str(repository), ("untracked.txt",), staged=False, environment=git_environment)

    assert raised.value.reason == "failed"
    assert raised.value.result is not None
    assert "untracked.txt" in raised.value.result.stderr_text
    # Nothing was staged as a side effect of the failed call.
    changes = read_workspace_changes(str(repository), environment=git_environment)
    assert [(entry.path, entry.change_type) for entry in changes.entries] == [
        ("untracked.txt", "untracked")
    ]
