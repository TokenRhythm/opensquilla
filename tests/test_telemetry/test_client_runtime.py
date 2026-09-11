from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from opensquilla.telemetry.consent import (
    CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
    CURRENT_RELIABILITY_NOTICE_VERSION,
    ConsentDecision,
    ScopeConsentState,
    TelemetryScope,
)
from opensquilla.telemetry.contracts import TELEMETRY_EVENT_ADAPTER
from opensquilla.telemetry.coordination import scope_consent_coordinator_for
from opensquilla.telemetry.recorder import RecordStatus
from opensquilla.telemetry.runtime import ScopedTelemetryRuntime


def _config(state_dir: Path, *, reliability: bool | None, growth: bool | None):
    config = SimpleNamespace(
        state_dir=str(state_dir),
        privacy=SimpleNamespace(
            disable_network_observability=False,
            reliability_diagnostics_enabled=reliability,
            reliability_notice_version=(
                CURRENT_RELIABILITY_NOTICE_VERSION if reliability is True else None
            ),
            reliability_consented_at_utc=(
                "2026-09-02T01:00:00Z" if reliability is True else None
            ),
            product_analytics_enabled=growth,
            product_analytics_notice_version=(
                CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION if growth is True else None
            ),
            product_analytics_consented_at_utc=(
                "2026-09-02T01:00:00Z" if growth is True else None
            ),
        ),
    )
    decisions = {
        TelemetryScope.RELIABILITY: reliability,
        TelemetryScope.GROWTH: growth,
    }

    def state_provider(scope: TelemetryScope) -> ScopeConsentState:
        enabled = decisions[scope]
        notice = (
            CURRENT_RELIABILITY_NOTICE_VERSION
            if scope is TelemetryScope.RELIABILITY
            else CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION
        )
        return ScopeConsentState(
            scope=scope,
            decision=(
                ConsentDecision.GRANTED
                if enabled is True
                else ConsentDecision.DECLINED
                if enabled is False
                else ConsentDecision.UNSET
            ),
            notice_version=notice if enabled is True else None,
            consented_at_utc="2026-09-02T01:00:00Z" if enabled is True else None,
            record_complete=enabled is True,
            notice_current=enabled is True,
        )

    coordinator = scope_consent_coordinator_for(config, state_provider=state_provider)
    config._test_telemetry_decisions = decisions
    config._test_telemetry_coordinator = coordinator
    return config


def _turn_event(number: int = 1):
    return TELEMETRY_EVENT_ADAPTER.validate_json(
        json.dumps(
            {
                "event_name": "turn_result",
                "event_version": 1,
                "event_id": f"00000000-0000-4000-8000-{number:012d}",
                "occurred_at_utc": "2026-09-02T01:02:03.456Z",
                "source": "gateway",
                "app_version": "1.2.3",
                "platform": "linux",
                "outcome": "success",
                "error_code": None,
                "duration_ms": 120,
                "consent_scope": "reliability",
                "notice_version": CURRENT_RELIABILITY_NOTICE_VERSION,
                "sample_rate": 1.0,
                "app_session_id": "00000000-0000-4000-8000-000000000900",
                "ttft_ms": 40,
                "stall_count": 0,
                "stall_threshold_ms": 15_000,
            }
        ),
        strict=True,
    )


async def test_start_does_not_create_state_or_network_without_consent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = 0

    async def forbidden_request(*_args, **_kwargs):
        nonlocal requests
        requests += 1
        raise AssertionError("network must remain idle")

    monkeypatch.setattr(httpx.AsyncClient, "stream", forbidden_request)
    state_dir = tmp_path / "state"
    runtime = ScopedTelemetryRuntime(
        config=_config(state_dir, reliability=None, growth=False),
        upload_interval_seconds=0.01,
    )
    await runtime.start()
    try:
        await asyncio.sleep(0.03)
        result = await runtime.record(_turn_event())
        assert result.status is RecordStatus.CONSENT_BLOCKED
        assert runtime.opened_scopes == frozenset()
        assert not (state_dir / "telemetry").exists()
        assert requests == 0
    finally:
        await runtime.close()


async def test_record_lazily_opens_only_the_granted_scope(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    runtime = ScopedTelemetryRuntime(
        config=_config(state_dir, reliability=True, growth=False),
    )
    try:
        result = await runtime.record(_turn_event())
        assert result.status is RecordStatus.RECORDED
        assert runtime.opened_scopes == frozenset({TelemetryScope.RELIABILITY})
        assert (state_dir / "telemetry" / "reliability-outbox.sqlite3").is_file()
        assert not (state_dir / "telemetry" / "growth-outbox.sqlite3").exists()
    finally:
        await runtime.close()


async def test_live_decline_blocks_after_scope_was_opened(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    config = _config(state_dir, reliability=True, growth=False)
    runtime = ScopedTelemetryRuntime(config=config)
    try:
        assert (await runtime.record(_turn_event(1))).status is RecordStatus.RECORDED
        config._test_telemetry_decisions[TelemetryScope.RELIABILITY] = False
        config.privacy.reliability_diagnostics_enabled = False
        config.privacy.reliability_notice_version = None
        config.privacy.reliability_consented_at_utc = None
        assert (await runtime.record(_turn_event(2))).status is RecordStatus.CONSENT_BLOCKED
    finally:
        await runtime.close()


async def test_background_record_failure_does_not_escape(tmp_path: Path) -> None:
    runtime = ScopedTelemetryRuntime(
        config=_config(tmp_path / "state", reliability=True, growth=False)
    )
    await runtime.close()
    runtime.record_background(_turn_event())


async def test_background_record_from_worker_returns_to_owner_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_network_upload(_self) -> None:
        return None

    monkeypatch.setattr(
        "opensquilla.telemetry.uploader.TelemetryUploader.upload_once",
        no_network_upload,
    )
    state_dir = tmp_path / "state"
    runtime = ScopedTelemetryRuntime(
        config=_config(state_dir, reliability=True, growth=False),
        upload_interval_seconds=60,
    )
    await runtime.start()
    try:
        await asyncio.to_thread(runtime.record_background, _turn_event())
        for _ in range(20):
            scoped = runtime._scopes.get(TelemetryScope.RELIABILITY)
            if scoped is not None and (await scoped.outbox.stats()).pending_events == 1:
                break
            await asyncio.sleep(0.01)
        assert scoped is not None
        assert (await scoped.outbox.stats()).pending_events == 1
    finally:
        await runtime.close()


async def test_close_is_idempotent_and_rejects_new_direct_work(tmp_path: Path) -> None:
    runtime = ScopedTelemetryRuntime(
        config=_config(tmp_path / "state", reliability=True, growth=False)
    )
    await runtime.start()
    await runtime.close()
    await runtime.close()
    with pytest.raises(RuntimeError, match="closed"):
        await runtime.record(_turn_event())


async def test_start_drains_consented_desktop_scope_into_isolated_outbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_network_upload(_self) -> None:
        return None

    monkeypatch.setattr(
        "opensquilla.telemetry.uploader.TelemetryUploader.upload_once",
        no_network_upload,
    )
    state_dir = tmp_path / "state"
    event = {
        "event_name": "first_app_ready",
        "event_version": 1,
        "event_id": "00000000-0000-4000-8000-000000000777",
        "occurred_at_utc": "2026-09-02T01:02:03.456Z",
        "source": "desktop",
        "app_version": "1.2.3",
        "platform": "linux",
        "outcome": None,
        "error_code": None,
        "duration_ms": None,
        "consent_scope": "growth",
        "notice_version": CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
        "sample_rate": 1,
        "analytics_user_id": "00000000-0000-4000-8000-000000000778",
    }
    scope_dir = state_dir / "telemetry" / "desktop-early-spool" / "growth"
    scope_dir.mkdir(parents=True)
    ready = scope_dir / f"{event['event_id']}.ready"
    ready.write_text(json.dumps(event), encoding="utf-8")
    runtime = ScopedTelemetryRuntime(
        config=_config(state_dir, reliability=False, growth=True),
        upload_interval_seconds=60,
        env={},
    )

    await runtime.start()
    try:
        scoped = runtime._scopes[TelemetryScope.GROWTH]
        assert (await scoped.outbox.stats()).pending_events == 1
        assert not ready.exists()
        assert runtime.opened_scopes == frozenset({TelemetryScope.GROWTH})
        assert not (state_dir / "telemetry" / "reliability-outbox.sqlite3").exists()
    finally:
        await runtime.close()
