from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import opensquilla.engine.agent as agent_mod
from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.tool_result_store import (
    ToolResultStore,
    ToolResultStoreBudgetError,
)

_SESSION_ID = "session-1"
_SESSION_KEY = "agent:main:webchat:session-1"


def _write(store: ToolResultStore, content: str, *, tool_use_id: str = "tool-1", **over: Any):
    kwargs: dict[str, Any] = dict(
        tool_use_id=tool_use_id,
        tool_name="fetch",
        session_id=_SESSION_ID,
        session_key=_SESSION_KEY,
        agent_id="main",
    )
    kwargs.update(over)
    return store.write(content, **kwargs)


def test_identical_content_dedupes_to_one_record(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path)
    body = "same output\n" + "x" * 2000
    first = _write(store, body, tool_use_id="tool-1")
    second = _write(store, body, tool_use_id="tool-2")

    # Same content -> same content-addressed handle, and the repeat write reuses the
    # record rather than writing a second directory.
    assert second.handle == first.handle
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert first.handle == f"tr-{sha[:32]}"
    assert len(list(tmp_path.rglob("content.txt"))) == 1


def test_dedup_hit_still_enforces_retention(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path)
    hot = _write(store, "hot output", tool_use_id="t1")
    victim = _write(store, "stale output", tool_use_id="t2")

    # Age the victim well past retention.
    victim_content = store._record_dir(victim.handle, session_id=_SESSION_ID) / "content.txt"
    os.utime(victim_content, (1_000.0, 1_000.0))

    # Re-writing identical "hot output" is a dedup hit; it must still reap the expired
    # victim (retention is not bypassed on hits) while keeping the reused record alive.
    again = _write(store, "hot output", tool_use_id="t3", retention_seconds=1)

    assert again.handle == hot.handle
    with pytest.raises(FileNotFoundError):
        store.read(victim.handle, session_id=_SESSION_ID)
    assert store.read(hot.handle, session_id=_SESSION_ID).content == "hot output"


def test_dedup_hit_with_zero_retention_returns_usable_handle(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path)
    _write(store, "recurring output", tool_use_id="t1")

    # retention_seconds=0 expires everything written before "now"; a repeat write must
    # still return a handle that resolves (the record is re-created, never dangling).
    second = _write(store, "recurring output", tool_use_id="t2", retention_seconds=0)

    assert store.read(second.handle, session_id=_SESSION_ID).content == "recurring output"


def test_distinct_content_gets_distinct_handles(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path)
    a = _write(store, "alpha" * 100, tool_use_id="t1")
    b = _write(store, "bravo" * 100, tool_use_id="t2")
    assert a.handle != b.handle
    assert len(list(tmp_path.rglob("content.txt"))) == 2


def test_round_trip_read(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path)
    record = _write(store, "payload body")
    got = store.read(record.handle, session_id=_SESSION_ID)
    assert got.content == "payload body"
    assert got.sha256 == record.sha256


def test_session_scoped_reads(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path)
    record = _write(store, "private output")
    with pytest.raises(FileNotFoundError):
        store.read(record.handle, session_id="session-2")


def test_rejects_single_result_over_budget(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path)
    with pytest.raises(ToolResultStoreBudgetError):
        _write(store, "abcdef", max_bytes=5)
    assert not list(tmp_path.rglob("content.txt"))


def test_prunes_oldest_for_disk_budget(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path)
    first = _write(store, "a" * 40, tool_use_id="t1", disk_budget_bytes=120)
    second = _write(store, "b" * 40, tool_use_id="t2", disk_budget_bytes=120)

    # Pin deterministic (but recent, non-expired) ages so eviction order is stable:
    # cleanup orders by the content file's mtime.
    now = time.time()
    first_content = store._record_dir(first.handle, session_id=_SESSION_ID) / "content.txt"
    second_content = store._record_dir(second.handle, session_id=_SESSION_ID) / "content.txt"
    os.utime(first_content, (now - 10, now - 10))
    os.utime(second_content, (now - 5, now - 5))

    third = _write(store, "c" * 40, tool_use_id="t3", disk_budget_bytes=90)

    with pytest.raises(FileNotFoundError):
        store.read(first.handle, session_id=_SESSION_ID)
    assert store.read(second.handle, session_id=_SESSION_ID).content == "b" * 40
    assert store.read(third.handle, session_id=_SESSION_ID).content == "c" * 40


def test_expired_records_removed_before_write(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path)
    expired = _write(store, "old output", tool_use_id="t1")

    # Age the record well past retention by backdating its content file's mtime;
    # cleanup keys on mtime, not the recorded created_at string.
    content = store._record_dir(expired.handle, session_id=_SESSION_ID) / "content.txt"
    os.utime(content, (1_000.0, 1_000.0))

    _write(store, "new output", tool_use_id="t2", retention_seconds=1)

    with pytest.raises(FileNotFoundError):
        store.read(expired.handle, session_id=_SESSION_ID)


def test_truncated_digest_collision_falls_back_to_random_handle(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path)
    content = "real content payload"
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    primary_handle = f"tr-{sha[:32]}"

    # Simulate the ~2**-64 truncated-digest collision: a *different* payload already
    # occupies the content-addressed directory (its recorded sha256 does not match).
    collision_dir = store._record_dir(primary_handle, session_id=_SESSION_ID)
    collision_dir.mkdir(parents=True, exist_ok=True)
    (collision_dir / "meta.json").write_text(
        json.dumps({"handle": primary_handle, "sha256": "0" * 64, "created_at": ""}),
        encoding="utf-8",
    )
    (collision_dir / "content.txt").write_text("different payload", encoding="utf-8")

    record = _write(store, content)

    # Falls back to a fresh random handle; the colliding record is left untouched.
    assert record.handle != primary_handle
    assert store.read(record.handle, session_id=_SESSION_ID).content == content
    assert (collision_dir / "content.txt").read_text(encoding="utf-8") == "different payload"


def test_cleanup_ignores_non_record_directories(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path)
    record = _write(store, "kept output", tool_use_id="t1")

    # A stray content.txt under the store tree whose directory is NOT a valid handle
    # must never be enumerated or deleted by retention/budget cleanup.
    stray = tmp_path / "s" / _SESSION_ID / "zz" / "not-a-handle"
    stray.mkdir(parents=True, exist_ok=True)
    (stray / "content.txt").write_text("foreign file", encoding="utf-8")
    os.utime(stray / "content.txt", (1_000.0, 1_000.0))  # ancient -> would expire if scanned

    # A fresh write triggers cleanup. Keep the retention window comfortably
    # above slow Windows filesystem/antivirus scans so the record written just
    # above cannot expire while this test prepares the stray directory. The
    # 1970 timestamp remains old enough to prove a stray path is never scanned.
    _write(store, "new output", tool_use_id="t2", retention_seconds=60)

    assert (stray / "content.txt").read_text(encoding="utf-8") == "foreign file"
    assert store.read(record.handle, session_id=_SESSION_ID).content == "kept output"


class _NoopProvider:
    provider_name = "fake"

    def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:  # pragma: no cover
        raise AssertionError("provider should not be used")

    async def list_models(self) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_projection_dedupes_identical_tool_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Real path: the live tokenjuice projection externalizes a large tool result to the
    store, and re-projecting identical content reuses a single record instead of growing
    the store every turn."""

    def fake_reduce(**kwargs: Any) -> Any:
        return SimpleNamespace(
            inline_text="[tokenjuice]\nreduced",
            raw_chars=len(kwargs["content"]),
            reduced_chars=18,
            ratio=0.1,
            reducer="tests/pytest",
        )

    monkeypatch.setattr(agent_mod, "reduce_tool_result_with_tokenjuice", fake_reduce, raising=False)

    store_dir = tmp_path / "tool-results"
    agent = Agent(
        provider=_NoopProvider(),
        config=AgentConfig(
            tool_result_store_dir=str(store_dir),
            tool_result_store_session_id=_SESSION_ID,
            tool_result_store_session_key=_SESSION_KEY,
            tool_result_store_agent_id="main",
        ),
    )
    raw = "raw output\n" + ("x" * 8000)

    first = await agent._canonicalize_tool_result(
        ToolResult(tool_use_id="tool-1", tool_name="exec_command", content=raw)
    )
    second = await agent._canonicalize_tool_result(
        ToolResult(tool_use_id="tool-2", tool_name="exec_command", content=raw)
    )

    handle_re = re.compile(r"tool_result_handle: (tr-[0-9a-f]{32})")
    m1 = handle_re.search(first.content)
    m2 = handle_re.search(second.content)
    assert m1 is not None and m2 is not None
    # Both projections point at the same content-addressed handle, and only one record
    # exists on disk despite two separate tool results.
    assert m1.group(1) == m2.group(1)
    assert len(list(store_dir.rglob("content.txt"))) == 1
