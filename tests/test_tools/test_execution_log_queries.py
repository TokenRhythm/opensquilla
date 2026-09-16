from __future__ import annotations

import json
import tracemalloc
from pathlib import Path

import pytest

from opensquilla.engine.tool_result_query import (
    MAX_REGEX_LINE_CHARS,
    character_page,
    iter_log_lines,
)
from opensquilla.engine.tool_result_store import ToolResultStore
from opensquilla.tools.builtin.tool_results import (
    query_stored_tool_result,
    read_stored_tool_result_page,
)
from opensquilla.tools.types import SafeToolError


def _write_log(root: Path, content: str, *, complete: bool = True) -> str:
    spool = ToolResultStore(root).open_output_spool(
        tool_name="exec", session_id="test-session", session_key="agent:test", agent_id="test",
    )
    payload = content.encode()
    for offset in range(0, len(payload), 65_537):
        spool.append(payload[offset:offset + 65_537])
    return spool.finish(complete=complete)


def test_execution_log_middle_is_retrievable_beyond_snapshot_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = ("initial diagnostic line " + "x" * 80 + "\n") * 85_000
    content = prefix + "ERROR middle-evidence-42\n" + "following context\n" * 40_000
    handle = _write_log(tmp_path, content)

    def reject_full_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("execution log query loaded a complete record")

    monkeypatch.setattr(ToolResultStore, "read", reject_full_read)
    result = query_stored_tool_result(
        tmp_path, "test-session", handle, mode="grep", pattern="middle-evidence-42",
        context_lines=1,
    )
    assert "ERROR middle-evidence-42" in result
    assert "following context" in result
    assert "85001|" in result
    page = read_stored_tool_result_page(
        tmp_path, "test-session", handle, offset=len(prefix), limit=24,
    )
    assert page["content"] == "ERROR middle-evidence-42"
    assert page["next_offset"] == len(prefix) + 24
    assert page["complete"] is True


def test_execution_log_metadata_never_reads_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = _write_log(tmp_path, "first\nsecond\n", complete=False)

    def reject_payload_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("metadata read payload")

    monkeypatch.setattr(ToolResultStore, "iter_text_chunks", reject_payload_read)
    result = query_stored_tool_result(tmp_path, "test-session", handle)
    metadata = json.loads(result.split("---\n", 1)[1])
    assert metadata["line_count"] == 2
    assert metadata["complete"] is False
    assert "stored_log_is_complete: false" in result


def test_execution_log_character_pages_join_without_gaps(tmp_path: Path) -> None:
    content = "first🙂中\r\n" + "λ\u2028tail" * 50
    handle = _write_log(tmp_path, content)
    pages: list[str] = []
    offset = 0
    while True:
        page = read_stored_tool_result_page(
            tmp_path, "test-session", handle, offset=offset, limit=7,
        )
        assert page["offset"] == offset
        assert page["returned_chars"] == len(page["content"])
        pages.append(page["content"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert "".join(pages) == content
    eof = read_stored_tool_result_page(
        tmp_path, "test-session", handle, offset=len(content) + 10,
    )
    assert eof["offset"] == len(content)
    assert eof["content"] == ""
    assert eof["next_offset"] is None


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 100])
def test_streamed_lines_preserve_splitlines_and_character_offsets(chunk_size: int) -> None:
    content = "\nfirst\r\nsecond\rthird\v\f\x1c\x1d\x1e\x85\u2028\u2029中🙂"
    chunks = (content[index:index + chunk_size] for index in range(0, len(content), chunk_size))
    lines = list(iter_log_lines(chunks, preview_chars=100))
    assert [line.text for line in lines] == content.splitlines()
    for number, line in enumerate(lines, start=1):
        assert line.number == number
        assert content[line.offset:line.offset + line.chars] == line.text


def test_execution_log_line_query_and_regex_match_snapshot_semantics(tmp_path: Path) -> None:
    content = "alpha\nbeta context\nERROR target line\ndelta context\nepsilon\n"
    handle = _write_log(tmp_path, content)
    stored = ToolResultStore(tmp_path).write(
        content, tool_use_id="call-test", tool_name="exec", session_id="test-session",
        session_key="agent:test", agent_id="test",
    )
    for arguments in (
        {"mode": "slice", "start_line": 2, "end_line": 4},
        {"mode": "head_tail"},
        {"mode": "grep", "pattern": r"^ERROR\s+\w+", "context_lines": 1},
        {"mode": "query", "query": "show L2 and line 4", "context_lines": 0},
        {"mode": "query", "query": "L2-L4", "context_lines": 0},
        {"mode": "query", "query": "ERROR", "context_lines": 1},
    ):
        execution = query_stored_tool_result(tmp_path, "test-session", handle, **arguments)
        snapshot = query_stored_tool_result(tmp_path, "test-session", stored.handle, **arguments)
        assert execution.split("---\n", 1)[1] == snapshot.split("---\n", 1)[1]


def test_oversized_regex_line_reports_incomplete_search_and_keeps_raw_access(
    tmp_path: Path,
) -> None:
    content = "prefix\n" + "x" * (MAX_REGEX_LINE_CHARS + 1) + "TARGET\n"
    handle = _write_log(tmp_path, content)
    result = query_stored_tool_result(
        tmp_path, "test-session", handle, mode="grep", pattern="TAR.*GET",
    )
    assert "Search incomplete" in result
    assert "line 2" in result
    assert "offset=7" in result
    assert "No matches" not in result
    assert "returned_content_is_complete: false" in result
    page = read_stored_tool_result_page(
        tmp_path, "test-session", handle, offset=len(content) - 7, limit=7,
    )
    assert page["content"] == "TARGET\n"


def test_literal_search_matches_across_chunks_in_oversized_line(tmp_path: Path) -> None:
    content = "x" * (MAX_REGEX_LINE_CHARS + 65_535) + "TARGET" + "x" * 100
    handle = _write_log(tmp_path, content)
    result = query_stored_tool_result(
        tmp_path, "test-session", handle, mode="grep", pattern="TARGET", max_chars=1_000,
    )
    assert "No matches" not in result
    assert "[lines 1-1]" in result
    assert f"character offset {MAX_REGEX_LINE_CHARS + 65_535}" in result
    assert len(result) <= 1_000
    assert "returned_content_is_complete: false" in result


def test_execution_pages_refuse_snapshots_and_cross_session(tmp_path: Path) -> None:
    handle = _write_log(tmp_path, "private session text")
    with pytest.raises(SafeToolError):
        read_stored_tool_result_page(tmp_path, "another-session", handle)
    snapshot = ToolResultStore(tmp_path).write(
        "snapshot", tool_use_id="call", tool_name="read", session_id="test-session",
        session_key="agent:test", agent_id="test",
    )
    with pytest.raises(SafeToolError):
        read_stored_tool_result_page(tmp_path, "test-session", snapshot.handle)


def test_character_page_stops_after_requested_range() -> None:
    def chunks():
        yield "abc🙂"
        yield "中文def"
        raise AssertionError("page read beyond its requested range")

    assert character_page(chunks(), 3, 3) == "🙂中文"


@pytest.mark.parametrize("max_chars", [700, 1_000, 2_000])
def test_execution_raw_slice_continuation_never_skips_clipped_characters(
    tmp_path: Path, max_chars: int,
) -> None:
    content = "first🙂中\r\n" + "λ\u2028tail" * 800
    handle = _write_log(tmp_path, content)
    arguments = {"mode": "raw_slice", "limit": 10_000, "max_chars": max_chars}
    parts: list[str] = []
    while True:
        result = query_stored_tool_result(tmp_path, "test-session", handle, **arguments)
        assert len(result) <= max_chars
        body = result.split("---\n", 1)[1]
        start, returned, next_offset, rest = body.split("\n", 3)
        count = int(returned.removeprefix("returned_chars: "))
        assert count > 0
        assert int(start.removeprefix("offset: ")) == sum(map(len, parts))
        parts.append(rest[:count])
        if next_offset == "next_offset: ":
            break
        consumed = sum(map(len, parts))
        assert next_offset == f"next_offset: {consumed}"
        continuation = json.loads(rest[count:].split("continuation.next_call: ", 1)[1])
        assert continuation["arguments"]["offset"] == consumed
        assert continuation["arguments"]["max_chars"] == max_chars
        arguments = dict(continuation["arguments"])
        arguments.pop("handle")
    assert "".join(parts) == content


def test_execution_raw_slice_at_maximum_page_size_does_not_skip_header_budget(
    tmp_path: Path,
) -> None:
    content = "中" * 600_000
    handle = _write_log(tmp_path, content)
    result = query_stored_tool_result(
        tmp_path, "test-session", handle, mode="raw_slice", limit=500_000,
    )
    _, returned, next_offset, rest = result.split("---\n", 1)[1].split("\n", 3)
    count = int(returned.removeprefix("returned_chars: "))
    assert len(result) <= 500_000
    assert 0 < count < 500_000
    assert rest[:count] == content[:count]
    assert next_offset == f"next_offset: {count}"


def test_execution_raw_slice_rejects_budget_without_room_for_progress(tmp_path: Path) -> None:
    handle = _write_log(tmp_path, "output")
    with pytest.raises(SafeToolError, match="retry the same offset"):
        query_stored_tool_result(
            tmp_path, "test-session", handle, mode="raw_slice", max_chars=10,
        )


def test_execution_head_tail_preserves_snapshot_omission_notice(tmp_path: Path) -> None:
    content = "\n".join(f"line {number}" for number in range(250))
    handle = _write_log(tmp_path, content)
    snapshot = ToolResultStore(tmp_path).write(
        content, tool_use_id="call-test", tool_name="exec", session_id="test-session",
        session_key="agent:test", agent_id="test",
    )
    execution = query_stored_tool_result(tmp_path, "test-session", handle, mode="head_tail")
    original = query_stored_tool_result(
        tmp_path, "test-session", snapshot.handle, mode="head_tail",
    )
    assert execution.split("---\n", 1)[1] == original.split("---\n", 1)[1]
    assert "[... omitted 90 lines ...]" in execution


@pytest.mark.parametrize("pattern", ["absent literal", r"absent.*regex"])
def test_execution_query_memory_is_bounded_for_long_single_line(
    tmp_path: Path, pattern: str,
) -> None:
    spool = ToolResultStore(tmp_path).open_output_spool(
        tool_name="exec", session_id="test-session", session_key="agent:test", agent_id="test",
    )
    for _ in range(192):
        spool.append(b"x" * 65_536)
    handle = spool.finish()
    tracemalloc.start()
    try:
        result = query_stored_tool_result(
            tmp_path, "test-session", handle, mode="grep", pattern=pattern,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024
    if pattern == "absent literal":
        assert "No matches" in result
        assert "returned_content_is_complete: true" in result
    else:
        assert "Search incomplete" in result
        assert "No matches" not in result


def test_model_query_reports_pending_log_instead_of_missing(tmp_path: Path) -> None:
    from opensquilla.tools.types import SafeToolError

    spool = ToolResultStore(tmp_path).open_output_spool(
        tool_name="exec", session_id="test-session", session_key="agent:test", agent_id="test",
    )
    try:
        spool.append(b"running")
        with pytest.raises(SafeToolError, match="still being saved"):
            query_stored_tool_result(tmp_path, "test-session", spool.handle)
        handle = spool.finish()
        result = query_stored_tool_result(tmp_path, "test-session", handle, mode="raw_slice")
        assert "running" in result
    finally:
        spool.close()
