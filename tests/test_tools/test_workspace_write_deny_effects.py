"""Effect-based workspace write deny enforcement (post-execution).

Covers OPENSQUILLA_WORKSPACE_WRITE_DENY_EFFECT (off/warn/revert),
OPENSQUILLA_WORKSPACE_WRITE_DENY_TRACKED_ONLY,
OPENSQUILLA_WORKSPACE_WRITE_DENY_SYMLINK_GUARD, the ln/interpreter
argv-table extensions, and the sh -c wrapper unwrap. Motivation: pre-execution
screens only see paths named in tool arguments; a command can still mutate a
protected path through a helper program it wrote and ran (observed escape
class), through a symlink, or through `ln` itself.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from opensquilla.git_runtime import GitRunState
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, reset_runtime
from opensquilla.sandbox.types import (
    DenialReason,
    DenialResult,
    SecurityLevel,
    SuggestedNextStep,
)
from opensquilla.tools import write_policy
from opensquilla.tools.builtin import code_exec, shell
from opensquilla.tools.builtin.code_exec import execute_code
from opensquilla.tools.builtin.shell import exec_command
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context
from opensquilla.tools.write_tracking import WorkspaceMutationSnapshot

_EFFECT_ENV = "OPENSQUILLA_WORKSPACE_WRITE_DENY_EFFECT"
_TRACKED_ONLY_ENV = "OPENSQUILLA_WORKSPACE_WRITE_DENY_TRACKED_ONLY"
_SYMLINK_GUARD_ENV = "OPENSQUILLA_WORKSPACE_WRITE_DENY_SYMLINK_GUARD"
_GUIDANCE_ENV = "OPENSQUILLA_WORKSPACE_WRITE_DENY_GUIDANCE"
_HOST_SHELL_ENV = "OPENSQUILLA_WORKSPACE_WRITE_DENY_HOST_SHELL"
_COMMAND_TARGETS_ENV = "OPENSQUILLA_WORKSPACE_WRITE_DENY_COMMAND_TARGETS"
_INTERPRETER_TARGETS_ENV = "OPENSQUILLA_WORKSPACE_WRITE_DENY_INTERPRETER_TARGETS"
_SANDBOX_FULL_HOST_ENV = "OPENSQUILLA_SANDBOX_DISABLED_FULL_HOST"
_ALL_ENVS = (
    _EFFECT_ENV,
    _TRACKED_ONLY_ENV,
    _SYMLINK_GUARD_ENV,
    _GUIDANCE_ENV,
    _HOST_SHELL_ENV,
    _COMMAND_TARGETS_ENV,
    _INTERPRETER_TARGETS_ENV,
    _SANDBOX_FULL_HOST_ENV,
)

# The deny glob list shipped by the SWE arm configs; the effect layer must
# work against it verbatim (root-level *_test.* files included).
_ARM_DENY_GLOBS = [
    "test/**",
    "tests/**",
    "**/test/**",
    "**/tests/**",
    "__tests__/**",
    "**/__tests__/**",
    "*.spec.*",
    "*.test.*",
    "**/*.spec.*",
    "**/*.test.*",
    "**/*_test.*",
    "**/*_spec.*",
    "**/test_*.py",
]


def _init_git_workspace(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)


def _commit_all(path: Path, message: str = "base") -> None:
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=path, check=True
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for name in _ALL_ENVS:
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def effect_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # These policies run in deployments that disable the sandbox but opt out
    # of the Full Host Access fallback; the fixture mirrors that setup.
    monkeypatch.setenv(_SANDBOX_FULL_HOST_ENV, "off")
    reset_runtime()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _init_git_workspace(workspace)
    (workspace / "replacer_test.go").write_text("original\n", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")
    _commit_all(workspace)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    configure_runtime(
        SandboxSettings(
            sandbox=False,
            security_grading=False,
            allow_legacy_mode=True,
        ),
        workspace=workspace,
    )
    events: list[dict] = []
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        workspace_dir=str(workspace),
        scratch_dir=str(scratch),
        session_key="agent:main:test",
        on_runtime_event=events.append,
    )
    ctx.workspace_write_deny_globs = list(_ARM_DENY_GLOBS)  # type: ignore[attr-defined]
    token = current_tool_context.set(ctx)
    try:
        yield workspace, scratch, ctx, events
    finally:
        current_tool_context.reset(token)
        reset_runtime()


def _helper_script(scratch: Path, name: str, body: str) -> Path:
    script = scratch / name
    script.write_text(body, encoding="utf-8")
    return script


def _effect_events(events: list[dict]) -> list[dict]:
    return [event for event in events if event.get("name") == "effect_enforcement"]


def _effect_unavailable_events(events: list[dict]) -> list[dict]:
    return [
        event
        for event in events
        if event.get("name") == "effect_enforcement_unavailable"
    ]


@pytest.mark.asyncio
async def test_helper_script_escape_is_reverted_and_denied(effect_context, monkeypatch):
    """Reproduces the observed escape class: argv names no protected path."""

    workspace, scratch, ctx, events = effect_context
    monkeypatch.setenv(_EFFECT_ENV, "revert")
    script = _helper_script(
        scratch, "helper.sh", "printf 'hacked\\n' > replacer_test.go\n"
    )
    result = await exec_command(f"sh {script}", workdir=str(workspace))

    assert "[workspace write deny] exec_command modified write-protected file(s)" in result
    assert "replacer_test.go" in result
    assert "restored to their last committed content" in result
    assert (workspace / "replacer_test.go").read_text(encoding="utf-8") == "original\n"

    enforcement = _effect_events(events)
    assert len(enforcement) == 1
    event = enforcement[0]
    assert event["feature"] == "workspace_write_deny"
    assert event["operation"] == "effect_enforcement"
    assert event["mode"] == "revert"
    assert event["injected_to_model"] is True
    assert event["reverted_paths"] == ["replacer_test.go"]
    assert event["revert_failures"] == []
    assert "hint_text_sha256" in event
    # The ledger keeps recording the raw escape before enforcement rewrites it.
    ledger = [e for e in events if e.get("name") == "workspace_mutation_observed"]
    assert len(ledger) == 1
    assert ledger[0]["paths"][0]["relative_path"] == "replacer_test.go"
    assert ctx.workspace_mutation_records[-1]["operation"] == "effect_enforcement"


@pytest.mark.asyncio
async def test_warn_mode_keeps_change_but_prepends_notice(effect_context, monkeypatch):
    workspace, scratch, _ctx, events = effect_context
    monkeypatch.setenv(_EFFECT_ENV, "warn")
    script = _helper_script(
        scratch, "helper.sh", "printf 'hacked\\n' > replacer_test.go\n"
    )
    result = await exec_command(f"sh {script}", workdir=str(workspace))

    assert "[workspace write deny]" in result
    assert "revert them before finishing" in result
    assert (workspace / "replacer_test.go").read_text(encoding="utf-8") == "hacked\n"
    assert _effect_events(events)[0]["mode"] == "warn"
    assert _effect_events(events)[0]["reverted_paths"] == []


@pytest.mark.asyncio
async def test_effect_mode_off_by_default(effect_context):
    workspace, scratch, _ctx, events = effect_context
    script = _helper_script(
        scratch, "helper.sh", "printf 'hacked\\n' > replacer_test.go\n"
    )
    result = await exec_command(f"sh {script}", workdir=str(workspace))

    assert "[workspace write deny]" not in result
    assert (workspace / "replacer_test.go").read_text(encoding="utf-8") == "hacked\n"
    assert _effect_events(events) == []


@pytest.mark.parametrize(
    ("run_mode", "effect_mode", "expected_runs", "warning_expected"),
    [
        ("safe", "off", 1, False),
        ("safe", "warn", 1, True),
        ("safe", "revert", 0, False),
        ("full", "off", 1, False),
        ("full", "warn", 1, False),
        ("full", "revert", 1, False),
    ],
)
@pytest.mark.asyncio
async def test_git_unavailable_effect_mode_matrix_in_safe_and_full(
    effect_context,
    monkeypatch: pytest.MonkeyPatch,
    run_mode: str,
    effect_mode: str,
    expected_runs: int,
    warning_expected: bool,
) -> None:
    workspace, _scratch, ctx, events = effect_context
    ctx.run_mode = run_mode
    monkeypatch.setenv(_EFFECT_ENV, effect_mode)
    unavailable = WorkspaceMutationSnapshot(git_state=GitRunState.UNAVAILABLE)
    monkeypatch.setattr(
        shell,
        "snapshot_current_workspace_mutations",
        lambda: unavailable,
    )
    executions: list[str] = []

    async def fake_full_host(*_args: object, **_kwargs: object) -> str:
        executions.append("full")
        return "ran\n"

    async def fake_host(*_args: object, **kwargs: object) -> str:
        executions.append("safe")
        on_process_started = kwargs.get("on_process_started")
        if callable(on_process_started):
            on_process_started()
        return "exit_code=0\nran\n"

    monkeypatch.setattr(shell, "_run_full_host_shell_command", fake_full_host)
    monkeypatch.setattr(shell, "_run_host_shell_command", fake_host)

    result = await exec_command("printf ran", workdir=str(workspace))

    assert len(executions) == expected_runs
    assert ("[workspace write protection unavailable]" in result) is warning_expected
    if expected_runs == 0:
        payload = json.loads(result)
        assert payload["code"] == "WORKSPACE_WRITE_PROTECTION_UNAVAILABLE"
    assert len(_effect_unavailable_events(events)) == int(warning_expected)


@pytest.mark.asyncio
async def test_shell_preexecution_denial_does_not_claim_postexecution_warning(
    effect_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _scratch, ctx, events = effect_context
    ctx.run_mode = "safe"
    monkeypatch.setenv(_EFFECT_ENV, "warn")
    unavailable = WorkspaceMutationSnapshot(git_state=GitRunState.UNAVAILABLE)
    monkeypatch.setattr(
        shell,
        "snapshot_current_workspace_mutations",
        lambda: unavailable,
    )
    monkeypatch.setattr(
        shell,
        "_strict_runtime_unavailable_envelope",
        lambda *_args, **_kwargs: {
            "status": "error",
            "reason": "runtime_unavailable",
        },
    )

    async def unexpected_execution(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("pre-execution denial must not launch the command")

    monkeypatch.setattr(shell, "_run_host_shell_command", unexpected_execution)
    monkeypatch.setattr(shell, "_run_full_host_shell_command", unexpected_execution)

    result = await exec_command("printf never", workdir=str(workspace))

    assert json.loads(result)["reason"] == "runtime_unavailable"
    assert "[workspace write protection unavailable]" not in result
    assert _effect_unavailable_events(events) == []


@pytest.mark.asyncio
async def test_execute_code_preexecution_denial_does_not_claim_postexecution_warning(
    effect_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _scratch, ctx, events = effect_context
    ctx.run_mode = "safe"
    monkeypatch.setenv(_EFFECT_ENV, "warn")
    reset_runtime()
    unavailable = WorkspaceMutationSnapshot(git_state=GitRunState.UNAVAILABLE)
    monkeypatch.setattr(
        code_exec,
        "snapshot_current_workspace_mutations",
        lambda: unavailable,
    )

    denial = DenialResult(
        reason=DenialReason.POLICY_DENIED,
        suggested_next_step=SuggestedNextStep.REPLAN,
        level=SecurityLevel.STANDARD,
        action_fingerprint="pre-execution-denial",
        message="denied before execution",
        retryable=False,
    )

    async def deny_action(**_kwargs: object) -> tuple[object, object, object]:
        return denial, object(), object()

    async def unexpected_execution(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("pre-execution denial must not launch Python")

    monkeypatch.setattr(code_exec, "gate_action", deny_action)
    monkeypatch.setattr(code_exec, "create_owned_subprocess_exec", unexpected_execution)

    result = await execute_code("print('never')")

    assert json.loads(result)["reason"] == DenialReason.POLICY_DENIED.value
    assert "[workspace write protection unavailable]" not in result
    assert _effect_unavailable_events(events) == []


@pytest.mark.asyncio
async def test_execute_code_spawn_failure_does_not_claim_postexecution_warning(
    effect_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace, _scratch, ctx, events = effect_context
    ctx.run_mode = "safe"
    monkeypatch.setenv(_EFFECT_ENV, "warn")
    unavailable = WorkspaceMutationSnapshot(git_state=GitRunState.UNAVAILABLE)
    monkeypatch.setattr(
        code_exec,
        "snapshot_current_workspace_mutations",
        lambda: unavailable,
    )

    async def fail_before_spawn(*_args: object, **_kwargs: object) -> object:
        raise OSError("interpreter could not be started")

    monkeypatch.setattr(code_exec, "create_owned_subprocess_exec", fail_before_spawn)

    result = await execute_code("print('never started')")

    payload = json.loads(result)
    assert payload["exit_code"] == -1
    assert "interpreter could not be started" in payload["stderr"]
    assert "[workspace write protection unavailable]" not in result
    assert _effect_unavailable_events(events) == []


@pytest.mark.asyncio
async def test_shell_spawn_failure_does_not_claim_postexecution_warning(
    effect_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _scratch, ctx, events = effect_context
    ctx.run_mode = "safe"
    monkeypatch.setenv(_EFFECT_ENV, "warn")
    unavailable = WorkspaceMutationSnapshot(git_state=GitRunState.UNAVAILABLE)
    monkeypatch.setattr(
        shell,
        "snapshot_current_workspace_mutations",
        lambda: unavailable,
    )

    async def fail_before_spawn(*_args: object, **_kwargs: object) -> object:
        raise OSError("shell could not be started")

    monkeypatch.setattr(shell, "_create_host_shell_subprocess", fail_before_spawn)

    result = await exec_command("printf never", workdir=str(workspace))

    assert result == "[error] shell could not be started"
    assert "[workspace write protection unavailable]" not in result
    assert _effect_unavailable_events(events) == []


@pytest.mark.asyncio
async def test_untracked_protected_creation_is_unlinked(effect_context, monkeypatch):
    workspace, scratch, _ctx, events = effect_context
    monkeypatch.setenv(_EFFECT_ENV, "revert")
    script = _helper_script(
        scratch,
        "helper.sh",
        "mkdir -p tests && printf 'x\\n' > tests/test_new.py\n",
    )
    result = await exec_command(f"sh {script}", workdir=str(workspace))

    assert "[workspace write deny]" in result
    assert not (workspace / "tests" / "test_new.py").exists()
    assert _effect_events(events)[0]["reverted_paths"] == ["tests/test_new.py"]


@pytest.mark.asyncio
async def test_tracked_only_allows_untracked_creation_but_reverts_tracked_edit(
    effect_context, monkeypatch
):
    workspace, scratch, _ctx, events = effect_context
    monkeypatch.setenv(_EFFECT_ENV, "revert")
    monkeypatch.setenv(_TRACKED_ONLY_ENV, "on")
    script = _helper_script(
        scratch,
        "helper.sh",
        "mkdir -p tests && printf 'x\\n' > tests/test_new.py\n"
        "printf 'hacked\\n' > replacer_test.go\n",
    )
    result = await exec_command(f"sh {script}", workdir=str(workspace))

    assert "[workspace write deny]" in result
    # The agent-created file stays; the tracked protected file is restored.
    assert (workspace / "tests" / "test_new.py").read_text(encoding="utf-8") == "x\n"
    assert (workspace / "replacer_test.go").read_text(encoding="utf-8") == "original\n"
    assert _effect_events(events)[0]["reverted_paths"] == ["replacer_test.go"]


@pytest.mark.asyncio
async def test_tracked_only_spares_staged_new_file(effect_context, monkeypatch):
    """Staging an agent-created file must not turn its creation into a
    violation: under tracked-only the enforced set is the suite committed at
    HEAD, and the index is not HEAD."""

    workspace, scratch, _ctx, events = effect_context
    monkeypatch.setenv(_EFFECT_ENV, "revert")
    monkeypatch.setenv(_TRACKED_ONLY_ENV, "on")
    script = _helper_script(
        scratch,
        "helper.sh",
        "mkdir -p tests && printf 'x\\n' > tests/test_staged.py\n"
        "git add tests/test_staged.py\n",
    )
    result = await exec_command(f"sh {script}", workdir=str(workspace))

    assert "[workspace write deny]" not in result
    assert (workspace / "tests" / "test_staged.py").read_text(encoding="utf-8") == "x\n"
    assert _effect_events(events) == []


@pytest.mark.asyncio
async def test_tracked_only_still_reverts_head_tracked_edit_when_staged(
    effect_context, monkeypatch
):
    """Staging a mutation of a HEAD-tracked protected file must not dodge
    enforcement: the HEAD-presence skip only covers paths with no committed
    version."""

    workspace, scratch, _ctx, events = effect_context
    monkeypatch.setenv(_EFFECT_ENV, "revert")
    monkeypatch.setenv(_TRACKED_ONLY_ENV, "on")
    script = _helper_script(
        scratch,
        "helper.sh",
        "printf 'hacked\\n' > replacer_test.go\n"
        "git add replacer_test.go\n",
    )
    result = await exec_command(f"sh {script}", workdir=str(workspace))

    assert "[workspace write deny]" in result
    assert (workspace / "replacer_test.go").read_text(encoding="utf-8") == "original\n"
    assert _effect_events(events)[0]["reverted_paths"] == ["replacer_test.go"]


@pytest.mark.asyncio
async def test_staged_new_protected_file_falls_back_to_unstage_and_unlink(
    effect_context, monkeypatch
):
    workspace, scratch, _ctx, events = effect_context
    monkeypatch.setenv(_EFFECT_ENV, "revert")
    script = _helper_script(
        scratch,
        "helper.sh",
        "mkdir -p tests && printf 'x\\n' > tests/test_staged.py\n"
        "git add tests/test_staged.py\n",
    )
    result = await exec_command(f"sh {script}", workdir=str(workspace))

    assert "[workspace write deny]" in result
    assert not (workspace / "tests" / "test_staged.py").exists()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "test_staged" not in status.stdout
    assert _effect_events(events)[0]["reverted_paths"] == ["tests/test_staged.py"]


@pytest.mark.asyncio
async def test_restoring_a_protected_file_is_not_a_violation(effect_context, monkeypatch):
    workspace, _scratch, _ctx, events = effect_context
    monkeypatch.setenv(_EFFECT_ENV, "revert")
    # Pre-existing modification; the command RESTORES the file. The status
    # entry disappears between snapshots and must not be treated as a write.
    (workspace / "replacer_test.go").write_text("dirty\n", encoding="utf-8")
    result = await exec_command(
        "git checkout -- replacer_test.go", workdir=str(workspace)
    )

    assert "[workspace write deny]" not in result
    assert _effect_events(events) == []
    assert (workspace / "replacer_test.go").read_text(encoding="utf-8") == "original\n"


@pytest.mark.skipif(os.name == "nt", reason="test command requires POSIX ln -s and &&")
@pytest.mark.asyncio
async def test_symlink_creation_at_protected_path_reverted_with_guard(
    effect_context, monkeypatch, tmp_path: Path
):
    workspace, _scratch, _ctx, events = effect_context
    monkeypatch.setenv(_EFFECT_ENV, "revert")
    monkeypatch.setenv(_SYMLINK_GUARD_ENV, "on")
    outside = tmp_path / "outside.py"
    outside.write_text("x\n", encoding="utf-8")
    result = await exec_command(
        f"mkdir -p tests && ln -s {outside} tests/link_test.py",
        workdir=str(workspace),
    )

    assert "[workspace write deny]" in result
    assert not (workspace / "tests" / "link_test.py").is_symlink()
    assert _effect_events(events)[0]["reverted_paths"] == ["tests/link_test.py"]


@pytest.mark.asyncio
async def test_enforcement_runs_without_runtime_event_sink(effect_context, monkeypatch):
    """The ledger's on_runtime_event guard must not gate enforcement."""

    workspace, scratch, ctx, _events = effect_context
    monkeypatch.setenv(_EFFECT_ENV, "revert")
    ctx.on_runtime_event = None
    script = _helper_script(
        scratch, "helper.sh", "printf 'hacked\\n' > replacer_test.go\n"
    )
    result = await exec_command(f"sh {script}", workdir=str(workspace))

    assert "[workspace write deny]" in result
    assert (workspace / "replacer_test.go").read_text(encoding="utf-8") == "original\n"
    assert ctx.workspace_mutation_records[-1]["operation"] == "effect_enforcement"


@pytest.mark.asyncio
async def test_guidance_env_text_lands_in_effect_message(effect_context, monkeypatch):
    workspace, scratch, _ctx, _events = effect_context
    monkeypatch.setenv(_EFFECT_ENV, "revert")
    monkeypatch.setenv(_GUIDANCE_ENV, "Test files are graded externally.")
    script = _helper_script(
        scratch, "helper.sh", "printf 'hacked\\n' > replacer_test.go\n"
    )
    result = await exec_command(f"sh {script}", workdir=str(workspace))

    assert "Test files are graded externally." in result


@pytest.mark.asyncio
async def test_execute_code_effect_enforcement(effect_context, monkeypatch):
    workspace, _scratch, _ctx, events = effect_context
    monkeypatch.setenv(_EFFECT_ENV, "revert")
    result = await execute_code(
        "from pathlib import Path\n"
        "Path('replacer_test.go').write_text('hacked\\n')\n",
        timeout=5,
    )

    assert result.startswith("[workspace write deny] execute_code")
    assert (workspace / "replacer_test.go").read_text(encoding="utf-8") == "original\n"
    assert _effect_events(events)[0]["tool"] == "execute_code"


def test_root_level_test_file_matches_arm_globs(effect_context):
    """Regression lock-in: root-level *_test.* is covered by the arm globs."""

    workspace, _scratch, ctx, _events = effect_context
    match = write_policy.match_workspace_write_deny(
        workspace / "replacer_test.go",
        original_path="replacer_test.go",
        workspace=workspace,
        ctx=ctx,
    )
    assert match is not None
    assert match.pattern == "**/*_test.*"


def test_tracked_only_matcher_skips_untracked_paths(effect_context, monkeypatch):
    workspace, _scratch, ctx, _events = effect_context
    (workspace / "tests").mkdir(exist_ok=True)
    (workspace / "tests" / "test_new.py").write_text("x\n", encoding="utf-8")

    def match(path: str):
        return write_policy.match_workspace_write_deny(
            workspace / path, original_path=path, workspace=workspace, ctx=ctx
        )

    # Lever off: both tracked and untracked protected paths deny.
    assert match("replacer_test.go") is not None
    assert match("tests/test_new.py") is not None
    monkeypatch.setenv(_TRACKED_ONLY_ENV, "on")
    assert match("replacer_test.go") is not None
    assert match("tests/test_new.py") is None
    # Never-created paths are untracked too: creation is allowed.
    assert match("tests/test_missing.py") is None


def test_tracked_only_fails_closed_outside_git(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "plain"
    workspace.mkdir()
    (workspace / "sample_test.py").write_text("x\n", encoding="utf-8")
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        workspace_dir=str(workspace),
        session_key="agent:main:test",
    )
    ctx.workspace_write_deny_globs = ["**/*_test.*"]  # type: ignore[attr-defined]
    monkeypatch.setenv(_TRACKED_ONLY_ENV, "on")
    match = write_policy.match_workspace_write_deny(
        workspace / "sample_test.py",
        original_path="sample_test.py",
        workspace=workspace,
        ctx=ctx,
    )
    # git cannot answer authoritatively here, so the deny stands.
    assert match is not None


def test_symlink_guard_matches_lexical_spelling(effect_context, monkeypatch, tmp_path):
    workspace, _scratch, ctx, _events = effect_context
    (workspace / "tests").mkdir(exist_ok=True)
    outside = tmp_path / "outside.py"
    outside.write_text("x\n", encoding="utf-8")
    (workspace / "tests" / "link_test.py").symlink_to(outside)

    def match():
        return write_policy.match_workspace_write_deny(
            workspace / "tests" / "link_test.py",
            original_path="tests/link_test.py",
            workspace=workspace,
            ctx=ctx,
        )

    # Guard off: resolution escapes the workspace and matching is skipped
    # (the historical gap, kept for parity when the lever is off).
    assert match() is None
    monkeypatch.setenv(_SYMLINK_GUARD_ENV, "on")
    guarded = match()
    assert guarded is not None
    assert guarded.path == "tests/link_test.py"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("ln -s /tmp/x tests/test_a.py", ["tests/test_a.py"]),
        ("ln /tmp/x tests/test_a.py", ["tests/test_a.py"]),
        ("ln -sf /tmp/x /tmp/y tests", ["tests"]),
        ("ln -t tests /tmp/x", ["tests"]),
        ("ln --target-directory=tests /tmp/x", ["tests"]),
        # Single-operand ln creates ./<basename> in the cwd.
        ("ln -s ../tests/test_a.py", ["test_a.py"]),
        ("link /tmp/x tests/test_a.py", ["tests/test_a.py"]),
        # Wrapper unwrap: the inner command string is screened too.
        ("sh -c 'rm tests/test_a.py'", ["tests/test_a.py"]),
        ("bash -lc 'sed -i s/a/b/ tests/test_a.py'", ["tests/test_a.py"]),
        ("busybox sh -c 'rm tests/test_a.py'", ["tests/test_a.py"]),
        (
            "sh -c \"sh -c 'sh -c \\\"rm tests/test_a.py\\\"'\"",
            ["tests/test_a.py"],
        ),
        # Depth cap: four wrapper levels stay out of scope.
        (
            'sh -c "sh -c \'sh -c \\"sh -c \\\\\\"rm tests/test_a.py\\\\\\"\\"\'"',
            [],
        ),
        ("sh script.sh", []),
    ],
)
def test_ln_and_wrapper_write_target_extraction(
    monkeypatch, command: str, expected: list[str]
) -> None:
    # The ln/link extractors and the wrapper unwrap ride the effect lever.
    monkeypatch.setenv(_EFFECT_ENV, "warn")
    assert shell._mutating_command_write_targets(command) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # Hardened-only surfaces are inert while the effect lever is unset,
        # keeping the pre-lever screen byte-for-byte (ARM-L12 guarantee).
        ("ln -s /tmp/x tests/test_a.py", []),
        ("link /tmp/x tests/test_a.py", []),
        ("sh -c 'rm tests/test_a.py'", []),
        ("busybox sh -c 'rm tests/test_a.py'", []),
        # Pre-lever extractors keep working without the lever.
        ("rm tests/test_a.py", ["tests/test_a.py"]),
        ("sed -i s/a/b/ tests/test_a.py", ["tests/test_a.py"]),
    ],
)
def test_hardened_extraction_inert_without_effect_lever(
    command: str, expected: list[str]
) -> None:
    assert shell._mutating_command_write_targets(command) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            'bun -e \'require("fs").writeFileSync("tests/test_a.js", "x")\'',
            ["tests/test_a.js"],
        ),
        (
            'deno eval \'Deno.writeTextFileSync("tests/test_b.ts", "x")\'',
            ["tests/test_b.ts"],
        ),
        ('lua -e \'io.open("tests/test_c.lua", "w")\'', ["tests/test_c.lua"]),
        (
            'sh -c "python3 -c \\"open(\'tests/test_d.py\', \'w\')\\""',
            ["tests/test_d.py"],
        ),
        ("deno run script.ts", []),
        ('deno eval \'console.log("read only")\'', []),
    ],
)
def test_interpreter_extension_write_target_extraction(
    monkeypatch, command: str, expected: list[str]
) -> None:
    # deno eval, the bun/lua code flags, and the wrapper unwrap ride the
    # effect lever.
    monkeypatch.setenv(_EFFECT_ENV, "warn")
    assert shell._interpreter_write_targets_from_command(command) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # Hardened-only interpreter surfaces are inert with the lever unset.
        (
            'bun -e \'require("fs").writeFileSync("tests/test_a.js", "x")\'',
            [],
        ),
        ('deno eval \'Deno.writeTextFileSync("tests/test_b.ts", "x")\'', []),
        ('lua -e \'io.open("tests/test_c.lua", "w")\'', []),
        (
            'sh -c "python3 -c \\"open(\'tests/test_d.py\', \'w\')\\""',
            [],
        ),
        # Pre-lever interpreter flags keep working without the lever.
        (
            "python3 -c \"open('tests/test_d.py', 'w')\"",
            ["tests/test_d.py"],
        ),
    ],
)
def test_interpreter_extension_inert_without_effect_lever(
    command: str, expected: list[str]
) -> None:
    assert shell._interpreter_write_targets_from_command(command) == expected


@pytest.mark.parametrize(
    ("env_name", "value"),
    [
        (_EFFECT_ENV, "revert"),
        (_EFFECT_ENV, "warn"),
        (_EFFECT_ENV, "off"),
        (_EFFECT_ENV, ""),
        (_TRACKED_ONLY_ENV, "on"),
        (_TRACKED_ONLY_ENV, "off"),
        (_SYMLINK_GUARD_ENV, "1"),
        (_HOST_SHELL_ENV, "enabled"),
        (_COMMAND_TARGETS_ENV, "no"),
        (_INTERPRETER_TARGETS_ENV, "false"),
    ],
)
def test_bootstrap_validation_accepts_recognized_values(
    monkeypatch, env_name: str, value: str
) -> None:
    monkeypatch.setenv(env_name, value)
    write_policy.validate_workspace_write_deny_env()


@pytest.mark.parametrize(
    ("env_name", "value"),
    [
        (_EFFECT_ENV, "bogus"),
        (_EFFECT_ENV, "reverts"),
        (_TRACKED_ONLY_ENV, "bogus"),
        (_SYMLINK_GUARD_ENV, "maybe"),
        (_HOST_SHELL_ENV, "yess"),
    ],
)
def test_bootstrap_validation_raises_on_unrecognized_values(
    monkeypatch, env_name: str, value: str
) -> None:
    monkeypatch.setenv(env_name, value)
    with pytest.raises(ValueError, match=env_name):
        write_policy.validate_workspace_write_deny_env()


def test_dispatch_reads_fail_safe_to_off(monkeypatch) -> None:
    monkeypatch.setenv(_EFFECT_ENV, "bogus")
    assert write_policy.workspace_write_deny_effect_mode() == "off"
    monkeypatch.setenv(_TRACKED_ONLY_ENV, "bogus")
    assert write_policy.workspace_write_deny_tracked_only() is False
