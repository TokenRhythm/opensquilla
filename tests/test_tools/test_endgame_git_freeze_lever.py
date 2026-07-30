"""Endgame git freeze lever: shell-side blocking of workspace-reverting git.

Covers the tools half of OPENSQUILLA_ENDGAME_GIT_FREEZE_MARGIN_SECONDS: the
engine arms ToolContext.endgame_git_freeze_active near the turn deadline, and
the shell tools then block destructive git commands — restore, path/branch
checkouts, hard resets, force-clean, stash push/drop/clear — outright, with
no protected-path intersection, so the current workspace diff survives
runner-side collection. Unarmed contexts (the default) are untouched.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from opensquilla.gateway.approval_queue import reset_approval_queue
from opensquilla.sandbox.integration import reset_runtime
from opensquilla.tools import source_diff_preservation
from opensquilla.tools.builtin import shell
from opensquilla.tools.source_diff_preservation import (
    endgame_git_freeze_block_json,
    endgame_git_freeze_decision,
)
from opensquilla.tools.types import (
    CallerKind,
    InteractionMode,
    ToolContext,
    current_tool_context,
)


@pytest.fixture(autouse=True)
def _tool_context():
    reset_approval_queue()
    reset_runtime()
    token = current_tool_context.set(
        ToolContext(is_owner=True, caller_kind=CallerKind.CLI, session_key="agent:main:test")
    )
    yield
    current_tool_context.reset(token)
    reset_approval_queue()
    reset_runtime()


def _configure_ctx(workspace: Path | None = None, *, frozen: bool = False) -> ToolContext:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    if workspace is not None:
        ctx.workspace_dir = str(workspace)
    ctx.endgame_git_freeze_active = frozen
    return ctx


def _init_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "workspace"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "agent@test.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "agent"], check=True)
    target = repo / "pkg.py"
    target.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    return repo, target


def test_decision_none_when_flag_unset() -> None:
    # The default: nothing armed the context, so even a hard reset is left to
    # the ordinary guards.
    _configure_ctx(frozen=False)

    assert endgame_git_freeze_decision(command="git reset --hard") is None


@pytest.mark.parametrize(
    ("command", "operation"),
    [
        ("git reset --hard", "git_reset_hard"),
        ("git reset --hard HEAD~1", "git_reset_hard"),
        ("git checkout -- pkg.py", "git_checkout"),
        ("git checkout main", "git_checkout"),
        ("git checkout -f", "git_checkout_force"),
        ("git restore pkg.py", "git_restore"),
        ("git restore --staged pkg.py", "git_restore"),
        ("git clean -fd", "git_clean"),
        ("git stash", "git_stash"),
        ("git stash -u", "git_stash"),
        # -m consumes a message, not a subcommand: still an implicit push.
        ("git stash -m wip", "git_stash"),
        ("git stash --message wip", "git_stash"),
        ("git stash push -m wip", "git_stash"),
        ("git stash save wip", "git_stash"),
        ("git stash drop", "git_stash"),
        ("git stash clear", "git_stash"),
        # Global options before the verb revert just as effectively.
        ("git -C . checkout -- pkg.py", "git_checkout"),
        ("git --no-pager reset --hard", "git_reset_hard"),
        ("git -c core.pager=cat restore pkg.py", "git_restore"),
        ("git --git-dir=.git --work-tree=. reset --hard", "git_reset_hard"),
        # switch only freezes in its change-discarding forms.
        ("git switch -f main", "git_switch_force"),
        ("git switch --discard-changes main", "git_switch_force"),
        # Shell wrappers around a frozen command are unwrapped.
        ("sh -c 'git reset --hard'", "git_reset_hard"),
        ("bash -lc 'git checkout -- .'", "git_checkout"),
        ("echo done && git reset --hard", "git_reset_hard"),
    ],
)
def test_decision_blocks_destructive_git_when_frozen(command: str, operation: str) -> None:
    _configure_ctx(frozen=True)

    payload = endgame_git_freeze_decision(command=command)

    assert payload is not None
    assert payload["status"] == "blocked"
    assert payload["reason"] == "endgame_git_freeze"
    assert payload["matched_operation"] == operation
    assert payload["retry_allowed"] is True


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git diff",
        "git add -A",
        "git commit -m done",
        "git log --oneline",
        "git stash pop",
        "git stash apply",
        "git stash list",
        "git stash show -p",
        "git checkout -b feature",
        # Plain switch refuses to clobber local changes on its own.
        "git switch main",
        "git switch -c feature",
        "git -C . diff",
        "sh -c 'git status'",
        "ls -la",
        "sed -i 's/a/b/' pkg.py",
    ],
)
def test_decision_allows_non_destructive_commands_when_frozen(command: str) -> None:
    # The freeze only blocks workspace-reverting operations; committing,
    # inspecting, applying stashed work, editing files, and creating branches
    # all remain available in the wrap-up window.
    _configure_ctx(frozen=True)

    assert endgame_git_freeze_decision(command=command) is None


def test_decision_emits_runtime_event() -> None:
    ctx = _configure_ctx(frozen=True)
    events: list[dict] = []
    ctx.on_runtime_event = events.append

    payload = endgame_git_freeze_decision(command="git reset --hard")

    assert payload is not None
    assert len(events) == 1
    assert events[0]["feature"] == "endgame_git_freeze"
    assert events[0]["name"] == "endgame_git_freeze.blocked"
    assert events[0]["matched_operation"] == "git_reset_hard"
    assert events[0]["command"] == "git reset --hard"


def test_block_guidance_speaks_of_pending_changes_only() -> None:
    # The model-facing guidance describes the effect on the pending changes;
    # it must not reference runner internals.
    _configure_ctx(frozen=True)

    payload = endgame_git_freeze_decision(command="git reset --hard")

    assert payload is not None
    guidance = str(payload["recommended_next_action"])
    assert "pending changes stay intact" in guidance
    assert "collection" not in guidance


def test_block_json_round_trips_payload() -> None:
    _configure_ctx(frozen=True)

    raw = endgame_git_freeze_block_json(command="git checkout -- pkg.py")

    assert raw is not None
    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "endgame_git_freeze"
    assert payload["target_paths"] == ["pkg.py"]
    assert endgame_git_freeze_block_json(command="git status") is None


@pytest.mark.asyncio
async def test_exec_command_blocks_checkout_when_frozen(tmp_path: Path) -> None:
    repo, target = _init_repo(tmp_path)
    target.write_text("value = 2\n", encoding="utf-8")
    _configure_ctx(repo, frozen=True)

    result = await shell.exec_command("git checkout -- pkg.py", workdir=str(repo))

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "endgame_git_freeze"
    assert target.read_text(encoding="utf-8") == "value = 2\n"


@pytest.mark.asyncio
async def test_exec_command_default_lets_checkout_revert(tmp_path: Path) -> None:
    # Documents the default gap the lever closes: with the freeze unarmed the
    # revert executes and the pending diff is gone.
    repo, target = _init_repo(tmp_path)
    target.write_text("value = 2\n", encoding="utf-8")
    _configure_ctx(repo, frozen=False)

    result = await shell.exec_command("git checkout -- pkg.py", workdir=str(repo))

    assert result.startswith("exit_code=0")
    assert target.read_text(encoding="utf-8") == "value = 1\n"


@pytest.mark.asyncio
async def test_exec_command_scans_stdin_when_frozen(tmp_path: Path) -> None:
    repo, target = _init_repo(tmp_path)
    target.write_text("value = 2\n", encoding="utf-8")
    _configure_ctx(repo, frozen=True)

    result = await shell.exec_command(
        "sh", workdir=str(repo), stdin="git reset --hard\n"
    )

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "endgame_git_freeze"
    assert target.read_text(encoding="utf-8") == "value = 2\n"


@pytest.mark.asyncio
async def test_background_process_blocks_destructive_git_when_frozen(
    tmp_path: Path,
) -> None:
    repo, target = _init_repo(tmp_path)
    target.write_text("value = 2\n", encoding="utf-8")
    _configure_ctx(repo, frozen=True)

    result = await shell.background_process("git reset --hard", workdir=str(repo))

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "endgame_git_freeze"
    assert target.read_text(encoding="utf-8") == "value = 2\n"


@pytest.mark.asyncio
async def test_exec_command_blocks_checkout_when_frozen_under_host_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The freeze check sits before the host-execution branch, so host-executed
    # shells are frozen too — unlike the sandbox-only policy block.
    monkeypatch.setattr(shell, "_host_execution_allowed", lambda: True)
    repo, target = _init_repo(tmp_path)
    target.write_text("value = 2\n", encoding="utf-8")
    _configure_ctx(repo, frozen=True)

    result = await shell.exec_command("git checkout -- pkg.py", workdir=str(repo))

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "endgame_git_freeze"
    assert target.read_text(encoding="utf-8") == "value = 2\n"


# ---------------------------------------------------------------------------
# Instrumentation exemption (OPENSQUILLA_ENDGAME_GIT_FREEZE_INSTRUMENTATION_EXEMPT)
# ---------------------------------------------------------------------------


def _exempt_ctx(workspace: Path | None = None) -> ToolContext:
    ctx = _configure_ctx(workspace, frozen=True)
    ctx.endgame_git_freeze_instrumentation_exempt = True
    return ctx


def test_decision_exempts_instrumentation_only_revert(tmp_path: Path) -> None:
    # Cleaning up diagnostic prints is exactly what the wrap-up window is
    # for: a revert whose targeted diff only added print lines goes through.
    repo, target = _init_repo(tmp_path)
    target.write_text('value = 1\nprint("debug")\n', encoding="utf-8")
    ctx = _exempt_ctx(repo)
    events: list[dict] = []
    ctx.on_runtime_event = events.append

    assert endgame_git_freeze_decision(command="git checkout -- pkg.py") is None
    assert [event["name"] for event in events] == ["endgame_git_freeze.exempted"]
    assert events[0]["feature"] == "endgame_git_freeze"
    assert events[0]["reason"] == "instrumentation_only_diff"
    assert events[0]["status"] == "exempted"
    assert events[0]["matched_operation"] == "git_checkout"
    assert events[0]["target_paths"] == ["pkg.py"]


def test_decision_blocks_substantive_revert_despite_exemption(tmp_path: Path) -> None:
    repo, target = _init_repo(tmp_path)
    target.write_text("value = 2\n", encoding="utf-8")
    _exempt_ctx(repo)

    payload = endgame_git_freeze_decision(command="git checkout -- pkg.py")

    assert payload is not None
    assert payload["status"] == "blocked"
    assert payload["reason"] == "endgame_git_freeze"


def test_decision_blocks_instrumentation_revert_when_exemption_off(
    tmp_path: Path,
) -> None:
    # Documents the flag gate: without the exemption lever the freeze blocks
    # instrumentation-only reverts like any other.
    repo, target = _init_repo(tmp_path)
    target.write_text('value = 1\nprint("debug")\n', encoding="utf-8")
    _configure_ctx(repo, frozen=True)

    payload = endgame_git_freeze_decision(command="git checkout -- pkg.py")

    assert payload is not None
    assert payload["status"] == "blocked"


def test_decision_exempts_per_target_but_blocks_whole_worktree(tmp_path: Path) -> None:
    # The probed diff follows the command's targets: a checkout of the
    # instrumented file is exempt while a whole-worktree reset that would also
    # revert the substantive file stays blocked.
    repo, target = _init_repo(tmp_path)
    other = repo / "other.py"
    other.write_text("keep = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "add other"], check=True)
    target.write_text('value = 1\nprint("debug")\n', encoding="utf-8")
    other.write_text("keep = 2\n", encoding="utf-8")
    _exempt_ctx(repo)

    assert endgame_git_freeze_decision(command="git checkout -- pkg.py") is None
    payload = endgame_git_freeze_decision(command="git reset --hard")
    assert payload is not None
    assert payload["status"] == "blocked"


def test_decision_keeps_blocking_untracked_only_clean(tmp_path: Path) -> None:
    # git clean removes untracked files; `git diff HEAD` cannot classify what
    # it would destroy, so the exemption never applies to untracked-only ops.
    repo, target = _init_repo(tmp_path)
    target.write_text('value = 1\nprint("debug")\n', encoding="utf-8")
    (repo / "scratch.txt").write_text("scratch\n", encoding="utf-8")
    _exempt_ctx(repo)

    payload = endgame_git_freeze_decision(command="git clean -fd")

    assert payload is not None
    assert payload["status"] == "blocked"


def test_decision_keeps_blocking_when_workspace_unresolvable() -> None:
    # Probe failure fails conservative: no workspace to diff means the freeze
    # stays in force.
    _exempt_ctx(workspace=None)

    payload = endgame_git_freeze_decision(command="git checkout -- pkg.py")

    assert payload is not None
    assert payload["status"] == "blocked"


def _add_second_commit(repo: Path, target: Path) -> None:
    target.write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "second"], check=True)


def test_decision_blocks_ref_source_revert_despite_instrumentation_only(
    tmp_path: Path,
) -> None:
    # `git diff HEAD` cannot see what `checkout <older ref> --` destroys:
    # the worktree diff is instrumentation-only, but the command rewrites the
    # target to pre-HEAD content. The exemption must never model that as a
    # clean instrumentation revert.
    repo, target = _init_repo(tmp_path)
    _add_second_commit(repo, target)
    target.write_text('value = 2\nprint("debug")\n', encoding="utf-8")
    _exempt_ctx(repo)

    # Positive control: the same command with HEAD as the source is exactly
    # worktree-vs-HEAD scoped and stays exempt.
    assert endgame_git_freeze_decision(command="git checkout HEAD -- pkg.py") is None

    for command in (
        "git checkout HEAD~1 -- pkg.py",
        "git restore --source=HEAD~1 pkg.py",
        "git reset --hard HEAD~1",
        "sh -c 'git checkout HEAD~1 -- pkg.py'",
        "git checkout -- pkg.py && git reset --hard HEAD~1",
    ):
        payload = endgame_git_freeze_decision(command=command)
        assert payload is not None, command
        assert payload["status"] == "blocked", command
    assert target.read_text(encoding="utf-8") == 'value = 2\nprint("debug")\n'


def test_decision_blocks_force_switch_despite_instrumentation_only(
    tmp_path: Path,
) -> None:
    # A force switch moves HEAD to another branch; the instrumentation-only
    # worktree diff says nothing about that damage.
    repo, target = _init_repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "branch", "other"], check=True)
    target.write_text('value = 1\nprint("debug")\n', encoding="utf-8")
    _exempt_ctx(repo)

    payload = endgame_git_freeze_decision(command="git switch -f other")

    assert payload is not None
    assert payload["status"] == "blocked"


@pytest.mark.parametrize(
    ("command", "beyond"),
    [
        ("git checkout -- pkg.py", False),
        ("git checkout HEAD -- pkg.py", False),
        ("git checkout HEAD~1 -- pkg.py", True),
        ("git checkout abc1234 -- pkg.py", True),
        ("git checkout -f", False),
        ("git checkout -f main", True),
        ("git checkout -", True),
        ("git restore pkg.py", False),
        ("git restore --source=HEAD pkg.py", False),
        ("git restore --source=HEAD~2 pkg.py", True),
        ("git restore -s HEAD~2 pkg.py", True),
        ("git restore -sHEAD~2 pkg.py", True),
        ("git reset --hard", False),
        ("git reset --hard HEAD", False),
        ("git reset --hard HEAD~1", True),
        ("git switch -f main", True),
        ("git switch --discard-changes main", True),
        ("git stash", False),
        ("sh -c 'git checkout HEAD~1 -- pkg.py'", True),
        ("git checkout -- pkg.py && git reset --hard HEAD~1", True),
        ("git -C . checkout HEAD~1 -- pkg.py", True),
    ],
)
def test_freeze_exemption_scope_beyond_head(command: str, beyond: bool) -> None:
    assert (
        source_diff_preservation._freeze_exemption_scope_beyond_head(command) is beyond
    )


@pytest.mark.asyncio
async def test_exec_command_executes_exempted_instrumentation_revert(
    tmp_path: Path,
) -> None:
    repo, target = _init_repo(tmp_path)
    target.write_text('value = 1\nprint("debug")\n', encoding="utf-8")
    _exempt_ctx(repo)

    result = await shell.exec_command("git checkout -- pkg.py", workdir=str(repo))

    assert result.startswith("exit_code=0")
    assert target.read_text(encoding="utf-8") == "value = 1\n"


@pytest.mark.asyncio
async def test_freeze_short_circuits_before_source_diff_bookkeeping(
    tmp_path: Path,
) -> None:
    # When the freeze blocks a command, the source-diff preservation guard
    # must not run first: its log-mode side effects (candidate marked lost,
    # revert-observed event) describe a revert that never executed.
    repo, target = _init_repo(tmp_path)
    target.write_text("value = 2\n", encoding="utf-8")
    ctx = _configure_ctx(repo, frozen=True)
    ctx.source_diff_preservation_mode = "log"
    candidate = {
        "candidate_id": "srcdiff-1",
        "paths": ["pkg.py"],
        "patch": "stub\n",
        "lost": False,
        "restored": False,
    }
    ctx.source_diff_candidates = [candidate]
    events: list[dict] = []
    ctx.on_runtime_event = events.append

    result = await shell.exec_command("git checkout -- pkg.py", workdir=str(repo))

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "endgame_git_freeze"
    assert candidate["lost"] is False
    assert all(event.get("feature") != "source_diff_preservation" for event in events)
    assert all(event.get("name") != "source_diff_candidate.marked_lost" for event in events)
    assert target.read_text(encoding="utf-8") == "value = 2\n"
