from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import (
    active_file_system_profile,
    active_sandbox_policy,
    configure_runtime,
    reset_runtime,
)
from opensquilla.sandbox.operation_profile import OperationProfile
from opensquilla.sandbox.operation_runtime import SandboxOperationResult
from opensquilla.sandbox.path_validation import decide_path_access
from opensquilla.sandbox.permissions import (
    FileSystemAccess,
    FileSystemPermissionEntry,
    FileSystemPermissionProfile,
)
from opensquilla.sandbox.policy_models import FilePolicySettings, SandboxPolicy
from opensquilla.sandbox.run_context import MountGrant, RunContext
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.tools.builtin import code_exec, filesystem, git, shell
from opensquilla.tools.types import ToolContext, current_tool_context


class _AllowedGate:
    allowed = True
    approval_id = "approved"


class _PendingGate:
    allowed = False
    approval_id = "pending"

    @staticmethod
    def to_envelope() -> dict[str, object]:
        return {"status": "approval_required", "approval_id": "pending"}


class _DeniedGate:
    allowed = False
    approval_id = "pending"
    status = "approval_denied"

    @staticmethod
    def to_envelope() -> dict[str, object]:
        return {"status": "approval_denied", "approval_id": "pending"}


@pytest.mark.parametrize(
    "command",
    (
        "rm discard.txt",
        "sh -lc 'rm discard.txt'",
        "powershell -Command \"Remove-Item -Force 'discard.txt'\"",
        'cmd /c "del /F discard.txt"',
        "command rm discard.txt",
        "env OPENSQUILLA_TEST=1 rm discard.txt",
        "sudo rm discard.txt",
        "ri -Force discard.txt",
    ),
)
def test_literal_file_delete_parser_covers_direct_shell_wrappers(
    tmp_path: Path,
    command: str,
) -> None:
    target = tmp_path / "discard.txt"
    target.write_text("discard", encoding="utf-8")

    parsed = shell._delete_target(command, str(tmp_path))

    assert parsed == (target.resolve(), False)


@pytest.mark.parametrize(
    "command",
    (
        'cmd /c "del /F discard.txt"',
        "powershell -Command \"Remove-Item -Force 'discard.txt'\"",
    ),
)
def test_literal_delete_parser_uses_native_windows_tokenization(
    tmp_path: Path,
    command: str,
) -> None:
    target = tmp_path / "discard.txt"
    target.write_text("discard", encoding="utf-8")

    assert shell._delete_target(command, str(tmp_path), windows=True) == (
        target.resolve(),
        False,
    )


def test_cmd_wrapper_accepts_common_startup_switches_before_delete(tmp_path: Path) -> None:
    target = tmp_path / "discard.txt"
    target.write_text("discard", encoding="utf-8")

    assert shell._delete_target(
        'cmd /d /s /c "del /F discard.txt"',
        str(tmp_path),
        windows=True,
    ) == (target.resolve(), False)


def test_busybox_delete_is_unwrapped_to_the_exact_target(tmp_path: Path) -> None:
    target = tmp_path / "discard.txt"
    target.write_text("discard", encoding="utf-8")

    assert shell._delete_target("busybox rm discard.txt", str(tmp_path)) == (
        target.resolve(),
        False,
    )


@pytest.mark.parametrize(
    "command",
    (
        "FOO=1 rm discard.txt",
        "nice rm discard.txt",
        "nohup rm discard.txt",
        "exec rm discard.txt",
        "time rm discard.txt",
    ),
)
def test_assignment_and_process_wrappers_reach_the_exact_delete_target(
    tmp_path: Path,
    command: str,
) -> None:
    target = tmp_path / "discard.txt"
    target.write_text("discard", encoding="utf-8")

    assert shell._delete_target(command, str(tmp_path), windows=False) == (
        target.resolve(),
        False,
    )


@pytest.mark.parametrize("command", (r"r\m discard.txt", "r''m discard.txt"))
def test_shell_lexical_escapes_still_reach_the_exact_delete_target(
    tmp_path: Path,
    command: str,
) -> None:
    target = tmp_path / "discard.txt"
    target.write_text("discard", encoding="utf-8")

    assert shell._delete_target(command, str(tmp_path), windows=False) == (
        target.resolve(),
        False,
    )


@pytest.mark.parametrize(
    "command",
    (
        "env -C nested rm discard.txt",
        "env --chdir=nested rm discard.txt",
        "sudo -D nested rm discard.txt",
        "sudo --chdir=nested rm discard.txt",
    ),
)
def test_chdir_wrappers_require_an_explicit_tool_workdir(
    tmp_path: Path,
    command: str,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    target = nested / "discard.txt"
    target.write_text("discard", encoding="utf-8")

    assert shell._delete_target(command, str(tmp_path), windows=False) == (None, False)


def test_missing_chdir_wrapper_cannot_turn_failed_command_into_absolute_delete(
    tmp_path: Path,
) -> None:
    target = tmp_path / "important.txt"
    target.write_text("important", encoding="utf-8")

    assert shell._delete_target(
        f"env -C missing rm {target}",
        str(tmp_path),
        windows=False,
    ) == (None, False)


@pytest.mark.asyncio
async def test_env_split_string_delete_fails_closed_when_not_exact(tmp_path: Path) -> None:
    target = tmp_path / "important.txt"
    target.write_text("important", encoding="utf-8")

    result = await shell._gate_recursive_delete(
        "env -S 'rm -v important.txt'",
        cwd=str(tmp_path),
        approval_id=None,
        require_exact=False,
    )

    payload = json.loads(result or "{}")
    assert payload["status"] == "blocked"
    assert target.read_text(encoding="utf-8") == "important"


def test_non_executing_echo_of_delete_name_is_not_treated_as_delete(tmp_path: Path) -> None:
    assert shell._delete_target("echo rm important.txt", str(tmp_path)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    (
        "echo $(rm important.txt)",
        "printf '%s' \"$(rm important.txt)\"",
        "rg --pre rm needle important.txt",
    ),
)
async def test_outer_commands_that_can_execute_delete_subprocesses_fail_closed(
    tmp_path: Path,
    command: str,
) -> None:
    target = tmp_path / "important.txt"
    target.write_text("important", encoding="utf-8")

    result = await shell._gate_recursive_delete(
        command,
        cwd=str(tmp_path),
        approval_id=None,
        require_exact=False,
    )

    payload = json.loads(result or "{}")
    assert payload["status"] == "blocked"
    assert target.read_text(encoding="utf-8") == "important"


def test_posix_sh_c_only_treats_the_next_token_as_script(tmp_path: Path) -> None:
    target = tmp_path / "important.txt"
    target.write_text("important", encoding="utf-8")

    assert shell._delete_target(
        "sh -c rm important.txt",
        str(tmp_path),
        windows=False,
    ) == (
        None,
        False,
    )
    assert shell._delete_target(
        "sh -c 'rm important.txt'",
        str(tmp_path),
        windows=False,
    ) == (
        target.resolve(),
        False,
    )


@pytest.mark.parametrize(
    "command",
    (
        "env --help rm important.txt",
        "sudo --help rm important.txt",
        "sh --help -c 'rm important.txt'",
    ),
)
def test_wrapper_help_or_unknown_options_never_become_brokered_deletes(
    tmp_path: Path,
    command: str,
) -> None:
    target = tmp_path / "important.txt"
    target.write_text("important", encoding="utf-8")

    assert shell._delete_target(command, str(tmp_path), windows=False) == (None, False)


def test_command_lookup_of_rm_is_not_treated_as_execution(tmp_path: Path) -> None:
    assert shell._delete_target("command -v rm", str(tmp_path), windows=False) is None
    assert shell._delete_target("command -V rm", str(tmp_path), windows=False) is None


@pytest.mark.parametrize("command", ("env -i rm discard.txt", "sudo -n rm discard.txt"))
def test_supported_wrapper_options_still_reach_exact_delete(
    tmp_path: Path,
    command: str,
) -> None:
    target = tmp_path / "discard.txt"
    target.write_text("discard", encoding="utf-8")

    assert shell._delete_target(command, str(tmp_path), windows=False) == (
        target.resolve(),
        False,
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.name == "nt",
    reason="backslash escaping inside command substitution is POSIX shell syntax",
)
async def test_escaped_delete_inside_command_substitution_fails_closed(
    tmp_path: Path,
) -> None:
    target = tmp_path / "important.txt"
    target.write_text("important", encoding="utf-8")

    result = await shell._gate_recursive_delete(
        r"echo $(r\m important.txt)",
        cwd=str(tmp_path),
        approval_id=None,
        require_exact=False,
    )

    payload = json.loads(result or "{}")
    assert payload["status"] == "blocked"
    assert target.exists()


@pytest.mark.parametrize(
    "command",
    (
        "r${OPENSQUILLA_REVIEW_UNSET_91F3}m important.txt",
        "$DELETE_CMD important.txt",
        '"$DELETE_CMD" important.txt',
    ),
)
def test_dynamic_executable_is_never_treated_as_a_non_delete(
    tmp_path: Path,
    command: str,
) -> None:
    assert shell._delete_target(command, str(tmp_path), windows=False) == (None, False)


@pytest.mark.parametrize(
    "command",
    (
        "command $DELETE_CMD important.txt",
        "nice $DELETE_CMD important.txt",
        "nohup $DELETE_CMD important.txt",
        "env DELETE_CMD=rm sh -c '$DELETE_CMD important.txt'",
    ),
)
def test_dynamic_executable_behind_known_wrapper_fails_closed(
    tmp_path: Path,
    command: str,
) -> None:
    assert shell._delete_target(command, str(tmp_path), windows=False) == (None, False)


@pytest.mark.parametrize(
    "command",
    (
        'cmd /v:on /c "!DELETE_CMD! /Q important.txt"',
        'cmd /v:on /c "!DELETE-CMD! /Q important.txt"',
        'cmd /v:on /c "!DELETE""! /Q important.txt"',
        'cmd /c "%9.DELETE(CMD)% /Q important.txt"',
        'cmd /c "%DELETE""% /Q important.txt"',
        'cmd /c "d^el /Q important.txt"',
        'cmd /c d""el /Q important.txt',
        'cmd /s /c "d""el /Q important.txt"',
        'Remove-"Item" important.txt',
    ),
)
def test_cmd_dynamic_or_caret_executable_fails_closed(
    tmp_path: Path,
    command: str,
) -> None:
    assert shell._delete_target(command, str(tmp_path), windows=True) == (None, False)


@pytest.mark.parametrize(
    "command",
    (
        "call %DELETE_CMD% important.txt",
        "start %DELETE_CMD% important.txt",
        "& $DELETE_CMD important.txt",
        "eval $DELETE_CMD important.txt",
        "xargs $DELETE_CMD",
    ),
)
def test_execution_carriers_with_dynamic_command_fail_closed(
    tmp_path: Path,
    command: str,
) -> None:
    assert shell._delete_target(command, str(tmp_path), windows=True) == (None, False)


def test_quoted_static_windows_executable_path_is_not_a_dynamic_delete(
    tmp_path: Path,
) -> None:
    command = r'& "C:\Program Files\Acme\tool.exe" --version'

    assert shell._delete_target(command, str(tmp_path), windows=True) is None


@pytest.mark.parametrize(
    "command",
    (
        'sh -lc \'printf "HTTP_PROXY=%s\\n" "$HTTP_PROXY"\'',
        (
            "python - <<'PY'\n"
            "import urllib.request\n"
            "urllib.request.urlopen('https://example.com')\n"
            "PY"
        ),
    ),
)
def test_windows_quoted_data_is_not_treated_as_a_dynamic_delete(
    tmp_path: Path,
    command: str,
) -> None:
    assert shell._delete_target(command, str(tmp_path), windows=True) is None


def test_explicit_posix_delete_analysis_never_falls_back_to_host_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platforms: list[str | None] = []
    parse_segments = shell.parse_shell_segments

    def _capture_platform(command: str, *, platform: str | None = None):
        platforms.append(platform)
        return parse_segments(command, platform=platform)

    monkeypatch.setattr(shell, "parse_shell_segments", _capture_platform)

    assert shell._delete_target("echo ok", str(tmp_path), windows=False) is None
    assert platforms == ["linux"]


def test_echo_of_fragmented_windows_variable_is_not_treated_as_execution(
    tmp_path: Path,
) -> None:
    assert (
        shell._delete_target('echo "%PATH""%"', str(tmp_path), windows=True) is None
    )


@pytest.mark.parametrize(
    "command",
    ("sh script.sh", "command ls", "env FOO=1 printf ok", "nice echo ok"),
)
def test_known_wrappers_without_delete_or_dynamic_executable_are_ignored(
    tmp_path: Path,
    command: str,
) -> None:
    assert shell._delete_target(command, str(tmp_path), windows=False) is None


@pytest.mark.parametrize(
    "command",
    (
        "echo ok; $DELETE_CMD important.txt",
        "echo ok && $DELETE_CMD important.txt",
        "echo ok & $DELETE_CMD important.txt",
        "printf ok | $DELETE_CMD important.txt",
        "Write-Output ok; & $DELETE_CMD important.txt",
    ),
)
def test_dynamic_delete_in_later_compound_segment_fails_closed(
    tmp_path: Path,
    command: str,
) -> None:
    assert shell._delete_target(command, str(tmp_path)) == (None, False)


def test_compound_inert_commands_without_delete_are_ignored(tmp_path: Path) -> None:
    assert shell._delete_target("echo ok; printf done", str(tmp_path)) is None


@pytest.mark.parametrize("windows", (False, True))
def test_heredoc_body_delete_words_are_data_not_commands(
    tmp_path: Path,
    windows: bool,
) -> None:
    command = "cat <<'EOF'\nrm important.txt\n<?php echo 'debug';\nEOF\n"

    assert shell._delete_target(command, str(tmp_path), windows=windows) is None


@pytest.mark.parametrize("windows", (False, True))
def test_delete_after_heredoc_terminator_fails_closed(
    tmp_path: Path,
    windows: bool,
) -> None:
    command = "cat <<'EOF'\nrm body-only.txt\nEOF\nrm important.txt"

    assert shell._delete_target(command, str(tmp_path), windows=windows) == (
        None,
        False,
    )


def test_heredoc_marker_inside_comment_cannot_hide_later_delete(tmp_path: Path) -> None:
    command = "echo ok # <<EOF\nrm important.txt\nEOF"

    assert shell._delete_target(command, str(tmp_path), windows=False) == (None, False)


@pytest.mark.parametrize(
    "command",
    (
        ": $((1 << EOF))\nrm important.txt\nEOF",
        ": $[1 << EOF ]\nrm important.txt\nEOF",
        "((1 << EOF))\nrm important.txt\nEOF",
    ),
)
def test_arithmetic_shift_cannot_hide_later_delete(
    tmp_path: Path,
    command: str,
) -> None:
    assert shell._delete_target(command, str(tmp_path), windows=False) == (None, False)


@pytest.mark.parametrize(
    "command",
    (
        "Remove-Item first.txt,second.txt",
        "Remove-Item -Path first.txt,second.txt",
        "Remove-Item -LiteralPath first.txt,second.txt",
        "ri first.txt,second.txt",
        "rm first.txt,second.txt",
    ),
)
def test_powershell_unquoted_path_lists_fail_closed(
    tmp_path: Path,
    command: str,
) -> None:
    assert shell._delete_target(command, str(tmp_path), windows=True) == (None, False)


def test_powershell_quoted_comma_stays_one_literal_path(tmp_path: Path) -> None:
    target = tmp_path / "first.txt,second.txt"
    target.write_text("discard", encoding="utf-8")

    assert shell._delete_target(
        'Remove-Item "first.txt,second.txt"',
        str(tmp_path),
        windows=True,
    ) == (target.resolve(), False)


def test_recursive_windows_directory_alias_is_parsed_exactly(tmp_path: Path) -> None:
    target = tmp_path / "discard"
    target.mkdir()

    parsed = shell._delete_target("rd /S discard", str(tmp_path), windows=True)

    assert parsed == (target.resolve(), True)


def test_rm_dir_flag_preserves_nonrecursive_empty_directory_semantics(
    tmp_path: Path,
) -> None:
    target = tmp_path / "discard"
    target.mkdir()

    assert shell._delete_target("rm -d discard", str(tmp_path), windows=False) == (
        target.resolve(),
        False,
    )


@pytest.mark.parametrize("command", ("rm discard", "unlink discard", "del discard"))
def test_file_only_delete_commands_do_not_gain_directory_semantics(
    tmp_path: Path,
    command: str,
) -> None:
    (tmp_path / "discard").mkdir()

    assert shell._delete_target(command, str(tmp_path)) is None


@pytest.mark.parametrize(
    "command",
    (
        "rm --help important.txt",
        "rm --invalid-option important.txt",
        "Remove-Item -WhatIf important.txt",
    ),
)
def test_delete_options_that_change_or_prevent_execution_are_not_brokered(
    tmp_path: Path,
    command: str,
) -> None:
    target = tmp_path / "important.txt"
    target.write_text("important", encoding="utf-8")

    assert shell._delete_target(command, str(tmp_path)) == (None, False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    (
        "rm -v important.txt",
        "rm --verbose important.txt",
        "Remove-Item -Verbose important.txt",
    ),
)
async def test_unmodelled_delete_options_fail_closed_even_without_legacy_approval(
    tmp_path: Path,
    command: str,
) -> None:
    target = tmp_path / "important.txt"
    target.write_text("important", encoding="utf-8")

    result = await shell._gate_recursive_delete(
        command,
        cwd=str(tmp_path),
        approval_id=None,
        require_exact=False,
    )

    payload = json.loads(result or "{}")
    assert payload["status"] == "blocked"
    assert target.read_text(encoding="utf-8") == "important"


@pytest.mark.parametrize("command", ("rm -rf .", "rm -rf .."))
def test_rm_dot_operands_are_never_converted_to_structured_deletes(
    tmp_path: Path,
    command: str,
) -> None:
    assert shell._delete_target(command, str(tmp_path), windows=False) == (None, True)


@pytest.mark.parametrize("command", ("rmdir important.txt", "rd important.txt"))
def test_directory_delete_commands_do_not_gain_file_unlink_semantics(
    tmp_path: Path,
    command: str,
) -> None:
    (tmp_path / "important.txt").write_text("important", encoding="utf-8")

    assert shell._delete_target(command, str(tmp_path)) is None


@pytest.mark.asyncio
async def test_file_delete_preserves_symlink_referent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(shell, "_PENDING_RECURSIVE_DELETES", {})
    referent = tmp_path / "workspace" / "important.txt"
    referent.parent.mkdir(parents=True)
    referent.write_text("important", encoding="utf-8")
    link = referent.parent / "discard-link"
    link.symlink_to(referent)
    context = ToolContext(
        run_mode="safe",
        workspace_dir=str(referent.parent),
        session_key="test",
        sandbox_policy=SandboxPolicy(),
        sandbox_gateway_config=SimpleNamespace(state_dir=str(tmp_path / "state")),
    )
    monkeypatch.setattr(shell, "gate_elevated_action", lambda *_a, **_k: _AllowedGate())
    token = current_tool_context.set(context)
    try:
        result = await shell._gate_recursive_delete(
            f"rm '{link}'",
            cwd=str(referent.parent),
            approval_id=None,
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result or "{}")
    assert payload["status"] == "deleted"
    assert not link.exists()
    assert not link.is_symlink()
    assert referent.read_text(encoding="utf-8") == "important"


@pytest.mark.asyncio
async def test_compound_delete_is_blocked_when_command_requires_approval(
    tmp_path,
) -> None:
    target = tmp_path / "discard.txt"
    target.write_text("keep", encoding="utf-8")

    sandboxed = await shell._gate_recursive_delete(
        f"rm '{target}' && echo cleanup",
        cwd=str(tmp_path),
        approval_id=None,
        require_exact=False,
    )
    elevated = await shell._gate_recursive_delete(
        f"rm '{target}' && echo cleanup",
        cwd=str(tmp_path),
        approval_id=None,
        require_exact=True,
    )

    sandboxed_payload = json.loads(sandboxed or "{}")
    elevated_payload = json.loads(elevated or "{}")
    assert sandboxed_payload["status"] == "blocked"
    assert elevated_payload["status"] == "blocked"
    assert elevated_payload["reason"] == "recursive_delete_target_not_static"
    assert target.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    (
        "echo ok && rm discard.txt",
        "cd . && rm discard.txt",
        "echo ok && busybox rm discard.txt",
    ),
)
async def test_non_delete_leading_compound_fails_closed_when_approval_is_required(
    tmp_path,
    command: str,
) -> None:
    target = tmp_path / "discard.txt"
    target.write_text("keep", encoding="utf-8")

    result = await shell._gate_recursive_delete(
        command,
        cwd=str(tmp_path),
        approval_id=None,
        require_exact=True,
    )

    payload = json.loads(result or "{}")
    assert payload["status"] == "blocked"
    assert target.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(
    os.name == "nt",
    reason="intermediate symlink traversal uses POSIX path resolution semantics",
)
def test_intermediate_symlink_is_resolved_while_final_target_is_preserved(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    nested = outside / "nested"
    nested.mkdir(parents=True)
    victim = outside / "victim.txt"
    victim.write_text("victim", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(nested, target_is_directory=True)

    assert shell._delete_target(
        "rm link/../victim.txt",
        str(tmp_path),
        windows=False,
    ) == (
        victim.resolve(),
        False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "process_call",
    [
        lambda workspace: shell.exec_command(
            "Write-Output must-not-run",
            workdir=str(workspace),
            env={"OPENSQUILLA_GUEST_SAFE": "0"},
        ),
        lambda workspace: shell.background_process(
            "Write-Output must-not-run",
            workdir=str(workspace),
        ),
        lambda _workspace: code_exec.execute_code("print('must-not-run')"),
        lambda _workspace: git.git_status(),
    ],
    ids=("shell-env-override", "background", "python-code", "git"),
)
async def test_windows_guest_process_tools_fail_before_runtime_enrichment(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    process_call,
) -> None:
    """Guest authority, not a caller-controlled environment marker, denies launch."""

    runtime = configure_runtime(
        SandboxSettings(
            run_mode="standard",
            backend="noop",
            allow_legacy_mode=True,
        ),
        workspace=tmp_path,
    )
    runtime.backend = SimpleNamespace(name="windows_default")

    def enrichment_must_not_run(*_args, **_kwargs):
        raise AssertionError("Windows guest denial must precede runtime enrichment")

    monkeypatch.setattr(shell, "_runtime_shell_environment", enrichment_must_not_run)
    monkeypatch.setattr(
        shell,
        "_policy_with_windows_shell_runtime_mounts",
        enrichment_must_not_run,
    )
    token = current_tool_context.set(
        ToolContext(
            guest_safe=True,
            run_mode="safe",
            workspace_dir=str(tmp_path),
            environment={"PATH": "", "OPENSQUILLA_GUEST_SAFE": "1"},
        )
    )
    try:
        with pytest.raises(
            Exception,
            match="GUEST_WINDOWS_PROCESS_UNAVAILABLE",
        ):
            await process_call(tmp_path)
    finally:
        current_tool_context.reset(token)
        reset_runtime()


@pytest.mark.asyncio
async def test_windows_guest_file_tools_still_use_filesystem_worker(
    tmp_path,
) -> None:
    """The process fallback must not disable managed-workspace file operations."""

    calls: list[str] = []

    class FilesystemWorkerBackend:
        name = "windows_default"

        @staticmethod
        def operation_domains_supported() -> tuple[str, ...]:
            return ("filesystem",)

        async def run_operation(self, operation):
            calls.append(operation.kind)
            request = operation.request
            assert request.path is not None
            if operation.kind == "write_text":
                request.path.write_text(request.content, encoding="utf-8")
                return SandboxOperationResult(message="written", created=True)
            if operation.kind == "read_file":
                return SandboxOperationResult(message=request.path.read_text(encoding="utf-8"))
            raise AssertionError(f"unexpected filesystem operation: {operation.kind}")

        async def run(self, _request):
            raise AssertionError("file tools must not use the process runner")

    runtime = configure_runtime(
        SandboxSettings(
            run_mode="standard",
            backend="noop",
            allow_legacy_mode=True,
        ),
        workspace=tmp_path,
    )
    runtime.backend = FilesystemWorkerBackend()
    target = tmp_path / "guest.txt"
    token = current_tool_context.set(
        ToolContext(
            guest_safe=True,
            run_mode="safe",
            workspace_dir=str(tmp_path),
            sandbox_file_system_profile=FileSystemPermissionProfile(
                entries=(
                    FileSystemPermissionEntry(
                        tmp_path,
                        FileSystemAccess.WRITE,
                    ),
                )
            ),
        )
    )
    try:
        assert await filesystem.write_file(str(target), "guest data") == "written"
        assert await filesystem.read_file(str(target)) == "guest data"
    finally:
        current_tool_context.reset(token)
        reset_runtime()

    assert calls == ["write_text", "read_file"]


def test_active_policy_comes_from_turn_snapshot() -> None:
    snapshot = SandboxPolicy(
        policy_version=7,
        files=FilePolicySettings(backup_quota_bytes=1234),
    )
    token = current_tool_context.set(ToolContext(run_mode="safe", sandbox_policy=snapshot))
    try:
        first = active_sandbox_policy()
        first.files.backup_quota_bytes = 9999
        second = active_sandbox_policy()
    finally:
        current_tool_context.reset(token)

    assert first.policy_version == second.policy_version == 7
    assert first.files.backup_quota_bytes == 9999
    assert second.files.backup_quota_bytes == 1234


def test_saved_file_policy_compiles_into_the_live_safe_profile(tmp_path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    snapshot = SandboxPolicy(files=FilePolicySettings(custom_deny_write_paths=[str(protected)]))
    token = current_tool_context.set(
        ToolContext(
            run_mode="safe",
            workspace_dir=str(tmp_path),
            sandbox_policy=snapshot,
            sandbox_gateway_config=SimpleNamespace(state_dir=str(state)),
        )
    )
    try:
        profile = active_file_system_profile(tmp_path)
    finally:
        current_tool_context.reset(token)

    assert profile is not None
    expected_default = (
        FileSystemAccess.READ if os.name == "nt" else FileSystemAccess.WRITE
    )
    assert profile.default_access is expected_default
    protected_write = decide_path_access(
        protected / "secret.txt",
        workspace=tmp_path,
        write=True,
        profile=profile,
    )
    authority_read = decide_path_access(
        state / "sessions.db",
        workspace=tmp_path,
        write=False,
        profile=profile,
    )
    ordinary_write = decide_path_access(
        tmp_path / "ordinary.txt",
        workspace=tmp_path,
        write=True,
        profile=profile,
    )
    assert protected_write.status != "allowed"
    assert authority_read.status == "blocked"
    assert ordinary_write.status == "allowed"


def test_saved_file_policy_cannot_widen_runtime_sandbox_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside" / "notes.txt"
    protected = workspace / "protected"
    snapshot = SandboxPolicy()

    from opensquilla.sandbox import file_policy

    monkeypatch.setattr(
        file_policy,
        "compile_safe_file_profile",
        lambda *_args, **_kwargs: FileSystemPermissionProfile(
            entries=(
                FileSystemPermissionEntry(outside.parent, FileSystemAccess.WRITE),
                FileSystemPermissionEntry(protected, FileSystemAccess.READ),
            ),
            default_access=FileSystemAccess.WRITE,
        ),
    )
    configure_runtime(
        SandboxSettings(
            run_mode="standard",
            backend="noop",
            allow_legacy_mode=True,
            exclude_slash_tmp=True,
            exclude_tmpdir_env_var=True,
        ),
        workspace=workspace,
    )
    token = current_tool_context.set(
        ToolContext(
            run_mode="safe",
            workspace_dir=str(workspace),
            sandbox_policy=snapshot,
        )
    )
    try:
        profile = active_file_system_profile(workspace)
    finally:
        current_tool_context.reset(token)
        reset_runtime()

    assert profile is not None
    decision = decide_path_access(
        outside,
        workspace=workspace,
        write=True,
        profile=profile,
    )
    assert decision.status == "request"


def test_guest_safe_profile_keeps_workspace_boundary_and_protected_carveouts(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    guest_home = tmp_path / "guest-home"
    guest_temp = tmp_path / "guest-temp"
    runtime_root = tmp_path / "runtime"
    protected = workspace / "protected"
    for directory in (protected, guest_home, guest_temp, runtime_root):
        directory.mkdir(parents=True)
    runtime = configure_runtime(
        SandboxSettings(
            run_mode="standard",
            backend="noop",
            allow_legacy_mode=True,
            exclude_slash_tmp=True,
            exclude_tmpdir_env_var=True,
        ),
        workspace=workspace,
    )
    snapshot = SandboxPolicy(files=FilePolicySettings(custom_deny_write_paths=[str(protected)]))
    token = current_tool_context.set(
        ToolContext(
            run_mode="safe",
            guest_safe=True,
            workspace_dir=str(workspace),
            sandbox_run_context=RunContext(
                run_mode=RunMode.SAFE,
                workspace=str(workspace),
                mounts=(
                    MountGrant(path=str(guest_home), access="rw", scope="once"),
                    MountGrant(path=str(guest_temp), access="rw", scope="once"),
                    MountGrant(path=str(runtime_root), access="ro", scope="once"),
                ),
            ),
            sandbox_policy=snapshot,
            sandbox_gateway_config=SimpleNamespace(state_dir=str(tmp_path / "state")),
        )
    )
    try:
        profile = active_file_system_profile(workspace)
    finally:
        current_tool_context.reset(token)
        reset_runtime()

    assert runtime is not None
    assert profile is not None
    protected_write = decide_path_access(
        protected / "secret.txt",
        workspace=workspace,
        write=True,
        profile=profile,
    )
    ordinary_write = decide_path_access(
        workspace / "ordinary.txt",
        workspace=workspace,
        write=True,
        profile=profile,
    )
    outside_write = decide_path_access(
        tmp_path / "outside.txt",
        workspace=workspace,
        write=True,
        profile=profile,
    )
    sensitive_read = decide_path_access(
        Path.home() / ".ssh" / "id_ed25519",
        workspace=workspace,
        write=False,
        profile=profile,
    )
    ordinary_read = decide_path_access(
        tmp_path / "ordinary-host-file.txt",
        workspace=workspace,
        write=False,
        profile=profile,
    )
    home_write = decide_path_access(
        guest_home / "notes.txt",
        workspace=workspace,
        write=True,
        profile=profile,
    )
    temp_write = decide_path_access(
        guest_temp / "scratch.txt",
        workspace=workspace,
        write=True,
        profile=profile,
    )
    runtime_read = decide_path_access(
        runtime_root / "python.exe",
        workspace=workspace,
        write=False,
        profile=profile,
    )
    assert protected_write.status != "allowed"
    assert ordinary_write.status == "allowed"
    assert outside_write.status != "allowed"
    assert sensitive_read.status == "blocked"
    assert ordinary_read.status != "allowed"
    assert profile.resolve(tmp_path / "ordinary-host-file.txt") is FileSystemAccess.DENY
    assert home_write.status == "allowed"
    assert temp_write.status == "allowed"
    assert runtime_read.status == "allowed"


@pytest.mark.asyncio
async def test_guest_safe_outside_write_cannot_request_or_consume_an_approval(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    configure_runtime(
        SandboxSettings(
            run_mode="standard",
            backend="noop",
            allow_legacy_mode=True,
            exclude_slash_tmp=True,
            exclude_tmpdir_env_var=True,
        ),
        workspace=workspace,
    )
    token = current_tool_context.set(
        ToolContext(
            run_mode="safe",
            guest_safe=True,
            workspace_dir=str(workspace),
            sandbox_policy=SandboxPolicy(),
            sandbox_gateway_config=SimpleNamespace(state_dir=str(tmp_path / "state")),
        )
    )
    try:
        file_payload = filesystem._sandbox_path_access_envelope(
            outside,
            write=True,
            approval_id="forged-or-stale-approval",
        )
        file_gate_payload, elevated, _backups = await filesystem._gate_out_of_workspace_write(
            "write_file",
            outside,
            str(outside),
            "forged-or-stale-approval",
            sandbox_permissions="require_escalated",
            justification="try to cross the guest boundary",
        )
        payload = shell._sandbox_write_path_access_envelope(
            OperationProfile(
                name="guest-outside-write",
                requested_write_paths=(str(outside),),
            ),
            str(workspace),
            f"write {outside}",
            approval_id="forged-or-stale-approval",
        )
        shell_escalation_payload = json.loads(
            await shell.exec_command(
                "Write-Output guest",
                workdir=str(workspace),
                sandbox_permissions="require_escalated",
                justification="try to leave the sandbox",
                approval_id="forged-or-stale-approval",
            )
        )
    finally:
        current_tool_context.reset(token)
        reset_runtime()

    assert payload is not None
    assert payload["status"] == "blocked"
    assert payload["reason"] == "GUEST_WRITE_OUTSIDE_DEFAULT_WORKSPACE"
    assert "approval_id" not in payload
    assert file_payload is not None
    assert file_payload["status"] == "blocked"
    assert file_payload["reason"] == "GUEST_WRITE_OUTSIDE_DEFAULT_WORKSPACE"
    assert "approval_id" not in file_payload
    assert file_gate_payload is not None
    assert file_gate_payload["status"] == "blocked"
    assert file_gate_payload["reason"] == "GUEST_WRITE_OUTSIDE_DEFAULT_WORKSPACE"
    assert elevated is False
    assert shell_escalation_payload["status"] == "blocked"
    assert shell_escalation_payload["reason"] == "GUEST_HOST_EXECUTION_DENIED"
    assert "approval_id" not in shell_escalation_payload


@pytest.mark.asyncio
async def test_recursive_delete_requires_warning_before_mutation(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(shell, "_PENDING_RECURSIVE_DELETES", {})
    target = tmp_path / "workspace" / "discard"
    target.mkdir(parents=True)
    (target / "data.txt").write_text("keep a backup", encoding="utf-8")
    context = ToolContext(
        run_mode="safe",
        workspace_dir=str(tmp_path / "workspace"),
        session_key="test",
        sandbox_policy=SandboxPolicy(),
        sandbox_gateway_config=SimpleNamespace(state_dir=str(tmp_path / "state")),
    )
    reviewed_actions = []

    def _capture_action(action, **_kwargs):
        reviewed_actions.append(action)
        return _PendingGate()

    monkeypatch.setattr(shell, "gate_elevated_action", _capture_action)
    token = current_tool_context.set(context)
    try:
        result = await shell._gate_recursive_delete(
            f"Remove-Item -Recurse -Force '{target}'",
            cwd=str(tmp_path / "workspace"),
            approval_id=None,
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result or "{}")
    assert payload["status"] == "approval_required"
    assert payload["recursive"] is True
    assert payload["irreversible"] is False
    assert payload["backup_state"] == "enabled"
    assert "无法撤回" in payload["warning"]
    assert reviewed_actions[0].display.kind == "delete"
    assert reviewed_actions[0].display.target == str(target)
    assert reviewed_actions[0].display.backup_state == "enabled"
    assert target.exists()


@pytest.mark.asyncio
async def test_approved_recursive_delete_is_backed_up_then_removed(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(shell, "_PENDING_RECURSIVE_DELETES", {})
    target = tmp_path / "workspace" / "discard"
    target.mkdir(parents=True)
    (target / "data.txt").write_text("keep a backup", encoding="utf-8")
    context = ToolContext(
        run_mode="safe",
        workspace_dir=str(tmp_path / "workspace"),
        session_key="test",
        sandbox_policy=SandboxPolicy(),
        sandbox_gateway_config=SimpleNamespace(state_dir=str(tmp_path / "state")),
    )
    monkeypatch.setattr(shell, "gate_elevated_action", lambda *_a, **_k: _AllowedGate())
    offloaded: list[str] = []

    async def inline_to_thread(function, *args, **kwargs):
        offloaded.append(function.__name__)
        return function(*args, **kwargs)

    monkeypatch.setattr(shell.asyncio, "to_thread", inline_to_thread)
    token = current_tool_context.set(context)
    try:
        result = await shell._gate_recursive_delete(
            f"Remove-Item -Recurse -Force '{target}'",
            cwd=str(tmp_path / "workspace"),
            approval_id=None,
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result or "{}")
    assert payload["status"] == "deleted"
    assert payload["backup"]["sizeBytes"] > 0
    assert payload["backup"]["createdAt"] > 0
    assert "entryPath" not in payload["backup"]
    assert not target.exists()
    assert (tmp_path / "state" / "backup-vault" / "entries").is_dir()
    assert offloaded == ["plan_delete", "execute"]


@pytest.mark.asyncio
async def test_literal_file_delete_uses_the_same_approval_and_backup_gate(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(shell, "_PENDING_RECURSIVE_DELETES", {})
    target = tmp_path / "workspace" / "discard.txt"
    target.parent.mkdir(parents=True)
    target.write_text("keep a backup", encoding="utf-8")
    context = ToolContext(
        run_mode="safe",
        workspace_dir=str(target.parent),
        session_key="test",
        sandbox_policy=SandboxPolicy(),
        sandbox_gateway_config=SimpleNamespace(state_dir=str(tmp_path / "state")),
    )
    gates = iter((_PendingGate(), _AllowedGate()))
    monkeypatch.setattr(shell, "gate_elevated_action", lambda *_a, **_k: next(gates))
    token = current_tool_context.set(context)
    try:
        first = await shell._gate_recursive_delete(
            f"rm '{target}'",
            cwd=str(target.parent),
            approval_id=None,
        )
        second = await shell._gate_recursive_delete(
            f"rm '{target}'",
            cwd=str(target.parent),
            approval_id="pending",
        )
    finally:
        current_tool_context.reset(token)

    first_payload = json.loads(first or "{}")
    result = json.loads(second or "{}")
    assert first_payload["status"] == "approval_required"
    assert first_payload["recursive"] is False
    assert first_payload["backup_state"] == "enabled"
    assert result["status"] == "deleted"
    assert result["recursive"] is False
    assert result["backup"]["sizeBytes"] > 0
    assert not target.exists()


@pytest.mark.asyncio
async def test_missing_state_dir_is_reported_only_after_first_delete_approval(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(shell, "_PENDING_RECURSIVE_DELETES", {})
    target = tmp_path / "workspace" / "discard.txt"
    target.parent.mkdir(parents=True)
    target.write_text("important", encoding="utf-8")
    context = ToolContext(
        run_mode="safe",
        workspace_dir=str(target.parent),
        session_key="test",
        sandbox_policy=SandboxPolicy(),
        sandbox_gateway_config=SimpleNamespace(state_dir=""),
    )
    gates = iter((_PendingGate(), _AllowedGate(), _PendingGate()))
    monkeypatch.setattr(shell, "gate_elevated_action", lambda *_a, **_k: next(gates))
    token = current_tool_context.set(context)
    try:
        first = await shell._gate_recursive_delete(
            f"rm '{target}'",
            cwd=str(target.parent),
            approval_id=None,
        )
        second = await shell._gate_recursive_delete(
            f"rm '{target}'",
            cwd=str(target.parent),
            approval_id="pending",
        )
    finally:
        current_tool_context.reset(token)

    first_payload = json.loads(first or "{}")
    second_payload = json.loads(second or "{}")
    assert first_payload["status"] == "approval_required"
    assert second_payload["status"] == "approval_required"
    assert second_payload["backup_state"] == "unavailable_requires_confirmation"
    assert second_payload["irreversible"] is True
    assert target.exists()


@pytest.mark.asyncio
async def test_file_delete_with_backup_disabled_warns_then_deletes_without_backup(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(shell, "_PENDING_RECURSIVE_DELETES", {})
    target = tmp_path / "workspace" / "discard.txt"
    target.parent.mkdir(parents=True)
    target.write_text("discard", encoding="utf-8")
    policy = SandboxPolicy.model_validate({"files": {"recursiveDeleteBackupEnabled": False}})
    context = ToolContext(
        run_mode="safe",
        workspace_dir=str(target.parent),
        session_key="test",
        sandbox_policy=policy,
        sandbox_gateway_config=SimpleNamespace(state_dir=str(tmp_path / "state")),
    )
    gates = iter((_PendingGate(), _AllowedGate()))
    monkeypatch.setattr(shell, "gate_elevated_action", lambda *_a, **_k: next(gates))
    token = current_tool_context.set(context)
    try:
        first = await shell._gate_recursive_delete(
            f"unlink '{target}'",
            cwd=str(target.parent),
            approval_id=None,
        )
        second = await shell._gate_recursive_delete(
            f"unlink '{target}'",
            cwd=str(target.parent),
            approval_id="pending",
        )
    finally:
        current_tool_context.reset(token)

    warning = json.loads(first or "{}")
    result = json.loads(second or "{}")
    assert warning["backup_state"] == "disabled"
    assert warning["irreversible"] is True
    assert result["status"] == "deleted"
    assert result["backup"] is None
    assert not target.exists()


@pytest.mark.asyncio
async def test_denied_file_delete_keeps_the_exact_target(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(shell, "_PENDING_RECURSIVE_DELETES", {})
    target = tmp_path / "workspace" / "discard.txt"
    target.parent.mkdir(parents=True)
    target.write_text("keep", encoding="utf-8")
    context = ToolContext(
        run_mode="safe",
        workspace_dir=str(target.parent),
        session_key="test",
        sandbox_policy=SandboxPolicy(),
        sandbox_gateway_config=SimpleNamespace(state_dir=str(tmp_path / "state")),
    )
    gates = iter((_PendingGate(), _DeniedGate()))
    monkeypatch.setattr(shell, "gate_elevated_action", lambda *_a, **_k: next(gates))
    token = current_tool_context.set(context)
    try:
        first = await shell._gate_recursive_delete(
            f"rm '{target}'",
            cwd=str(target.parent),
            approval_id=None,
        )
        denied = await shell._gate_recursive_delete(
            f"rm '{target}'",
            cwd=str(target.parent),
            approval_id="pending",
        )
    finally:
        current_tool_context.reset(token)

    assert json.loads(first or "{}")["status"] == "approval_required"
    assert json.loads(denied or "{}")["status"] == "approval_denied"
    assert target.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_recursive_delete_cannot_target_sandbox_authority_path(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(shell, "_PENDING_RECURSIVE_DELETES", {})
    state = tmp_path / "state"
    target = state / "protected"
    target.mkdir(parents=True)
    context = ToolContext(
        run_mode="safe",
        workspace_dir=str(tmp_path / "workspace"),
        session_key="test",
        sandbox_policy=SandboxPolicy(),
        sandbox_gateway_config=SimpleNamespace(state_dir=str(state)),
    )
    token = current_tool_context.set(context)
    try:
        result = await shell._gate_recursive_delete(
            f"Remove-Item -Recurse -Force '{target}'",
            cwd=str(tmp_path),
            approval_id=None,
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result or "{}")
    assert payload["status"] == "blocked"
    assert payload["reason"] == "sandbox_authority_read_denied"
    assert target.exists()


@pytest.mark.asyncio
async def test_recursive_delete_detects_target_change_after_approval(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(shell, "_PENDING_RECURSIVE_DELETES", {})
    target = tmp_path / "workspace" / "discard"
    target.mkdir(parents=True)
    data = target / "data.txt"
    data.write_text("before", encoding="utf-8")
    context = ToolContext(
        run_mode="safe",
        workspace_dir=str(tmp_path / "workspace"),
        session_key="test",
        sandbox_policy=SandboxPolicy(),
        sandbox_gateway_config=SimpleNamespace(state_dir=str(tmp_path / "state")),
    )
    monkeypatch.setattr(shell, "gate_elevated_action", lambda *_a, **_k: _PendingGate())
    token = current_tool_context.set(context)
    try:
        first = await shell._gate_recursive_delete(
            f"Remove-Item -Recurse -Force '{target}'",
            cwd=str(tmp_path / "workspace"),
            approval_id=None,
        )
        assert json.loads(first or "{}")["approval_id"] == "pending"
        data.write_text("changed", encoding="utf-8")
        monkeypatch.setattr(shell, "gate_elevated_action", lambda *_a, **_k: _AllowedGate())
        result = await shell._gate_recursive_delete(
            f"Remove-Item -Recurse -Force '{target}'",
            cwd=str(tmp_path / "workspace"),
            approval_id="pending",
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result or "{}")
    assert payload["status"] == "blocked"
    assert payload["reason"] == "recursive_delete_target_changed"
    assert target.exists()


def test_pending_recursive_delete_cache_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(shell, "_PENDING_RECURSIVE_DELETES", {})

    for index in range(300):
        shell._remember_pending_recursive_delete(str(index), object())

    assert len(shell._PENDING_RECURSIVE_DELETES) == 256
    assert "0" not in shell._PENDING_RECURSIVE_DELETES
    assert "299" in shell._PENDING_RECURSIVE_DELETES
