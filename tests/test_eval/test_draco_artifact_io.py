from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from pathlib import Path

import pytest

from opensquilla.eval import draco_artifact_io as artifact_io
from opensquilla.eval.draco_artifact_integrity import (
    seal_result_row,
    trace_row_from_result,
)


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    return (
        tmp_path / "results.jsonl",
        tmp_path / "trace.jsonl",
        tmp_path / "checkpoint.json",
    )


def _pair(
    *, task_id: str = "任务-一", final_text: str = "中文结果"
) -> tuple[dict[str, object], dict[str, object]]:
    result = seal_result_row(
        {
            "group": "B0",
            "task_id": task_id,
            "row_index": 1,
            "final_text": final_text,
            "error": None,
        }
    )
    return result, trace_row_from_result(result)


def _durable_pair(
    *, task_id: str, final_text: str
) -> tuple[dict[str, object], dict[str, object]]:
    result = seal_result_row(
        {
            "group": "B0",
            "task_id": task_id,
            "row_index": 1,
            "final_text": final_text,
            "error": None,
            artifact_io.DRACO_DURABLE_RESULT_ROW_FIELD: (
                artifact_io.durable_artifact_capability_contract()
            ),
        }
    )
    return result, trace_row_from_result(result)


def _checkpoint(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_paid_pair_write_loops_over_short_writes_and_preserves_json_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_path, trace_path, checkpoint_path = _paths(tmp_path)
    result, trace = _pair()
    writer = artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    )
    real_write = artifact_io._write_once
    write_calls = 0

    def short_write(fd: int, payload: bytes) -> int:
        nonlocal write_calls
        write_calls += 1
        return real_write(fd, payload[:7])

    with monkeypatch.context() as scoped:
        scoped.setattr(artifact_io, "_write_once", short_write)
        with writer:
            assert writer.append(result, trace) is True

    assert write_calls > 2
    assert results_path.read_bytes() == (
        json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")
    assert trace_path.read_bytes() == (
        json.dumps(trace, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")
    assert _checkpoint(checkpoint_path)["rows_written"] == 1
    assert {
        path.stat().st_mode & 0o777 for path in (results_path, trace_path, checkpoint_path)
    } == {0o600}


def test_atomic_replace_failure_keeps_previous_document_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("old\n", encoding="utf-8")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("synthetic replace failure")

    real_fsync_directory = artifact_io.fsync_directory
    cleanup_barriers: list[Path] = []

    def record_cleanup_barrier(directory: Path) -> None:
        cleanup_barriers.append(Path(directory))
        real_fsync_directory(directory)

    monkeypatch.setattr(artifact_io, "_replace", fail_replace)
    monkeypatch.setattr(artifact_io, "fsync_directory", record_cleanup_barrier)
    with pytest.raises(OSError, match="synthetic replace failure"):
        artifact_io.atomic_write_text(path, "new\n")

    assert path.read_text(encoding="utf-8") == "old\n"
    assert list(tmp_path.glob(".manifest.json.*.tmp")) == []
    assert cleanup_barriers == [tmp_path]


def test_checkpoint_replace_failure_reopens_without_duplicate_sealed_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_path, trace_path, checkpoint_path = _paths(tmp_path)
    result, trace = _pair()
    writer = artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    )
    real_replace = artifact_io._replace

    def fail_checkpoint_replace(source: Path, destination: Path) -> None:
        if Path(destination) == checkpoint_path:
            raise OSError("synthetic checkpoint replace failure")
        real_replace(source, destination)

    with monkeypatch.context() as scoped:
        scoped.setattr(artifact_io, "_replace", fail_checkpoint_replace)
        with pytest.raises(OSError, match="synthetic checkpoint replace failure"):
            writer.append(result, trace)
    writer.close()

    assert len(results_path.read_text(encoding="utf-8").splitlines()) == 1
    assert len(trace_path.read_text(encoding="utf-8").splitlines()) == 1
    assert _checkpoint(checkpoint_path)["rows_written"] == 0

    with artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
        create=False,
    ) as reopened:
        assert reopened.paired_row_count == 1
        assert reopened.append(result, trace) is False

    assert len(results_path.read_text(encoding="utf-8").splitlines()) == 1
    assert len(trace_path.read_text(encoding="utf-8").splitlines()) == 1
    assert _checkpoint(checkpoint_path)["rows_written"] == 1


def test_reopen_repairs_result_durable_before_trace_without_duplicate_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_path, trace_path, checkpoint_path = _paths(tmp_path)
    result, trace = _pair()
    writer = artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    )
    real_write = artifact_io._write_once
    trace_fd = writer._trace_fd

    def fail_trace_write(fd: int, payload: bytes) -> int:
        if fd == trace_fd:
            raise OSError("synthetic trace write failure")
        return real_write(fd, payload)

    with monkeypatch.context() as scoped:
        scoped.setattr(artifact_io, "_write_once", fail_trace_write)
        with pytest.raises(OSError, match="synthetic trace write failure"):
            writer.append(result, trace)
    writer.close()

    assert len(results_path.read_text(encoding="utf-8").splitlines()) == 1
    assert trace_path.read_bytes() == b""
    assert _checkpoint(checkpoint_path)["rows_written"] == 0

    with artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
        create=False,
    ) as reopened:
        assert reopened.append(result, trace) is True
        assert reopened.paired_row_count == 1

    assert len(results_path.read_text(encoding="utf-8").splitlines()) == 1
    assert len(trace_path.read_text(encoding="utf-8").splitlines()) == 1
    assert _checkpoint(checkpoint_path)["rows_written"] == 1


def test_reopen_truncates_only_torn_trailing_line_before_retry(tmp_path: Path) -> None:
    results_path, trace_path, checkpoint_path = _paths(tmp_path)
    writer = artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    )
    writer.close()
    with results_path.open("ab") as handle:
        handle.write(b'{"group":"B0"')
        handle.flush()

    result, trace = _pair()
    with artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
        create=False,
    ) as reopened:
        assert reopened.append(result, trace) is True

    assert len(results_path.read_text(encoding="utf-8").splitlines()) == 1
    assert _checkpoint(checkpoint_path)["rows_written"] == 1


@pytest.mark.parametrize("artifact", ["result", "trace"])
def test_reopen_preserves_complete_sealed_tail_that_only_lacks_newline(
    tmp_path: Path,
    artifact: str,
) -> None:
    results_path, trace_path, checkpoint_path = _paths(tmp_path)
    result, trace = _pair()
    with artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    ) as writer:
        assert writer.append(result, trace) is True

    path = results_path if artifact == "result" else trace_path
    payload = path.read_bytes()
    assert payload.endswith(b"\n")
    path.write_bytes(payload[:-1])

    with artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
        create=False,
    ) as reopened:
        assert reopened.paired_row_count == 1
        assert reopened.append(result, trace) is False

    assert path.read_bytes() == payload
    assert _checkpoint(checkpoint_path)["rows_written"] == 1


def test_same_identity_tampered_trace_projection_fails_closed(tmp_path: Path) -> None:
    results_path, trace_path, checkpoint_path = _paths(tmp_path)
    result, trace = _pair()
    tampered = dict(trace)
    tampered["error"] = "forged"

    with artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    ) as writer:
        with pytest.raises(
            artifact_io.DracoArtifactDurabilityError,
            match="deterministic projection",
        ):
            writer.append(result, tampered)
        assert writer.append(result, trace) is True

    trace_path.write_bytes(
        (json.dumps(tampered, ensure_ascii=False, allow_nan=False) + "\n").encode()
    )
    with pytest.raises(
        artifact_io.DracoArtifactDurabilityError,
        match="trace projection mismatch",
    ):
        artifact_io.DurableDracoArtifactWriter(
            results_path=results_path,
            trace_path=trace_path,
            checkpoint_path=checkpoint_path,
            create=False,
        )


def test_readonly_verifier_binds_every_result_trace_and_checkpoint_byte(
    tmp_path: Path,
) -> None:
    results_path, trace_path, checkpoint_path = _paths(tmp_path)
    result, trace = _pair()
    with artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    ) as writer:
        writer.append(result, trace)

    verification = artifact_io.verify_durable_draco_artifacts(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    )
    assert verification["rows_written"] == 1
    assert verification["results_bytes"] == results_path.stat().st_size
    assert verification["trace_bytes"] == trace_path.stat().st_size
    assert len(verification["results_sha256"]) == 64
    assert len(verification["trace_sha256"]) == 64
    assert len(verification["checkpoint_sha256"]) == 64


def test_streaming_verifier_preserves_golden_checkpoint_offsets_and_durable_keys(
    tmp_path: Path,
) -> None:
    results_path, trace_path, checkpoint_path = _paths(tmp_path)
    pairs = [
        _durable_pair(task_id="task-short", final_text="one"),
        _durable_pair(task_id="任务-long", final_text="界" * 137),
    ]
    with artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    ) as writer:
        for result, trace in pairs:
            assert writer.append(result, trace) is True

    results_payload = results_path.read_bytes()
    trace_payload = trace_path.read_bytes()
    checkpoint_payload = checkpoint_path.read_bytes()
    checkpoint = json.loads(checkpoint_payload)

    verification = artifact_io.verify_durable_draco_artifacts(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    )

    assert verification["results_bytes"] == len(results_payload)
    assert verification["trace_bytes"] == len(trace_payload)
    assert verification["results_sha256"] == hashlib.sha256(results_payload).hexdigest()
    assert verification["trace_sha256"] == hashlib.sha256(trace_payload).hexdigest()
    assert verification["checkpoint_sha256"] == hashlib.sha256(
        checkpoint_payload
    ).hexdigest()
    assert verification["durable_result_keys"] == (
        ("B0", "task-short"),
        ("B0", "任务-long"),
    )
    assert verification["paired_rows_sha256"] == checkpoint["paired_rows_sha256"]
    assert checkpoint["results_bytes"] == sum(
        len(line) for line in results_payload.splitlines(keepends=True)
    )
    assert checkpoint["trace_bytes"] == sum(
        len(line) for line in trace_payload.splitlines(keepends=True)
    )
    assert checkpoint_path.read_bytes() == checkpoint_payload
    result_lengths = [
        len(line) for line in results_payload.splitlines(keepends=True)
    ]
    trace_lengths = [
        len(line) for line in trace_payload.splitlines(keepends=True)
    ]
    with artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
        create=False,
    ) as reopened:
        assert [
            (line.start_offset, line.end_offset) for line in reopened._results
        ] == [
            (0, result_lengths[0]),
            (result_lengths[0], sum(result_lengths)),
        ]
        assert [
            (line.start_offset, line.end_offset) for line in reopened._traces
        ] == [
            (0, trace_lengths[0]),
            (trace_lengths[0], sum(trace_lengths)),
        ]


def test_verifier_hashes_the_checkpoint_bytes_it_parsed_when_path_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_path, trace_path, checkpoint_path = _paths(tmp_path)
    result, trace = _durable_pair(task_id="task-1", final_text="answer")
    with artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    ) as writer:
        writer.append(result, trace)

    checkpoint_payload = checkpoint_path.read_bytes()
    replacement_payload = checkpoint_payload + b" "
    real_json_loads = artifact_io.json.loads
    replaced = False

    def replace_path_after_checkpoint_parse(payload: object, *args: object, **kwargs: object):
        nonlocal replaced
        value = real_json_loads(payload, *args, **kwargs)
        if (
            not replaced
            and isinstance(value, dict)
            and value.get("schema") == artifact_io.DRACO_ARTIFACT_CHECKPOINT_SCHEMA
        ):
            replacement_path = tmp_path / "replacement.checkpoint.json"
            replacement_path.write_bytes(replacement_payload)
            replacement_path.replace(checkpoint_path)
            replaced = True
        return value

    monkeypatch.setattr(artifact_io.json, "loads", replace_path_after_checkpoint_parse)
    verification = artifact_io.verify_durable_draco_artifacts(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    )

    assert replaced is True
    assert checkpoint_path.read_bytes() == replacement_payload
    assert verification["checkpoint_sha256"] == hashlib.sha256(
        checkpoint_payload
    ).hexdigest()


def test_exact_checkpoint_reopen_retries_parent_directory_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_path, trace_path, checkpoint_path = _paths(tmp_path)
    result, trace = _pair()
    writer = artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    )
    real_fsync_directory = artifact_io.fsync_directory
    failed = False

    def fail_checkpoint_directory_once(path: Path) -> None:
        nonlocal failed
        if Path(path) == checkpoint_path.parent and not failed:
            failed = True
            raise OSError("synthetic checkpoint directory fsync failure")
        real_fsync_directory(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(
            artifact_io,
            "fsync_directory",
            fail_checkpoint_directory_once,
        )
        with pytest.raises(OSError, match="checkpoint directory fsync failure"):
            writer.append(result, trace)
    writer.close()
    assert _checkpoint(checkpoint_path)["rows_written"] == 1

    reopen_barriers: list[Path] = []

    def record_reopen_barrier(path: Path) -> None:
        reopen_barriers.append(Path(path))
        real_fsync_directory(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(artifact_io, "fsync_directory", record_reopen_barrier)
        with artifact_io.DurableDracoArtifactWriter(
            results_path=results_path,
            trace_path=trace_path,
            checkpoint_path=checkpoint_path,
            create=False,
        ) as reopened:
            assert reopened.paired_row_count == 1
    assert checkpoint_path.parent in reopen_barriers
    assert (
        artifact_io.verify_durable_draco_artifacts(
            results_path=results_path,
            trace_path=trace_path,
            checkpoint_path=checkpoint_path,
        )["rows_written"]
        == 1
    )


def test_run_lock_acquire_failure_releases_fd_and_kernel_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "run.lock"
    lock = artifact_io.DracoArtifactRunLock(lock_path)

    with monkeypatch.context() as scoped:
        scoped.setattr(
            artifact_io.os,
            "fsync",
            lambda _fd: (_ for _ in ()).throw(OSError("synthetic lock fsync failure")),
        )
        with pytest.raises(OSError, match="lock fsync failure"):
            lock.acquire()

    assert lock._fd is None
    with artifact_io.DracoArtifactRunLock(lock_path):
        pass


def test_run_lock_unlock_failure_still_closes_fd_and_clears_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "run.lock"
    lock = artifact_io.DracoArtifactRunLock(lock_path)
    lock.acquire()
    locked_fd = lock._fd
    assert locked_fd is not None
    real_flock = artifact_io.fcntl.flock

    def fail_unlock(fd: int, operation: int) -> None:
        if operation == artifact_io.fcntl.LOCK_UN:
            raise OSError("synthetic unlock failure")
        real_flock(fd, operation)

    with monkeypatch.context() as scoped:
        scoped.setattr(artifact_io.fcntl, "flock", fail_unlock)
        with pytest.raises(OSError, match="unlock failure"):
            lock.close()

    assert lock._fd is None
    with pytest.raises(OSError):
        artifact_io.os.fstat(locked_fd)
    with artifact_io.DracoArtifactRunLock(lock_path):
        pass


@pytest.mark.asyncio
async def test_async_writer_keeps_event_loop_responsive_during_slow_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_path, trace_path, checkpoint_path = _paths(tmp_path)
    result, trace = _pair()
    sync_writer = artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    )
    writer = artifact_io.AsyncDurableDracoArtifactWriter(sync_writer)
    real_fsync = artifact_io.os.fsync

    def slow_fsync(fd: int) -> None:
        time.sleep(0.02)
        real_fsync(fd)

    monkeypatch.setattr(artifact_io.os, "fsync", slow_fsync)
    ticks = 0
    append_task = asyncio.create_task(writer.append(result, trace))
    while not append_task.done():
        await asyncio.sleep(0.005)
        ticks += 1
    outcome = await append_task
    await writer.aclose()

    assert outcome.committed is True
    assert outcome.paired_row_count == 1
    assert ticks >= 3


@pytest.mark.asyncio
async def test_async_writer_settles_physical_write_before_propagating_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_path, trace_path, checkpoint_path = _paths(tmp_path)
    result, trace = _pair()
    sync_writer = artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    )
    writer = artifact_io.AsyncDurableDracoArtifactWriter(sync_writer)
    real_fsync = artifact_io.os.fsync
    entered = threading.Event()
    release = threading.Event()
    blocked_once = False

    def blocking_fsync(fd: int) -> None:
        nonlocal blocked_once
        if not blocked_once:
            blocked_once = True
            entered.set()
            assert release.wait(timeout=2)
        real_fsync(fd)

    monkeypatch.setattr(artifact_io.os, "fsync", blocking_fsync)
    append_task = asyncio.create_task(writer.append(result, trace))
    assert await asyncio.to_thread(entered.wait, 1)
    append_task.cancel()
    await asyncio.sleep(0)
    assert not append_task.done()
    assert writer.state == "settling_cancel"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await append_task
    assert writer.paired_row_count == 1
    assert writer.state == "idle"
    await writer.aclose()

    verification = artifact_io.verify_durable_draco_artifacts(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    )
    assert verification["rows_written"] == 1


@pytest.mark.asyncio
async def test_async_writer_settles_close_worker_before_propagating_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_path, trace_path, checkpoint_path = _paths(tmp_path)
    sync_writer = artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    )
    writer = artifact_io.AsyncDurableDracoArtifactWriter(sync_writer)
    real_close = sync_writer.close
    entered = threading.Event()
    release = threading.Event()

    def blocking_close() -> None:
        entered.set()
        assert release.wait(timeout=2)
        real_close()

    monkeypatch.setattr(sync_writer, "close", blocking_close)
    close_task = asyncio.create_task(writer.aclose())
    assert await asyncio.to_thread(entered.wait, 1)
    close_task.cancel()
    await asyncio.sleep(0)
    assert not close_task.done()
    assert writer.state == "settling_cancel"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await close_task
    assert writer.state == "closed"


def test_same_task_distinct_evidence_is_not_deduplicated(tmp_path: Path) -> None:
    results_path, trace_path, checkpoint_path = _paths(tmp_path)
    first_result, first_trace = _pair(final_text="attempt one")
    second_result, second_trace = _pair(final_text="attempt two")

    with artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    ) as writer:
        assert writer.append(first_result, first_trace) is True
        assert writer.append(second_result, second_trace) is True

    assert len(results_path.read_text(encoding="utf-8").splitlines()) == 2
    assert _checkpoint(checkpoint_path)["rows_written"] == 2


def test_same_evidence_with_different_serialized_bytes_fails_closed(tmp_path: Path) -> None:
    results_path, trace_path, checkpoint_path = _paths(tmp_path)
    result, trace = _pair()
    reordered = dict(reversed(list(result.items())))

    with artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    ) as writer:
        assert writer.append(result, trace) is True
        with pytest.raises(artifact_io.DracoArtifactDurabilityError, match="conflicts"):
            writer.append(reordered, trace)

    assert len(results_path.read_text(encoding="utf-8").splitlines()) == 1


def test_append_uses_constant_time_rolling_checkpoint_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_path, trace_path, checkpoint_path = _paths(tmp_path)
    writer = artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    )

    def reject_prefix_rescan(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("append rescanned an already committed checkpoint prefix")

    with monkeypatch.context() as scoped:
        scoped.setattr(
            artifact_io,
            "_checkpoint_prefix_digest",
            reject_prefix_rescan,
        )
        with writer:
            for index in range(8):
                result, trace = _pair(task_id=f"task-{index}")
                assert writer.append(result, trace) is True

    verification = artifact_io.verify_durable_draco_artifacts(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    )
    assert verification["rows_written"] == 8


def test_reopen_rebuilds_digest_once_then_appends_without_prefix_rescan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_path, trace_path, checkpoint_path = _paths(tmp_path)
    with artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    ) as writer:
        for index in range(4):
            result, trace = _pair(task_id=f"before-{index}")
            writer.append(result, trace)

    updates = 0
    real_update = artifact_io._update_checkpoint_prefix_digest

    def count_update(*args: object, **kwargs: object) -> None:
        nonlocal updates
        updates += 1
        real_update(*args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(artifact_io, "_update_checkpoint_prefix_digest", count_update)
        scoped.setattr(
            artifact_io,
            "_checkpoint_prefix_digest",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("writer used the offline prefix rescan")
            ),
        )
        with artifact_io.DurableDracoArtifactWriter(
            results_path=results_path,
            trace_path=trace_path,
            checkpoint_path=checkpoint_path,
            create=False,
        ) as reopened:
            assert updates == 4
            result, trace = _pair(task_id="after")
            assert reopened.append(result, trace) is True
            assert updates == 5


def test_writer_retains_only_compact_line_metadata_for_large_rows(tmp_path: Path) -> None:
    results_path, trace_path, checkpoint_path = _paths(tmp_path)
    result, trace = _pair(final_text="x" * (2 * 1024 * 1024))

    with artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    ) as writer:
        writer.append(result, trace)
        result_line = writer._results[0]
        trace_line = writer._traces[0]
        assert not hasattr(result_line, "row")
        assert not hasattr(result_line, "payload")
        assert not hasattr(trace_line, "row")
        assert not hasattr(trace_line, "payload")
        assert next(writer.iter_paired_result_rows())["final_text"] == result["final_text"]


def test_streaming_verifier_hashes_result_and_trace_during_pair_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_path, trace_path, checkpoint_path = _paths(tmp_path)
    with artifact_io.DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    ) as writer:
        for index in range(3):
            result, trace = _pair(task_id=f"task-{index}")
            writer.append(result, trace)

    open_calls: list[tuple[Path, str]] = []
    real_open = artifact_io._open_readonly_artifact

    def record_open(path: Path, *, artifact: str) -> int:
        open_calls.append((Path(path), artifact))
        return real_open(path, artifact=artifact)

    monkeypatch.setattr(artifact_io, "_open_readonly_artifact", record_open)
    verification = artifact_io.verify_durable_draco_artifacts(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    )

    assert verification["rows_written"] == 3
    assert open_calls == [
        (results_path, "result"),
        (trace_path, "trace"),
        (checkpoint_path, "checkpoint"),
    ]
    assert [row["task_id"] for row in artifact_io.iter_verified_result_rows(results_path)] == [
        "task-0",
        "task-1",
        "task-2",
    ]
