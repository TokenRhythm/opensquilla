from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from functools import partial
from types import SimpleNamespace

import httpx
import pytest

from opensquilla.gateway.config import PrivacyConfig
from opensquilla.telemetry import runtime as runtime_module
from opensquilla.telemetry.consent import TelemetryScope, resolve_scope_consent
from opensquilla.telemetry.contracts import (
    TELEMETRY_PROTOCOL_FINGERPRINT_SHA256,
    GrowthEventBatch,
)
from opensquilla.telemetry.contracts.common import ClientSurface, ConsentScope, Platform
from opensquilla.telemetry.coordination import scope_consent_coordinator_for
from opensquilla.telemetry.growth.state import growth_cohort_state_path
from opensquilla.telemetry.growth_sink import GrowthEventSink
from opensquilla.telemetry.outbox import TelemetryOutbox
from opensquilla.telemetry.runtime import ScopedTelemetryRuntime
from opensquilla.telemetry.server.collector import create_collector_app
from opensquilla.telemetry.server.settings import CollectorSettings
from opensquilla.telemetry.server.storage import TelemetryIngestStorage
from opensquilla.telemetry.uploader import TelemetryUploader


@pytest.mark.parametrize("platform", list(Platform))
async def test_product_activity_survives_restart_and_lost_upload_receipt(
    tmp_path, monkeypatch, platform
) -> None:
    config = SimpleNamespace(state_dir=str(tmp_path / "client"), privacy=PrivacyConfig())
    scope_consent_coordinator_for(
        config,
        state_provider=lambda scope: resolve_scope_consent(scope, config=config, env={}),
    )
    occurred_at = datetime(2026, 9, 2, 1, tzinfo=UTC)
    now_ms = int(occurred_at.timestamp() * 1000) + 1000
    open_outbox = TelemetryOutbox.open

    async def open_with_clock(_cls, path, scope, **kwargs):
        return await open_outbox(path, scope, clock=lambda: now_ms, **kwargs)

    monkeypatch.setattr(TelemetryOutbox, "open", classmethod(open_with_clock))
    settings = CollectorSettings(
        scope=ConsentScope.GROWTH, database_path=tmp_path / "collector.sqlite3"
    )
    app = create_collector_app(settings)
    requests = []
    receipts = []

    async def lose_first_receipt(response):
        await response.aread()
        assert response.status_code == 202
        assert response.request.url.path == "/v1/growth/events"
        requests.append(json.loads(response.request.content))
        receipts.append(response.json())
        if len(receipts) == 1:
            raise httpx.ReadTimeout("synthetic lost receipt", request=response.request)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            trust_env=False,
            event_hooks={"response": [lose_first_receipt]},
        ) as client,
    ):
        monkeypatch.setattr(
            runtime_module,
            "TelemetryUploader",
            partial(TelemetryUploader, http_client=client, clock=lambda: now_ms),
        )
        runtime = ScopedTelemetryRuntime(
            config=config, base_url="https://collector.invalid", env={}
        )
        sink = GrowthEventSink(runtime, config=config, platform=platform, clock=lambda: occurred_at)
        try:
            for surface in ClientSurface:
                assert await sink.record_product_active(surface=surface)
                assert not await sink.record_product_active(surface=surface)
            assert (await runtime._scopes[TelemetryScope.GROWTH].outbox.stats()).pending_events == 4
            assert requests == []
            await runtime.upload_once(TelemetryScope.GROWTH)
            assert len(receipts) == 1
            assert (await runtime._scopes[TelemetryScope.GROWTH].outbox.stats()).pending_events == 4
        finally:
            await sink.close()
            await runtime.close()

        now_ms += 120_000
        resumed_runtime = ScopedTelemetryRuntime(
            config=config, base_url="https://collector.invalid", env={}
        )
        resumed_sink = GrowthEventSink(
            resumed_runtime, config=config, platform=platform, clock=lambda: occurred_at
        )
        try:
            assert not await resumed_sink.record_product_active(surface=ClientSurface.DESKTOP)
            await resumed_runtime.upload_once(TelemetryScope.GROWTH)
            assert len(receipts) == 2
            stats = await resumed_runtime._scopes[TelemetryScope.GROWTH].outbox.stats()
            assert stats.pending_events == 0
        finally:
            await resumed_sink.close()
            await resumed_runtime.close()

    assert requests[0]["events"] == requests[1]["events"]
    assert len({event["event_id"] for event in requests[0]["events"]}) == 4
    assert all(event["event_name"] == "product_active" for event in requests[0]["events"])
    assert all(event["source"] == "gateway" for event in requests[0]["events"])
    assert all(event["platform"] == platform.value for event in requests[0]["events"])
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT analytics_user_id), "
            "COUNT(DISTINCT json_extract(payload_json, '$.surface')) FROM events"
        ).fetchone() == (4, 1, 4)
    assert not growth_cohort_state_path(config=config).exists()


async def test_pre_activity_protocol_upgrade_preserves_old_events_and_accepts_mixed_batch(tmp_path):
    old_fingerprint = "37eef99b9de090a2032669d3caa9cd10f4357061658b2458326595361582732f"
    settings = CollectorSettings(
        scope=ConsentScope.GROWTH, database_path=tmp_path / "previous-growth.sqlite3"
    )

    def event(number, name):
        payload = {
            "event_name": name,
            "event_version": 1,
            "event_id": f"00000000-0000-4000-8000-{number:012d}",
            "occurred_at_utc": "2026-09-02T01:00:00.000Z",
            "source": "runtime" if name == "metaskill_usage" else "gateway",
            "app_version": "1.2.3",
            "platform": "macos",
            "outcome": None,
            "error_code": None,
            "duration_ms": None,
            "consent_scope": "growth",
            "notice_version": "growth-v2",
            "sample_rate": 1,
            "analytics_user_id": "00000000-0000-4000-8000-000000000900",
        }
        if name != "metaskill_usage":
            payload["surface"] = "tui" if number == 1 else "cli"
        if name == "client_launch":
            payload.update(entrypoint="chat", execution_mode="gateway")
        return payload

    def batch(number, events):
        return {
            "batch_version": 1,
            "batch_id": f"00000000-0000-4000-8000-{number:012d}",
            "sent_at_utc": "2026-09-02T02:00:00.000Z",
            "events": events,
        }

    original = batch(100, [event(1, "client_launch"), event(2, "metaskill_usage")])
    storage = await TelemetryIngestStorage.open(
        settings.database_path, ConsentScope.GROWTH, protocol_fingerprint=old_fingerprint
    )
    try:
        receipt = await storage.ingest(GrowthEventBatch.model_validate_json(json.dumps(original)))
        assert receipt.accepted == 2
    finally:
        await storage.close()
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("SELECT protocol_fingerprint FROM meta").fetchone()[0] == (
            old_fingerprint
        )
        old_rows = connection.execute("SELECT * FROM events ORDER BY event_id").fetchall()
        old_schema = connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()

    app = create_collector_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://collector.invalid"
        ) as client,
    ):
        retried = await client.post(settings.endpoint_path, json=original)
        assert retried.status_code == 202
        assert retried.json()["duplicates"] == 2
        response = await client.post(
            settings.endpoint_path,
            json=batch(101, [
                event(3, "client_launch"), event(4, "metaskill_usage"), event(5, "product_active")
            ]),
        )
        assert response.status_code == 202
        assert response.json()["accepted"] == 3
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("SELECT protocol_fingerprint FROM meta").fetchone()[0] == (
            TELEMETRY_PROTOCOL_FINGERPRINT_SHA256
        )
        assert connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == (
            old_schema
        )
        rows = connection.execute("SELECT * FROM events ORDER BY event_id").fetchall()
        assert rows[:2] == old_rows
        assert len(rows) == 5
