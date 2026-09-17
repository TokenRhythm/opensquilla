from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from opensquilla.gateway.config import PrivacyConfig
from opensquilla.telemetry import runtime as runtime_module
from opensquilla.telemetry.consent import TelemetryScope, resolve_scope_consent
from opensquilla.telemetry.contracts.common import ClientSurface, ConsentScope, Platform
from opensquilla.telemetry.coordination import scope_consent_coordinator_for
from opensquilla.telemetry.growth_sink import GrowthEventSink, read_product_active_state
from opensquilla.telemetry.identity import (
    TelemetryIdentityKind,
    identity_state_path,
    load_or_create_identity,
)
from opensquilla.telemetry.outbox import TelemetryOutbox
from opensquilla.telemetry.runtime import ScopedTelemetryRuntime
from opensquilla.telemetry.server.collector import create_collector_app
from opensquilla.telemetry.server.dashboard_queries import DashboardQueries, UtcCohortWindow
from opensquilla.telemetry.server.settings import CollectorSettings
from opensquilla.telemetry.uploader import TelemetryUploader

_FIRST_DAY = datetime(2026, 9, 1, 12, tzinfo=UTC)


@pytest.fixture
def activity_clock(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    clock = SimpleNamespace(now=_FIRST_DAY)
    open_outbox = TelemetryOutbox.open

    async def open_with_clock(_cls, path, scope, **kwargs):
        return await open_outbox(
            path, scope, clock=lambda: int(clock.now.timestamp() * 1000), **kwargs
        )

    monkeypatch.setattr(TelemetryOutbox, "open", classmethod(open_with_clock))
    return clock


def _profile(path: Path, *, env: dict[str, str] | None = None, enabled: bool = True):
    config = SimpleNamespace(
        state_dir=str(path), privacy=PrivacyConfig(disable_network_observability=not enabled)
    )
    scope_consent_coordinator_for(
        config,
        state_provider=lambda scope: resolve_scope_consent(scope, config=config, env=env or {}),
    )
    return config


def _client(config, clock, *, identity_number: int):
    load_or_create_identity(
        identity_state_path(TelemetryIdentityKind.ANALYTICS_USER, config=config),
        TelemetryIdentityKind.ANALYTICS_USER,
        now=clock.now,
        uuid_factory=lambda: UUID(f"00000000-0000-4000-8000-{identity_number:012d}"),
    )
    runtime = ScopedTelemetryRuntime(config=config, base_url="https://collector.invalid", env={})
    sink = GrowthEventSink(
        runtime,
        config=config,
        app_version="1.2.3",
        platform=Platform.MACOS,
        clock=lambda: clock.now,
    )
    return sink, runtime


def _collector(tmp_path: Path):
    settings = CollectorSettings(
        scope=ConsentScope.GROWTH, database_path=tmp_path / "growth.sqlite3"
    )
    app = create_collector_app(settings)
    queries = DashboardQueries(
        reliability_db_path=tmp_path / "unused-reliability.sqlite3",
        growth_db_path=settings.database_path,
    )
    return app, queries


def _counts(queries: DashboardQueries, day: str) -> tuple[int, int]:
    activity = queries.growth(UtcCohortWindow.from_dates(day, day))["productActivity"]
    return activity["dau"], activity["mau"]


def _use_transport(monkeypatch: pytest.MonkeyPatch, client: httpx.AsyncClient, clock) -> None:
    monkeypatch.setattr(
        runtime_module,
        "TelemetryUploader",
        partial(
            TelemetryUploader,
            http_client=client,
            clock=lambda: int(clock.now.timestamp() * 1000),
            random_value=lambda: 0,
        ),
    )


async def test_product_activity_sink_upload_counts_profiles_across_surfaces_and_days(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    activity_clock,
) -> None:
    app, queries = _collector(tmp_path)
    requests = []
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), trust_env=False) as client,
    ):

        async def observe_request(request):
            assert request.url.path == "/v1/growth/events"
            requests.append(json.loads(request.content))

        client.event_hooks["request"].append(observe_request)
        _use_transport(monkeypatch, client, activity_clock)
        first_config = _profile(tmp_path / "first-profile")
        first, first_runtime = _client(first_config, activity_clock, identity_number=1)
        second, second_runtime = _client(
            _profile(tmp_path / "second-profile"), activity_clock, identity_number=2
        )
        try:
            for surface in ClientSurface:
                assert await first.record_product_active(surface=surface)
                assert not await first.record_product_active(surface=surface)
            assert (
                await first_runtime._scopes[TelemetryScope.GROWTH].outbox.stats()
            ).pending_events == 4
            await first_runtime.upload_once(TelemetryScope.GROWTH)
            assert _counts(queries, "2026-09-01") == (1, 1)
            assert (
                await first_runtime._scopes[TelemetryScope.GROWTH].outbox.stats()
            ).pending_events == 0

            activity_clock.now += timedelta(days=1)
            # Existing live sink receives a new actual-use signal after UTC midnight.
            assert await first.record_product_active(surface=ClientSurface.DESKTOP)
            await first_runtime.upload_once(TelemetryScope.GROWTH)
            assert _counts(queries, "2026-09-02") == (1, 1)
            assert await second.record_product_active(surface=ClientSurface.WEB)
            await second_runtime.upload_once(TelemetryScope.GROWTH)
            assert _counts(queries, "2026-09-02") == (2, 2)
            assert _counts(queries, "2026-09-01") == (1, 1)
            assert (await app.state.telemetry_storage.stats()).event_count == 6
            assert (
                await second_runtime._scopes[TelemetryScope.GROWTH].outbox.stats()
            ).pending_events == 0
            assert len(requests) == 3
            events = [event for request in requests for event in request["events"]]
            assert {event["event_name"] for event in events} == {"product_active"}
            assert {event["surface"] for event in requests[0]["events"]} == {
                "desktop",
                "web",
                "tui",
                "cli",
            }
            assert len({event["analytics_user_id"] for event in requests[0]["events"]}) == 1
            assert len({event["analytics_user_id"] for event in events}) == 2
            assert all(
                event["source"] == "gateway" and event["sample_rate"] == 1 for event in events
            )
            growth = queries.growth(UtcCohortWindow.from_dates("2026-09-01", "2026-09-02"))
            assert growth["productActivity"]["dailyTrend"] == [
                {"period": "2026-09-01", "dau": 1, "mau": 1},
                {"period": "2026-09-02", "dau": 2, "mau": 2},
            ]
            assert growth["clientUsage"]["totals"]["terminalUsers"] == 0
        finally:
            await first.close()
            await second.close()
            await first_runtime.close()
            await second_runtime.close()


async def test_product_activity_durable_offline_retry_and_lost_receipt_do_not_inflate_users(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    activity_clock,
) -> None:
    app, queries = _collector(tmp_path)
    requests, receipts = [], []
    offline = True
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), trust_env=False) as client,
    ):

        async def interrupt_network(request):
            requests.append(json.loads(request.content))
            if offline:
                raise httpx.ConnectError("synthetic offline network", request=request)

        async def lose_first_receipt(response):
            await response.aread()
            assert response.status_code == 202
            receipts.append(response.json())
            if len(receipts) == 1:
                raise httpx.ReadTimeout("synthetic lost receipt", request=response.request)

        client.event_hooks["request"].append(interrupt_network)
        client.event_hooks["response"].append(lose_first_receipt)
        _use_transport(monkeypatch, client, activity_clock)
        config = _profile(tmp_path / "offline-profile")
        sink, runtime = _client(config, activity_clock, identity_number=3)
        try:
            assert await sink.record_product_active(surface=ClientSurface.TUI)
            ledger = read_product_active_state(sink.product_active_path)
            original_event = next(iter(ledger.values())).event
            await runtime.upload_once(TelemetryScope.GROWTH)
            assert _counts(queries, "2026-09-01") == (0, 0)
            assert (await runtime._scopes[TelemetryScope.GROWTH].outbox.stats()).pending_events == 1
        finally:
            await sink.close()
            await runtime.close()

        offline = False
        activity_clock.now += timedelta(seconds=10)
        sink, runtime = _client(config, activity_clock, identity_number=3)
        try:
            assert not await sink.record_product_active(surface=ClientSurface.TUI)
            await runtime.upload_once(TelemetryScope.GROWTH)
            assert _counts(queries, "2026-09-01") == (1, 1)
            assert (await runtime._scopes[TelemetryScope.GROWTH].outbox.stats()).pending_events == 1
            activity_clock.now += timedelta(seconds=10)
            await runtime.upload_once(TelemetryScope.GROWTH)
            assert (await runtime._scopes[TelemetryScope.GROWTH].outbox.stats()).pending_events == 0
            assert _counts(queries, "2026-09-01") == (1, 1)
            assert (await app.state.telemetry_storage.stats()).event_count == 1
            assert [(item["accepted"], item["duplicates"]) for item in receipts] == [(1, 0), (0, 1)]
            assert len(requests) == 3
            assert requests[0]["events"] == requests[1]["events"] == requests[2]["events"]
            assert requests[0]["events"][0]["event_id"] == str(original_event.event_id)
        finally:
            await sink.close()
            await runtime.close()


@pytest.mark.parametrize("veto", ["disabled", "CI", "GITHUB_ACTIONS"])
async def test_product_activity_upload_policy_and_ci_do_not_collect_or_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    activity_clock,
    veto: str,
) -> None:
    app, queries = _collector(tmp_path)
    requests = []
    env = {} if veto == "disabled" else {veto: "true"}
    config = _profile(tmp_path / "blocked-profile", env=env, enabled=veto != "disabled")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), trust_env=False) as client,
    ):

        async def observe_request(request):
            requests.append(request)

        client.event_hooks["request"].append(observe_request)
        _use_transport(monkeypatch, client, activity_clock)
        runtime = ScopedTelemetryRuntime(
            config=config, base_url="https://collector.invalid", env=env
        )
        sink = GrowthEventSink(runtime, config=config, clock=lambda: activity_clock.now)
        try:
            for surface in ClientSurface:
                assert not await sink.record_product_active(surface=surface)
            await runtime.upload_once(TelemetryScope.GROWTH)
            assert requests == []
            assert runtime.opened_scopes == frozenset()
            assert not sink.product_active_path.exists()
            assert not identity_state_path(
                TelemetryIdentityKind.ANALYTICS_USER, config=config
            ).exists()
            assert _counts(queries, "2026-09-01") == (0, 0)
            assert (await app.state.telemetry_storage.stats()).event_count == 0
        finally:
            await sink.close()
            await runtime.close()
