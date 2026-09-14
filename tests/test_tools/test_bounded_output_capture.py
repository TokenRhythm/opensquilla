"""Output limits protect collection, without stopping useful process work."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from opensquilla.engine.tool_result_store import (
    DEFAULT_TOOL_RESULT_DISK_BUDGET_BYTES,
    DEFAULT_TOOL_RESULT_MAX_BYTES,
    ToolResultStore,
    ToolResultStoreBudgetError,
)
from opensquilla.tools.builtin import shell
from opensquilla.tools.output_capture import OUTPUT_PREVIEW_BYTES, BoundedOutputCapture
from opensquilla.tools.types import ToolContext, current_tool_context


def _spool(store: ToolResultStore):
    return store.open_output_spool(
        tool_name="exec", session_id="test-session", session_key="test-session",
        agent_id="main",
    )


def _write(store: ToolResultStore, text: str, *, budget: int):
    return store.write(
        text, tool_use_id="test-call", tool_name="test", session_id="test-session",
        session_key="test-session", agent_id="main", disk_budget_bytes=budget,
    )


def _retained_output(store: ToolResultStore):
    records = store._iter_output_stats()
    assert len(records) == 1
    return store.read(records[0].record_dir.name, session_id="test-session")


def test_execution_logs_do_not_share_snapshot_budget(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path)
    spool = _spool(store)
    try:
        spool.append(b"retained")
        os.utime(spool.record_dir / "output.spool", (1, 1))
        record = _write(store, "x" * 1100, budget=2048)
        assert spool.record_dir.exists()
        assert store.read(record.handle, session_id="test-session").content == "x" * 1100
        handle = spool.finish()
        _write(store, "y" * 1900, budget=2048)
        assert store.read(handle, session_id="test-session").content == "retained"
    finally:
        spool.close()


def test_active_spool_is_protected_across_processes(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path)
    spool = _spool(store)
    try:
        code = (
            "from opensquilla.engine.tool_result_store import (\n"
            " ToolResultStore, ToolResultStoreBudgetError)\n"
            f"store=ToolResultStore({str(tmp_path)!r})\n"
            "try:\n"
            " store.write('x'*1100, tool_use_id='x', tool_name='test', session_id='test-session', "
            "session_key='test-session', agent_id='main', disk_budget_bytes=2048)\n"
            "except ToolResultStoreBudgetError:\n print('unexpected snapshot failure')\n"
            "other = store.open_output_spool(tool_name='exec', session_id='test-session', "
            "session_key='test-session', agent_id='main', retention_seconds=0)\n"
            "other.close()\nprint('active output protected')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=10,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "active output protected"
        assert spool.record_dir.exists()
    finally:
        spool.close()


async def test_capture_preserves_complete_log_past_snapshot_cap(tmp_path: Path) -> None:
    token = current_tool_context.set(ToolContext(
        session_key="test-session", tool_result_store_dir=str(tmp_path), agent_id="main",
    ))
    try:
        capture = await BoundedOutputCapture.create("exec")
    finally:
        current_tool_context.reset(token)
    assert capture.spool is not None
    assert DEFAULT_TOOL_RESULT_DISK_BUDGET_BYTES == 256 * 1024 * 1024
    try:
        capture.feed(b"first diagnostic\n")
        for _ in range(160):
            capture.feed(b"x" * 65536)
        capture.feed(b"\nLATEST EXIT DIAGNOSTIC\n")
        view = capture.previews["stdout"]
        assert len(view.head) + len(view.tail) <= OUTPUT_PREVIEW_BYTES
        assert "LATEST EXIT DIAGNOSTIC" in capture.preview()
        await capture.finish_async()
        assert capture.handle
        store = ToolResultStore(tmp_path)
        meta = store.read_output_metadata(capture.handle, session_id="test-session")
        assert meta["size_bytes"] > DEFAULT_TOOL_RESULT_MAX_BYTES
        content = "".join(store.iter_text_chunks(capture.handle, session_id="test-session"))
        assert content == (
            "first diagnostic\n" + "x" * (160 * 65536) + "\nLATEST EXIT DIAGNOSTIC\n"
        )
        with pytest.raises(ToolResultStoreBudgetError, match="streaming"):
            store.read(capture.handle, session_id="test-session")
        assert capture.describe()["retained_output_complete"] is True
        assert "not a full log" not in capture.notice()
    finally:
        capture.spool.close()


async def test_disk_write_failure_keeps_draining_and_reports_retained_fragments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = BoundedOutputCapture(preview_bytes=256)
    capture.spool = _spool(ToolResultStore(tmp_path))
    capture.feed(b"before disk full\n")

    def disk_full(chunk: bytes) -> None:
        raise OSError(28, "synthetic disk full")

    monkeypatch.setattr(capture.spool, "append", disk_full)
    reader = asyncio.StreamReader()
    reader.feed_data(b"x" * 65536 + b"last failure diagnostic")
    reader.feed_eof()
    await capture.drain(reader)
    await capture.finish_async()
    assert capture.storage_error == "OSError"
    assert "last failure diagnostic" in capture.preview()
    assert capture.handle
    record = ToolResultStore(tmp_path).read(capture.handle, session_id="test-session")
    assert "before disk full" in record.content
    assert record.content == "before disk full\n"
    assert "last failure diagnostic" in capture.preview()
    assert "not a full log" in capture.notice()


def test_split_utf8_is_decoded_after_bounded_capture() -> None:
    capture = BoundedOutputCapture(preview_bytes=32)
    encoded = "alpha你好omega".encode()
    for byte in encoded:
        capture.feed(bytes([byte]))
    assert capture.preview() == "alpha你好omega"


@pytest.mark.parametrize("streams", [("stdout",), ("stdout", "stderr")])
async def test_retained_capture_preserves_process_newlines(
    tmp_path: Path, streams: tuple[str, ...],
) -> None:
    capture = BoundedOutputCapture(streams=streams)
    capture.spool = _spool(ToolResultStore(tmp_path))
    content = "progress 50%\rprogress 100%\r\ncomplete\n"
    for stream in streams:
        capture.feed(content.encode("utf-8"), stream)
    await capture.finish_async()

    assert capture.handle
    store = ToolResultStore(tmp_path)
    retained = store.read(capture.handle, session_id="test-session")
    expected = (
        "".join(f"\n[{stream}]\n{content}" for stream in streams)
        if len(streams) > 1 else content
    )
    assert retained.content == expected
    assert store.read_output_preview(
        capture.handle, session_id="test-session", max_bytes=1024,
    ) == expected
    assert capture.describe()["retained_output_complete"] is True


@pytest.mark.parametrize("output_size", [0, 30000])
async def test_exec_timeout_preserves_output_and_retrievable_retained_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output_size: int,
) -> None:
    token = current_tool_context.set(ToolContext(
        session_key="test-session", tool_result_store_dir=str(tmp_path), agent_id="main",
    ))
    code = (
        "import sys,time; "
        f"sys.stdout.buffer.write(b'x'*{output_size} + b'before waiting\\r\\n'); "
        "sys.stdout.flush(); sys.stderr.buffer.write(b'ready\\n'); "
        "sys.stderr.flush(); time.sleep(20)"
    )
    argv = [sys.executable, "-c", code]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    process_options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt" else {"start_new_session": True}
    )
    proc = None
    try:
        # Start the real child before timing its blocked work. Windows shell
        # and interpreter startup can consume the entire 0.5-second budget.
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **process_options,
        )
        assert proc.stderr is not None
        assert await asyncio.wait_for(proc.stderr.readline(), timeout=10) == b"ready\n"

        async def ready_process(_command: str, **_kwargs):
            return proc

        monkeypatch.setattr(shell, "_create_host_shell_subprocess", ready_process)
        result = await shell._run_host_shell_command(
            command, cwd=None, env=dict(os.environ), stdin_bytes=None, effective_timeout=0.5,
        )
        assert proc.returncode is not None
    finally:
        try:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
        finally:
            current_tool_context.reset(token)
    assert "[timeout after 0.5s]" in result
    # The command itself contains this text, so only inspect captured output.
    assert "before waiting" in result.split("--- partial output before timeout ---\n", 1)[1]
    stored = _retained_output(ToolResultStore(tmp_path))
    assert stored.content == "x" * output_size + "before waiting\r\n"
    if output_size:
        assert f"tool_result_handle={stored.handle}" in result
        assert "partial output truncated" in result
    else:
        assert "output capture" not in result


async def test_background_log_and_wait_retain_output_after_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = current_tool_context.set(ToolContext(
        session_key="test-session", tool_result_store_dir=str(tmp_path), agent_id="main",
        task_id="test-task",
    ))
    code = (
        "import sys; print('start'); sys.stdout.write('x'*2000000); "
        "print('last error'); sys.exit(7)"
    )
    argv = [sys.executable, "-c", code]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    # Exercise real pipe backpressure and the production collector/wait path.
    # Process-tree ownership has separate platform integration tests; use a
    # direct subprocess seam here so this test owns exactly the one child.
    async def create_process(_command: str, **kwargs):
        kwargs.pop("windows_host", None)
        return await asyncio.create_subprocess_exec(*argv, **kwargs)

    monkeypatch.setattr(shell, "_create_host_shell_subprocess", create_process)
    session_id = None
    try:
        result = await shell._start_host_background_process(
            command, cwd=None, effective_timeout=10, runtime=None,
        )
        session_id = result.splitlines()[0].split("=", 1)[1]
        payload = json.loads(await shell.process("wait", session_id=session_id, timeout=10))
        assert payload["exited"] is True
        assert payload["session"]["returncode"] == 7
        capture = shell._bg_sessions[session_id].output_capture
        assert "last error" in capture.preview()
        assert all(not view.head and not view.tail for view in capture.previews.values())
        restored = await capture.preview_async()
        assert "start" in restored and "last error" in restored
        log_payload = json.loads(await shell.process(
            "log", session_id=session_id, offset=2_000_000 - 50, limit=100,
        ))
        assert "last error" in log_payload["output"]
        handle = payload["session"]["output_capture"]["tool_result_handle"]
        assert "last error" in ToolResultStore(tmp_path).read(
            handle, session_id="test-session",
        ).content
    finally:
        if session_id:
            await shell.process("remove", session_id=session_id)
        current_tool_context.reset(token)


def test_execute_code_declares_budget_including_cleanup() -> None:
    from opensquilla.tools.builtin import code_exec
    from opensquilla.tools.registry import get_default_registry

    spec = get_default_registry().get("execute_code").spec
    required_padding = (
        shell._EXEC_TERMINATE_TIMEOUT + shell._EXEC_KILL_TIMEOUT
        + shell._BACKGROUND_KILL_TIMEOUT
    )
    assert spec.execution_timeout_seconds >= code_exec._DEFAULT_TIMEOUT + required_padding
    assert spec.execution_timeout_argument == "timeout"
    assert spec.execution_timeout_padding >= required_padding
    shell_spec = get_default_registry().get("exec_command").spec
    assert shell_spec.execution_timeout_padding >= (
        shell._APPROVAL_RETRY_WAIT_SECONDS + required_padding
    )


@pytest.mark.parametrize("cancel", [False, True])
async def test_capture_setup_owns_lease_until_open_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    import threading

    entered, release = threading.Event(), threading.Event()
    spools = []
    original_open = ToolResultStore.open_output_spool

    def stalled_open(store, **kwargs):
        spool = original_open(store, **kwargs)
        spools.append(spool)
        entered.set()
        assert release.wait(timeout=5)
        return spool

    monkeypatch.setattr(ToolResultStore, "open_output_spool", stalled_open)
    token = current_tool_context.set(ToolContext(
        session_key="test-session", tool_result_store_dir=str(tmp_path), agent_id="main",
    ))
    try:
        task = asyncio.create_task(BoundedOutputCapture.create("exec"))
    finally:
        current_tool_context.reset(token)
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        if cancel:
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        assert not spools[0].lease.closed
        assert ToolResultStore(tmp_path)._iter_output_stats()[0].active
    finally:
        release.set()
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    else:
        capture = await asyncio.wait_for(task, timeout=2)
        assert capture.spool is spools[0]
        assert capture.storage_error is None
        capture.feed(b"command completed after slow setup\n")
        await capture.finish_async()
        assert capture.handle
        assert "command completed" in capture.preview()
    assert spools[0].lease.closed


async def test_snapshot_budget_overrides_do_not_truncate_execution_logs(tmp_path: Path) -> None:
    token = current_tool_context.set(ToolContext(
        session_key="test-session", tool_result_store_dir=str(tmp_path),
        tool_result_store_max_bytes=128, tool_result_store_disk_budget_bytes=128,
        tool_result_store_retention_seconds=0,
    ))
    try:
        capture = await BoundedOutputCapture.create("exec")
        assert capture.spool is not None
        other = await BoundedOutputCapture.create("exec")
        assert other.spool is not None
        other.spool.close()
        capture.feed(b"FIRST_OUTPUT_835\n" + b"x" * 1024 + b"\nFINAL_EIO_927")
        await capture.finish_async()
        assert capture.handle is not None
        record = ToolResultStore(tmp_path).read(capture.handle, session_id="test-session")
        assert record.size_bytes > 128
        assert "FIRST_OUTPUT_835" in record.content
        assert "FINAL_EIO_927" in record.content
        assert capture.describe()["retained_output_complete"] is True
        # Completed background jobs release their large preview and query the
        # retained record. The actual error must survive that transition too.
        capture.release_preview()
        restored = await capture.preview_async()
        assert "FIRST_OUTPUT_835" in restored and "FINAL_EIO_927" in restored
    finally:
        current_tool_context.reset(token)


async def test_complete_output_does_not_add_capture_instructions(tmp_path: Path) -> None:
    capture = BoundedOutputCapture()
    capture.spool = _spool(ToolResultStore(tmp_path))
    capture.retrieval_available = True
    capture.feed(b"complete output")
    await capture.finish_async()

    assert capture.handle is not None
    assert capture.describe()["retained_output_complete"] is True
    assert capture.describe(only_if_needed=True) == {}
    assert capture.notice() == ""
    assert _retained_output(ToolResultStore(tmp_path)).content == "complete output"


@pytest.mark.parametrize("output_size", [20, 200])
async def test_process_log_reads_full_output_independent_of_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output_size: int,
) -> None:
    from types import SimpleNamespace
    from typing import cast

    capture = BoundedOutputCapture(preview_bytes=64)
    capture.spool = _spool(ToolResultStore(tmp_path))
    capture.retrieval_available = True
    capture.feed(b"x" * output_size)
    await capture.finish_async()
    session = shell._BgSession(
        session_id="log-test", command="synthetic command",
        process=cast(asyncio.subprocess.Process, SimpleNamespace(returncode=0)),
        session_key="test-session", output_capture=capture, done=True, returncode=0,
    )
    monkeypatch.setitem(shell._bg_sessions, session.session_id, session)
    token = current_tool_context.set(ToolContext(session_key="test-session"))
    try:
        payload = json.loads(await shell.process("log", session_id=session.session_id, limit=1000))
    finally:
        current_tool_context.reset(token)

    assert len(payload["output"]) < payload["limit"]
    assert payload["truncated"] is False
    assert payload["output"] == "x" * output_size
    if output_size > 64:
        assert payload["session"]["output_capture"]["tool_result_handle"] == capture.handle
    else:
        assert "output_capture" not in payload["session"]


def test_mcp_declares_configured_execution_budget_plus_cleanup() -> None:
    from typing import cast

    from opensquilla.mcp.client import MCPClient
    from opensquilla.mcp.discovery import _make_tool_handler
    from opensquilla.mcp.types import MCPToolDef
    from opensquilla.tools.registry import ToolRegistry

    registry = ToolRegistry()
    _make_tool_handler(
        cast(MCPClient, object()), "test", "slow", MCPToolDef("slow", "slow", {}),
        registry, timeout_seconds=90,
    )
    definition = registry.to_tool_definitions()[0]
    assert definition.execution_timeout_seconds == 95


async def test_execute_code_timeout_retains_both_streams(tmp_path: Path) -> None:
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.integration import configure_runtime, reset_runtime
    from opensquilla.tools.builtin import code_exec

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    configure_runtime(SandboxSettings(sandbox=False, security_grading=False), workspace=workspace)
    token = current_tool_context.set(ToolContext(
        session_key="test-session", workspace_dir=str(workspace),
        tool_result_store_dir=str(tmp_path / "store"), is_owner=True,
    ))
    try:
        raw = await code_exec.execute_code(
            "import sys,time; print('stdout ready',flush=True); "
            "print('stderr ready',file=sys.stderr,flush=True); time.sleep(20)",
            timeout=1,
        )
        payload = json.loads(raw)
        assert payload["timed_out"] is True
        assert "stdout ready" in payload["stdout"]
        assert "stderr ready" in payload["stderr"]
        assert "output_capture" not in payload
        retained = _retained_output(ToolResultStore(tmp_path / "store"))
        assert "stdout ready" in retained.content
        assert "stderr ready" in retained.content
    finally:
        current_tool_context.reset(token)
        reset_runtime()


@pytest.mark.parametrize("character", ["x", "界", "🙂"])
@pytest.mark.parametrize("size", [50_000, 50_001])
def test_execute_code_model_preview_character_boundary(character: str, size: int) -> None:
    from opensquilla.tools.builtin.code_exec import _execution_result_json

    text = "start" + character * (size - 8) + "end"
    payload = json.loads(_execution_result_json(
        returncode=1, stdout=text, stderr=text, timed_out=False, elapsed_ms=1,
    ))
    for stream in ("stdout", "stderr"):
        if size == 50_000:
            assert payload[stream] == text
        else:
            assert payload[stream] == (
                text[:25_000] + "\n[output preview omitted characters]\n" + text[-25_000:]
            )


@pytest.mark.parametrize("retrieval_available", [False, True])
@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
async def test_execute_code_shortened_model_preview_keeps_full_log(
    tmp_path: Path, retrieval_available: bool, newline: str,
) -> None:
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.integration import configure_runtime, reset_runtime
    from opensquilla.tools.builtin import code_exec

    configure_runtime(SandboxSettings(sandbox=False, security_grading=False), workspace=tmp_path)
    token = current_tool_context.set(ToolContext(
        session_key="test-session", workspace_dir=str(tmp_path), is_owner=True,
        tool_result_store_dir=str(tmp_path / "store"),
        tool_result_retrieval_available=retrieval_available,
    ))
    text = (
        "start\n" + "界" * 40_000 + "\nretained middle\n" + "z" * 40_000 + "\nend"
    ).replace("\n", newline)
    code = (
        "import sys; "
        "text = 'start\\n' + '\\u754c' * 40000 + '\\nretained middle\\n' + 'z' * 40000 + '\\nend'; "
        f"data = text.replace('\\n', {newline!r}).encode('utf-8'); "
        "sys.stdout.buffer.write(data); sys.stderr.buffer.write(data)"
    )
    try:
        payload = json.loads(await code_exec.execute_code(code, timeout=10))
        assert payload["exit_code"] == 0
        for stream in ("stdout", "stderr"):
            assert "retained middle" not in payload[stream]
            assert payload[stream].startswith("start" + newline)
            assert payload[stream].endswith(newline + "end")
            assert len(payload[stream]) < 50_100
        info = payload["output_capture"]
        assert info["preview_omitted_bytes"] == 0
        assert info["retained_output_complete"] is True
        assert ("retrieval" in info) is retrieval_available
        store = ToolResultStore(tmp_path / "store")
        retained = store.read(info["tool_result_handle"], session_id="test-session")
        assert retained.content == f"\n[stdout]\n{text}\n[stderr]\n{text}"
    finally:
        current_tool_context.reset(token)
        reset_runtime()


@pytest.mark.parametrize("failure", ["timeout", "error"])
async def test_execute_code_appended_diagnostic_can_trigger_preview_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.integration import configure_runtime, reset_runtime
    from opensquilla.tools.builtin import code_exec

    async def finished_process(proc, timeout):
        await proc.wait()
        if failure == "error":
            raise OSError("synthetic wait failure")
        return False

    monkeypatch.setattr(shell, "_wait_exec_process", finished_process)
    configure_runtime(SandboxSettings(sandbox=False, security_grading=False), workspace=tmp_path)
    token = current_tool_context.set(ToolContext(
        session_key="test-session", workspace_dir=str(tmp_path), is_owner=True,
        tool_result_store_dir=str(tmp_path / "store"),
    ))
    try:
        payload = json.loads(await code_exec.execute_code(
            "import sys; sys.stderr.write('x' * 50000)", timeout=10,
        ))
        assert payload["exit_code"] == -1
        assert payload["timed_out"] is (failure == "timeout")
        diagnostic = (
            "Execution timed out after 10.0s" if failure == "timeout"
            else "Execution error: synthetic wait failure"
        )
        assert payload["stderr"].endswith(diagnostic)
        assert "[output preview omitted characters]" in payload["stderr"]
        assert len(payload["stderr"]) < 50_100
        info = payload["output_capture"]
        assert info["preview_omitted_bytes"] == 0
        assert info["retained_output_complete"] is True
        retained = ToolResultStore(tmp_path / "store").read(
            info["tool_result_handle"], session_id="test-session",
        )
        assert retained.content == "\n[stderr]\n" + "x" * 50_000
    finally:
        current_tool_context.reset(token)
        reset_runtime()


@pytest.mark.parametrize("failure", ["no_store", "append", "finalize"])
async def test_execute_code_preview_with_unavailable_full_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    from opensquilla.engine import tool_result_store
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.integration import configure_runtime, reset_runtime
    from opensquilla.tools.builtin import code_exec

    def storage_failure(*args, **kwargs):
        raise OSError("synthetic log storage failure")

    if failure == "append":
        monkeypatch.setattr(tool_result_store.ToolOutputSpool, "append", storage_failure)
    elif failure == "finalize":
        monkeypatch.setattr(tool_result_store, "_atomic_write_bytes", storage_failure)
    configure_runtime(SandboxSettings(sandbox=False, security_grading=False), workspace=tmp_path)
    token = current_tool_context.set(ToolContext(
        session_key="test-session", workspace_dir=str(tmp_path), is_owner=True,
        tool_result_store_dir="" if failure == "no_store" else str(tmp_path / "store"),
        tool_result_retrieval_available=True,
    ))
    try:
        payload = json.loads(await code_exec.execute_code(
            "import sys; sys.stdout.write('x' * 80000 + 'last diagnostic')", timeout=10,
        ))
        assert payload["exit_code"] == 0
        assert len(payload["stdout"]) < 50_100
        assert payload["stdout"].endswith("last diagnostic")
        info = payload["output_capture"]
        assert info["retained_output_complete"] is False
        if failure == "no_store":
            assert info["retained_bytes"] == 0
            assert "storage_error" not in info
        else:
            assert info["storage_error"] == "OSError"
        if failure == "append":
            store = ToolResultStore(tmp_path / "store")
            assert store.read_output_metadata(
                info["tool_result_handle"], session_id="test-session",
            )["complete"] is False
        else:
            assert "tool_result_handle" not in info
            assert "retrieval" not in info
    finally:
        current_tool_context.reset(token)
        reset_runtime()


async def test_slow_spool_does_not_block_preview_or_queue_unbounded_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    capture = BoundedOutputCapture()
    capture.spool = _spool(ToolResultStore(tmp_path))
    entered = threading.Event()
    release = threading.Event()
    original = capture.spool.append
    calls = 0

    def slow_write(chunk: bytes) -> None:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=5)
        original(chunk)

    monkeypatch.setattr(capture.spool, "append", slow_write)
    reader = asyncio.StreamReader()
    reader.feed_data(b"x" * (64 * 1024 * 4))
    reader.feed_eof()
    task = asyncio.create_task(capture.drain(reader))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        # The worker has one chunk in flight and cannot enqueue the rest.
        assert calls == 1
        # This runs on the event loop while its write remains blocked. A
        # thread-side probe bounds the test if preview regresses to an IO lock.
        probe = asyncio.create_task(asyncio.to_thread(capture.describe))
        info = await asyncio.wait_for(probe, timeout=1)
        assert info["observed_bytes"] == 64 * 1024
        assert capture.preview() == "x" * (64 * 1024)
    finally:
        release.set()
        await task
        await capture.finish_async()



async def test_released_preview_uses_bounded_diagnostic_if_stored_output_is_evicted(
    tmp_path: Path,
) -> None:
    capture = BoundedOutputCapture()
    capture.spool = _spool(ToolResultStore(tmp_path))
    capture.feed(b"x" * 2000 + b"last diagnostic")
    await capture.finish_async()
    capture.release_preview()
    assert not capture.previews["stdout"].head
    assert not capture.previews["stdout"].tail
    # Explicit expiry remains separate from snapshot budget pressure.
    store = ToolResultStore(tmp_path)
    store._remove_expired(store._iter_output_stats(), 0)
    preview = await capture.preview_async()
    assert "last diagnostic" in preview
    assert "retained output unavailable" in preview


async def test_failed_storage_keeps_bounded_preview_for_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = BoundedOutputCapture(preview_bytes=256)
    capture.spool = _spool(ToolResultStore(tmp_path))

    def disk_full(**_kwargs) -> str:
        raise OSError(28, "synthetic disk full at completion")

    monkeypatch.setattr(capture.spool, "finish", disk_full)
    capture.feed(b"head diagnostic" + b"x" * 1000 + b"tail diagnostic")
    await capture.finish_async()
    capture.release_preview()
    assert capture.handle is None
    assert len(capture.previews["stdout"].head) + len(capture.previews["stdout"].tail) <= 256
    assert "head diagnostic" in await capture.preview_async()
    assert "tail diagnostic" in await capture.preview_async()


def test_multistream_spool_keeps_split_utf8_intact(tmp_path: Path) -> None:
    capture = BoundedOutputCapture(streams=("stdout", "stderr"))
    capture.spool = _spool(ToolResultStore(tmp_path))
    text = "你好".encode()
    for byte in text:
        capture.feed(bytes([byte]), "stdout")
    capture.feed(b"diagnostic", "stderr")
    capture.finish()
    assert capture.handle
    retained = ToolResultStore(tmp_path).read(capture.handle, session_id="test-session").content
    assert "�" not in retained
    assert "你好" in retained and "diagnostic" in retained


@pytest.mark.parametrize(
    ("fallback_encoding", "legacy_text"),
    [
        ("cp936", "中文错误：文件不存在"),
        ("cp932", "日本語エラー：ファイルがありません"),
    ],
)
@pytest.mark.parametrize("legacy_stream", ["stdout", "stderr"])
def test_multistream_retention_preserves_independent_legacy_and_utf8_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    fallback_encoding: str, legacy_text: str, legacy_stream: str,
) -> None:
    from functools import partial

    from opensquilla.subprocess_encoding import (
        decode_subprocess_output,
        select_subprocess_output_encoding,
    )
    from opensquilla.tools import output_capture

    # Exercise the Windows code-page decision on every host. A Python process
    # can forward a native child's legacy output while its own stream is UTF-8.
    monkeypatch.setattr(output_capture, "decode_subprocess_output", partial(
        decode_subprocess_output, fallback_encoding=fallback_encoding,
    ))
    monkeypatch.setattr(output_capture, "select_subprocess_output_encoding", partial(
        select_subprocess_output_encoding, fallback_encoding=fallback_encoding,
    ))
    capture = BoundedOutputCapture(streams=("stdout", "stderr"))
    capture.spool = _spool(ToolResultStore(tmp_path))
    utf8_text = "UTF8_MESSAGE_184：你好、日本語、🙂"
    utf8_stream = "stderr" if legacy_stream == "stdout" else "stdout"
    expected = {legacy_stream: legacy_text, utf8_stream: utf8_text}
    encoded = {
        legacy_stream: legacy_text.encode(fallback_encoding),
        utf8_stream: utf8_text.encode(),
    }
    # Interleaving single-byte fragments cuts every multibyte character. The
    # retained content must not depend on which stream supplies the next read.
    for offset in range(max(map(len, encoded.values()))):
        for stream, raw in encoded.items():
            if offset < len(raw):
                capture.feed(raw[offset:offset + 1], stream)
    capture.finish()
    assert capture.handle is not None
    record = ToolResultStore(tmp_path).read(capture.handle, session_id="test-session")
    for stream, text in expected.items():
        assert capture.preview(stream) == text
        assert f"[{stream}]\n{text}" in record.content
    assert "�" not in record.content
    assert capture.describe()["retained_output_complete"] is True


@pytest.mark.parametrize(
    ("fallback_encoding", "legacy_text"),
    [("cp936", "中文诊断：文件不存在"), ("cp932", "日本語診断：ファイルがありません")],
)
async def test_execute_code_retains_native_child_bytes_alongside_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    fallback_encoding: str, legacy_text: str,
) -> None:
    from functools import partial

    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.integration import configure_runtime, reset_runtime
    from opensquilla.subprocess_encoding import (
        decode_subprocess_output,
        select_subprocess_output_encoding,
    )
    from opensquilla.tools import output_capture
    from opensquilla.tools.builtin import code_exec

    monkeypatch.setattr(output_capture, "decode_subprocess_output", partial(
        decode_subprocess_output, fallback_encoding=fallback_encoding,
    ))
    monkeypatch.setattr(output_capture, "select_subprocess_output_encoding", partial(
        select_subprocess_output_encoding, fallback_encoding=fallback_encoding,
    ))
    configure_runtime(SandboxSettings(sandbox=False, security_grading=False), workspace=tmp_path)
    token = current_tool_context.set(ToolContext(
        session_key="test-session", workspace_dir=str(tmp_path),
        tool_result_store_dir=str(tmp_path / "store"), is_owner=True,
    ))
    utf8_text = "UTF8_PROCESS_289：你好、日本語、🙂"
    code = (
        "import os; "
        f"os.write(1, bytes.fromhex({utf8_text.encode().hex()!r})); "
        f"os.write(2, bytes.fromhex({legacy_text.encode(fallback_encoding).hex()!r}))"
    )
    try:
        payload = json.loads(await code_exec.execute_code(code, timeout=10))
        assert payload["exit_code"] == 0
        assert payload["stdout"] == utf8_text
        assert payload["stderr"] == legacy_text
        assert "output_capture" not in payload
        retained = _retained_output(ToolResultStore(tmp_path / "store")).content
        assert f"[stdout]\n{utf8_text}" in retained
        assert f"[stderr]\n{legacy_text}" in retained
        assert "�" not in retained
    finally:
        current_tool_context.reset(token)
        reset_runtime()


def test_small_output_after_spool_write_failure_retains_real_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = BoundedOutputCapture()
    capture.spool = _spool(ToolResultStore(tmp_path))
    capture.feed(b"SAVED_PREFIX_649\n")

    def fail_write(_chunk: bytes) -> None:
        raise OSError(28, "synthetic output write failure")

    monkeypatch.setattr(capture.spool, "append", fail_write)
    capture.feed(b"FINAL_WRITE_ERROR_396\n")
    capture.finish()
    assert capture.handle is not None
    retained = ToolResultStore(tmp_path).read(capture.handle, session_id="test-session").content
    assert "SAVED_PREFIX_649" in retained
    assert "FINAL_WRITE_ERROR_396" in capture.preview()
    assert "FINAL_WRITE_ERROR_396" not in retained
    assert capture.describe()["retained_output_complete"] is False


def test_decoding_expansion_does_not_truncate_execution_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from functools import partial

    from opensquilla.subprocess_encoding import (
        decode_subprocess_output,
        select_subprocess_output_encoding,
    )
    from opensquilla.tools import output_capture

    monkeypatch.setattr(output_capture, "decode_subprocess_output", partial(
        decode_subprocess_output, fallback_encoding="cp936",
    ))
    monkeypatch.setattr(output_capture, "select_subprocess_output_encoding", partial(
        select_subprocess_output_encoding, fallback_encoding="cp936",
    ))
    capture = BoundedOutputCapture()
    capture.spool = _spool(ToolResultStore(tmp_path))
    raw = ("中" * 48 + "FINAL_EIO_748").encode("cp936")
    assert len(raw) < 128 < len(raw.decode("cp936").encode())
    capture.feed(raw)
    capture.finish()
    assert capture.handle is not None
    record = ToolResultStore(tmp_path).read(capture.handle, session_id="test-session")
    assert "FINAL_EIO_748" in record.content
    assert capture.describe()["retained_output_complete"] is True


def test_multistream_output_preserves_complete_frames(tmp_path: Path) -> None:
    capture = BoundedOutputCapture(streams=("stdout", "stderr"))
    capture.spool = _spool(ToolResultStore(tmp_path))
    capture.feed("prefix你好".encode(), "stdout")
    capture.feed(b"x" * 256 + b"FINAL_FRAME_684", "stderr")
    capture.finish()
    assert capture.handle is not None
    record = ToolResultStore(tmp_path).read(capture.handle, session_id="test-session")
    assert "prefix你好" in record.content
    assert "FINAL_FRAME_684" in record.content
    assert capture.describe()["retained_output_complete"] is True



def test_failed_eviction_does_not_spend_bytes_still_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine import tool_result_store

    store = ToolResultStore(tmp_path)
    record = _write(store, "existing bytes", budget=20)
    os.utime(store._record_dir(record.handle, session_id="test-session") / "content.txt", (1, 1))
    monkeypatch.setattr(tool_result_store, "_remove_record_dir", lambda _path: None)
    with pytest.raises(ToolResultStoreBudgetError):
        _write(store, "additional bytes", budget=20)
    assert store.read(record.handle, session_id="test-session").content == "existing bytes"


async def test_finalization_waits_for_output_writer_before_closing_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    capture = BoundedOutputCapture()
    store = ToolResultStore(tmp_path)
    capture.spool = _spool(store)
    entered, release, finishing = threading.Event(), threading.Event(), threading.Event()
    original_append = capture.spool.append
    original_finish = capture.finish

    def blocked_append(chunk: bytes) -> None:
        entered.set()
        assert release.wait(timeout=5)
        original_append(chunk)

    def finish() -> None:
        finishing.set()
        original_finish()

    monkeypatch.setattr(capture.spool, "append", blocked_append)
    monkeypatch.setattr(capture, "finish", finish)
    writer = asyncio.create_task(asyncio.to_thread(capture.feed, b"last diagnostic"))
    assert await asyncio.to_thread(entered.wait, 1)
    finalizer = asyncio.create_task(capture.finish_async())
    try:
        assert await asyncio.to_thread(finishing.wait, 1)
        assert not finalizer.done()
        assert not capture.finished
        assert capture.handle is None
        assert not capture.spool.lease.closed
        assert store._iter_output_stats()[0].active
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(writer, finalizer), timeout=2)
    assert capture.spool.lease.closed
    assert capture.handle is not None
    assert store.read(capture.handle, session_id="test-session").content == "last diagnostic"
    capture.release_preview()
    assert not capture.previews["stdout"].head


async def test_finalization_waits_for_retained_output_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    capture = BoundedOutputCapture()
    capture.spool = _spool(ToolResultStore(tmp_path))
    entered, release = threading.Event(), threading.Event()
    original_finish = capture.spool.finish
    calls = 0

    def delayed_finish(**kwargs) -> str:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=5)
        return original_finish(**kwargs)

    monkeypatch.setattr(capture.spool, "finish", delayed_finish)
    capture.feed(b"result already produced")
    tasks = [asyncio.create_task(capture.finish_async()) for _ in range(2)]
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert not any(task.done() for task in tasks)
        assert not capture.finished
        assert capture.handle is None
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
    await capture.finish_async()
    assert calls == 1
    assert capture.spool.lease.closed
    assert capture.handle is not None


async def test_cancelled_finalizer_still_closes_lease_when_executor_was_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    capture = BoundedOutputCapture()
    store = ToolResultStore(tmp_path)
    capture.spool = _spool(store)
    capture.feed(b"retained before cancellation")
    entered, release = threading.Event(), threading.Event()
    queued, closed = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    original_finish = capture.finish

    def busy_worker() -> None:
        entered.set()
        assert release.wait(timeout=5)

    def finish() -> None:
        try:
            original_finish()
        finally:
            loop.call_soon_threadsafe(closed.set)

    with ThreadPoolExecutor(max_workers=1) as executor:
        blocker = executor.submit(busy_worker)
        assert entered.wait(timeout=1)

        async def run_in_test_executor(function, *args):
            future = loop.run_in_executor(executor, function, *args)
            queued.set()
            return await future

        monkeypatch.setattr(asyncio, "to_thread", run_in_test_executor)
        monkeypatch.setattr(capture, "finish", finish)
        task = asyncio.create_task(capture.finish_async())
        try:
            await asyncio.wait_for(queued.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not capture.spool.lease.closed
        finally:
            release.set()
            await asyncio.wait_for(closed.wait(), timeout=2)
            blocker.result(timeout=1)

    assert capture.spool.lease.closed
    assert capture.handle is not None
    assert store.read(capture.handle, session_id="test-session").content == (
        "retained before cancellation"
    )


async def test_code_capture_setup_cancellation_removes_temporary_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.integration import configure_runtime, reset_runtime
    from opensquilla.tools.builtin import code_exec

    configure_runtime(SandboxSettings(sandbox=False, security_grading=False), workspace=tmp_path)
    directory = tmp_path / "ephemeral-code-cwd"

    def create_directory(*args, **kwargs):
        directory.mkdir()
        return str(directory)

    async def cancelled_capture(*args, **kwargs):
        assert directory.is_dir()
        raise asyncio.CancelledError()

    monkeypatch.setattr(code_exec.tempfile, "mkdtemp", create_directory)
    monkeypatch.setattr(BoundedOutputCapture, "create", cancelled_capture)
    token = current_tool_context.set(ToolContext(session_key="test-session", is_owner=True))
    try:
        with pytest.raises(asyncio.CancelledError):
            await code_exec.execute_code("print('never executed')")
        assert not directory.exists()
    finally:
        current_tool_context.reset(token)
        reset_runtime()


async def test_unregistered_background_spawn_cleanup_survives_repeated_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    entered, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    paths = [tmp_path / name for name in ("profile", "bridge", "proxy")]
    for path in paths:
        path.write_text("synthetic resource")

    async def terminate(*args):
        entered.set()
        await release.wait()

    async def cleanup_bridge():
        paths[1].unlink()

    async def cleanup_proxy():
        paths[2].unlink()
        cleaned.set()

    monkeypatch.setattr(shell, "_terminate_exec_process_tree", terminate)
    spawned = shell._SpawnedBackgroundProcess(
        process=SimpleNamespace(), process_tree=SimpleNamespace(),
        cleanup_callbacks=[lambda: paths[0].unlink()],
        async_cleanup_callbacks=[cleanup_bridge],
    )
    task = asyncio.create_task(shell._cleanup_unregistered_background_spawn(spawned, cleanup_proxy))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.wait_for(cleaned.wait(), timeout=1)
    assert all(not path.exists() for path in paths)


def test_full_log_finalization_and_middle_reads_keep_memory_bounded(tmp_path: Path) -> None:
    import tracemalloc

    capture = BoundedOutputCapture(preview_bytes=128)
    store = ToolResultStore(tmp_path)
    capture.spool = _spool(store)
    chunk = b"x" * 65536
    for _ in range(160):
        capture.feed(chunk)
    capture.feed(b"MIDDLE_FAILURE_731\n")
    for _ in range(32):
        capture.feed(chunk)
    tracemalloc.start()
    try:
        capture.finish()
        text, total = capture.read_slice(
            160 * len(chunk), 160 * len(chunk) + len("MIDDLE_FAILURE_731\nx"),
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert text == "MIDDLE_FAILURE_731\nx"
    assert total == 192 * len(chunk) + len(b"MIDDLE_FAILURE_731\n")
    assert peak < 4 * 1024 * 1024
    assert capture.describe()["retained_output_complete"] is True
    assert capture.spool.path.stat().st_size == total


def test_execution_log_expiry_is_separate_and_protects_active_writers(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path)
    active = _spool(store)
    completed = _spool(store)
    try:
        active.append(b"active")
        completed.append(b"complete")
        handle = completed.finish()
        os.utime(active.path, (1, 1))
        os.utime(completed.path, (1, 1))
        # Ordinary snapshots cannot delete logs, even with zero retention.
        store.write(
            "new snapshot", tool_use_id="call", tool_name="test",
            session_id="test-session", session_key="test-session", agent_id="main",
            retention_seconds=0, disk_budget_bytes=32,
        )
        assert store.read(handle, session_id="test-session").content == "complete"
        other = _spool(store)
        other.close()
        assert active.path.exists()
        assert not completed.path.exists()
    finally:
        active.close()
        completed.close()


def test_execution_log_metadata_and_reads_enforce_session_scope(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path)
    spool = store.open_output_spool(
        tool_name="exec", session_id="session:a", session_key="session:a", agent_id="main",
    )
    spool.append(b"private synthetic output")
    handle = spool.finish()
    # These different session names deliberately share a sanitized directory.
    with pytest.raises(ValueError, match="session mismatch"):
        store.read_output_metadata(handle, session_id="session/a")
    with pytest.raises(ValueError, match="session mismatch"):
        list(store.iter_text_chunks(handle, session_id="session/a"))


def test_execution_log_metadata_counts_lines_across_chunk_boundaries(tmp_path: Path) -> None:
    capture = BoundedOutputCapture()
    store = ToolResultStore(tmp_path)
    capture.spool = _spool(store)
    text = "x" * 65535 + "\r\nnext\rfinal\x85last\u2028"
    capture.feed(text.encode())
    capture.finish()
    meta = store.read_output_metadata(capture.handle, session_id="test-session")
    assert meta["chars"] == len(text)
    assert meta["line_count"] == len(text.splitlines())
    assert "".join(store.iter_text_chunks(
        capture.handle, session_id="test-session", chunk_size=1,
    )) == text


def test_active_log_pages_do_not_emit_a_partial_utf8_character(tmp_path: Path) -> None:
    capture = BoundedOutputCapture()
    store = ToolResultStore(tmp_path)
    capture.spool = _spool(store)
    try:
        character = "你".encode()
        capture.feed(b"start " + character[:2])
        first, count = capture.read_slice(0, 100)
        assert (first, count) == ("start ", 6)
        capture.feed(character[2:] + "好\nend".encode())
        rest, count = capture.read_slice(len(first), 100)
        assert (rest, count) == ("你好\nend", 12)
        capture.finish()
        assert first + rest == store.read(capture.handle, session_id="test-session").content
    finally:
        capture.spool.close()


@pytest.mark.parametrize("written_bytes", [0, 3, 5, 8])
def test_partial_frame_write_preserves_saved_bytes_and_marks_log_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, written_bytes: int,
) -> None:
    capture = BoundedOutputCapture(streams=("stdout", "stderr"))
    store = ToolResultStore(tmp_path)
    capture.spool = _spool(store)
    capture.feed(b"saved output", "stdout")
    original_write = capture.spool.output.write

    def short_write(chunk: bytes) -> int:
        return original_write(chunk[:written_bytes])

    monkeypatch.setattr(capture.spool.output, "write", short_write)
    capture.feed(b"last diagnostic", "stderr")
    capture.finish()
    assert capture.storage_error == "OSError"
    assert capture.handle is not None
    meta = store.read_output_metadata(capture.handle, session_id="test-session")
    assert meta["complete"] is False
    assert meta["stored_size_bytes"] == capture.spool.path.stat().st_size
    assert "saved output" in store.read(capture.handle, session_id="test-session").content
    if written_bytes > 5:
        assert "[stderr]\nlas" in store.read(capture.handle, session_id="test-session").content
    assert "last diagnostic" in capture.preview("stderr")
    assert "not a full log" in capture.notice()


def test_finalization_publishes_metadata_after_content_is_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine import tool_result_store

    capture = BoundedOutputCapture()
    store = ToolResultStore(tmp_path)
    capture.spool = _spool(store)
    capture.feed(b"finished output")
    original_write = tool_result_store._atomic_write_bytes

    def publish_metadata(path: Path, content: bytes) -> None:
        assert capture.spool.path.name == "content.bin"
        assert capture.spool.path.read_bytes() == b"finished output"
        assert not capture.spool.lease.closed
        original_write(path, content)

    monkeypatch.setattr(tool_result_store, "_atomic_write_bytes", publish_metadata)
    capture.finish()
    assert capture.handle is not None
    assert capture.spool.lease.closed
    assert store.read(capture.handle, session_id="test-session").content == "finished output"


def test_metadata_write_failure_does_not_publish_a_complete_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine import tool_result_store

    capture = BoundedOutputCapture()
    store = ToolResultStore(tmp_path)
    capture.spool = _spool(store)
    capture.feed(b"finished output")

    def failed_metadata_write(path: Path, content: bytes) -> None:
        raise OSError(28, "synthetic metadata disk full")

    monkeypatch.setattr(tool_result_store, "_atomic_write_bytes", failed_metadata_write)
    capture.finish()
    assert capture.handle is None
    assert capture.storage_error == "OSError"
    assert capture.spool.output.closed and capture.spool.lease.closed
    assert capture.spool.path.read_bytes() == b"finished output"
    assert not (capture.spool.record_dir / "meta.json").exists()
    assert capture.describe()["retained_output_complete"] is False


@pytest.mark.parametrize("field,value", [
    ("streams", [[]]), ("encodings", []), ("decoder_final", ["yes"]),
    ("size_bytes", -1), ("chars", "12"), ("encodings", ["unknown-codec"]),
])
def test_malformed_execution_log_metadata_returns_a_read_error(
    tmp_path: Path, field: str, value: object,
) -> None:
    store = ToolResultStore(tmp_path)
    spool = _spool(store)
    spool.append(b"output")
    handle = spool.finish()
    metadata_path = spool.record_dir / "meta.json"
    metadata = json.loads(metadata_path.read_text())
    metadata[field] = value
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="invalid tool output"):
        list(store.iter_text_chunks(handle, session_id="test-session"))


def test_execution_log_detects_truncated_stored_payload(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path)
    spool = _spool(store)
    spool.append(b"output")
    handle = spool.finish()
    spool.path.write_bytes(b"out")
    with pytest.raises(ValueError, match="size mismatch"):
        store.read_output_metadata(handle, session_id="test-session")


@pytest.mark.parametrize("encoding", [None, "cp936", "cp932", "invalid-encoding"])
@pytest.mark.parametrize("raw", [b"plain", "UTF8你好🙂".encode(), b"\xc2\x80", b"\xe4\xbd"])
def test_streaming_encoding_selection_matches_existing_decoder(encoding, raw) -> None:
    import codecs

    from opensquilla.subprocess_encoding import (
        decode_subprocess_output,
        select_subprocess_output_encoding,
    )

    chunks = [raw[index:index + 1] for index in range(len(raw))]
    selected, final = select_subprocess_output_encoding(chunks, fallback_encoding=encoding)
    decoder = codecs.getincrementaldecoder(selected)("replace")
    content = "".join(decoder.decode(chunk, final=False) for chunk in chunks)
    content += decoder.decode(b"", final=final)
    assert content == decode_subprocess_output(raw, fallback_encoding=encoding)


@pytest.mark.parametrize("completed", [False, True])
async def test_process_log_reads_middle_beyond_saved_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, completed: bool,
) -> None:
    from types import SimpleNamespace
    from typing import cast

    token = current_tool_context.set(ToolContext(
        session_key="test-session", tool_result_store_dir=str(tmp_path),
    ))
    capture = await BoundedOutputCapture.create("background_process")
    try:
        chunk = b"x" * (64 * 1024)
        for _ in range(160):
            capture.feed(chunk)
        middle = "唯一的中间错误\r\n"
        capture.feed(middle.encode())
        for _ in range(160):
            capture.feed(chunk)
        assert middle not in capture.preview()
        if completed:
            await capture.finish_async()
            capture.release_preview()
        session = shell._BgSession(
            session_id="full-log-test", command="synthetic command",
            process=cast(asyncio.subprocess.Process, SimpleNamespace(
                returncode=0 if completed else None,
            )), session_key="test-session", output_capture=capture, done=completed,
            output_lines=["synthetic process status"],
        )
        monkeypatch.setitem(shell._bg_sessions, session.session_id, session)
        offset = len(chunk) * 160
        first = json.loads(await shell.process(
            "log", session_id=session.session_id, offset=offset, limit=4,
        ))
        second = json.loads(await shell.process(
            "log", session_id=session.session_id, offset=offset + 4, limit=len(middle) - 4,
        ))
        assert first["output"] + second["output"] == middle
        assert first["total_chars"] == len(chunk) * 320 + len(middle)
        assert "capture" not in first["output"]
    finally:
        await capture.finish_async()
        current_tool_context.reset(token)


async def test_successful_exec_does_not_cancel_output_while_disk_write_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from opensquilla.engine.tool_result_store import ToolOutputSpool

    original_append = ToolOutputSpool.append
    entered, release = threading.Event(), threading.Event()
    first = True

    def delayed_append(spool, chunk):
        nonlocal first
        if first:
            first = False
            entered.set()
            assert release.wait(timeout=5)
        original_append(spool, chunk)

    expected = b"x" * 131072 + b"EXPECTED_END"
    proc = None

    async def create_process(_command, **kwargs):
        nonlocal proc
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "import sys;sys.stdout.buffer.write(b'x'*131072+b'EXPECTED_END');sys.stdout.flush()",
            **kwargs,
        )
        return proc

    monkeypatch.setattr(ToolOutputSpool, "append", delayed_append)
    monkeypatch.setattr(shell, "_create_host_shell_subprocess", create_process)
    monkeypatch.setattr(shell, "_BACKGROUND_KILL_TIMEOUT", 0.05)
    token = current_tool_context.set(ToolContext(
        session_key="test-session", tool_result_store_dir=str(tmp_path),
    ))
    task = asyncio.create_task(shell._run_host_shell_command(
        "synthetic output", cwd=None, env=dict(os.environ), stdin_bytes=None,
        effective_timeout=10,
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert proc is not None
        assert await shell._wait_exec_process(proc, 3)
        # The command already exited; only a healthy disk write remains pending.
        await asyncio.sleep(0.15)
    finally:
        release.set()
        try:
            result = await asyncio.wait_for(task, timeout=5)
        finally:
            current_tool_context.reset(token)
    record = _retained_output(ToolResultStore(tmp_path))
    assert result.startswith("exit_code=0\n")
    assert record.content.encode() == expected
    metadata = ToolResultStore(tmp_path).read_output_metadata(
        record.handle, session_id="test-session",
    )
    assert metadata["complete"] is True


@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
async def test_background_reader_failure_finalizes_partial_log_and_releases_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception],
) -> None:
    from types import SimpleNamespace

    class FailedReader:
        calls = 0

        async def read(self, size):
            self.calls += 1
            if self.calls == 1:
                return b"saved before pipe failure\n"
            raise error_type("synthetic output read failure")

    async def exited():
        return 0

    proc = SimpleNamespace(returncode=0, stdout=FailedReader(), wait=exited, pid=99999999)

    async def create_process(*args, **kwargs):
        return proc

    monkeypatch.setattr(shell, "_create_host_shell_subprocess", create_process)
    monkeypatch.setattr(shell, "capture_process_tree_owner", lambda *args, **kwargs: None)
    token = current_tool_context.set(ToolContext(
        session_key="test-session", tool_result_store_dir=str(tmp_path),
    ))
    session = None
    try:
        result = await shell._start_host_background_process(
            "synthetic command", cwd=None, effective_timeout=10, runtime=None,
        )
        session_id = result.splitlines()[0].split("=", 1)[1]
        session = shell._bg_sessions[session_id]
        cleaned = []
        session.cleanup_callbacks.append(lambda: cleaned.append("cleanup"))
        if error_type is OSError:
            await session.collector_task
        else:
            with pytest.raises(error_type, match="synthetic output read failure"):
                await session.collector_task
        assert cleaned == ["cleanup"]
        assert session.done
        assert session.returncode == 0
        capture = session.output_capture
        assert capture.finished and capture.spool.lease.closed
        store = ToolResultStore(tmp_path)
        metadata = store.read_output_metadata(capture.handle, session_id="test-session")
        assert metadata["complete"] is False
        assert store.read(capture.handle, session_id="test-session").content == (
            "saved before pipe failure\n"
        )
    finally:
        if session is not None:
            await session.output_capture.finish_async()
            shell._bg_sessions.pop(session.session_id, None)
        current_tool_context.reset(token)


@pytest.mark.parametrize("native_pipe", [False, True])
async def test_post_exit_output_idle_timer_restarts_after_each_chunk(
    tmp_path: Path, native_pipe: bool,
) -> None:
    read_fd = write_fd = None
    stream_reader = asyncio.StreamReader()
    if native_pipe:
        read_fd, write_fd = os.pipe()
        reader = shell._NonblockingOutputPipe(read_fd)
    else:
        reader = stream_reader
    capture = BoundedOutputCapture()
    capture.spool = _spool(ToolResultStore(tmp_path))
    exited = asyncio.Event()
    exited.set()
    task = asyncio.create_task(capture.drain(reader, process_exited=exited, idle_timeout=0.05))
    expected = b""
    try:
        # Total post-exit output time exceeds the idle allowance several times.
        for index in range(20):
            chunk = f"line {index}\n".encode()
            expected += chunk
            if write_fd is not None:
                os.write(write_fd, chunk)
            else:
                stream_reader.feed_data(chunk)
            await asyncio.sleep(0.01)
            assert not task.done()
        if write_fd is not None:
            os.close(write_fd)
            write_fd = None
        else:
            stream_reader.feed_eof()
        await asyncio.wait_for(task, timeout=1)
        await capture.finish_async()
        record = _retained_output(ToolResultStore(tmp_path))
        assert record.content.encode() == expected
        assert capture.describe()["retained_output_complete"] is True
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await capture.finish_async()
        for fd in (read_fd, write_fd):
            if fd is not None:
                os.close(fd)


@pytest.mark.parametrize("native_pipe", [False, True])
async def test_quiet_output_is_limited_only_after_process_exit(
    tmp_path: Path, native_pipe: bool,
) -> None:
    read_fd = write_fd = None
    stream_reader = asyncio.StreamReader()
    if native_pipe:
        read_fd, write_fd = os.pipe()
        reader = shell._NonblockingOutputPipe(read_fd)
    else:
        reader = stream_reader
    capture = BoundedOutputCapture()
    capture.spool = _spool(ToolResultStore(tmp_path))
    exited = asyncio.Event()
    task = asyncio.create_task(capture.drain(reader, process_exited=exited, idle_timeout=0.03))
    try:
        await asyncio.sleep(0.1)
        assert not task.done()
        exited.set()
        # Keep the pipe open without more output, like an inherited daemon handle.
        await asyncio.wait_for(task, timeout=1)
        await capture.finish_async()
        assert capture.describe()["retained_output_complete"] is False
        assert capture.incomplete_reason == "output pipe remained open after process exit"
        assert capture.spool.lease.closed
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await capture.finish_async()
        for fd in (read_fd, write_fd):
            if fd is not None:
                os.close(fd)


async def test_stop_returns_promptly_while_received_output_finishes_saving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from opensquilla.engine.cancellation import cancel_task

    capture = BoundedOutputCapture()
    capture.spool = _spool(ToolResultStore(tmp_path))
    original_append = capture.spool.append
    entered, release = threading.Event(), threading.Event()

    def delayed_append(chunk):
        entered.set()
        assert release.wait(timeout=5)
        original_append(chunk)

    monkeypatch.setattr(capture.spool, "append", delayed_append)
    reader = asyncio.StreamReader()
    reader.feed_data(b"output already read before Stop")
    reader.feed_eof()

    async def collect():
        try:
            await capture.drain(reader)
        finally:
            await capture.finish_async()

    task = asyncio.create_task(collect())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        settled = await asyncio.wait_for(cancel_task(
            task, policy="bounded", operation="test-output-stop", grace_seconds=0.01,
        ), timeout=1)
        assert settled is False
        assert not task.done()
        assert not capture.spool.lease.closed
        task.cancel()  # A second Stop must not abandon the in-flight write.
        await asyncio.sleep(0)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    assert capture.spool.lease.closed
    record = _retained_output(ToolResultStore(tmp_path))
    assert record.content == "output already read before Stop"
    assert capture.describe()["retained_output_complete"] is False


@pytest.mark.skipif(os.name != "nt", reason="Native Windows stdin and pipe behavior")
async def test_windows_stdin_exec_retains_output_through_slow_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    from opensquilla.engine.tool_result_store import ToolOutputSpool

    original_append = ToolOutputSpool.append
    first = True

    def delayed_append(spool, chunk):
        nonlocal first
        if first:
            first = False
            time.sleep(0.15)
        original_append(spool, chunk)

    def create_process(_command, **kwargs):
        return subprocess.Popen([
            sys.executable, "-c",
            "import sys;data=sys.stdin.buffer.read();"
            "sys.stdout.buffer.write(data+b'EXPECTED_END');sys.stdout.flush()",
        ], **kwargs)

    monkeypatch.setattr(ToolOutputSpool, "append", delayed_append)
    monkeypatch.setattr(shell, "_create_windows_host_shell_process", create_process)
    monkeypatch.setattr(shell, "_BACKGROUND_KILL_TIMEOUT", 0.05)
    token = current_tool_context.set(ToolContext(
        session_key="test-session", tool_result_store_dir=str(tmp_path),
    ))
    expected = b"input and output\n" * 20000
    try:
        result = await asyncio.wait_for(shell._run_windows_host_shell_command_with_stdin(
            "synthetic command", cwd=None, env=dict(os.environ), stdin_bytes=expected,
            effective_timeout=10,
        ), timeout=15)
    finally:
        current_tool_context.reset(token)
    record = _retained_output(ToolResultStore(tmp_path))
    assert result.startswith("exit_code=0\n")
    assert record.content.encode() == expected + b"EXPECTED_END"
    metadata = ToolResultStore(tmp_path).read_output_metadata(
        record.handle, session_id="test-session",
    )
    assert metadata["complete"] is True


async def test_windows_pipe_setup_failure_closes_both_descriptors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_pipe = os.pipe
    descriptors = []

    def create_pipe():
        pair = original_pipe()
        descriptors.extend(pair)
        return pair

    def fail_nonblocking(fd, blocking):
        raise OSError("synthetic pipe setup failure")

    monkeypatch.setattr(os, "pipe", create_pipe)
    monkeypatch.setattr(os, "set_blocking", fail_nonblocking)
    result = await shell._run_windows_host_shell_command_with_stdin(
        "synthetic command", cwd=None, env=dict(os.environ), stdin_bytes=b"input",
        effective_timeout=1,
    )
    assert "synthetic pipe setup failure" in result
    assert len(descriptors) == 2
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)
