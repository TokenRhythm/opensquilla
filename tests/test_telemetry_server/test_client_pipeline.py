from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from opensquilla.gateway.config import PrivacyConfig
from opensquilla.observability.network_policy import PRODUCT_ANALYTICS_DISABLED_ENV
from opensquilla.telemetry import runtime as runtime_module
from opensquilla.telemetry.consent import (
    CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
    CURRENT_RELIABILITY_NOTICE_VERSION,
    TelemetryScope,
    resolve_scope_consent,
)
from opensquilla.telemetry.contracts import TELEMETRY_EVENT_ADAPTER
from opensquilla.telemetry.contracts.common import ConsentScope
from opensquilla.telemetry.coordination import scope_consent_coordinator_for
from opensquilla.telemetry.outbox import TelemetryOutbox
from opensquilla.telemetry.recorder import RecordStatus
from opensquilla.telemetry.runtime import ScopedTelemetryRuntime
from opensquilla.telemetry.server.collector import create_collector_app
from opensquilla.telemetry.server.dashboard_queries import DashboardQueries, UtcCohortWindow
from opensquilla.telemetry.server.settings import CollectorSettings
from opensquilla.telemetry.uploader import TelemetryUploader


def _event_payload(*, number: int, day: str) -> dict[str, object]:
    return {
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


def _spool_event(state_dir: Path, *, number: int, day: str) -> Path:
    event = _event_payload(number=number, day=day)
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
        privacy=PrivacyConfig(),
    )
    # Isolate this collector's retry schedule without a separate saved choice.
    policy_env = {PRODUCT_ANALYTICS_DISABLED_ENV: "true"}
    scope_consent_coordinator_for(
        config,
        state_provider=lambda scope: resolve_scope_consent(scope, config=config, env=policy_env),
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
                config=config, base_url="https://collector.invalid", env=policy_env
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
                assert (
                    queries.reliability(UtcCohortWindow.from_dates("2026-08-01", "2026-08-01"))[
                        "appStart"
                    ]["estimatedEvents"]
                    == 1
                )

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


@pytest.mark.parametrize("event_name", ["app_start_result", "metaskill_usage", "coding_mode_usage"])
async def test_unified_default_upload_pause_resume_and_dashboard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event_name: str,
) -> None:
    scope = (
        TelemetryScope.RELIABILITY if event_name == "app_start_result" else TelemetryScope.GROWTH
    )
    config = SimpleNamespace(state_dir=str(tmp_path / "client"), privacy=PrivacyConfig())
    scope_consent_coordinator_for(
        config,
        state_provider=lambda item: resolve_scope_consent(item, config=config, env={}),
    )
    state = resolve_scope_consent(scope, config=config, env={})
    assert state.enabled and state.consented_at_utc is None
    payload = _event_payload(number=10, day="2026-08-02")
    if scope is TelemetryScope.GROWTH:
        payload.pop("app_session_id")
        payload.pop("failure_stage")
        payload.update(
            event_name=event_name,
            source="runtime",
            outcome=None,
            duration_ms=None,
            consent_scope="growth",
            notice_version=CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
            analytics_user_id="00000000-0000-4000-8000-000000000901",
        )
    event = TELEMETRY_EVENT_ADAPTER.validate_json(json.dumps(payload), strict=True)
    settings = CollectorSettings(
        scope=ConsentScope(scope.value),
        database_path=tmp_path / "collector.sqlite3",
    )
    app = create_collector_app(settings)
    queries = DashboardQueries(
        reliability_db_path=settings.database_path,
        growth_db_path=settings.database_path,
    )
    window = UtcCohortWindow.from_dates("2026-08-02", "2026-08-02")

    def dashboard_count() -> int:
        if scope is TelemetryScope.RELIABILITY:
            return queries.reliability(window)["appStart"]["estimatedEvents"]
        key = "metaskillUsage" if event_name == "metaskill_usage" else "codingModeUsage"
        return queries.growth(window)[key]["totalUses"]

    requests = []
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            trust_env=False,
        ) as client,
    ):

        async def observe_request(request):
            requests.append(request.url.path)

        client.event_hooks["request"].append(observe_request)
        monkeypatch.setattr(
            runtime_module,
            "TelemetryUploader",
            partial(TelemetryUploader, http_client=client),
        )
        runtime = ScopedTelemetryRuntime(
            config=config, base_url="https://collector.invalid", env={}
        )
        try:
            assert (await runtime.record(event)).status is RecordStatus.RECORDED
            config.privacy.disable_network_observability = True
            assert (await runtime.record(event)).status is RecordStatus.CONSENT_BLOCKED
            await runtime.upload_once(scope)
            assert requests == [] and dashboard_count() == 0
            assert (await runtime._scopes[scope].outbox.stats()).pending_events == 1

            config.privacy.disable_network_observability = False
            await runtime.upload_once(scope)
            assert requests == [settings.endpoint_path]
            assert dashboard_count() == 1
            assert (await runtime._scopes[scope].outbox.stats()).pending_events == 0
            await runtime.upload_once(scope)
            assert requests == [settings.endpoint_path] and dashboard_count() == 1
        finally:
            await runtime.close()


@pytest.mark.parametrize("platform", ["macos", "windows", "linux"])
async def test_desktop_activation_upload_and_lost_receipt_retry_preserve_funnel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    config = SimpleNamespace(state_dir=str(tmp_path / "fresh-client"), privacy=PrivacyConfig())
    scope_consent_coordinator_for(
        config,
        state_provider=lambda scope: resolve_scope_consent(scope, config=config, env={}),
    )
    events = []
    for minute, (event_name, source, outcome) in enumerate(
        [
            ("onboarding_result", "desktop", "completed"),
            ("first_app_ready", "desktop", None),
            ("first_turn_started", "gateway", None),
            ("first_turn_result", "runtime", "success"),
        ]
    ):
        payload = _event_payload(number=20 + minute, day="2026-08-02")
        payload.pop("app_session_id")
        payload.pop("failure_stage")
        payload.update(
            event_name=event_name,
            occurred_at_utc=f"2026-08-02T01:{minute:02d}:00.000Z",
            source=source,
            platform=platform,
            outcome=outcome,
            duration_ms=None,
            consent_scope="growth",
            notice_version=CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
            analytics_user_id="00000000-0000-4000-8000-000000000902",
        )
        if event_name == "onboarding_result":
            payload["flow_version"] = 1
        events.append(TELEMETRY_EVENT_ADAPTER.validate_json(json.dumps(payload), strict=True))

    settings = CollectorSettings(
        scope=ConsentScope.GROWTH, database_path=tmp_path / "collector.sqlite3"
    )
    app = create_collector_app(settings)
    queries = DashboardQueries(
        reliability_db_path=tmp_path / "unused-reliability.sqlite3",
        growth_db_path=settings.database_path,
    )
    window = UtcCohortWindow.from_dates("2026-08-02", "2026-08-02")
    now_ms = int(datetime(2026, 8, 2, 2, tzinfo=UTC).timestamp() * 1000)
    open_outbox = TelemetryOutbox.open

    async def open_with_clock(_cls, path, scope, **kwargs):
        return await open_outbox(path, scope, clock=lambda: now_ms, **kwargs)

    monkeypatch.setattr(TelemetryOutbox, "open", classmethod(open_with_clock))
    receipts = []
    requests = []
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), trust_env=False
        ) as client,
    ):

        async def lose_first_receipt(response):
            await response.aread()
            assert response.status_code == 202
            receipts.append(response.json())
            requests.append(json.loads(response.request.content))
            assert response.request.url.path == settings.endpoint_path
            if len(receipts) == 1:
                # The collector committed the batch, but its receipt was lost.
                raise httpx.ReadTimeout("synthetic lost receipt", request=response.request)

        client.event_hooks["response"].append(lose_first_receipt)
        monkeypatch.setattr(
            runtime_module,
            "TelemetryUploader",
            partial(
                TelemetryUploader,
                http_client=client,
                clock=lambda: now_ms,
                random_value=lambda: 0,
            ),
        )
        runtime = ScopedTelemetryRuntime(
            config=config, base_url="https://collector.invalid", env={}
        )
        try:
            for event in events:
                assert (await runtime.record(event)).status is RecordStatus.RECORDED
            outbox = runtime._scopes[TelemetryScope.GROWTH].outbox
            assert (await outbox.stats()).pending_events == 4

            for expected_pending in [4, 0]:
                await runtime.upload_once(TelemetryScope.GROWTH)
                assert (await outbox.stats()).pending_events == expected_pending
                activation = queries.growth(window)["activation"]
                assert [stage["stage"] for stage in activation["stages"]] == [
                    "onboarding_completed", "first_app_ready",
                    "first_turn_started", "first_turn_succeeded",
                ]
                assert [stage["deduplicatedCount"] for stage in activation["stages"]] == [1] * 4
                assert [step["dropoffRate"] for step in activation["transitions"]] == [0] * 3
                now_ms += 10_000

            assert [(receipt["accepted"], receipt["duplicates"]) for receipt in receipts] == [
                (4, 0), (0, 4),
            ]
            assert requests[0]["events"] == requests[1]["events"]
        finally:
            await runtime.close()
