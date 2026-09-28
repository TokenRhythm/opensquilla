from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from opensquilla.skills.script_runtime import SkillScriptError, SkillScriptGrant, SkillScriptRunner
from opensquilla.tools.builtin.skill_scripts import run_skill_script
from opensquilla.tools.types import SafeToolError, ToolContext, current_tool_context


def setup(tmp_path: Path, source: str) -> tuple[SkillScriptRunner, Path]:
    skill = tmp_path / "installed/demo"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: test\n---\n")
    (skill / "scripts/main.py").write_text(source)
    work, inputs = tmp_path / "work", tmp_path / "inputs"
    work.mkdir()
    inputs.mkdir()
    (inputs / "receipts.json").write_text("{}")
    runner = SkillScriptRunner(
        grants=(SkillScriptGrant.pin("demo", skill, frozenset({"scripts/main.py"})),),
        workspace=work,
        inputs=inputs,
        execution_id="run-test",
    )
    return runner, skill


@pytest.mark.parametrize("script", ["../escape.py", "/usr/bin/python3", "SKILL.md"])
def test_grants_reject_non_entrypoints(tmp_path: Path, script: str) -> None:
    _, skill = setup(tmp_path, "print('ok')")
    with pytest.raises(SkillScriptError):
        SkillScriptGrant.pin("demo", skill, frozenset({script}))


def test_grant_rejects_symlink_helper(tmp_path: Path) -> None:
    _, skill = setup(tmp_path, "print('ok')")
    (skill / "helper.py").symlink_to("/etc/passwd")
    with pytest.raises(SkillScriptError):
        SkillScriptGrant.pin("demo", skill, frozenset({"scripts/main.py"}))


async def test_changed_installation_is_not_executed(tmp_path: Path) -> None:
    runner, skill = setup(tmp_path, "print('ok')")
    (skill / "scripts/main.py").write_text("print('changed')")
    with pytest.raises(SkillScriptError, match="changed"):
        await runner.run("demo", "scripts/main.py", [])


async def test_ungranted_script_or_skill_is_denied(tmp_path: Path) -> None:
    runner, _ = setup(tmp_path, "print('ok')")
    with pytest.raises(SkillScriptError):
        await runner.run("other", "scripts/main.py", [])
    with pytest.raises(SkillScriptError):
        await runner.run("demo", "scripts/other.py", [])


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Linux bubblewrap required")
async def test_real_script_has_only_granted_inputs_and_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = tmp_path / "host-secret"
    private.write_text("private sentinel")
    monkeypatch.setenv("T0_PRIVATE_SENTINEL", "must-not-leak")
    source = (
        "import os,json,socket\nfrom pathlib import Path\n"
        f"result={{'secret_exists':Path({str(private)!r}).exists(),'secret_env':os.getenv('T0_PRIVATE_SENTINEL')}}\n"
        "paths=[('input_write','/inputs/receipts.json'),('skill_write','/skill/SKILL.md')]\n"
        "for key,path in paths:\n"
        " try: Path(path).write_text('bad'); result[key]=True\n"
        " except OSError: result[key]=False\n"
        "Path('/work/result.json').write_text(json.dumps(result))\nprint(json.dumps(result))\n"
    )
    runner, _ = setup(tmp_path, source)
    result = await runner.run("demo", "scripts/main.py", [])
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "secret_exists": False,
        "secret_env": None,
        "input_write": False,
        "skill_write": False,
    }
    assert (runner.workspace / "result.json").exists()
    assert (runner.inputs / "receipts.json").read_text() == "{}"


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Linux bubblewrap required")
async def test_validation_mount_makes_workspace_readonly(tmp_path: Path) -> None:
    runner, _ = setup(tmp_path, "from pathlib import Path\nPath('/work/mutated').write_text('bad')")
    result = await runner.run("demo", "scripts/main.py", [], readonly=True)
    assert result.returncode != 0
    assert not (runner.workspace / "mutated").exists()


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Linux bubblewrap required")
async def test_script_output_is_bounded(tmp_path: Path) -> None:
    runner, _ = setup(tmp_path, "print('x' * 20000)")
    runner.max_output_bytes = 1024
    with pytest.raises(SkillScriptError, match="output"):
        await runner.run("demo", "scripts/main.py", [])


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Linux bubblewrap required")
async def test_script_timeout_kills_process(tmp_path: Path) -> None:
    runner, _ = setup(tmp_path, "import time\ntime.sleep(10)")
    runner.timeout_seconds = 0.1
    with pytest.raises(TimeoutError):
        await runner.run("demo", "scripts/main.py", [])


def test_overlapping_mounts_are_rejected(tmp_path: Path) -> None:
    runner, _ = setup(tmp_path, "print('ok')")
    with pytest.raises(SkillScriptError, match="disjoint"):
        SkillScriptRunner(
            grants=tuple(runner.grants.values()),
            workspace=runner.workspace,
            inputs=runner.workspace,
            execution_id="run-test",
        )


def test_arguments_are_not_shell_interpreted(tmp_path: Path) -> None:
    runner, _ = setup(tmp_path, "print('ok')")
    grant = runner.grants["demo"]
    value = "$(touch /work/not-run); $HOME"
    command = runner.command(grant, "scripts/main.py", [value], readonly=False)
    assert command[-1] == value
    assert "/bin/sh" not in command
    assert "--clearenv" in command
    assert os.environ.get("T0_PRIVATE_SENTINEL") is None


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Linux bubblewrap required")
async def test_stdout_and_stderr_share_one_output_budget(tmp_path: Path) -> None:
    runner, _ = setup(tmp_path, "import sys\nprint('x' * 800)\nprint('y' * 800, file=sys.stderr)")
    runner.max_output_bytes = 1024
    with pytest.raises(SkillScriptError, match="output"):
        await runner.run("demo", "scripts/main.py", [])


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Linux bubblewrap required")
async def test_closed_streams_do_not_restart_execution_deadline(tmp_path: Path) -> None:
    runner, _ = setup(
        tmp_path,
        "import os,time\ntime.sleep(0.6)\nos.close(1)\nos.close(2)\ntime.sleep(10)",
    )
    runner.timeout_seconds = 1
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await runner.run("demo", "scripts/main.py", [])
    assert time.monotonic() - started < 1.4


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Linux bubblewrap required")
async def test_script_cannot_reach_host_loopback(tmp_path: Path) -> None:
    import socket

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        runner, _ = setup(
            tmp_path,
            "import socket\n"
            f"address=('127.0.0.1',{port})\n"
            "try:\n socket.create_connection(address,timeout=0.2); print('reachable')\n"
            "except OSError:\n print('isolated')\n",
        )
        result = await runner.run("demo", "scripts/main.py", [])
    assert result.returncode == 0
    assert result.stdout.strip() == "isolated"


@pytest.mark.parametrize("mismatch", ["no_context", "no_runner", "execution", "workspace"])
async def test_script_tool_requires_matching_host_execution_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    runner, _ = setup(tmp_path, "print('ok')")
    run = AsyncMock()
    monkeypatch.setattr(runner, "run", run)
    context = ToolContext(
        workspace_dir=str(runner.workspace),
        execution_id=runner.execution_id,
        skill_script_runner=runner,
    )
    if mismatch == "no_runner":
        context.skill_script_runner = None
    elif mismatch == "execution":
        context.execution_id = "another-run"
    elif mismatch == "workspace":
        context.workspace_dir = str(tmp_path)
    token = current_tool_context.set(None if mismatch == "no_context" else context)
    try:
        with pytest.raises(SafeToolError, match="matching host"):
            await run_skill_script("demo", "scripts/main.py", [])
    finally:
        current_tool_context.reset(token)
    run.assert_not_awaited()


@pytest.mark.parametrize("arguments", [["bad\0argument"], ["x"] * 65, ["x" * 16_385], [1]])
def test_invalid_script_arguments_are_denied_before_execution(
    tmp_path: Path, arguments: list
) -> None:
    runner, _ = setup(tmp_path, "print('ok')")
    with pytest.raises(SkillScriptError, match="arguments"):
        runner.command(runner.grants["demo"], "scripts/main.py", arguments, readonly=False)
