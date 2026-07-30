"""Opt-in workspace write deny levers: host shell, mutator targets, guidance.

Covers OPENSQUILLA_WORKSPACE_WRITE_DENY_HOST_SHELL,
OPENSQUILLA_WORKSPACE_WRITE_DENY_COMMAND_TARGETS and
OPENSQUILLA_WORKSPACE_WRITE_DENY_GUIDANCE (all off by default). Motivation:
deny globs are enforced for filesystem and patch tools, but shell-side
enforcement only recognizes redirection/tee write targets and is skipped
entirely under host execution, and the default block message suggests
recreating the file under the scratch directory — the wrong remediation when
deny globs protect files that must not be modified at all (e.g. test files).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from opensquilla.gateway.approval_queue import reset_approval_queue
from opensquilla.sandbox.integration import reset_runtime
from opensquilla.tools import write_policy
from opensquilla.tools.builtin import shell
from opensquilla.tools.types import (
    CallerKind,
    InteractionMode,
    SafeToolError,
    ToolContext,
    current_tool_context,
)

_HOST_SHELL_ENV = "OPENSQUILLA_WORKSPACE_WRITE_DENY_HOST_SHELL"
_COMMAND_TARGETS_ENV = "OPENSQUILLA_WORKSPACE_WRITE_DENY_COMMAND_TARGETS"
_INTERPRETER_TARGETS_ENV = "OPENSQUILLA_WORKSPACE_WRITE_DENY_INTERPRETER_TARGETS"
_GUIDANCE_ENV = "OPENSQUILLA_WORKSPACE_WRITE_DENY_GUIDANCE"


@pytest.fixture(autouse=True)
def _tool_context(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(_HOST_SHELL_ENV, raising=False)
    monkeypatch.delenv(_COMMAND_TARGETS_ENV, raising=False)
    monkeypatch.delenv(_INTERPRETER_TARGETS_ENV, raising=False)
    monkeypatch.delenv(_GUIDANCE_ENV, raising=False)
    reset_approval_queue()
    reset_runtime()
    token = current_tool_context.set(
        ToolContext(is_owner=True, caller_kind=CallerKind.CLI, session_key="agent:main:test")
    )
    yield
    current_tool_context.reset(token)
    reset_approval_queue()
    reset_runtime()


def _configure_ctx(workspace: Path, globs: list[str]) -> ToolContext:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.workspace_write_deny_globs = globs  # type: ignore[attr-defined]
    return ctx


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("sed -i 's/a/b/' tests/test_a.py", ["tests/test_a.py"]),
        ("sed -i.bak -e 's/a/b/' tests/test_a.py", ["tests/test_a.py"]),
        (
            "sed -ri 's/a/b/' tests/test_a.py tests/test_b.py",
            ["tests/test_a.py", "tests/test_b.py"],
        ),
        ("sed --in-place=.bak 's/a/b/' tests/test_a.py", ["tests/test_a.py"]),
        # BSD sed: the empty token is -i's backup suffix, not the script.
        ("sed -i '' 's/a/b/' tests/test_a.py", ["tests/test_a.py"]),
        # Script delimiters and separators are not shell operators.
        ("sed -i 's|a|b|' tests/test_a.py", ["tests/test_a.py"]),
        ("sed -i 's/a/b/; s/c/d/' tests/test_a.py", ["tests/test_a.py"]),
        # No in-place flag: read-only sed must not produce write targets.
        ("sed -n '1p' tests/test_a.py", []),
        ("perl -pi -e 's/a/b/' tests/test_a.py", ["tests/test_a.py"]),
        ("perl -i.bak -pe 's/a/b/' tests/test_a.py", ["tests/test_a.py"]),
        ("perl -e 'print 1' tests/test_a.py", []),
        # -I consumes the rest of the token; its 'i' is not the in-place switch.
        ("perl -Ilib -e 'print 1' tests/test_a.py", []),
        ("rm -f tests/test_a.py", ["tests/test_a.py"]),
        # Directory operands are extracted; the gate matches them as dir/**.
        ("rm -rf tests", ["tests"]),
        ("unlink tests/test_a.py", ["tests/test_a.py"]),
        # mv mutates both operands: the source is removed, the dest written.
        ("mv tests/test_a.py /tmp/aside.py", ["tests/test_a.py", "/tmp/aside.py"]),
        ("cp /tmp/patched.py tests/test_a.py", ["tests/test_a.py"]),
        ("cp -t tests src_a.py", ["tests"]),
        ("dd if=/dev/zero of=tests/test_a.py bs=1", ["tests/test_a.py"]),
        ("truncate -s 0 tests/test_a.py", ["tests/test_a.py"]),
        ("git rm tests/test_a.py", ["tests/test_a.py"]),
        # git rm --cached only unstages; the worktree file is untouched.
        ("git rm --cached tests/test_a.py", []),
        ("git -C sub mv tests/test_a.py aside.py", ["tests/test_a.py", "aside.py"]),
        # git checkout/restore recover original content; not treated as writes.
        ("git checkout -- tests/test_a.py", []),
        ("LC_ALL=C sed -i 's/a/b/' tests/test_a.py", ["tests/test_a.py"]),
        (
            "cat tests/test_a.py | grep x && sed -i 's/a/b/' tests/test_b.py",
            ["tests/test_b.py"],
        ),
        # Operators inside quotes are data, not command separators.
        ("git commit -m 'cleanup; rm tests/test_a.py'", []),
        # Heredoc bodies are data, not commands.
        ("cat <<'EOF' > /tmp/notes.txt\nrm tests/test_a.py\nEOF", []),
        ("pytest tests/test_a.py", []),
        ("cat tests/test_a.py", []),
    ],
)
def test_mutating_command_write_target_extraction(command: str, expected: list[str]) -> None:
    assert shell._mutating_command_write_targets(command) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # django-11885's verbatim bypass shape: python -c open().write.
        (
            "python3 -c \"open('tests/test_a.py','w').write('x')\"",
            ["tests/test_a.py"],
        ),
        (
            "python -c \"open('tests/test_a.py', 'a').write('x')\"",
            ["tests/test_a.py"],
        ),
        (
            "python3 -c \"open('tests/test_a.py', mode='w').write('x')\"",
            ["tests/test_a.py"],
        ),
        # Read-only opens must not match: no mode / explicit 'r' / 'rb'.
        ("python3 -c \"print(open('tests/test_a.py').read())\"", []),
        ("python3 -c \"print(open('tests/test_a.py','r').read())\"", []),
        ("python3 -c \"print(open('tests/test_a.py','rb').read())\"", []),
        (
            "python3 -c \"from pathlib import Path; Path('tests/test_a.py').write_text('x')\"",
            ["tests/test_a.py"],
        ),
        (
            "python3 -c \"import pathlib; pathlib.Path('tests/test_a.py').unlink()\"",
            ["tests/test_a.py"],
        ),
        ("python3 -c \"print(Path('tests/test_a.py').read_text())\"", []),
        (
            "python3 -c \"import os; os.remove('tests/test_a.py')\"",
            ["tests/test_a.py"],
        ),
        (
            "python3 -c \"import shutil; shutil.rmtree('tests')\"",
            ["tests"],
        ),
        (
            'node -e "require(\'fs\').writeFileSync(\'tests/test_a.py\', \'x\')"',
            ["tests/test_a.py"],
        ),
        (
            'node --eval "fs.appendFileSync(\'tests/test_a.py\', \'x\')"',
            ["tests/test_a.py"],
        ),
        ('node -e "console.log(fs.readFileSync(\'tests/test_a.py\'))"', []),
        (
            'ruby -e "File.write(\'tests/test_a.py\', \'x\')"',
            ["tests/test_a.py"],
        ),
        (
            'ruby -e "FileUtils.rm_rf(\'tests\')"',
            ["tests"],
        ),
        ('ruby -e "puts File.read(\'tests/test_a.py\')"', []),
        (
            "php -r \"file_put_contents('tests/test_a.py', 'x');\"",
            ["tests/test_a.py"],
        ),
        ("php -r \"echo file_get_contents('tests/test_a.py');\"", []),
        (
            "perl -e \"open(FH, '>tests/test_a.py'); print FH 'x';\"",
            ["tests/test_a.py"],
        ),
        (
            "perl -e \"open(my \\$fh, '>', 'tests/test_a.py');\"",
            ["tests/test_a.py"],
        ),
        ("perl -e \"open(FH, '<tests/test_a.py');\"", []),
        # Fused short flag and env-assignment prefixes.
        (
            "PYTHONPATH=. python3 -c\"open('tests/test_a.py','w').write('x')\"",
            ["tests/test_a.py"],
        ),
        # Versioned interpreter names resolve to their base extractor.
        (
            "python3.11 -c \"open('tests/test_a.py','w').write('x')\"",
            ["tests/test_a.py"],
        ),
        # Non-interpreter heads and plain scripts are out of scope.
        ("python3 setup.py build", []),
        ("gcc -c main.c", []),
        # Later pipeline segments are scanned too.
        (
            "echo x | python3 -c \"open('tests/test_a.py','w').write('x')\"",
            ["tests/test_a.py"],
        ),
    ],
)
def test_interpreter_write_target_extraction(command: str, expected: list[str]) -> None:
    assert shell._interpreter_write_targets_from_command(command) == expected


def test_interpreter_stdin_program_is_scanned_as_code() -> None:
    targets = shell._interpreter_write_targets_from_inputs(
        "python3 -",
        stdin="open('tests/test_a.py', 'w').write('x')\n",
    )
    assert targets == ["tests/test_a.py"]

    # With -c code present, stdin is data for the program, not more code —
    # but shell-command scanning of stdin still applies for sh-like heads.
    targets = shell._interpreter_write_targets_from_inputs(
        "python3 -c \"print('ok')\"",
        stdin="open('tests/test_a.py', 'w').write('x')\n",
    )
    assert targets == []


def test_interpreter_stdin_shell_commands_still_scanned() -> None:
    targets = shell._interpreter_write_targets_from_inputs(
        "sh",
        stdin="python3 -c \"open('tests/test_a.py','w').write('x')\"\n",
    )
    assert targets == ["tests/test_a.py"]


def test_interpreter_targets_lever_default_off_skips_extraction(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)
    _configure_ctx(workspace, ["tests/**"])

    block = shell._workspace_write_deny_shell_block(
        "exec_command",
        "python3 -c \"open('tests/test_a.py','w').write('x')\"",
        str(workspace),
    )

    assert block is None


def test_interpreter_targets_lever_adds_interpreter_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_INTERPRETER_TARGETS_ENV, "1")
    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)
    _configure_ctx(workspace, ["tests/**"])

    block = shell._workspace_write_deny_shell_block(
        "exec_command",
        "python3 -c \"open('tests/test_a.py','w').write('x')\"",
        str(workspace),
    )

    assert block is not None
    assert block["reason"] == "workspace_write_deny"
    assert block["matched_pattern"] == "tests/**"
    assert block["target"] == "tests/test_a.py"


def test_interpreter_targets_lever_blocks_directory_operands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_INTERPRETER_TARGETS_ENV, "1")
    workspace = tmp_path / "workspace"
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_a.py").write_text("assert a\n", encoding="utf-8")
    _configure_ctx(workspace, ["tests/**"])

    block = shell._workspace_write_deny_shell_block(
        "exec_command",
        "python3 -c \"import shutil; shutil.rmtree('tests')\"",
        str(workspace),
    )

    assert block is not None
    assert block["reason"] == "workspace_write_deny"
    assert block["matched_pattern"] == "tests/**"
    assert block["target"] == "tests"


@pytest.mark.asyncio
async def test_exec_command_blocks_interpreter_write_when_lever_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_INTERPRETER_TARGETS_ENV, "1")
    workspace = tmp_path / "workspace"
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    target = tests_dir / "test_a.py"
    target.write_text("assert a\n", encoding="utf-8")
    _configure_ctx(workspace, ["tests/**"])

    result = await shell.exec_command(
        "python3 -c \"open('tests/test_a.py','w').write('assert b')\"",
        workdir=str(workspace),
    )

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workspace_write_deny"
    assert payload["matched_pattern"] == "tests/**"
    assert target.read_text(encoding="utf-8") == "assert a\n"


@pytest.mark.asyncio
async def test_exec_command_interpreter_write_passes_through_by_default(
    tmp_path: Path,
) -> None:
    # Documents the default gap the lever closes: interpreter one-liner
    # writes are not recognized and execute against denied paths.
    workspace = tmp_path / "workspace"
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    target = tests_dir / "test_a.py"
    target.write_text("assert a\n", encoding="utf-8")
    _configure_ctx(workspace, ["tests/**"])

    result = await shell.exec_command(
        "python3 -c \"open('tests/test_a.py','w').write('assert b')\"",
        workdir=str(workspace),
    )

    assert result.startswith("exit_code=0")
    assert target.read_text(encoding="utf-8") == "assert b"


@pytest.mark.asyncio
async def test_exec_command_interpreter_reads_stay_unblocked_with_lever_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_INTERPRETER_TARGETS_ENV, "1")
    workspace = tmp_path / "workspace"
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_a.py").write_text("assert a\n", encoding="utf-8")
    _configure_ctx(workspace, ["tests/**"])

    result = await shell.exec_command(
        "python3 -c \"print(open('tests/test_a.py').read())\"",
        workdir=str(workspace),
    )

    assert result.startswith("exit_code=0")
    assert "assert a" in result


@pytest.mark.asyncio
async def test_exec_command_scans_stdin_program_when_interpreter_lever_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_INTERPRETER_TARGETS_ENV, "1")
    workspace = tmp_path / "workspace"
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_a.py").write_text("assert a\n", encoding="utf-8")
    _configure_ctx(workspace, ["tests/**"])

    result = await shell.exec_command(
        "python3 -",
        workdir=str(workspace),
        stdin="open('tests/test_a.py', 'w').write('x')\n",
    )

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workspace_write_deny"


def test_command_targets_lever_default_off_skips_mutator_extraction(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)
    _configure_ctx(workspace, ["tests/**"])

    block = shell._workspace_write_deny_shell_block(
        "exec_command", "sed -i 's/a/b/' tests/test_a.py", str(workspace)
    )

    assert block is None


def test_command_targets_lever_adds_mutator_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_COMMAND_TARGETS_ENV, "1")
    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)
    _configure_ctx(workspace, ["tests/**"])

    block = shell._workspace_write_deny_shell_block(
        "exec_command", "sed -i 's/a/b/' tests/test_a.py", str(workspace)
    )

    assert block is not None
    assert block["reason"] == "workspace_write_deny"
    assert block["matched_pattern"] == "tests/**"
    assert block["target"] == "tests/test_a.py"


def test_command_targets_lever_blocks_directory_operands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_COMMAND_TARGETS_ENV, "1")
    workspace = tmp_path / "workspace"
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_a.py").write_text("assert a\n", encoding="utf-8")
    _configure_ctx(workspace, ["tests/**"])

    for command in ("rm -rf tests", "cp -t tests src_a.py"):
        block = shell._workspace_write_deny_shell_block(
            "exec_command", command, str(workspace)
        )
        assert block is not None, command
        assert block["reason"] == "workspace_write_deny"
        assert block["matched_pattern"] == "tests/**"
        assert block["target"] == "tests"


def test_command_targets_quoted_operators_and_heredocs_stay_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_COMMAND_TARGETS_ENV, "1")
    workspace = tmp_path / "workspace"
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_a.py").write_text("assert a\n", encoding="utf-8")
    _configure_ctx(workspace, ["tests/**"])

    for command in (
        "git commit -m 'cleanup; rm tests/test_a.py'",
        "cat <<'EOF' > notes.txt\nrm tests/test_a.py\nEOF",
    ):
        block = shell._workspace_write_deny_shell_block(
            "exec_command", command, str(workspace)
        )
        assert block is None, command


def test_match_workspace_write_deny_directory_form_is_opt_in(
    tmp_path: Path,
) -> None:
    # The trailing-slash candidates only apply on request; existing callers
    # (file tools, patch gate) keep byte-identical matching for bare dirs.
    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)
    _configure_ctx(workspace, ["tests/**"])
    target = workspace / "tests"

    assert write_policy.match_workspace_write_deny(target, workspace=workspace) is None

    match = write_policy.match_workspace_write_deny(
        target, workspace=workspace, as_directory=True
    )
    assert match is not None
    assert match.pattern == "tests/**"


@pytest.mark.asyncio
async def test_exec_command_blocks_mutator_on_denied_path_when_lever_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_COMMAND_TARGETS_ENV, "1")
    workspace = tmp_path / "workspace"
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    target = tests_dir / "test_a.py"
    target.write_text("assert a\n", encoding="utf-8")
    _configure_ctx(workspace, ["tests/**"])

    result = await shell.exec_command(
        "sed -i 's/a/b/' tests/test_a.py", workdir=str(workspace)
    )

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workspace_write_deny"
    assert payload["matched_pattern"] == "tests/**"
    assert target.read_text(encoding="utf-8") == "assert a\n"


@pytest.mark.asyncio
async def test_background_process_blocks_mutator_on_denied_path_when_lever_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_COMMAND_TARGETS_ENV, "1")
    workspace = tmp_path / "workspace"
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    target = tests_dir / "test_a.py"
    target.write_text("assert a\n", encoding="utf-8")
    _configure_ctx(workspace, ["tests/**"])

    result = await shell.background_process(
        "sed -i 's/a/b/' tests/test_a.py", workdir=str(workspace)
    )

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workspace_write_deny"
    assert payload["tool"] == "background_process"
    assert target.read_text(encoding="utf-8") == "assert a\n"


@pytest.mark.asyncio
async def test_exec_command_scans_stdin_for_mutators_when_lever_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_COMMAND_TARGETS_ENV, "1")
    workspace = tmp_path / "workspace"
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_a.py").write_text("assert a\n", encoding="utf-8")
    _configure_ctx(workspace, ["tests/**"])

    result = await shell.exec_command(
        "sh",
        workdir=str(workspace),
        stdin="sed -i 's/a/b/' tests/test_a.py\n",
    )

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workspace_write_deny"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="test command requires POSIX sed")
async def test_exec_command_mutator_passes_through_by_default(
    tmp_path: Path,
) -> None:
    # Documents the default gap the lever closes: without the lever, in-place
    # mutators are not recognized as writes and execute against denied paths.
    workspace = tmp_path / "workspace"
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    target = tests_dir / "test_a.py"
    target.write_text("assert a\n", encoding="utf-8")
    _configure_ctx(workspace, ["tests/**"])

    result = await shell.exec_command(
        "sed -i 's/assert a/assert b/' tests/test_a.py", workdir=str(workspace)
    )

    assert result.startswith("exit_code=0")
    assert target.read_text(encoding="utf-8") == "assert b\n"


@pytest.mark.asyncio
async def test_exec_command_reads_stay_unblocked_with_lever_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_COMMAND_TARGETS_ENV, "1")
    workspace = tmp_path / "workspace"
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_a.py").write_bytes(b"assert a\n")
    _configure_ctx(workspace, ["tests/**"])

    result = await shell.exec_command("cat tests/test_a.py", workdir=str(workspace))

    assert result.replace("\r\n", "\n") == "exit_code=0\nassert a\n"


@pytest.mark.asyncio
async def test_host_shell_lever_enforces_deny_globs_under_host_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_HOST_SHELL_ENV, "1")
    monkeypatch.setattr(shell, "_host_execution_allowed", lambda: True)
    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)
    _configure_ctx(workspace, ["tests/**"])

    result = await shell.exec_command(
        "echo ok > tests/out.txt", workdir=str(workspace)
    )

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workspace_write_deny"
    assert not (workspace / "tests" / "out.txt").exists()


@pytest.mark.asyncio
async def test_host_shell_default_off_keeps_host_execution_unchecked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Documents the default gap the lever closes: under host execution the
    # whole sandbox policy block — including deny globs — is skipped.
    monkeypatch.setattr(shell, "_host_execution_allowed", lambda: True)
    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)
    _configure_ctx(workspace, ["tests/**"])

    result = await shell.exec_command(
        "echo ok > tests/out.txt", workdir=str(workspace)
    )

    assert result.startswith("exit_code=0")
    assert (workspace / "tests" / "out.txt").exists()


@pytest.mark.asyncio
async def test_host_shell_and_command_targets_levers_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_HOST_SHELL_ENV, "1")
    monkeypatch.setenv(_COMMAND_TARGETS_ENV, "1")
    monkeypatch.setattr(shell, "_host_execution_allowed", lambda: True)
    workspace = tmp_path / "workspace"
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    target = tests_dir / "test_a.py"
    target.write_text("assert a\n", encoding="utf-8")
    _configure_ctx(workspace, ["tests/**"])

    result = await shell.exec_command(
        "sed -i 's/a/b/' tests/test_a.py", workdir=str(workspace)
    )

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workspace_write_deny"
    assert target.read_text(encoding="utf-8") == "assert a\n"


def test_deny_guidance_env_overrides_scratch_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guidance = "Protected files must not be modified; change the source instead."
    monkeypatch.setenv(_GUIDANCE_ENV, guidance)
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    ctx = _configure_ctx(workspace, ["tests/**"])
    ctx.scratch_dir = str(scratch)  # type: ignore[attr-defined]

    match = write_policy.match_workspace_write_deny(
        workspace / "tests" / "test_a.py", workspace=workspace
    )
    assert match is not None
    block = write_policy.workspace_write_deny_block("write_file", match)

    message = str(block["message"])
    assert message.endswith(guidance)
    assert "scratch directory" not in message


def test_deny_guidance_default_keeps_scratch_guidance(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    ctx = _configure_ctx(workspace, ["tests/**"])
    ctx.scratch_dir = str(scratch)  # type: ignore[attr-defined]

    match = write_policy.match_workspace_write_deny(
        workspace / "tests" / "test_a.py", workspace=workspace
    )
    assert match is not None
    block = write_policy.workspace_write_deny_block("write_file", match)

    message = str(block["message"])
    assert "must be written under the configured scratch directory" in message
    assert str(scratch) in message


def test_gate_raise_carries_same_message_as_block_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guidance = "Fix the source code so the existing checks pass."
    monkeypatch.setenv(_GUIDANCE_ENV, guidance)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _configure_ctx(workspace, ["tests/**"])
    target = workspace / "tests" / "test_a.py"

    match = write_policy.match_workspace_write_deny(target, workspace=workspace)
    assert match is not None
    expected = str(write_policy.workspace_write_deny_block("write_file", match)["message"])

    with pytest.raises(SafeToolError) as excinfo:
        write_policy.gate_workspace_write_deny("write_file", target, workspace=workspace)

    assert expected in str(excinfo.value)
    assert guidance in str(excinfo.value)


def test_lever_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (_HOST_SHELL_ENV, _COMMAND_TARGETS_ENV, _INTERPRETER_TARGETS_ENV):
        monkeypatch.delenv(name, raising=False)
        assert shell._write_deny_lever_enabled(name) is False
        monkeypatch.setenv(name, "1")
        assert shell._write_deny_lever_enabled(name) is True
        monkeypatch.setenv(name, "true")
        assert shell._write_deny_lever_enabled(name) is True
        monkeypatch.setenv(name, "0")
        assert shell._write_deny_lever_enabled(name) is False
        monkeypatch.setenv(name, "off")
        assert shell._write_deny_lever_enabled(name) is False
