from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from opensquilla.telemetry import runtime as runtime_module
from opensquilla.telemetry.consent import (
    CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
    CURRENT_RELIABILITY_NOTICE_VERSION,
    ConsentDecision,
    ScopeConsentState,
    TelemetryScope,
)
from opensquilla.telemetry.contracts import TELEMETRY_EVENT_ADAPTER
from opensquilla.telemetry.coordination import scope_consent_coordinator_for
from opensquilla.telemetry.outbox import TelemetryOutbox
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
    owner_loop = asyncio.get_running_loop()
    recorded = asyncio.Event()
    record_loops = []
    record = runtime.record

    async def capture_record(event, **kwargs):
        record_loops.append(asyncio.get_running_loop())
        result = await record(event, **kwargs)
        recorded.set()
        return result

    monkeypatch.setattr(runtime, "record", capture_record)
    await runtime.start()
    try:
        await asyncio.to_thread(runtime.record_background, _turn_event())
        await asyncio.wait_for(recorded.wait(), timeout=10)
        assert record_loops == [owner_loop]
        scoped = runtime._scopes[TelemetryScope.RELIABILITY]
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


async def test_upload_loop_recovers_scope_initialization_on_next_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = ScopedTelemetryRuntime(
        config=_config(tmp_path, reliability=True, growth=True)
    )
    attempts = {scope: 0 for scope in TelemetryScope}
    uploaded: list[TelemetryScope] = []
    sleeping = asyncio.Event()
    tick = asyncio.Event()
    delays: list[float] = []

    async def interval_sleep(delay):
        delays.append(delay)
        sleeping.set()
        await tick.wait()
        tick.clear()

    async def scope_runtime(scope):
        attempts[scope] += 1
        if scope is TelemetryScope.RELIABILITY and attempts[scope] == 1:
            raise OSError("synthetic scope initialization failure")

        async def upload_once():
            uploaded.append(scope)

        return SimpleNamespace(uploader=SimpleNamespace(upload_once=upload_once))

    monkeypatch.setattr(runtime, "_scope_runtime", scope_runtime)
    monkeypatch.setattr(
        runtime_module, "asyncio", SimpleNamespace(**(vars(asyncio) | {"sleep": interval_sleep}))
    )
    await runtime.start()
    try:
        await asyncio.wait_for(sleeping.wait(), timeout=1)
        assert uploaded == [TelemetryScope.GROWTH]
        sleeping.clear()
        tick.set()
        await asyncio.wait_for(sleeping.wait(), timeout=1)
        assert uploaded == [
            TelemetryScope.GROWTH,
            TelemetryScope.RELIABILITY,
            TelemetryScope.GROWTH,
        ]
        assert delays == [30.0, 30.0]
    finally:
        await runtime.close()


async def test_spool_initialization_failure_does_not_block_other_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "telemetry" / "desktop-early-spool"
    for scope in TelemetryScope:
        (root / scope.value).mkdir(parents=True)
    runtime = ScopedTelemetryRuntime(
        config=_config(tmp_path, reliability=True, growth=True), env={}
    )
    imports = []

    async def scope_runtime(scope):
        if scope is TelemetryScope.RELIABILITY:
            raise OSError("synthetic scope initialization failure")
        return SimpleNamespace(recorder="growth-recorder")

    async def capture_drain(_root, *, recorders, **_kwargs):
        imports.append(recorders)

    monkeypatch.setattr(runtime, "_scope_runtime", scope_runtime)
    monkeypatch.setattr("opensquilla.telemetry.runtime.drain_desktop_early_spool", capture_drain)
    await runtime._drain_desktop_spool()
    await runtime.close()

    assert imports == [{TelemetryScope.GROWTH: "growth-recorder"}]


async def test_start_replaces_a_finished_upload_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = ScopedTelemetryRuntime(
        config=_config(tmp_path, reliability=None, growth=None)
    )
    entered = asyncio.Event()

    async def completed():
        return None

    async def upload_loop():
        entered.set()
        await asyncio.Event().wait()

    old_task = asyncio.create_task(completed())
    await old_task
    runtime._upload_task = old_task
    monkeypatch.setattr(runtime, "_upload_loop", upload_loop)
    await runtime.start()
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert runtime._upload_task is not old_task
    finally:
        await runtime.close()


async def test_close_drains_accepted_local_record_without_starting_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = ScopedTelemetryRuntime(
        config=_config(tmp_path, reliability=True, growth=False)
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    recorded = asyncio.Event()
    starts = 0
    record = runtime.record

    async def slow_record(event, *, priority=None):
        entered.set()
        await release.wait()
        result = await record(event, priority=priority)
        recorded.set()
        return result

    async def forbidden_start():
        nonlocal starts
        starts += 1
        raise AssertionError("shutdown must not start uploads")

    monkeypatch.setattr(runtime, "record", slow_record)
    monkeypatch.setattr(runtime, "start", forbidden_start)
    runtime.record_background(_turn_event())
    await asyncio.wait_for(entered.wait(), timeout=1)
    closing = asyncio.create_task(runtime.close())
    await asyncio.sleep(0)
    runtime.record_background(_turn_event(2))
    release.set()
    await asyncio.wait_for(closing, timeout=1)

    assert recorded.is_set()
    assert starts == 0
    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        assert (await outbox.stats()).pending_events == 1
    finally:
        await outbox.close()


async def test_close_during_start_does_not_leave_an_upload_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = ScopedTelemetryRuntime(
        config=_config(tmp_path, reliability=True, growth=False)
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_drain():
        entered.set()
        await release.wait()

    monkeypatch.setattr(runtime, "_drain_desktop_spool", slow_drain)
    runtime.record_background(_turn_event())
    await asyncio.wait_for(entered.wait(), timeout=1)
    closing = asyncio.create_task(runtime.close())
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(closing, timeout=1)

    assert runtime._upload_task is None
    assert runtime._record_tasks == set()
