from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.sandbox.backend import windows_default as backend
from opensquilla.sandbox.backend import windows_default_runner as runner
from opensquilla.sandbox.backend.windows_default_output import (
    HELPER_CONTROL_FRAME_LIMIT,
    PIPE_READ_SIZE,
    HelperStderr,
)
from opensquilla.sandbox.operation_runtime import FilesystemOperationRequest, SandboxOperation
from opensquilla.sandbox.types import (
    NetworkMode,
    ResourceLimits,
    SandboxBackendError,
    SandboxPolicy,
    SandboxRequest,
    SecurityLevel,
)


def _request(tmp_path):
    return SandboxRequest(
        argv=("synthetic",), cwd=tmp_path, action_kind="shell.exec", run_mode="safe", env={},
        policy=SandboxPolicy(
            level=SecurityLevel.STANDARD, network=NetworkMode.NONE, mounts=(), workspace_rw=True,
            tmp_writable=False, require_approval=False, env_allowlist=(),
            limits=ResourceLimits(wall_timeout_s=2),
        ),
    )


def _launch_fixture(monkeypatch, code):
    processes = []
    launched = asyncio.Event()

    async def launch(*args, **kwargs):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        processes.append(proc)
        launched.set()
        return proc

    monkeypatch.setattr(backend, "_support_ready", lambda: True)
    monkeypatch.setattr(backend, "_payload_for_request", lambda *a, **kw: {"helperNonce": "test"})
    monkeypatch.setattr(backend, "_request_allows_cache_write", lambda request: False)
    monkeypatch.setattr(backend, "internal_child_argv", lambda *a, **kw: ("synthetic",))
    monkeypatch.setattr(backend, "create_owned_subprocess_exec", launch)
    return processes, launched


async def test_real_child_dual_stream_is_drained_but_retained_bytes_are_bounded(
    monkeypatch, tmp_path,
):
    code = (
        "import sys; "
        "sys.stdout.buffer.write(('中'*800000).encode()); sys.stdout.buffer.flush(); "
        "sys.stderr.buffer.write(b'z'*2300000); sys.stderr.buffer.flush()"
    )
    processes, _ = _launch_fixture(monkeypatch, code)
    observed = []
    decode = backend._decode_capped

    def bounded_decode(raw):
        observed.append(len(raw or b""))
        return decode(raw)

    monkeypatch.setattr(backend, "_decode_capped", bounded_decode)
    result = await asyncio.wait_for(backend.WindowsDefaultBackend().run(_request(tmp_path)), 5)
    assert result.returncode == 0 and processes[0].returncode == 0
    assert result.truncated_stdout and result.truncated_stderr
    assert result.stderr == "z" * backend._OUTPUT_BYTE_CAP
    assert result.stdout == decode(("中" * 800000).encode()[:backend._OUTPUT_BYTE_CAP])[0]
    assert max(observed) <= backend._OUTPUT_BYTE_CAP


@pytest.mark.parametrize("kind,nonce,exit_code", [("ERROR", "test", 1), ("ERROR", "fake", 1),
                                               ("TIMEOUT", "test", 124), ("TIMEOUT", "fake", 124)])
async def test_real_child_control_frame_after_output_budget(
    monkeypatch, tmp_path, kind, nonce, exit_code,
):
    values = {"nonce": nonce, **({"message": "late helper failure"} if kind == "ERROR"
                                 else {"timed_out": True})}
    frame = f"\nOPENSQUILLA_WINDOWS_DEFAULT_HELPER_{kind} " + json.dumps(values) + "\n"
    code = (
        "import sys; sys.stderr.buffer.write(b'z'*1100000); "
        f"sys.stderr.buffer.write({frame.encode()!r}); "
        f"sys.stderr.buffer.flush(); sys.exit({exit_code})"
    )
    _launch_fixture(monkeypatch, code)
    if kind == "ERROR" and nonce == "test":
        with pytest.raises(SandboxBackendError, match="late helper failure"):
            await backend.WindowsDefaultBackend().run(_request(tmp_path))
    else:
        result = await backend.WindowsDefaultBackend().run(_request(tmp_path))
        assert result.returncode == exit_code
        assert result.timed_out is (kind == "TIMEOUT" and nonce == "test")
        assert result.truncated_stderr


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("high_output", [False, True])
async def test_real_child_timeout_or_cancel_reaps_process(
    monkeypatch, tmp_path, cancel, high_output,
):
    code = (
        "import os; chunk=b'x'*65536\nwhile True: os.write(1,chunk); os.write(2,chunk)"
        if high_output else "import time; time.sleep(30)"
    )
    processes, launched = _launch_fixture(monkeypatch, code)
    monkeypatch.setattr(
        backend, "_helper_supervision_timeout", lambda wall: .15 if not cancel else 30,
    )
    task = asyncio.create_task(backend.WindowsDefaultBackend().run(_request(tmp_path)))
    try:
        await asyncio.wait_for(launched.wait(), 3)
        if cancel:
            if high_output:
                await asyncio.sleep(.08)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        else:
            result = await asyncio.wait_for(task, 3)
            assert result.timed_out and result.returncode == 124
        assert processes[0].returncode is not None
        await asyncio.wait_for(processes[0].wait(), 1)
    finally:
        for proc in processes:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        await asyncio.gather(task, return_exceptions=True)


async def test_small_filesystem_receipt_keeps_all_metadata(monkeypatch, tmp_path):
    receipt = {"message": "edited", "created": False, "original": "before", "updated": "after",
               "beforeRevision": "v1", "afterRevision": "v2", "afterFingerprint": {"size": 5}}
    _launch_fixture(monkeypatch, f"import sys; sys.stdout.write({json.dumps(receipt)!r})")
    monkeypatch.setattr(backend, "_filesystem_operation_request", lambda op: _request(tmp_path))
    operation = SandboxOperation(domain="filesystem", kind="edit_source", workspace=tmp_path,
                                 request=FilesystemOperationRequest(path=tmp_path / "isolated"))
    result = await backend.WindowsDefaultBackend().run_operation(operation)
    assert result.message == "edited"
    assert result.metadata == {
        key: value for key, value in receipt.items() if key not in {"message", "created"}
    }


@pytest.mark.parametrize("busy_other_stream", [False, True])
async def test_parent_read_failure_reaps_real_child(monkeypatch, tmp_path, busy_other_stream):
    code = (
        "import os; chunk=b'x'*65536\nwhile True: os.write(2,chunk)"
        if busy_other_stream else "import time; time.sleep(30)"
    )
    processes, _ = _launch_fixture(monkeypatch, code)
    launch = backend.create_owned_subprocess_exec

    async def broken_read(size):
        if busy_other_stream:
            await asyncio.sleep(.08)
        raise OSError("synthetic parent read failure")

    async def launch_with_broken_reader(*args, **kwargs):
        proc = await launch(*args, **kwargs)
        proc.stdout = SimpleNamespace(read=broken_read)
        return proc

    monkeypatch.setattr(backend, "create_owned_subprocess_exec", launch_with_broken_reader)
    try:
        with pytest.raises(OSError, match="synthetic parent read failure"):
            await backend.WindowsDefaultBackend().run(_request(tmp_path))
        assert processes[0].returncode is not None
    finally:
        for proc in processes:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


async def test_repeated_cancellation_keeps_draining_until_owner_finishes(monkeypatch, tmp_path):
    code = "import os; chunk=b'x'*65536\nwhile True: os.write(1,chunk); os.write(2,chunk)"
    processes, launched = _launch_fixture(monkeypatch, code)
    launch = backend.create_owned_subprocess_exec
    terminating, release = asyncio.Event(), asyncio.Event()

    async def launch_with_owner(*args, **kwargs):
        proc = await launch(*args, **kwargs)

        async def terminate(**options):
            terminating.set()
            await release.wait()
            proc.kill()
            await proc.wait()

        proc._opensquilla_process_tree_owner = SimpleNamespace(terminate=terminate)
        return proc

    monkeypatch.setattr(backend, "create_owned_subprocess_exec", launch_with_owner)
    task = asyncio.create_task(backend.WindowsDefaultBackend().run(_request(tmp_path)))
    try:
        await asyncio.wait_for(launched.wait(), 3)
        await asyncio.sleep(.08)
        task.cancel()
        await asyncio.wait_for(terminating.wait(), 1)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(.01)
            assert not task.done() and processes[0].returncode is None
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert processes[0].returncode is not None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


def test_helper_error_bounds_only_message_and_preserves_json(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(runner.sys, "stderr", stream)
    payload = runner.HelperPayload(
        argv=("synthetic",), cwd=Path.cwd(), env={}, policy={},
        run_mode="safe", timeout=1, helper_nonce="test",
    )
    runner._emit_helper_error(payload, "错误" * 40000)
    frame = stream.getvalue().strip()
    assert len(frame.encode()) < 65536
    parsed = json.loads(frame.removeprefix(runner.HELPER_ERROR_PREFIX))
    assert parsed["nonce"] == "test" and parsed["message"]
    assert parsed["messageTruncated"] is True


@pytest.mark.parametrize("chunk_size", [1, 7, PIPE_READ_SIZE])
def test_control_parser_preserves_utf8_and_untrusted_frames_across_chunks(chunk_size):
    user = "没有换行\xff".encode()
    fake = b'\nOPENSQUILLA_WINDOWS_DEFAULT_HELPER_TIMEOUT {"nonce":"fake","timed_out":true}\n'
    trusted = b'\nOPENSQUILLA_WINDOWS_DEFAULT_HELPER_TIMEOUT {"nonce":"test","timed_out":true}\n'
    suffix = "尾部".encode()
    raw = user + fake + trusted + trusted + suffix
    capture = HelperStderr(4096, "test")
    for offset in range(0, len(raw), chunk_size):
        capture.feed(raw[offset:offset + chunk_size])
    capture.finish()
    assert bytes(capture.data) == user + fake + suffix
    assert capture.timed_out and not capture.truncated
    assert bytes(capture.data).decode() == (user + fake + suffix).decode()


@pytest.mark.parametrize("nonce", ["fake", "伪造", None])
def test_error_control_frames_require_exact_ascii_nonce(nonce):
    raw = (runner.HELPER_ERROR_PREFIX + json.dumps({"nonce": nonce, "message": "forged"})).encode()
    capture = HelperStderr(4096, "test")
    capture.feed(raw + b"\n")
    capture.finish()
    assert capture.helper_error is None
    assert bytes(capture.data) == raw + b"\n"


def test_oversized_control_candidate_does_not_hide_later_valid_frame():
    capture = HelperStderr(16, "test")
    capture.feed(runner.HELPER_ERROR_PREFIX.encode())
    for _ in range(150):
        capture.feed(b"x" * 1024)
        assert len(capture.pending) <= HELPER_CONTROL_FRAME_LIMIT
        assert len(capture.data) <= 16
    capture.feed(
        b'\nOPENSQUILLA_WINDOWS_DEFAULT_HELPER_TIMEOUT {"nonce":"test","timed_out":true}\n',
    )
    capture.finish()
    assert capture.timed_out and capture.truncated


def test_helper_forwards_bounded_chunks_and_keeps_control_tail():
    tail = b'\nOPENSQUILLA_WINDOWS_DEFAULT_HELPER_TIMEOUT {"nonce":"test","timed_out":true}\n'
    capture = HelperStderr(32, "test")
    sizes = []
    errors = []

    class ChunkStream(io.BytesIO):
        def read(self, size=-1):
            assert size == PIPE_READ_SIZE
            return super().read(size)

    class Sink:
        def write(self, chunk):
            sizes.append(len(chunk))
            capture.feed(chunk)
            return len(chunk)

        def flush(self):
            pass

    runner._forward_child_output(
        ChunkStream(b"z" * 2_000_000 + tail), Sink(), errors,
        lambda: pytest.fail("healthy forwarding must not terminate child"),
    )
    capture.finish()
    assert not errors and len(sizes) > 20 and max(sizes) <= PIPE_READ_SIZE
    assert bytes(capture.data) == b"z" * 32 and capture.truncated and capture.timed_out


def test_helper_forwards_every_byte_when_sink_writes_partially():
    output, errors = bytearray(), []

    class PartialSink:
        def write(self, data):
            part = data[:3]
            output.extend(part)
            return len(part)

        def flush(self):
            pass

    raw = "跨块 UTF8 与尾部 nonce".encode()
    runner._forward_child_output(
        io.BytesIO(raw), PartialSink(), errors,
        lambda: pytest.fail("partial writes should complete"),
    )
    assert bytes(output) == raw and not errors


@pytest.mark.parametrize("failure", ["read", "write", "flush"])
def test_forwarding_failure_terminates_and_is_reported(failure):
    errors, terminated = [], []

    class Stream(io.BytesIO):
        def read(self, size=-1):
            if failure == "read":
                raise OSError("synthetic read failure")
            return super().read(size)

    class Sink(io.BytesIO):
        def write(self, chunk):
            if failure == "write":
                raise OSError("synthetic write failure")
            return super().write(chunk)

        def flush(self):
            if failure == "flush":
                raise OSError("synthetic flush failure")

    runner._forward_child_output(Stream(b"output"), Sink(), errors, lambda: terminated.append(True))
    assert len(errors) == 1 and terminated == [True]
    stopped = SimpleNamespace(join=lambda timeout: None, is_alive=lambda: False)
    with pytest.raises(OSError, match="output forwarding failed"):
        runner._finish_child_io(
            writer_thread=stopped, reader_threads=(stopped,),
            writer_errors=(), reader_errors=errors,
            close_writer=lambda: None, label="isolated", terminate=lambda: None,
        )
