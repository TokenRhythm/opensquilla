from __future__ import annotations

import json
import multiprocessing
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import structlog.testing

from opensquilla.observability import ensemble_execution_metrics as metrics_module
from opensquilla.observability import ensemble_execution_metrics_jsonl as jsonl_module
from opensquilla.observability.ensemble_execution_metrics import (
    build_ensemble_execution_metrics,
    log_ensemble_execution_metrics,
)
from opensquilla.observability.ensemble_execution_metrics_contract import (
    ENSEMBLE_EXECUTION_METRICS_EVENT,
    ENSEMBLE_EXECUTION_METRICS_JSONL_SCHEMA,
)
from opensquilla.observability.ensemble_execution_metrics_jsonl import (
    ENSEMBLE_METRICS_JSONL_DIR_ENV,
    ENSEMBLE_METRICS_JSONL_ENV,
    LOCK_FILE_NAME,
    MAX_ROW_BYTES,
    METRICS_FILE_NAME,
    EnsembleMetricsJSONLTransportError,
    _reset_ensemble_metrics_jsonl_writer_for_tests,
    _SecureRotatingJSONLWriter,
    build_ensemble_metrics_jsonl_row,
    write_ensemble_execution_metrics_jsonl,
)

_REAL_OS_WRITE = os.write


def _metrics() -> dict[str, Any]:
    return build_ensemble_execution_metrics(
        {"fallback_used": False},
        terminal_outcome="completed",
    )


def _row() -> bytes:
    return build_ensemble_metrics_jsonl_row(
        _metrics(),
        emitted_at=datetime(2026, 8, 11, 1, 2, 3, tzinfo=UTC),
    )


def _multiprocess_write_rows(
    directory: str,
    row: bytes,
    count: int,
    max_file_bytes: int,
    backup_count: int,
) -> None:
    writer = _SecureRotatingJSONLWriter(
        Path(directory),
        max_file_bytes=max_file_bytes,
        backup_count=backup_count,
    )
    try:
        for _ in range(count):
            writer.write_row(row)
    finally:
        writer.close()


def _forked_public_sink(connection: Any) -> None:
    try:
        result = write_ensemble_execution_metrics_jsonl(_metrics())
        connection.send(str(result) if result is not None else None)
    finally:
        connection.close()


def _forked_inspect_descriptors_then_write(
    connection: Any,
    inherited_descriptors: tuple[tuple[int, tuple[int, int]], ...],
) -> None:
    try:
        # The parent test gates only its active metrics data write.  Restore
        # the real syscall in child before using the newly created writer.
        jsonl_module.os.write = _REAL_OS_WRITE
        inherited_metrics_fds: list[bool] = []
        for fd, expected_identity in inherited_descriptors:
            try:
                info = os.fstat(fd)
            except OSError:
                inherited_metrics_fds.append(False)
            else:
                inherited_metrics_fds.append(
                    (int(info.st_dev), int(info.st_ino)) == expected_identity
                )
        connection.send(("inherited", inherited_metrics_fds))
        if connection.recv() != "write":
            raise RuntimeError("unexpected fork regression test command")
        result = write_ensemble_execution_metrics_jsonl(_metrics())
        connection.send(("write", str(result) if result is not None else None))
    except Exception as exc:
        connection.send(("error", type(exc).__name__))
    finally:
        connection.close()


def _hold_lock_until_released(
    lock: Any,
    acquired: threading.Event,
    release: threading.Event,
) -> None:
    with lock:
        acquired.set()
        release.wait(timeout=20)


def _assert_forked_sink_writes_while_parent_lock_is_held(
    lock: Any,
    expected_path: Path,
) -> None:
    context = multiprocessing.get_context("fork")
    acquired = threading.Event()
    release = threading.Event()
    lock_holder = threading.Thread(
        target=_hold_lock_until_released,
        args=(lock, acquired, release),
        daemon=True,
    )
    lock_holder.start()
    assert acquired.wait(timeout=5)

    receive_connection, send_connection = context.Pipe(duplex=False)
    process = context.Process(target=_forked_public_sink, args=(send_connection,))
    try:
        process.start()
        send_connection.close()
        process.join(timeout=5)
        timed_out = process.is_alive()
        if timed_out:
            process.terminate()
            process.join(timeout=5)
        assert not timed_out, "fork child blocked on an inherited thread lock"
        assert process.exitcode == 0
        assert receive_connection.poll(timeout=1)
        assert receive_connection.recv() == str(expected_path)
    finally:
        release.set()
        lock_holder.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        send_connection.close()
        receive_connection.close()
    assert not lock_holder.is_alive()


@pytest.fixture(autouse=True)
def _reset_writer() -> None:
    _reset_ensemble_metrics_jsonl_writer_for_tests()
    yield
    _reset_ensemble_metrics_jsonl_writer_for_tests()


def _enable(monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
    monkeypatch.setenv(ENSEMBLE_METRICS_JSONL_ENV, "1")
    monkeypatch.setenv(ENSEMBLE_METRICS_JSONL_DIR_ENV, str(directory))


def _transport_files(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.iterdir()
        if path.name == METRICS_FILE_NAME or path.name.startswith(f"{METRICS_FILE_NAME}.")
    )


def test_disabled_transport_has_no_filesystem_or_writer_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "must-not-exist"
    monkeypatch.delenv(ENSEMBLE_METRICS_JSONL_ENV, raising=False)
    monkeypatch.setenv(ENSEMBLE_METRICS_JSONL_DIR_ENV, str(target))

    assert write_ensemble_execution_metrics_jsonl(_metrics()) is None
    assert not target.exists()
    assert jsonl_module._writer is None


@pytest.mark.skipif(os.name != "posix", reason="secure writer is POSIX-only")
def test_enabled_transport_writes_flat_bounded_owner_only_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "metrics"
    _enable(monkeypatch, target)

    path = write_ensemble_execution_metrics_jsonl(_metrics())

    assert path == target / METRICS_FILE_NAME
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((target / LOCK_FILE_NAME).stat().st_mode) == 0o600
    raw = path.read_bytes()
    assert len(raw) <= MAX_ROW_BYTES
    assert raw.count(b"\n") == 1
    payload = json.loads(raw)
    assert payload["transport_schema"] == ENSEMBLE_EXECUTION_METRICS_JSONL_SCHEMA
    assert payload["event"] == ENSEMBLE_EXECUTION_METRICS_EVENT
    assert payload["emitted_at"].endswith("Z")
    assert payload["schema"] == "opensquilla.ensemble-execution-metrics/v1"
    assert not any(
        key in payload
        for key in (
            "model",
            "provider",
            "deployment",
            "session_id",
            "task_id",
            "user_id",
            "policy_sha256",
            "selection_plan",
            "candidates",
            "final_request",
        )
    )


def test_row_builder_rejects_naive_timestamp_and_hard_cap() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        build_ensemble_metrics_jsonl_row(
            _metrics(),
            emitted_at=datetime(2026, 8, 11),
        )
    with pytest.raises(EnsembleMetricsJSONLTransportError, match="hard byte cap"):
        build_ensemble_metrics_jsonl_row(_metrics(), max_row_bytes=10)


@pytest.mark.skipif(os.name != "posix", reason="secure writer is POSIX-only")
def test_threaded_writes_remain_complete_json_lines(tmp_path: Path) -> None:
    writer = _SecureRotatingJSONLWriter(tmp_path / "metrics")
    row = _row()
    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _: writer.write_row(row), range(100)))
    finally:
        writer.close()

    lines = (tmp_path / "metrics" / METRICS_FILE_NAME).read_bytes().splitlines()
    assert len(lines) == 100
    assert all(json.loads(line)["event"] == ENSEMBLE_EXECUTION_METRICS_EVENT for line in lines)


@pytest.mark.skipif(os.name != "posix", reason="secure writer is POSIX-only")
def test_rotation_is_bounded_and_preserves_private_modes(tmp_path: Path) -> None:
    row = _row()
    writer = _SecureRotatingJSONLWriter(
        tmp_path / "metrics",
        max_file_bytes=len(row) * 2,
        backup_count=3,
    )
    try:
        for _ in range(12):
            writer.write_row(row)
    finally:
        writer.close()

    files = _transport_files(tmp_path / "metrics")
    assert [path.name for path in files] == [
        METRICS_FILE_NAME,
        f"{METRICS_FILE_NAME}.1",
        f"{METRICS_FILE_NAME}.2",
        f"{METRICS_FILE_NAME}.3",
    ]
    for path in files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.stat().st_size <= len(row) * 2
        for line in path.read_bytes().splitlines():
            assert json.loads(line)["transport_schema"] == (ENSEMBLE_EXECUTION_METRICS_JSONL_SCHEMA)


@pytest.mark.skipif(
    os.name != "posix" or "fork" not in multiprocessing.get_all_start_methods(),
    reason="cross-process flock test requires POSIX fork",
)
def test_multiprocess_rotation_has_no_interleaved_or_lost_rows(tmp_path: Path) -> None:
    target = tmp_path / "metrics"
    row = _row()
    process_count = 4
    rows_per_process = 25
    context = multiprocessing.get_context("fork")
    processes = [
        context.Process(
            target=_multiprocess_write_rows,
            args=(str(target), row, rows_per_process, len(row) * 3, 64),
        )
        for _ in range(process_count)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    lines = [line for path in _transport_files(target) for line in path.read_bytes().splitlines()]
    assert len(lines) == process_count * rows_per_process
    assert all(json.loads(line)["event"] == ENSEMBLE_EXECUTION_METRICS_EVENT for line in lines)


@pytest.mark.skipif(
    os.name != "posix" or "fork" not in multiprocessing.get_all_start_methods(),
    reason="fork lock-reset regression requires POSIX fork",
)
@pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded, use of fork.*:DeprecationWarning"
)
def test_fork_child_resets_inherited_locked_global_writer_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "metrics"
    _enable(monkeypatch, target)
    expected_path = target / METRICS_FILE_NAME
    assert write_ensemble_execution_metrics_jsonl(_metrics()) == expected_path

    _assert_forked_sink_writes_while_parent_lock_is_held(
        jsonl_module._writer_guard,
        expected_path,
    )

    assert write_ensemble_execution_metrics_jsonl(_metrics()) == expected_path
    assert len(expected_path.read_bytes().splitlines()) == 3


@pytest.mark.skipif(
    os.name != "posix" or "fork" not in multiprocessing.get_all_start_methods(),
    reason="fork lock-reset regression requires POSIX fork",
)
@pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded, use of fork.*:DeprecationWarning"
)
def test_fork_child_discards_writer_with_inherited_locked_thread_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "metrics"
    _enable(monkeypatch, target)
    expected_path = target / METRICS_FILE_NAME
    assert write_ensemble_execution_metrics_jsonl(_metrics()) == expected_path
    writer = jsonl_module._writer
    assert writer is not None

    _assert_forked_sink_writes_while_parent_lock_is_held(
        writer._thread_lock,
        expected_path,
    )

    assert write_ensemble_execution_metrics_jsonl(_metrics()) == expected_path
    assert len(expected_path.read_bytes().splitlines()) == 3


@pytest.mark.skipif(
    os.name != "posix" or "fork" not in multiprocessing.get_all_start_methods(),
    reason="active data fd fork regression requires POSIX fork",
)
@pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded, use of fork.*:DeprecationWarning"
)
def test_fork_child_closes_inherited_active_data_fd_and_keeps_rotation_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "metrics"
    _enable(monkeypatch, target)
    row = _row()
    backup_count = 2
    writer = _SecureRotatingJSONLWriter(
        target,
        max_file_bytes=len(row) * 2,
        backup_count=backup_count,
    )
    jsonl_module._writer = writer
    jsonl_module._writer_key = (os.getpid(), str(target))

    active_write_entered = threading.Event()
    release_active_write = threading.Event()
    parent_errors: list[BaseException] = []

    def gated_write(fd: int, data: bytes) -> int:
        tracked = writer._active_data_descriptor
        if tracked is not None and fd == tracked[0] and not release_active_write.is_set():
            active_write_entered.set()
            if not release_active_write.wait(timeout=20):
                raise TimeoutError("timed out waiting to release active metrics write")
        return _REAL_OS_WRITE(fd, data)

    def parent_write() -> None:
        try:
            writer.write_row(row)
        except BaseException as exc:
            parent_errors.append(exc)

    monkeypatch.setattr(jsonl_module.os, "write", gated_write)
    parent_thread = threading.Thread(target=parent_write, daemon=True)
    parent_thread.start()

    process: multiprocessing.Process | None = None
    process_started = False
    parent_connection: Any = None
    child_connection: Any = None
    start_thread: threading.Thread | None = None
    start_errors: list[BaseException] = []
    try:
        assert active_write_entered.wait(timeout=5)
        active_data_descriptor = writer._active_data_descriptor
        assert active_data_descriptor is not None
        inherited_descriptors = (
            (writer._lock_fd, writer._lock_identity),
            (writer._directory_fd, writer._directory_identity),
            active_data_descriptor,
        )

        context = multiprocessing.get_context("fork")
        parent_connection, child_connection = context.Pipe(duplex=True)
        process = context.Process(
            target=_forked_inspect_descriptors_then_write,
            args=(child_connection, inherited_descriptors),
        )
        fork_requested = threading.Event()
        real_fork = os.fork

        def observable_fork() -> int:
            fork_requested.set()
            return real_fork()

        monkeypatch.setattr(os, "fork", observable_fork)

        def start_process() -> None:
            try:
                assert process is not None
                process.start()
            except BaseException as exc:
                start_errors.append(exc)

        start_thread = threading.Thread(target=start_process, daemon=True)
        start_thread.start()
        assert fork_requested.wait(timeout=5)
        assert start_thread.is_alive()

        # The at-fork before callback must wait for this active write to close
        # its transient descriptor before the process can actually fork.
        release_active_write.set()
        parent_thread.join(timeout=5)
        assert not parent_thread.is_alive()
        assert parent_errors == []
        assert writer._active_data_descriptor is None

        start_thread.join(timeout=5)
        assert not start_thread.is_alive()
        assert start_errors == []
        process_started = True
        child_connection.close()

        assert parent_connection.poll(timeout=5)
        assert parent_connection.recv() == ("inherited", [False, False, False])

        parent_connection.send("write")
        assert parent_connection.poll(timeout=5)
        assert parent_connection.recv() == (
            "write",
            str(target / METRICS_FILE_NAME),
        )
        process.join(timeout=5)
        assert process.exitcode == 0

        for _ in range(10):
            writer.write_row(row)
        files = _transport_files(target)
        assert [path.name for path in files] == [
            METRICS_FILE_NAME,
            f"{METRICS_FILE_NAME}.1",
            f"{METRICS_FILE_NAME}.2",
        ]
        assert all(path.stat().st_size <= len(row) * 2 for path in files)
    finally:
        release_active_write.set()
        parent_thread.join(timeout=5)
        if start_thread is not None:
            start_thread.join(timeout=5)
        if process is not None and process_started and process.is_alive():
            process.terminate()
            process.join(timeout=5)
        if child_connection is not None:
            child_connection.close()
        if parent_connection is not None:
            parent_connection.close()


@pytest.mark.skipif(
    os.name != "posix" or "fork" not in multiprocessing.get_all_start_methods(),
    reason="data-open fork serialization regression requires POSIX fork",
)
@pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded, use of fork.*:DeprecationWarning"
)
def test_fork_waits_until_data_open_is_tracked_and_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "metrics"
    _enable(monkeypatch, target)
    row = _row()
    writer = _SecureRotatingJSONLWriter(target)
    jsonl_module._writer = writer
    jsonl_module._writer_key = (os.getpid(), str(target))

    data_open_returned = threading.Event()
    release_data_open = threading.Event()
    fork_requested = threading.Event()
    parent_errors: list[BaseException] = []
    start_errors: list[BaseException] = []
    opened_descriptor: list[tuple[int, tuple[int, int]]] = []
    real_open_secure_file = writer._open_secure_file

    def gated_open_secure_file(
        name: str,
        *,
        access_flags: int,
        create: bool,
        exclusive: bool = False,
    ) -> int:
        fd = real_open_secure_file(
            name,
            access_flags=access_flags,
            create=create,
            exclusive=exclusive,
        )
        if name == METRICS_FILE_NAME and not opened_descriptor:
            info = os.fstat(fd)
            opened_descriptor.append((fd, (int(info.st_dev), int(info.st_ino))))
            # This is the exact vulnerable interval: os.open and all secure
            # validation have completed, but _open_tracked_data_file has not
            # yet recorded the descriptor for the child hook.
            assert writer._active_data_descriptor is None
            data_open_returned.set()
            if not release_data_open.wait(timeout=20):
                raise TimeoutError("timed out waiting to release tracked data open")
        return fd

    def parent_write() -> None:
        try:
            writer.write_row(row)
        except BaseException as exc:
            parent_errors.append(exc)

    monkeypatch.setattr(writer, "_open_secure_file", gated_open_secure_file)
    parent_thread = threading.Thread(target=parent_write, daemon=True)
    parent_thread.start()
    assert data_open_returned.wait(timeout=5)
    assert len(opened_descriptor) == 1

    context = multiprocessing.get_context("fork")
    parent_connection, child_connection = context.Pipe(duplex=True)
    process = context.Process(
        target=_forked_inspect_descriptors_then_write,
        args=(child_connection, tuple(opened_descriptor)),
    )
    real_fork = os.fork

    def observable_fork() -> int:
        fork_requested.set()
        return real_fork()

    monkeypatch.setattr(os, "fork", observable_fork)

    def start_process() -> None:
        try:
            process.start()
        except BaseException as exc:
            start_errors.append(exc)

    start_thread = threading.Thread(target=start_process, daemon=True)
    start_thread.start()
    process_started = False
    try:
        assert fork_requested.wait(timeout=5)
        # os.fork has been entered, but its registered before-callback must
        # remain blocked on the writer's fork guard until the untracked fd is
        # recorded and closed by the parent write.
        assert start_thread.is_alive()

        release_data_open.set()
        parent_thread.join(timeout=5)
        assert not parent_thread.is_alive()
        assert parent_errors == []
        assert writer._active_data_descriptor is None

        start_thread.join(timeout=5)
        assert not start_thread.is_alive()
        assert start_errors == []
        process_started = True
        child_connection.close()

        assert parent_connection.poll(timeout=5)
        assert parent_connection.recv() == ("inherited", [False])
        parent_connection.send("write")
        assert parent_connection.poll(timeout=5)
        assert parent_connection.recv() == (
            "write",
            str(target / METRICS_FILE_NAME),
        )
        process.join(timeout=5)
        assert process.exitcode == 0

        assert writer._active_data_descriptor is None
        writer.write_row(row)
        assert len((target / METRICS_FILE_NAME).read_bytes().splitlines()) == 3
    finally:
        release_data_open.set()
        parent_thread.join(timeout=5)
        start_thread.join(timeout=5)
        if process_started and process.is_alive():
            process.terminate()
            process.join(timeout=5)
        child_connection.close()
        parent_connection.close()


@pytest.mark.skipif(os.name != "posix", reason="secure writer is POSIX-only")
def test_symlink_data_path_is_rejected_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "metrics"
    target.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_text("sentinel", encoding="utf-8")
    outside.chmod(0o600)
    (target / METRICS_FILE_NAME).symlink_to(outside)
    writer = _SecureRotatingJSONLWriter(target)
    try:
        with pytest.raises(OSError):
            writer.write_row(_row())
    finally:
        writer.close()
    assert outside.read_text(encoding="utf-8") == "sentinel"


@pytest.mark.skipif(os.name != "posix", reason="secure writer is POSIX-only")
def test_non_private_directory_and_hardlinked_file_are_rejected(tmp_path: Path) -> None:
    broad = tmp_path / "broad"
    broad.mkdir(mode=0o755)
    broad.chmod(0o755)
    with pytest.raises(EnsembleMetricsJSONLTransportError, match="0700"):
        _SecureRotatingJSONLWriter(broad)

    target = tmp_path / "metrics"
    target.mkdir(mode=0o700)
    data = target / METRICS_FILE_NAME
    data.write_bytes(b"")
    data.chmod(0o600)
    os.link(data, tmp_path / "second-link")
    writer = _SecureRotatingJSONLWriter(target)
    try:
        with pytest.raises(EnsembleMetricsJSONLTransportError, match="hard link"):
            writer.write_row(_row())
    finally:
        writer.close()


@pytest.mark.skipif(os.name != "posix", reason="secure writer is POSIX-only")
def test_lock_and_directory_identity_replacement_are_detected(tmp_path: Path) -> None:
    target = tmp_path / "metrics"
    writer = _SecureRotatingJSONLWriter(target)
    lock_path = target / LOCK_FILE_NAME
    lock_path.unlink()
    lock_path.write_bytes(b"")
    lock_path.chmod(0o600)
    try:
        with pytest.raises(
            EnsembleMetricsJSONLTransportError,
            match="hard link|lock path",
        ):
            writer.write_row(_row())
    finally:
        writer.close()

    writer = _SecureRotatingJSONLWriter(target)
    moved = tmp_path / "metrics-moved"
    target.rename(moved)
    target.mkdir(mode=0o700)
    try:
        with pytest.raises(EnsembleMetricsJSONLTransportError, match="path changed"):
            writer.write_row(_row())
    finally:
        writer.close()


@pytest.mark.skipif(os.name != "posix", reason="secure writer is POSIX-only")
def test_short_write_is_rolled_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "metrics"
    writer = _SecureRotatingJSONLWriter(target)
    real_write = os.write

    def short_write(fd: int, data: bytes) -> int:
        return real_write(fd, data[:-1])

    monkeypatch.setattr(jsonl_module.os, "write", short_write)
    try:
        with pytest.raises(EnsembleMetricsJSONLTransportError, match="short write"):
            writer.write_row(_row())
    finally:
        writer.close()
    assert (target / METRICS_FILE_NAME).stat().st_size == 0


@pytest.mark.skipif(os.name != "posix", reason="secure writer is POSIX-only")
def test_sink_failure_does_not_suppress_structlog_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "metrics-link"
    target.symlink_to(tmp_path, target_is_directory=True)
    _enable(monkeypatch, target)

    with structlog.testing.capture_logs() as captured:
        log_ensemble_execution_metrics(
            {"fallback_used": False},
            terminal_outcome="completed",
        )

    assert any(row.get("event") == ENSEMBLE_EXECUTION_METRICS_EVENT for row in captured)
    assert not (tmp_path / METRICS_FILE_NAME).exists()


@pytest.mark.skipif(os.name != "posix", reason="secure writer is POSIX-only")
def test_broken_structlog_does_not_suppress_jsonl_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "metrics"
    _enable(monkeypatch, target)

    class BrokenLogger:
        def info(self, *_: Any, **__: Any) -> None:
            raise RuntimeError("broken info processor")

        def warning(self, *_: Any, **__: Any) -> None:
            raise RuntimeError("broken warning processor")

    monkeypatch.setattr(metrics_module, "log", BrokenLogger())
    log_ensemble_execution_metrics(
        {"fallback_used": False},
        terminal_outcome="completed",
    )

    payload = json.loads((target / METRICS_FILE_NAME).read_text(encoding="utf-8"))
    assert payload["event"] == ENSEMBLE_EXECUTION_METRICS_EVENT


def test_public_sink_fails_open_when_secure_platform_primitives_are_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "metrics"
    _enable(monkeypatch, target)
    monkeypatch.setattr(jsonl_module, "fcntl", None)

    assert write_ensemble_execution_metrics_jsonl(_metrics()) is None
    assert not target.exists()
