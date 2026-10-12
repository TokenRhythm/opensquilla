from __future__ import annotations

import asyncio
import builtins
import contextlib
import threading
from types import SimpleNamespace

import pytest

from opensquilla.application.observability import LogTailQuery
from opensquilla.gateway import log_status_runtime, rpc_logs, rpc_memory
from opensquilla.gateway.rpc import RpcContext


@pytest.mark.parametrize(
    "operation", ["memory.list", "memory.show", "logs.tail", "logs.trace", "logs.status"],
)
async def test_slow_inspection_io_leaves_gateway_loop_available(
    operation, tmp_path, monkeypatch,
):
    """Hold a real reader at its I/O boundary until the loop releases it."""
    (tmp_path / "MEMORY.md").write_text("remember this\n", encoding="utf-8")
    (tmp_path / "debug.log").write_text("[INFO] hello\n", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    ctx = RpcContext(
        conn_id="file-worker-test",
        memory_managers={"main": SimpleNamespace(workspace_dir=tmp_path)},
    )
    calls = {
        "memory.list": (rpc_memory, "_memory_source_rows", rpc_memory._handle_memory_list, {}),
        "memory.show": (
            rpc_memory, "_read_memory_source", rpc_memory._handle_memory_show,
            {"path": "MEMORY.md"},
        ),
        "logs.tail": (rpc_logs, "read_log_tail", rpc_logs._handle_logs_tail, {}),
        "logs.trace": (
            rpc_logs, "load_trace_events", rpc_logs._handle_logs_trace, {"trace_id": "absent"},
        ),
        "logs.status": (
            log_status_runtime, "read_log_status", rpc_logs._handle_logs_status, {},
        ),
    }
    module, name, handler, params = calls[operation]
    original = getattr(module, name)
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    worker_threads = []

    def slow_reader(*args, **kwargs):
        worker_threads.append(threading.get_ident())
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(5):
            raise TimeoutError("The event loop could not release the file reader")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, slow_reader)
    request = asyncio.create_task(handler(params, ctx))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        assert not request.done()
        # This control callback can run while the filesystem operation is held.
        callback = loop.create_future()
        loop.call_soon(callback.set_result, "responsive")
        assert await asyncio.wait_for(callback, timeout=1) == "responsive"
        assert worker_threads and all(thread != loop_thread for thread in worker_threads)
        release.set()
        result = await asyncio.wait_for(request, timeout=5)
        if operation == "memory.list":
            assert [row["path"] for row in result["files"]] == ["MEMORY.md"]
        elif operation == "memory.show":
            assert result["content"] == "remember this\n"
        elif operation == "logs.tail":
            assert result["lines"] == ["[INFO] hello"]
        elif operation == "logs.trace":
            assert result["events"] == []
        else:
            assert result["gateway_file_log"]["exists"] is True
    finally:
        release.set()
        if not request.done():
            request.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(request, timeout=5)


def test_log_tail_streams_and_keeps_older_level_matches(tmp_path, monkeypatch):
    """A byte-window shortcut would silently lose this older matching record."""
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    log_file = tmp_path / "debug.log"
    log_file.write_bytes(
        "[INFO] 中文 first\r\n".encode()
        + b"[DEBUG] " + b"x" * (1024 * 1024 + 1) + b"\r\n"
        + b"[INFO] last\r\n"
    )

    class StreamingFile:
        def __init__(self, *args, **kwargs):
            self.file = builtins.open(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def seek(self, *args):
            return self.file.seek(*args)

        def tell(self):
            return self.file.tell()

        def readline(self):
            return self.file.readline()

        def readlines(self):
            pytest.fail("The full log must not be materialized")

    monkeypatch.setattr(rpc_logs, "open", StreamingFile, raising=False)
    result = rpc_logs.read_log_tail(LogTailQuery(limit=2, level="INFO"))
    assert result == {
        "lines": ["[INFO] 中文 first", "[INFO] last"],
        "cursor": log_file.stat().st_size,
        "has_more": False,
    }
    limited = rpc_logs.read_log_tail(LogTailQuery(limit=1, level="INFO"))
    assert limited["lines"] == ["[INFO] last"]
    assert limited["has_more"] is True

    with builtins.open(log_file, "ab") as writer:
        writer.write("[INFO] 新增\r\n".encode())
    appended = rpc_logs.read_log_tail(LogTailQuery(cursor=result["cursor"], limit=2))
    assert appended == {
        "lines": ["[INFO] 新增"],
        "cursor": log_file.stat().st_size,
        "has_more": False,
    }
