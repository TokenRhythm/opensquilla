"""In-process 24h router calibration job.

A tiny background-loop service that periodically recomputes the on-device
calibration state from local decision records and writes
``router_calibration.json``. It shares the exact same pure aggregation
(:func:`opensquilla.engine.routing.calibration.aggregate_calibration`) as the
``opensquilla router calibrate`` CLI, so the CLI and the daily job never
diverge.

Opt-in: the gateway only constructs and starts this service when
``squilla_router.calibration_enabled`` is true (see ``gateway/boot.py``). With
the flag off — the default — no calibration job runs and the confidence gate is
byte-identical to today. The loop is fail-open: a tick failure is logged and the
next tick still runs.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

import structlog

from opensquilla.asyncio_utils import create_background_task
from opensquilla.engine.routing.calibration import (
    CalibrationState,
    aggregate_calibration,
    load_calibration,
    save_calibration,
)

if TYPE_CHECKING:
    from opensquilla.persistence.router_decision_writer import RouterDecisionWriter

log = structlog.get_logger(__name__)

_DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60.0
_DEFAULT_MAX_RECORDS = 5000
_PAGE_SIZE = 1000


def collect_decision_records(
    writer: RouterDecisionWriter,
    *,
    max_records: int = _DEFAULT_MAX_RECORDS,
    page_size: int = _PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Page newest-first decision records out of the writer, de-duplicated.

    Best-effort: any read failure yields whatever was gathered so far (the
    writer's own methods already fail-open to ``[]``).
    """
    cap = max(1, int(max_records))
    page = max(1, min(int(page_size), 1000))
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    before_ts_ms: int | None = None
    while len(records) < cap:
        batch = writer.list_decisions(limit=page, before_ts_ms=before_ts_ms)
        if not batch:
            break
        added = 0
        oldest = before_ts_ms
        for record in batch:
            if not isinstance(record, Mapping):
                continue
            decision_id = record.get("decision_id")
            if isinstance(decision_id, str):
                if decision_id in seen:
                    continue
                seen.add(decision_id)
            records.append(dict(record))
            added += 1
            ts = record.get("ts_ms")
            if isinstance(ts, int) and not isinstance(ts, bool):
                oldest = ts if oldest is None else min(oldest, ts)
            if len(records) >= cap:
                break
        if added == 0 or oldest is None or oldest == before_ts_ms:
            break
        before_ts_ms = oldest
    return records


class RouterCalibrationService:
    """Recompute the router calibration state every ``interval_seconds``."""

    def __init__(
        self,
        *,
        writer: RouterDecisionWriter,
        interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
        max_records: int = _DEFAULT_MAX_RECORDS,
        enabled: bool = True,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._writer = writer
        self._interval_seconds = max(1.0, float(interval_seconds))
        self._max_records = max(1, int(max_records))
        self._enabled = bool(enabled)
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._task: asyncio.Task[Any] | None = None
        self._stop_event = asyncio.Event()

    def run_once(self) -> CalibrationState:
        """Read records, aggregate (blending the prior file), and persist.

        Deterministic given the current records and clock; the pure aggregation
        does all the math. Returns the state that was written.
        """
        records = collect_decision_records(self._writer, max_records=self._max_records)
        now = int(self._clock())
        prior = load_calibration()
        state = aggregate_calibration(records, now=now, prior=prior)
        save_calibration(state)
        log.info(
            "router_calibration.updated",
            samples=state.sample_count,
            threshold_adjust=state.threshold_adjust,
            biased_tiers=sorted(state.per_class_bias),
        )
        return state

    def start(self) -> None:
        if not self._enabled or self._task is not None:
            return
        self._stop_event.clear()
        self._task = create_background_task(self._loop())

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is None:
            return
        try:
            # ``asyncio.to_thread`` cancellation cannot stop the worker. Let
            # the current calibration finish before callers close its writer,
            # then the stop event prevents another interval from starting.
            await task
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            log.warning("router_calibration.stop_failed", error=str(exc))

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.to_thread(self.run_once)
            except Exception as exc:  # noqa: BLE001 - a tick must never kill the loop
                log.warning("router_calibration.tick_failed", error=str(exc))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                continue
