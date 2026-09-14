from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from opensquilla.telemetry import runtime as runtime_module
from opensquilla.telemetry.consent import (
    CURRENT_RELIABILITY_NOTICE_VERSION,
    TelemetryScope,
    resolve_scope_consent,
)
from opensquilla.telemetry.contracts.common import ConsentScope
from opensquilla.telemetry.coordination import scope_consent_coordinator_for
from opensquilla.telemetry.outbox import TelemetryOutbox
from opensquilla.telemetry.runtime import ScopedTelemetryRuntime
from opensquilla.telemetry.server.collector import create_collector_app
from opensquilla.telemetry.server.dashboard_queries import DashboardQueries, UtcCohortWindow
from opensquilla.telemetry.server.settings import CollectorSettings
from opensquilla.telemetry.uploader import TelemetryUploader


def _spool_event(state_dir: Path, *, number: int, day: str) -> Path:
    event = {
        "event_name": "app_start_result",
        "event_version": 1,
        "event_id": f"00000000-0000-4000-8000-{number:012d}",
        "occurred_at_utc": f"{day}T01:00:00.000Z",
        "source": "desktop",
        "app_version": "1.2.3",
        "platform": "macos",
        "outcome": "success",
        "error_code": None,
        "duration_ms": 120,
        "consent_scope": "reliability",
        "notice_version": CURRENT_RELIABILITY_NOTICE_VERSION,
        "sample_rate": 1,
        "app_session_id": f"00000000-0000-4000-8000-{number + 100:012d}",
        "failure_stage": None,
    }
    directory = state_dir / "telemetry" / "desktop-early-spool" / "reliability"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{event['event_id']}.ready"
    path.write_text(json.dumps(event), encoding="utf-8")
    return path


async def test_spool_retry_upload_advances_collector_watermark_and_dashboard_dates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "client"
    config = SimpleNamespace(
        state_dir=str(state_dir),
        privacy=SimpleNamespace(
            disable_network_observability=False,
            reliability_diagnostics_enabled=True,
            reliability_notice_version=CURRENT_RELIABILITY_NOTICE_VERSION,
            reliability_consented_at_utc="2026-08-01T00:00:00Z",
            product_analytics_enabled=False,
        ),
    )
    scope_consent_coordinator_for(
        config, state_provider=lambda scope: resolve_scope_consent(scope, config=config, env={})
    )
    first = _spool_event(state_dir, number=1, day="2026-08-01")
    settings = CollectorSettings(
        scope=ConsentScope.RELIABILITY, database_path=tmp_path / "collector.sqlite3"
    )
    app = create_collector_app(settings)
    queries = DashboardQueries(
        reliability_db_path=settings.database_path,
        growth_db_path=tmp_path / "unused-growth.sqlite3",
    )
    selected_day = UtcCohortWindow.from_dates("2026-08-02", "2026-08-02")
    sleeping = asyncio.Event()
    tick = asyncio.Event()
    delays = []
    attempts = 0
    open_outbox = TelemetryOutbox.open

    async def interval_sleep(delay):
        delays.append(delay)
        sleeping.set()
        await tick.wait()
        tick.clear()

    async def transient_open(_cls, path, scope, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise OSError("synthetic database temporarily unavailable")
        return await open_outbox(path, scope, **kwargs)

    monkeypatch.setattr(TelemetryOutbox, "open", classmethod(transient_open))
    monkeypatch.setattr(
        runtime_module, "asyncio", SimpleNamespace(**(vars(asyncio) | {"sleep": interval_sleep}))
    )
    async with app.router.lifespan_context(app):
        received_at = datetime(2026, 8, 2, 2, tzinfo=UTC)
        app.state.telemetry_storage._clock = lambda: received_at
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), trust_env=False
        ) as client:
            monkeypatch.setattr(
                runtime_module, "TelemetryUploader", partial(TelemetryUploader, http_client=client)
            )
            runtime = ScopedTelemetryRuntime(
                config=config, base_url="https://collector.invalid", env={}
            )
            await runtime.start()
            try:
                await asyncio.wait_for(sleeping.wait(), timeout=2)
                assert attempts == 3
                assert first.exists()
                assert queries.reliability(selected_day)["asOfReceivedUtc"] is None

                sleeping.clear()
                tick.set()
                await asyncio.wait_for(sleeping.wait(), timeout=2)
                assert not first.exists()
                initial = queries.reliability(selected_day)
                assert initial["asOfReceivedUtc"] == "2026-08-02T02:00:00.000Z"
                assert initial["appStart"]["estimatedEvents"] == 0
                assert queries.reliability(
                    UtcCohortWindow.from_dates("2026-08-01", "2026-08-01")
                )["appStart"]["estimatedEvents"] == 1

                second = _spool_event(state_dir, number=2, day="2026-08-02")
                received_at = datetime(2026, 8, 3, 2, tzinfo=UTC)
                sleeping.clear()
                tick.set()
                await asyncio.wait_for(sleeping.wait(), timeout=2)
                assert not second.exists()
                updated = queries.reliability(selected_day)
                assert updated["asOfReceivedUtc"] == "2026-08-03T02:00:00.000Z"
                assert updated["appStart"]["estimatedEvents"] == 1
                assert updated["dailyTrend"] == [
                    {"date": "2026-08-02", "estimatedEvents": 1, "estimatedIssues": 0}
                ]
                scoped = runtime._scopes[TelemetryScope.RELIABILITY]
                assert (await scoped.outbox.stats()).pending_events == 0
                assert delays == [30.0, 30.0, 30.0]
            finally:
                await runtime.close()
