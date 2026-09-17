"""Native shell contracts exercised through the public Full Host tool handlers."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from opensquilla.tools.builtin import shell
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context

pytestmark = pytest.mark.ci_serial


def _quote(value: str) -> str:
    if os.name == "nt":
        return "'" + value.replace("'", "''") + "'"
    return shlex.quote(value)


def _python(*args: str, bare: bool = False) -> str:
    executable = ("python" if os.name == "nt" else "python3") if bare else sys.executable
    prefix = "& " if os.name == "nt" and not bare else ""
    return prefix + " ".join([executable if bare else _quote(executable), *map(_quote, args)])


@pytest.fixture
def full_host_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.integration import configure_runtime, reset_runtime

    # Exercise the same interpreter via explicit and PATH-resolved invocations.
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    configure_runtime(SandboxSettings(run_mode="full", backend="noop"), workspace=tmp_path)
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key=f"native-shell:{tmp_path.name}",
            run_mode="full",
        )
    )
    try:
        yield tmp_path
    finally:
        current_tool_context.reset(token)
        reset_runtime()


async def _exec(workspace: Path, command: str, *, stdin: str | None = None) -> str:
    result = await shell.exec_command(command, workdir=str(workspace), stdin=stdin, timeout=15)
    return result.replace("\r\n", "\n")


@pytest.mark.parametrize("bare", [False, True], ids=["absolute-python", "bare-python"])
async def test_native_inline_python_preserves_quotes(full_host_workspace: Path, bare: bool) -> None:
    (full_host_workspace / "README.md").write_text("native fixture\n", encoding="utf-8")

    result = await _exec(
        full_host_workspace,
        _python("-c", 'from pathlib import Path; print(Path("README.md").name)', bare=bare),
    )

    assert result == "exit_code=0\nREADME.md\n"


@pytest.mark.skipif(os.name != "nt", reason="PowerShell 5 native double quote escaping")
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (r'print(\"hello\")', "hello"),
        (r'import json; print(json.dumps({\"ok\": 1}))', '{"ok": 1}'),
    ],
    ids=["escaped-string", "escaped-json-key"],
)
async def test_native_python_preserves_existing_native_quote_escapes(
    full_host_workspace: Path, source: str, expected: str,
) -> None:
    result = await _exec(full_host_workspace, _python("-c", source))

    assert result == f"exit_code=0\n{expected}\n"


@pytest.mark.parametrize("bare", [False, True], ids=["absolute-python", "bare-python"])
async def test_native_python_preserves_failure_exit_code(
    full_host_workspace: Path, bare: bool
) -> None:
    result = await _exec(
        full_host_workspace,
        _python("-c", 'print("FAILURE_MARKER"); raise SystemExit(7)', bare=bare),
    )

    status, output = result.split("\n", 1)
    # Windows PowerShell's -Command itself normalizes a native failure to 1.
    assert status in {"exit_code=1", "exit_code=7"}, result
    assert output == "FAILURE_MARKER\n"


async def test_native_explicit_exit_preserves_child_exit_code(full_host_workspace: Path) -> None:
    command = _python("-c", 'print("FAILURE_MARKER"); raise SystemExit(7)')
    command += "; exit $LASTEXITCODE" if os.name == "nt" else "; exit $?"

    result = await _exec(full_host_workspace, command)

    assert result == "exit_code=7\nFAILURE_MARKER\n"


@pytest.mark.parametrize(
    "arguments",
    [
        ["two words", "O'Reilly"],
        [r"C:\Program Files\OpenSquilla\file.txt", r"C:\other\file.txt"],
        [r"^\d+\s+\w+$", r"one\\two", "中文 café"],
    ],
    ids=["spaces-and-apostrophe", "windows-paths", "regex-and-unicode"],
)
async def test_native_python_preserves_argument_values(
    full_host_workspace: Path, arguments: list[str]
) -> None:
    result = await _exec(
        full_host_workspace,
        _python("-c", "import json, sys; print(json.dumps(sys.argv[1:]))", *arguments),
    )

    status, output = result.split("\n", 1)
    assert status == "exit_code=0", result
    assert json.loads(output) == arguments


async def test_native_python_single_quoted_source(full_host_workspace: Path) -> None:
    result = await _exec(full_host_workspace, _python("-c", "print('hello')"))

    assert result == "exit_code=0\nhello\n"


async def test_native_python_regex_semantics(full_host_workspace: Path) -> None:
    result = await _exec(
        full_host_workspace,
        _python("-c", r'import re; print(bool(re.fullmatch(r"\d+", "123")))'),
    )

    assert result == "exit_code=0\nTrue\n"


@pytest.mark.parametrize("options", [["-u"], ["-W", "ignore"], ["-X", "utf8"]])
async def test_native_python_options_before_inline_source(
    full_host_workspace: Path, options: list[str]
) -> None:
    result = await _exec(full_host_workspace, _python(*options, "-c", 'print("OPTIONS_OK")'))

    assert result == "exit_code=0\nOPTIONS_OK\n"


async def test_native_unicode_before_and_inside_inline_source(full_host_workspace: Path) -> None:
    command = "# 中文 😀\n" + _python("-c", 'print("中文 😀")')

    result = await _exec(full_host_workspace, command)

    assert result == "exit_code=0\n中文 😀\n"


@pytest.mark.parametrize(
    "comment",
    ['"' + "comment " * 2250, '"' + "a" * 29500, '"' * 9000],
    ids=["long-source", "near-outer-command-limit", "many-escaped-quotes"],
)
async def test_native_long_inline_source_stays_within_process_command_limit(
    full_host_workspace: Path, comment: str,
) -> None:
    # These are already valid native commands. Protecting a quote in a comment
    # must not expand the outer PowerShell or child Python command past 32K;
    # near the limits, preserving the original command is sufficient.
    source = "print(42)\n# " + comment

    result = await _exec(full_host_workspace, _python("-c", source))

    assert result == "exit_code=0\n42\n"


@pytest.mark.parametrize("invocation", [["argv_fixture.py"], ["-m", "argv_fixture"]])
async def test_native_script_and_module_c_arguments_are_not_interpreter_source(
    full_host_workspace: Path, invocation: list[str]
) -> None:
    (full_host_workspace / "argv_fixture.py").write_text(
        "import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8"
    )
    # A script's ordinary argv still follows the selected shell's native quoting
    # rules: Windows PowerShell 5 needs a backslash before a literal double quote.
    argument = r'print(\"x\")' if os.name == "nt" else 'print("x")'

    result = await _exec(full_host_workspace, _python(*invocation, "-c", argument))

    status, output = result.split("\n", 1)
    assert status == "exit_code=0", result
    assert json.loads(output) == ["-c", 'print("x")']


@pytest.mark.skipif(os.name != "nt", reason="PowerShell function and alias precedence")
@pytest.mark.parametrize("alias", [False, True], ids=["function", "alias"])
async def test_native_powershell_python_shadow_keeps_literal_argument(
    full_host_workspace: Path, alias: bool
) -> None:
    definition = (
        "function Invoke-TestPython { Write-Output $args[1] }; "
        "Set-Alias python Invoke-TestPython; "
        if alias
        else "function python { Write-Output $args[1] }; "
    )

    result = await _exec(
        full_host_workspace, definition + _python("-c", 'print("x")', bare=True)
    )

    assert result == 'exit_code=0\nprint("x")\n'


@pytest.mark.skipif(os.name != "nt", reason="PowerShell user scope must retain its own variables")
@pytest.mark.parametrize("alias", [False, True], ids=["function", "alias"])
async def test_native_powershell_python_shadow_does_not_expose_parser_variables(
    full_host_workspace: Path, alias: bool
) -> None:
    target = "Invoke-TestPython" if alias else "python"
    command = (
        f"function {target} {{ "
        "if (Get-Variable -Name name -ErrorAction SilentlyContinue) { "
        "Write-Output ('VALUE:' + $name) } else { Write-Output 'UNDEFINED' } }; "
    )
    if alias:
        command += "Set-Alias python Invoke-TestPython; "
    command += _python("-c", 'print("x")', bare=True)
    command += "; $name = 'USER_VALUE'; " + _python("-c", 'print("x")', bare=True)

    result = await _exec(full_host_workspace, command)

    assert result == "exit_code=0\nUNDEFINED\nVALUE:USER_VALUE\n"


async def test_native_semicolon_executes_following_command(full_host_workspace: Path) -> None:
    command = _python("-c", 'print("123")') + "; " + _python("-c", 'print("456")')

    result = await _exec(full_host_workspace, command)

    assert result == "exit_code=0\n123\n456\n"


async def test_native_pipeline_passes_python_output(full_host_workspace: Path) -> None:
    consumer = (
        "ForEach-Object { [int]$_ + 1 }"
        if os.name == "nt"
        else _python("-c", "import sys; print(int(sys.stdin.read()) + 1)")
    )
    command = _python("-c", 'print("123")') + " | " + consumer

    result = await _exec(full_host_workspace, command)

    assert result == "exit_code=0\n124\n"


async def test_native_python_reads_program_from_stdin(full_host_workspace: Path) -> None:
    result = await _exec(
        full_host_workspace,
        _python("-"),
        stdin='from pathlib import Path\nprint(Path("README.md").name)\n',
    )

    assert result == "exit_code=0\nREADME.md\n"


@pytest.mark.skipif(os.name != "nt", reason="PowerShell pipeline input enumeration")
async def test_native_powershell_input_pipeline_preserves_stdin(full_host_workspace: Path) -> None:
    # The quote is in a Python comment, so the baseline command is already valid.
    # Ignore PowerShell 5's UTF-8 BOM when reading its native pipeline output.
    source = 'import sys; sys.stdout.write(sys.stdin.read().lstrip(chr(65279))) # "quote"'
    command = "$input | " + _python("-c", source)

    result = await _exec(full_host_workspace, command, stdin="LINE_A\nLINE_B\n")

    assert result == "exit_code=0\nLINE_A\nLINE_B\n"


async def test_native_large_stderr_does_not_block_stdout(full_host_workspace: Path) -> None:
    result = await _exec(
        full_host_workspace,
        _python(
            "-c",
            'import sys; print("START", flush=True); '
            'sys.stderr.write("E" * 200000); sys.stderr.flush(); print("DONE", flush=True)',
        ),
    )

    assert result.startswith("exit_code=0\n"), result[:200]
    assert "START" in result
    assert "DONE" in result


@pytest.mark.skipif(os.name != "nt", reason="Windows must preserve an explicitly selected pwsh")
@pytest.mark.parametrize("bare", [False, True], ids=["absolute-pwsh", "bare-pwsh"])
async def test_native_explicit_powershell_7_is_not_replaced(
    full_host_workspace: Path, bare: bool
) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is not installed")
    command = (
        ("pwsh" if bare else "& " + _quote(pwsh)) + " -NoLogo -NoProfile -Command "
        + _quote("Write-Output (1 ?? 2); Write-Output $PSVersionTable.PSVersion.Major")
    )

    result = await _exec(full_host_workspace, command)

    lines = result.splitlines()
    assert lines[:2] == ["exit_code=0", "1"], result
    assert int(lines[2]) >= 7, result


async def _start_background(workspace: Path, command: str) -> str:
    started = await shell.background_process(command, workdir=str(workspace), timeout=30)
    first_line = started.splitlines()[0]
    assert first_line.startswith("session_id="), started
    return first_line.partition("=")[2]


async def _wait_for_live_output(session_id: str, marker: str) -> None:
    deadline = asyncio.get_running_loop().time() + 15
    while True:
        log = json.loads(await shell.process("log", session_id=session_id))
        if marker in log["output"].splitlines():
            assert log["session"]["returncode"] is None, log
            return
        assert asyncio.get_running_loop().time() < deadline, log
        assert log["session"]["returncode"] is None, log
        await asyncio.sleep(0.05)


async def _remove_background(session_id: str) -> None:
    await shell.process("kill", session_id=session_id)
    await shell.process("remove", session_id=session_id)


async def test_native_background_streams_output_accepts_stdin_and_reports_exit(
    full_host_workspace: Path,
) -> None:
    session_id = await _start_background(
        full_host_workspace,
        _python(
            "-u", "-c",
            'import sys; print("READY", flush=True); '
            'print("RECEIVED:" + sys.stdin.readline().strip(), flush=True); raise SystemExit(7)',
        ),
    )
    try:
        # READY must be observable before stdin releases the child process.
        await _wait_for_live_output(session_id, "READY")
        await shell.process("submit", session_id=session_id, data="release")
        waited = json.loads(await shell.process("wait", session_id=session_id, timeout=15))
        log = json.loads(await shell.process("log", session_id=session_id))

        assert waited["exited"] is True, waited
        assert waited["session"]["returncode"] in {1, 7}, waited
        assert waited["session"]["timed_out"] is False, waited
        assert "RECEIVED:release" in log["output"], log
    finally:
        await _remove_background(session_id)


async def test_native_background_kill_stops_running_child(full_host_workspace: Path) -> None:
    session_id = await _start_background(
        full_host_workspace,
        _python("-u", "-c", 'import time; print("READY", flush=True); time.sleep(60)'),
    )
    try:
        await _wait_for_live_output(session_id, "READY")
        await shell.process("kill", session_id=session_id)
        waited = json.loads(await shell.process("wait", session_id=session_id, timeout=5))

        assert waited["exited"] is True, waited
        assert waited["session"]["returncode"] is not None, waited
        assert waited["session"]["timed_out"] is False, waited
    finally:
        await _remove_background(session_id)
