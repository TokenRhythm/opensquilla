"""RouterCalibrationService: record collection, one-shot run, start/stop.

Uses a synthetic (hand-created) ``router_decisions`` table and a monkeypatched
state dir so ``run_once`` writes ``router_calibration.json`` under the temp home,
never the real one.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from opensquilla.engine.routing.calibration import calibration_path, load_calibration
from opensquilla.engine.routing.calibration_service import (
    RouterCalibrationService,
    collect_decision_records,
)
from opensquilla.persistence.router_decision_writer import RouterDecisionWriter

_CREATE_TABLE = (
    "CREATE TABLE router_decisions ("
    " decision_id TEXT PRIMARY KEY, session_key TEXT NOT NULL,"
    " turn_index INTEGER, ts_ms INTEGER NOT NULL, classifier TEXT,"
    " proposed_tier TEXT, confidence REAL, probs TEXT, flags TEXT,"
    " final_tier TEXT, provider TEXT, model TEXT, thinking_level TEXT,"
    " source TEXT, trail TEXT, baseline_model TEXT, savings_pct REAL,"
    " executed_kind TEXT, ensemble_profile TEXT,"
    " fallback_hops INTEGER NOT NULL DEFAULT 0)"
)


def _writer(tmp_path: Path, *, count: int = 30) -> RouterDecisionWriter:
    conn = sqlite3.connect(
        str(tmp_path / "sessions.db"), check_same_thread=False, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_TABLE)
    writer = RouterDecisionWriter(conn)
    # Recent, distinct, descending timestamps: recent so the writer's retention
    # prune (DELETE WHERE ts_ms < now-30d, every 64 inserts) keeps them, and
    # distinct so keyset paging is exact.
    base_ts = int(time.time() * 1000)
    for index in range(count):
        writer.record_decision(
            {
                "decision_id": f"r{index}",
                "session_key": "agent:svc:main",
                "turn_index": index,
                "ts_ms": base_ts - index,
                "proposed_tier": "c2",
                "final_tier": "c1",
                "source": "v4_phase3",
                "trail": [{"stage": "confidence_gate", "applied": True}],
            }
        )
    return writer


def test_collect_decision_records_pages_and_dedups(tmp_path: Path) -> None:
    # Five records at two records per page still exercises three keyset pages;
    # the production-sized 2500-row fixture only made this unit contract slow.
    writer = _writer(tmp_path, count=5)
    try:
        records = collect_decision_records(writer, max_records=5, page_size=2)
    finally:
        writer.close()
    ids = [r["decision_id"] for r in records]
    assert len(ids) == len(set(ids))  # no duplicates across pages
    assert len(records) == 5


def test_collect_decision_records_respects_max(tmp_path: Path) -> None:
    writer = _writer(tmp_path, count=100)
    try:
        records = collect_decision_records(writer, max_records=25)
    finally:
        writer.close()
    assert len(records) == 25


def test_run_once_writes_calibration(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    writer = _writer(tmp_path, count=30)
    try:
        service = RouterCalibrationService(
            writer=writer, interval_seconds=1.0, clock=lambda: 1_700_000_000_000
        )
        state = service.run_once()
    finally:
        writer.close()

    assert state.sample_count == 30
    assert state.per_class_bias == {"c2": -0.15}
    out = calibration_path()
    assert out.exists()
    assert load_calibration().sample_count == 30


async def test_start_stop_is_clean(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    writer = _writer(tmp_path, count=5)
    service = RouterCalibrationService(
        writer=writer, interval_seconds=3600.0, clock=lambda: 1_700_000_000_000
    )
    service.start()
    await service.stop()  # must not raise; task cancelled cleanly
    await service.stop()  # idempotent
    writer.close()


async def test_background_calibration_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    writer = _writer(tmp_path, count=1)
    service = RouterCalibrationService(writer=writer, interval_seconds=3600.0)
    started = threading.Event()
    release = threading.Event()

    def _blocking_run_once():
        started.set()
        release.wait(timeout=1.0)

    monkeypatch.setattr(service, "run_once", _blocking_run_once)
    service.start()
    try:
        assert await asyncio.to_thread(started.wait, 0.5)
        loop = asyncio.get_running_loop()
        before = loop.time()
        await asyncio.sleep(0.05)
        assert loop.time() - before < 0.2
    finally:
        release.set()
        await service.stop()
        writer.close()


async def test_stop_waits_for_inflight_calibration_before_writer_close(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    writer = _writer(tmp_path, count=1)
    service = RouterCalibrationService(writer=writer, interval_seconds=3600.0)
    started = threading.Event()
    release = threading.Event()

    def _blocking_run_once():
        started.set()
        release.wait(timeout=2.0)

    monkeypatch.setattr(service, "run_once", _blocking_run_once)
    service.start()
    assert await asyncio.to_thread(started.wait, 0.5)

    stop_task = asyncio.create_task(service.stop())
    await asyncio.sleep(0)
    assert not stop_task.done()

    release.set()
    await asyncio.wait_for(stop_task, timeout=1.0)
    writer.close()


def test_disabled_service_never_starts(tmp_path: Path) -> None:
    writer = _writer(tmp_path, count=5)
    service = RouterCalibrationService(writer=writer, enabled=False)
    service.start()
    assert service._task is None  # noqa: SLF001 - asserting the disabled guard
    writer.close()
