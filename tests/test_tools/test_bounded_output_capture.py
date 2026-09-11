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


def _spool(store: ToolResultStore, *, size: int = 1024, budget: int = 2048):
    return store.open_output_spool(
        tool_name="exec", session_id="test-session", session_key="test-session",
        agent_id="main", max_bytes=size, disk_budget_bytes=budget,
    )


def _write(store: ToolResultStore, text: str, *, budget: int):
    return store.write(
        text, tool_use_id="test-call", tool_name="test", session_id="test-session",
        session_key="test-session", agent_id="main", disk_budget_bytes=budget,
    )


def test_spool_reservation_shares_snapshot_budget_and_is_not_evicted(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path)
    spool = _spool(store)
    try:
        spool.append(b"retained")
        os.utime(spool.record_dir / "output.spool", (1, 1))
        # Expiry must not delete a running spool, and its full reservation must
        # count even though the file is currently only eight bytes long.
        with pytest.raises(ToolResultStoreBudgetError):
            _write(store, "x" * 1100, budget=2048)
        assert spool.prefix() == b"retained"
        assert spool.record_dir.exists()
        record = _write(store, "x" * 1024, budget=2048)
        assert store.read(record.handle, session_id="test-session").content == "x" * 1024
        handle = spool.finish("retained")
        assert store.read(handle, session_id="test-session").content == "retained"
        # Completed records return their unused reservation to the same budget.
        _write(store, "y" * 1900, budget=2048)
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
            "except ToolResultStoreBudgetError:\n print('budget protected')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=10,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "budget protected"
        assert spool.record_dir.exists()
    finally:
        spool.close()


async def test_capture_drains_past_spool_cap_and_retains_latest_diagnostics(tmp_path: Path) -> None:
    token = current_tool_context.set(ToolContext(
        session_key="test-session", tool_result_store_dir=str(tmp_path), agent_id="main",
    ))
    try:
        capture = await BoundedOutputCapture.create("exec")
    finally:
        current_tool_context.reset(token)
    assert capture.spool is not None
    assert capture.spool.max_bytes == DEFAULT_TOOL_RESULT_MAX_BYTES
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
        record = ToolResultStore(tmp_path).read(capture.handle, session_id="test-session")
        assert record.size_bytes <= DEFAULT_TOOL_RESULT_MAX_BYTES
        assert "first diagnostic" in record.content
        assert "LATEST EXIT DIAGNOSTIC" in record.content
        assert "output omitted between retained prefix" in record.content
        assert capture.describe()["retained_output_complete"] is False
        assert "not a full log" in capture.notice()
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
    assert "last failure diagnostic" in record.content
    assert "omitted" in record.content
    assert "not a full log" in capture.notice()


def test_split_utf8_is_decoded_after_bounded_capture() -> None:
    capture = BoundedOutputCapture(preview_bytes=32)
    encoded = "alpha你好omega".encode()
    for byte in encoded:
        capture.feed(bytes([byte]))
    assert capture.preview() == "alpha你好omega"


async def test_exec_timeout_preserves_output_and_retrievable_retained_text(tmp_path: Path) -> None:
    token = current_tool_context.set(ToolContext(
        session_key="test-session", tool_result_store_dir=str(tmp_path), agent_id="main",
    ))
    code = "import sys,time; print('before waiting', flush=True); time.sleep(20)"
    argv = [sys.executable, "-c", code]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    try:
        result = await shell._run_host_shell_command(
            command, cwd=None, env=dict(os.environ), stdin_bytes=None, effective_timeout=0.5,
        )
    finally:
        current_tool_context.reset(token)
    assert "[timeout after 0.5s]" in result
    assert "before waiting" in result
    handle = result.split("tool_result_handle=", 1)[1].split(";", 1)[0]
    stored = ToolResultStore(tmp_path).read(handle, session_id="test-session")
    assert "before waiting" in stored.content


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
            "log", session_id=session_id, offset=max(0, len(restored) - 100), limit=100,
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
    from opensquilla.tools.output_capture import (
        OUTPUT_FINALIZE_WAIT_SECONDS,
        OUTPUT_SETUP_WAIT_SECONDS,
    )
    from opensquilla.tools.registry import get_default_registry

    spec = get_default_registry().get("execute_code").spec
    required_padding = (
        OUTPUT_SETUP_WAIT_SECONDS + OUTPUT_FINALIZE_WAIT_SECONDS
        + shell._EXEC_TERMINATE_TIMEOUT + shell._EXEC_KILL_TIMEOUT
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
async def test_capture_setup_wait_is_bounded_and_closes_late_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    import threading

    from opensquilla.tools import output_capture

    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    spools = []
    original_open = ToolResultStore.open_output_spool

    def stalled_open(store, **kwargs):
        spool = original_open(store, **kwargs)
        spools.append(spool)
        original_close = spool.close

        def observed_close():
            original_close()
            closed.set()

        spool.close = observed_close
        entered.set()
        assert release.wait(timeout=5)
        return spool

    monkeypatch.setattr(ToolResultStore, "open_output_spool", stalled_open)
    monkeypatch.setattr(output_capture, "OUTPUT_SETUP_WAIT_SECONDS", 0.05)
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
        done, _pending = await asyncio.wait({task}, timeout=1)
        assert task in done
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                task.result()
        else:
            capture = task.result()
            assert capture.spool is None
            assert capture.storage_error == "OutputSetupTimeout"
            # The normal execution/drain/finalize path stays usable even while
            # the abandoned setup worker still owns its reservation.
            reader = asyncio.StreamReader()
            reader.feed_data(b"command completed after slow setup\n")
            reader.feed_eof()
            await capture.drain(reader)
            await capture.finish_async()
            assert "command completed" in capture.preview()
            assert "tool_result_handle" not in capture.describe()
        assert not spools[0].lease.closed
        assert ToolResultStore(tmp_path)._iter_record_stats()[0].active
    finally:
        release.set()
        assert await asyncio.to_thread(closed.wait, 2)
    assert spools[0].lease.closed


async def test_capture_respects_operator_budget_overrides(tmp_path: Path) -> None:
    token = current_tool_context.set(ToolContext(
        session_key="test-session", tool_result_store_dir=str(tmp_path),
        tool_result_store_max_bytes=128, tool_result_store_disk_budget_bytes=128,
        tool_result_store_retention_seconds=0,
    ))
    try:
        capture = await BoundedOutputCapture.create("exec")
        assert capture.spool is not None and capture.spool.max_bytes == 128
        denied = await BoundedOutputCapture.create("exec")
        assert denied.spool is None
        assert denied.storage_error == "ToolResultStoreBudgetError"
        capture.feed(b"FIRST_OUTPUT_835\n" + b"x" * 1024 + b"\nFINAL_EIO_927")
        await capture.finish_async()
        assert capture.handle is not None
        record = ToolResultStore(tmp_path).read(capture.handle, session_id="test-session")
        assert record.size_bytes <= 128
        assert "FIRST_OUTPUT_835" in record.content
        assert "FINAL_EIO_927" in record.content
        assert capture.describe()["retained_output_complete"] is False
        # Completed background jobs release their large preview and query the
        # retained record. The actual error must survive that transition too.
        capture.release_preview()
        restored = await capture.preview_async()
        assert "FIRST_OUTPUT_835" in restored and "FINAL_EIO_927" in restored
    finally:
        current_tool_context.reset(token)


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
        handle = payload["output_capture"]["tool_result_handle"]
        retained = ToolResultStore(tmp_path / "store").read(handle, session_id="test-session")
        assert "stdout ready" in retained.content
        assert "stderr ready" in retained.content
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
    _write(ToolResultStore(tmp_path), "other" * 300, budget=1500)
    preview = await capture.preview_async()
    assert "last diagnostic" in preview
    assert "retained output unavailable" in preview


async def test_failed_storage_keeps_bounded_preview_for_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = BoundedOutputCapture(preview_bytes=256)
    capture.spool = _spool(ToolResultStore(tmp_path))

    def disk_full(_content: str) -> str:
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

    from opensquilla.subprocess_encoding import decode_subprocess_output
    from opensquilla.tools import output_capture

    # Exercise the Windows code-page decision on every host. A Python process
    # can forward a native child's legacy output while its own stream is UTF-8.
    monkeypatch.setattr(output_capture, "decode_subprocess_output", partial(
        decode_subprocess_output, fallback_encoding=fallback_encoding,
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
    assert record.size_bytes <= capture.spool.max_bytes
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
    from opensquilla.subprocess_encoding import decode_subprocess_output
    from opensquilla.tools import output_capture
    from opensquilla.tools.builtin import code_exec

    monkeypatch.setattr(output_capture, "decode_subprocess_output", partial(
        decode_subprocess_output, fallback_encoding=fallback_encoding,
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
        retained = ToolResultStore(tmp_path / "store").read(
            payload["output_capture"]["tool_result_handle"], session_id="test-session",
        ).content
        assert f"[stdout]\n{utf8_text}" in retained
        assert f"[stderr]\n{legacy_text}" in retained
        assert "�" not in retained
        assert payload["output_capture"]["retained_output_complete"] is True
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
    assert "FINAL_WRITE_ERROR_396" in retained
    assert capture.describe()["retained_output_complete"] is False


def test_decoding_expansion_preserves_latest_diagnostic_within_spool_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from functools import partial

    from opensquilla.subprocess_encoding import decode_subprocess_output
    from opensquilla.tools import output_capture

    monkeypatch.setattr(output_capture, "decode_subprocess_output", partial(
        decode_subprocess_output, fallback_encoding="cp936",
    ))
    capture = BoundedOutputCapture()
    capture.spool = _spool(ToolResultStore(tmp_path), size=128)
    raw = ("中" * 48 + "FINAL_EIO_748").encode("cp936")
    assert len(raw) < capture.spool.max_bytes < len(raw.decode("cp936").encode())
    capture.feed(raw)
    capture.finish()
    assert capture.handle is not None
    record = ToolResultStore(tmp_path).read(capture.handle, session_id="test-session")
    assert "FINAL_EIO_748" in record.content
    assert record.size_bytes <= capture.spool.max_bytes
    assert capture.describe()["retained_output_complete"] is False


@pytest.mark.parametrize("spool_bytes", [1, 4, 5, 6, 127, 128])
def test_capped_multistream_frames_never_expand_disk_budget(
    tmp_path: Path, spool_bytes: int,
) -> None:
    capture = BoundedOutputCapture(streams=("stdout", "stderr"))
    capture.spool = _spool(ToolResultStore(tmp_path), size=spool_bytes)
    capture.feed("prefix你好".encode(), "stdout")
    capture.feed(b"x" * 256 + b"FINAL_FRAME_684", "stderr")
    capture.finish()
    assert capture.handle is not None
    record = ToolResultStore(tmp_path).read(capture.handle, session_id="test-session")
    assert record.size_bytes <= spool_bytes
    assert capture.spool.size <= spool_bytes
    assert capture.describe()["retained_output_complete"] is False
    if spool_bytes >= 127:
        assert "FINAL_FRAME_684" in record.content



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


async def test_stop_bounds_finalization_without_releasing_live_writer_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    capture = BoundedOutputCapture()
    store = ToolResultStore(tmp_path)
    capture.spool = _spool(store)
    entered, release = threading.Event(), threading.Event()
    original_append = capture.spool.append
    original_finish = capture.finish
    finalizer_calls = 0

    def blocked_append(chunk: bytes) -> None:
        entered.set()
        assert release.wait(timeout=5)
        original_append(chunk)

    def counted_finish() -> None:
        nonlocal finalizer_calls
        finalizer_calls += 1
        original_finish()

    monkeypatch.setattr(capture.spool, "append", blocked_append)
    monkeypatch.setattr(capture, "finish", counted_finish)
    writer = asyncio.create_task(asyncio.to_thread(capture.feed, b"last diagnostic"))
    assert await asyncio.to_thread(entered.wait, 1)
    started = asyncio.Event()

    async def cancelled_tool() -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            await capture.finish_async()
            capture.release_preview()
            # A second finally must not add another worker or wait budget.
            await capture.finish_async()

    task = asyncio.create_task(cancelled_tool())
    await started.wait()
    try:
        task.cancel()
        done, _pending = await asyncio.wait({task}, timeout=1)
        assert task in done
        with pytest.raises(asyncio.CancelledError):
            task.result()
        assert finalizer_calls == 1
        assert capture.describe()["finalization_pending"] is True
        assert "tool_result_handle" not in capture.describe()
        assert "not yet persisted" in capture.notice()
        assert "last diagnostic" in capture.preview()
        assert not capture.spool.lease.closed
        assert store._iter_record_stats()[0].active
    finally:
        release.set()
        await writer
        assert capture._finish_task is not None
        await asyncio.wait_for(asyncio.shield(capture._finish_task), timeout=2)
    assert capture.spool.lease.closed
    assert capture.handle is not None
    assert store.read(capture.handle, session_id="test-session").content == "last diagnostic"
    assert "finalization_pending" not in capture.describe()
    assert not capture.previews["stdout"].head


async def test_normal_finalization_observation_is_bounded_and_reuses_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from opensquilla.tools import output_capture

    capture = BoundedOutputCapture()
    capture.spool = _spool(ToolResultStore(tmp_path))
    entered, release = threading.Event(), threading.Event()
    original_finish = capture.finish
    calls = 0

    def delayed_finish() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=5)
        original_finish()

    assert output_capture.OUTPUT_FINALIZE_WAIT_SECONDS == 5
    monkeypatch.setattr(output_capture, "OUTPUT_FINALIZE_WAIT_SECONDS", 0.02)
    monkeypatch.setattr(capture, "finish", delayed_finish)
    capture.feed(b"result already produced")
    try:
        await asyncio.wait_for(capture.finish_async(), timeout=1)
        assert entered.is_set()
        for _ in range(3):
            await capture.finish_async()
        assert calls == 1
        assert capture.describe()["finalization_pending"] is True
    finally:
        release.set()
        assert capture._finish_task is not None
        await asyncio.wait_for(asyncio.shield(capture._finish_task), timeout=2)
    assert capture.spool.lease.closed
    assert capture.handle is not None


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
