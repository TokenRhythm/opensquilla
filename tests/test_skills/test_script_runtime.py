from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from opensquilla.skills.host import runtime_digest
from opensquilla.skills.script_runtime import (
    SkillScriptError,
    SkillScriptGrant,
    SkillScriptRunner,
    check_runtime_files,
    private_inventory,
    runtime_files_digest,
)
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


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Linux bubblewrap required")
async def test_stdin_is_bounded_and_eof_is_closed(tmp_path: Path) -> None:
    runner, _ = setup(tmp_path, "import sys\nprint(sys.stdin.buffer.read().decode())")
    result = await runner.run("demo", "scripts/main.py", [], stdin=b'{"key":"value"}')
    assert result.stdout.strip() == '{"key":"value"}'
    assert (await runner.run("demo", "scripts/main.py", [])).stdout.strip() == ""
    runner.max_input_bytes = 4
    with pytest.raises(SkillScriptError, match="input"):
        await runner.run("demo", "scripts/main.py", [], stdin=b"12345")


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Linux bubblewrap required")
async def test_cancelled_script_cannot_continue_writing(tmp_path: Path) -> None:
    runner, _ = setup(
        tmp_path,
        "import time\nfrom pathlib import Path\n"
        "Path('/work/started').touch()\ntime.sleep(2)\nPath('/work/late').touch()",
    )
    task = asyncio.create_task(runner.run("demo", "scripts/main.py", []))
    for _ in range(100):
        if (runner.workspace / "started").exists():
            break
        await asyncio.sleep(0.01)
    assert (runner.workspace / "started").exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(2.1)
    assert not (runner.workspace / "late").exists()


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Linux bubblewrap required")
async def test_host_profile_mounts_are_separate_and_readonly_for_validation(tmp_path: Path) -> None:
    source = """import os,json
from pathlib import Path
result={'config':Path(os.environ['DEMO_HOST_CONFIG']).read_text(),
        'receipt_visible':Path('/receipts/saved.json').exists()}
for root in ('private','media','work'):
 try: Path('/'+root+'/written').touch(); result[root]=True
 except OSError: result[root]=False
print(json.dumps(result))
"""
    old, _ = setup(tmp_path, source)
    private, media, receipts = (tmp_path / name for name in ("private", "media", "receipts"))
    for directory in (private, media, receipts):
        directory.mkdir()
    (receipts / "saved.json").write_text("{}")
    config = tmp_path / "config.toml"
    config.write_text("host-only-config")
    runner = SkillScriptRunner(
        grants=tuple(old.grants.values()),
        workspace=old.workspace,
        inputs=old.inputs,
        execution_id="run-test",
        private_root=private,
        media_root=media,
        receipt_root=receipts,
        caller_binding="task-owner",
        config_file=config,
        config_environment_variable="DEMO_HOST_CONFIG",
    )
    result = await runner.run("demo", "scripts/main.py", [])
    assert json.loads(result.stdout) == {
        "config": "host-only-config",
        "receipt_visible": False,
        "private": True,
        "media": True,
        "work": True,
    }
    readonly = await runner.run("demo", "scripts/main.py", [], readonly=True)
    assert json.loads(readonly.stdout) == {
        "config": "host-only-config",
        "receipt_visible": True,
        "private": False,
        "media": False,
        "work": False,
    }
    proofs = list((receipts / "invocations").glob("*.json"))
    assert len(proofs) == 1
    assert json.loads(proofs[0].read_text())["privateDigests"] == private_inventory(private)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_private_inventory_rejects_linked_or_special_state(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "private"
    root.mkdir()
    source = tmp_path / "other"
    source.write_text("outside")
    path = root / "state"
    if kind == "symlink":
        path.symlink_to(source)
    elif kind == "hardlink":
        path.hardlink_to(source)
    else:
        os.mkfifo(path)
    with pytest.raises(SkillScriptError, match="regular"):
        private_inventory(root)


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Linux bubblewrap required")
@pytest.mark.parametrize("mode", ["business_error", "timeout"])
async def test_invocation_proof_distinguishes_completed_errors_and_timeout(
    tmp_path: Path,
    mode: str,
) -> None:
    runner, _ = setup(
        tmp_path,
        "import sys,time\nfrom pathlib import Path\n"
        "Path('/private/state').write_text('committed')\n"
        "time.sleep(10) if sys.argv[1]=='timeout' else None\n"
        "print('business error')\nsys.exit(1)",
    )
    private, receipts = tmp_path / "private", tmp_path / "receipts"
    private.mkdir()
    receipts.mkdir()
    runner = SkillScriptRunner(
        grants=tuple(runner.grants.values()),
        workspace=runner.workspace,
        inputs=runner.inputs,
        execution_id="run-test",
        caller_binding="caller",
        private_root=private,
        receipt_root=receipts,
        timeout_seconds=0.2,
    )
    if mode == "timeout":
        with pytest.raises(TimeoutError):
            await runner.run("demo", "scripts/main.py", [mode])
    else:
        assert (await runner.run("demo", "scripts/main.py", [mode])).returncode == 1
    proof_files = list((receipts / "invocations").glob("*.json"))
    assert len(proof_files) == 1
    proof = json.loads(proof_files[0].read_text())
    assert proof["callerBinding"] == "caller"
    if mode == "timeout":
        assert proof["returncode"] is None and proof["privateDigests"] is None
    else:
        assert proof["returncode"] == 1 and proof["privateDigests"] == private_inventory(private)


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Linux bubblewrap required")
async def test_tool_preserves_nonzero_business_error_stdout(tmp_path: Path) -> None:
    runner, _ = setup(
        tmp_path,
        "import json,sys\nvalue=json.load(sys.stdin)\n"
        "print(json.dumps({'isError':True,'details':value}))\nsys.exit(1)",
    )
    context = ToolContext(
        workspace_dir=str(runner.workspace),
        execution_id=runner.execution_id,
        skill_script_runner=runner,
    )
    token = current_tool_context.set(context)
    try:
        result = json.loads(
            await run_skill_script("demo", "scripts/main.py", [], input={"code": "STALE_REVISION"})
        )
    finally:
        current_tool_context.reset(token)
    assert result["isError"] is True and result["exitCode"] == 1
    assert json.loads(result["stdout"])["details"] == {"code": "STALE_REVISION"}


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Linux bubblewrap required")
async def test_fixed_runtime_is_readonly_and_mutation_revokes_execution(tmp_path: Path) -> None:
    old, _ = setup(
        tmp_path,
        "import sys\nfrom pathlib import Path\nprint(sys.prefix)\n"
        "try: Path('/runtime/marker').write_text('changed')\n"
        "except OSError: print('readonly')",
    )
    runtime = tmp_path / "runtime"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin/python").symlink_to("/usr/bin/python3")
    (runtime / "pyvenv.cfg").write_text("home = /usr/bin\ninclude-system-site-packages = false\n")
    (runtime / "marker").write_text("pinned")
    runner = SkillScriptRunner(
        grants=tuple(old.grants.values()),
        workspace=old.workspace,
        inputs=old.inputs,
        execution_id="run-test",
        runtime_root=runtime,
        runtime_sha256=runtime_digest(runtime),
    )
    result = await runner.run("demo", "scripts/main.py", [])
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["/runtime", "readonly"]
    (runtime / "marker").write_text("external mutation")
    for readonly in (False, True):
        with pytest.raises(SkillScriptError, match="runtime changed"):
            await runner.run("demo", "scripts/main.py", [], readonly=readonly)


async def test_non_json_tool_input_is_a_safe_error(tmp_path: Path) -> None:
    runner, _ = setup(tmp_path, "print('not called')")
    context = ToolContext(
        workspace_dir=str(runner.workspace),
        execution_id=runner.execution_id,
        skill_script_runner=runner,
    )
    token = current_tool_context.set(context)
    try:
        with pytest.raises(SafeToolError):
            await run_skill_script("demo", "scripts/main.py", [], input={"value": float("nan")})
    finally:
        current_tool_context.reset(token)


def test_external_runtime_pins_read_actual_font_and_font_configuration() -> None:
    paths = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/etc/fonts/fonts.conf"]
    if not all(Path(path).is_file() for path in paths):
        pytest.skip("Linux system font fixture required")
    pins = runtime_files_digest(paths)
    assert set(pins) == set(paths)
    assert all(len(digest) == 64 for digest in pins.values())
    check_runtime_files(pins)
    assert runtime_files_digest([]) == {}


def test_external_runtime_accepts_fontconfig_link_inside_readonly_font_root() -> None:
    path = Path("/etc/fonts/conf.d/57-dejavu-sans.conf")
    if not path.is_symlink() or not path.resolve().is_relative_to("/etc/fonts"):
        pytest.skip("Same-root Linux fontconfig symlink fixture required")
    pins = runtime_files_digest([str(path)])
    assert pins[str(path)] == runtime_files_digest([str(path.resolve())])[str(path.resolve())]
    check_runtime_files(pins)


@pytest.mark.parametrize("path", ["/usr/share/fonts", "/etc/fonts/conf.d"])
def test_external_runtime_directories_are_not_file_dependencies(path: str) -> None:
    with pytest.raises(SkillScriptError, match="regular"):
        runtime_files_digest([path])


@pytest.mark.parametrize("path", ["relative.ttf", "/etc/passwd", "/usr/../etc/passwd"])
def test_external_runtime_rejects_outside_paths(path: str) -> None:
    with pytest.raises(SkillScriptError, match="under /usr"):
        runtime_files_digest([path])


def test_external_runtime_file_count_is_bounded() -> None:
    path = "/etc/fonts/fonts.conf"
    if not Path(path).is_file():
        pytest.skip("Linux font configuration fixture required")
    with pytest.raises(SkillScriptError, match="list exceeds"):
        runtime_files_digest([path] * 129)


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Linux bubblewrap required")
@pytest.mark.parametrize("mutation", ["missing", "sha256"])
async def test_missing_or_changed_external_dependency_never_starts_script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    old, _ = setup(tmp_path, "print('must not execute')")
    path = (
        "/usr/share/fonts/.opensquilla-m1-missing-test"
        if mutation == "missing"
        else "/etc/fonts/fonts.conf"
    )
    if mutation == "sha256" and not Path(path).is_file():
        pytest.skip("Linux font configuration fixture required")
    runner = SkillScriptRunner(
        grants=tuple(old.grants.values()),
        workspace=old.workspace,
        inputs=old.inputs,
        execution_id="run-test",
        runtime_files={path: "0" * 64},
    )
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    for readonly in (False, True):
        with pytest.raises(SkillScriptError, match="External runtime"):
            await runner.run("demo", "scripts/main.py", [], readonly=readonly)
    spawn.assert_not_awaited()


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Linux bubblewrap required")
async def test_external_runtime_dependencies_are_checked_after_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _ = setup(tmp_path, "print('normal result')")
    calls = 0

    def changed_after_execution(_pins: dict[str, str]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SkillScriptError("External runtime files changed after the host grant")

    monkeypatch.setattr(
        "opensquilla.skills.script_runtime.check_runtime_files", changed_after_execution
    )
    with pytest.raises(SkillScriptError, match="External runtime files changed"):
        await runner.run("demo", "scripts/main.py", [])
    assert calls == 2


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
