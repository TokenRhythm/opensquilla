from __future__ import annotations

import gc
import io
import json
import os
import re
import tracemalloc
from collections.abc import Iterator
from pathlib import Path

import pytest

from opensquilla.eval import draco_resume_source_index as resume_source_index
from opensquilla.eval.draco_artifact_integrity import seal_result_row
from opensquilla.eval.draco_resume_source_index import (
    DracoResumeSourceError,
    ResumeGroupTaskStates,
    ResumeRowLocator,
    ResumeSourceIndex,
)


def _sealed_row(task_id: str, *, padding: str = "") -> dict[str, object]:
    return seal_result_row(
        {
            "group": "B1",
            "task_id": task_id,
            "final_text": "accepted",
            "padding": padding,
        }
    )


def _write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("wb") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":")).encode())
            handle.write(b"\n")


def _scan_source(
    index: ResumeSourceIndex,
    path: Path,
    *,
    source_index: int,
) -> list[ResumeRowLocator]:
    locators: list[ResumeRowLocator] = []
    for indexed in index.iter_source(path, source_index=source_index):
        row = json.loads(indexed.payload)
        locators.append(
            indexed.locator.bind(
                group=str(row["group"]),
                task_id=str(row["task_id"]),
            )
        )
    return locators


def _scan(
    index: ResumeSourceIndex,
    path: Path,
) -> list[ResumeRowLocator]:
    locators = _scan_source(index, path, source_index=0)
    index.seal()
    return locators


def test_large_complete_history_keeps_only_locators_until_one_pending_consume(
    tmp_path: Path,
) -> None:
    path = tmp_path / "large-history.jsonl"
    padding = "x" * (1024 * 1024)
    with path.open("wb") as handle:
        for row_index in range(101):
            row = _sealed_row(
                f"complete-{row_index}" if row_index < 100 else "pending",
                padding=padding,
            )
            handle.write(json.dumps(row, separators=(",", ":")).encode())
            handle.write(b"\n")
            del row
    del padding
    gc.collect()

    tracemalloc.start()
    index = ResumeSourceIndex([path])
    locators = _scan(index, path)
    _, classification_peak = tracemalloc.get_traced_memory()

    assert len(locators) == 101
    assert index.materialized_row_count == 0
    assert classification_peak < 12 * 1024 * 1024

    pending = index.consume_row(locators[-1])
    _, consume_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert pending["task_id"] == "pending"
    assert index.materialized_row_count == 1
    assert consume_peak < 16 * 1024 * 1024
    index.close()


def test_lf_only_short_line_scan_is_single_pass_per_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    line = b'{"x":1}\n'
    line_count = (1024 * 1024 // len(line)) + 1
    payload = line * line_count
    chunk_size = 64 * 1024
    actual_pattern = resume_source_index._UNIVERSAL_NEWLINE_RE
    scanned_bytes = 0
    scan_calls = 0

    class CountingPattern:
        def finditer(
            self,
            value: bytes,
            pos: int = 0,
        ) -> Iterator[re.Match[bytes]]:
            nonlocal scanned_bytes, scan_calls
            scanned_bytes += len(value) - pos
            scan_calls += 1
            return actual_pattern.finditer(value, pos)

    monkeypatch.setattr(
        resume_source_index,
        "_SOURCE_READ_CHUNK_BYTES",
        chunk_size,
    )
    monkeypatch.setattr(
        resume_source_index,
        "_UNIVERSAL_NEWLINE_RE",
        CountingPattern(),
    )

    observed_count = 0
    observed_bytes = 0
    for observed in resume_source_index._iter_universal_binary_lines(
        io.BytesIO(payload)
    ):
        assert observed == line
        observed_count += 1
        observed_bytes += len(observed)

    assert observed_count == line_count
    assert observed_bytes == len(payload)
    assert scanned_bytes == len(payload)
    assert scan_calls == (len(payload) + chunk_size - 1) // chunk_size


@pytest.mark.parametrize("force_spool", [False, True])
def test_path_replacement_cannot_change_bound_row_and_fails_final_snapshot(
    tmp_path: Path,
    force_spool: bool,
) -> None:
    path = tmp_path / "source.jsonl"
    _write_rows(path, [_sealed_row("original")])
    index = ResumeSourceIndex([path], force_spool=force_spool)
    locator = _scan(index, path)[0]

    path.rename(tmp_path / "original.jsonl")
    _write_rows(path, [_sealed_row("replacement")])

    if force_spool:
        assert index.consume_row(locator)["task_id"] == "original"
    else:
        with pytest.raises(DracoResumeSourceError, match="changed after indexing"):
            index.consume_row(locator)
    with pytest.raises(DracoResumeSourceError, match="path was replaced|changed after"):
        index.close()
    assert index.closed


def test_same_inode_tamper_fails_before_row_delivery_and_does_not_consume(
    tmp_path: Path,
) -> None:
    path = tmp_path / "source.jsonl"
    _write_rows(path, [_sealed_row("task-1", padding="abc")])
    index = ResumeSourceIndex([path], force_spool=False)
    locator = _scan(index, path)[0]

    fd = os.open(path, os.O_RDWR)
    try:
        os.pwrite(fd, b"Z", locator.offset + 1)
        os.fsync(fd)
    finally:
        os.close(fd)

    with pytest.raises(DracoResumeSourceError, match="changed after indexing"):
        index.consume_row(locator)
    assert index.materialized_row_count == 0
    with pytest.raises(DracoResumeSourceError, match="changed after indexing"):
        index.consume_row(locator)
    index.close(verify=False)


def test_context_primary_error_survives_tamper_and_closes_descriptor(
    tmp_path: Path,
) -> None:
    class PrimaryError(RuntimeError):
        pass

    path = tmp_path / "source.jsonl"
    _write_rows(path, [_sealed_row("task-1", padding="abc")])
    index = ResumeSourceIndex([path], force_spool=False)
    locator = _scan(index, path)[0]
    bound_fd = index._sources[0].fd  # noqa: SLF001 - descriptor lifecycle gate.

    with pytest.raises(PrimaryError, match="business failure"):
        with index:
            tamper_fd = os.open(path, os.O_RDWR)
            try:
                os.pwrite(tamper_fd, b"Z", locator.offset + 1)
                os.fsync(tamper_fd)
            finally:
                os.close(tamper_fd)
            raise PrimaryError("business failure")

    assert index.closed
    assert bound_fd is not None
    with pytest.raises(OSError):
        os.fstat(bound_fd)


@pytest.mark.parametrize("force_spool", [False, True])
def test_two_sources_preserve_universal_newlines_and_exact_locators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    force_spool: bool,
) -> None:
    paths = [tmp_path / "one.jsonl", tmp_path / "two.jsonl"]
    encoded = [
        json.dumps(_sealed_row(task_id), separators=(",", ":")).encode()
        for task_id in ("a", "b", "c", "d", "e")
    ]
    # Make the first CR in each source land at a chunk boundary.  This covers
    # both a bare-CR record and a CRLF split across two reads.
    monkeypatch.setattr(
        resume_source_index,
        "_SOURCE_READ_CHUNK_BYTES",
        len(encoded[0]) + 1,
    )
    paths[0].write_bytes(encoded[0] + b"\r" + encoded[1] + b"\r")
    paths[1].write_bytes(
        encoded[2] + b"\r\n" + encoded[3] + b"\n" + encoded[4]
    )

    index = ResumeSourceIndex(paths, force_spool=force_spool)
    first = _scan_source(index, paths[0], source_index=0)
    second = _scan_source(index, paths[1], source_index=1)
    index.seal()

    assert [locator.line_number for locator in first] == [1, 2]
    assert [locator.line_number for locator in second] == [1, 2, 3]
    expected_first_offsets = [0, len(encoded[0]) + 1]
    second_base = paths[0].stat().st_size if force_spool else 0
    expected_second_offsets = [
        second_base,
        second_base + len(encoded[2]) + 2,
        second_base + len(encoded[2]) + 2 + len(encoded[3]) + 1,
    ]
    assert [locator.offset for locator in first] == expected_first_offsets
    assert [locator.offset for locator in second] == expected_second_offsets
    assert [index.consume_row(locator)["task_id"] for locator in first + second] == [
        "a",
        "b",
        "c",
        "d",
        "e",
    ]
    index.close()


@pytest.mark.parametrize("force_spool", [False, True])
def test_utf8_bom_is_rejected_for_direct_and_spooled_rows(
    tmp_path: Path,
    force_spool: bool,
) -> None:
    path = tmp_path / "bom.jsonl"
    encoded = json.dumps(_sealed_row("task-1"), separators=(",", ":")).encode()
    path.write_bytes(b"\xef\xbb\xbf" + encoded + b"\n")
    index = ResumeSourceIndex([path], force_spool=force_spool)
    indexed = list(index.iter_source(path, source_index=0))
    assert len(indexed) == 1
    locator = indexed[0].locator.bind(group="B1", task_id="task-1")
    index.seal()

    with pytest.raises(DracoResumeSourceError, match="invalid JSON"):
        index.consume_row(locator)
    index.close()


def test_consume_is_exactly_once_and_close_releases_bound_descriptor(
    tmp_path: Path,
) -> None:
    path = tmp_path / "source.jsonl"
    _write_rows(path, [_sealed_row("task-1")])
    index = ResumeSourceIndex([path], force_spool=False)
    locator = _scan(index, path)[0]
    bound_fd = index._sources[0].fd  # noqa: SLF001 - descriptor lifecycle gate.

    assert index.consume_row(locator)["task_id"] == "task-1"
    with pytest.raises(DracoResumeSourceError, match="already consumed"):
        index.consume_row(locator)
    index.close()

    assert bound_fd is not None
    with pytest.raises(OSError):
        os.fstat(bound_fd)


def test_failed_consume_keeps_locator_for_auditable_retry(tmp_path: Path) -> None:
    path = tmp_path / "unsealed.jsonl"
    _write_rows(
        path,
        [{"group": "B1", "task_id": "task-1", "final_text": "unsealed"}],
    )
    index = ResumeSourceIndex([path], force_spool=False)
    locator = _scan(index, path)[0]
    states = ResumeGroupTaskStates(
        {
            ("B1", "task-1"): {
                ResumeGroupTaskStates._LOCATOR_KEY: locator,
            }
        },
        source_index=index,
    )

    with pytest.raises(DracoResumeSourceError, match="evidence verification"):
        states.consume_row(("B1", "task-1"))
    assert (
        states[("B1", "task-1")][ResumeGroupTaskStates._LOCATOR_KEY]
        == locator
    )
    assert index.materialized_row_count == 0
    states.close()


def test_private_spool_rolling_hash_detects_tamper(tmp_path: Path) -> None:
    path = tmp_path / "source.jsonl"
    _write_rows(path, [_sealed_row("task-1")])
    index = ResumeSourceIndex([path], force_spool=True)
    locator = _scan(index, path)[0]
    assert index._spool_fd is not None  # noqa: SLF001 - adversarial gate.
    os.pwrite(index._spool_fd, b"Z", locator.offset + 1)  # noqa: SLF001
    os.fsync(index._spool_fd)  # noqa: SLF001

    with pytest.raises(DracoResumeSourceError, match="spool changed"):
        index.consume_row(locator)
    with pytest.raises(DracoResumeSourceError, match="spool changed"):
        index.close()
    assert index.closed


def test_source_count_over_fd_reserve_uses_private_0600_spool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resume_source_index, "_safe_bound_source_limit", lambda: 1)
    paths = [tmp_path / "one.jsonl", tmp_path / "two.jsonl"]
    index = ResumeSourceIndex(paths)

    assert index.backing == "spool"
    assert index._spool_fd is not None  # noqa: SLF001 - private spool gate.
    assert os.fstat(index._spool_fd).st_mode & 0o777 == 0o600  # noqa: SLF001
    index.seal()
    index.close()


def test_forced_spool_multisource_offsets_fsync_once_and_close_reverifies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = [tmp_path / "one.jsonl", tmp_path / "two.jsonl"]
    _write_rows(paths[0], [_sealed_row("a"), _sealed_row("b")])
    _write_rows(paths[1], [_sealed_row("c")])
    actual_fsync = os.fsync
    fsync_calls: list[int] = []
    actual_hash_fd = resume_source_index._hash_fd
    hash_calls: list[tuple[int, int]] = []

    def recording_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        actual_fsync(fd)

    def recording_hash_fd(fd: int, *, expected_size: int) -> str:
        hash_calls.append((fd, expected_size))
        return actual_hash_fd(fd, expected_size=expected_size)

    monkeypatch.setattr(resume_source_index.os, "fsync", recording_fsync)
    monkeypatch.setattr(resume_source_index, "_hash_fd", recording_hash_fd)
    index = ResumeSourceIndex(paths, force_spool=True)
    spool_fd = index._spool_fd  # noqa: SLF001 - spool durability gate.
    first = _scan_source(index, paths[0], source_index=0)
    second = _scan_source(index, paths[1], source_index=1)
    index.seal()
    index.seal()

    assert spool_fd is not None
    assert fsync_calls == [spool_fd]
    assert [locator.offset for locator in first + second] == [
        0,
        first[0].length,
        first[0].length + first[1].length,
    ]
    assert index.consume_row(first[1])["task_id"] == "b"
    assert index.consume_row(second[0])["task_id"] == "c"
    index.close()

    # Close authenticates the private spool and every original source before
    # releasing descriptors; one call per backing object is expected.
    assert len(hash_calls) == 3
    assert sorted(expected_size for _, expected_size in hash_calls) == sorted(
        [
            paths[0].stat().st_size,
            paths[1].stat().st_size,
            sum(path.stat().st_size for path in paths),
        ]
    )
