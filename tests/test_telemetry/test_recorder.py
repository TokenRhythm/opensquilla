from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

from opensquilla.telemetry.consent import (
    CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
    CURRENT_RELIABILITY_NOTICE_VERSION,
    ConsentDecision,
    ScopeConsentState,
    TelemetryScope,
    resolve_scope_consent,
)
from opensquilla.telemetry.consent_transition import (
    global_network_observability_transition,
)
from opensquilla.telemetry.contracts import TELEMETRY_EVENT_ADAPTER
from opensquilla.telemetry.coordination import (
    ConsentStateProvider,
    scope_consent_coordinator_for,
)
from opensquilla.telemetry.outbox import TelemetryOutbox
from opensquilla.telemetry.recorder import RecordStatus, TelemetryRecorder


def _event(*, notice: str = CURRENT_RELIABILITY_NOTICE_VERSION):
    return TELEMETRY_EVENT_ADAPTER.validate_json(
        json.dumps(
            {
                "event_name": "turn_result",
                "event_version": 1,
                "event_id": "00000000-0000-4000-8000-000000000001",
                "occurred_at_utc": "2026-09-02T01:02:03.456Z",
                "source": "gateway",
                "app_version": "1.2.3",
                "platform": "linux",
                "outcome": "success",
                "error_code": None,
                "duration_ms": 120,
                "consent_scope": "reliability",
                "notice_version": notice,
                "sample_rate": 1.0,
                "app_session_id": "00000000-0000-4000-8000-000000000002",
                "ttft_ms": 40,
                "stall_count": 0,
                "stall_threshold_ms": 15_000,
            }
        ),
        strict=True,
    )


def _state(
    *,
    decision: ConsentDecision = ConsentDecision.GRANTED,
    notice: str = CURRENT_RELIABILITY_NOTICE_VERSION,
    forced: bool = False,
) -> ScopeConsentState:
    return ScopeConsentState(
        scope=TelemetryScope.RELIABILITY,
        decision=decision,
        notice_version=notice,
        consented_at_utc="2026-09-02T01:00:00Z",
        record_complete=decision is ConsentDecision.GRANTED,
        notice_current=notice == CURRENT_RELIABILITY_NOTICE_VERSION,
        forced_off_reasons=("ci",) if forced else (),
    )


def _shared_config(provider: ConsentStateProvider) -> SimpleNamespace:
    config = SimpleNamespace()
    scope_consent_coordinator_for(config, state_provider=provider)
    return config


def _live_config(tmp_path: Path, *, global_disabled: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        state_dir=str(tmp_path / "state"),
        privacy=SimpleNamespace(
            disable_network_observability=global_disabled,
            reliability_diagnostics_enabled=True,
            reliability_notice_version=CURRENT_RELIABILITY_NOTICE_VERSION,
            reliability_consented_at_utc="2026-09-02T01:00:00Z",
            product_analytics_enabled=True,
            product_analytics_notice_version=CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
            product_analytics_consented_at_utc="2026-09-02T01:00:00Z",
        ),
    )


async def test_records_only_through_current_granted_notice(tmp_path: Path) -> None:
    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    recorder = TelemetryRecorder(
        outbox,
        config=_shared_config(lambda _scope: _state()),
    )
    try:
        assert (await recorder.record(_event())).status is RecordStatus.RECORDED
        assert (await recorder.record(_event())).status is RecordStatus.DUPLICATE
        assert (await outbox.stats()).pending_events == 1
    finally:
        await outbox.close()


async def test_declined_unset_forced_and_provider_failure_fail_closed(
    tmp_path: Path,
) -> None:
    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        providers = (
            lambda _scope: _state(decision=ConsentDecision.DECLINED),
            lambda _scope: _state(decision=ConsentDecision.UNSET),
            lambda _scope: _state(forced=True),
            lambda _scope: (_ for _ in ()).throw(RuntimeError("synthetic")),
            lambda _scope: True,
        )
        for provider in providers:
            recorder = TelemetryRecorder(outbox, config=_shared_config(provider))
            assert (await recorder.record(_event())).status is RecordStatus.CONSENT_BLOCKED
        assert (await outbox.stats()).pending_events == 0
    finally:
        await outbox.close()


async def test_stale_or_different_notice_is_not_persisted(tmp_path: Path) -> None:
    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        recorder = TelemetryRecorder(
            outbox,
            config=_shared_config(lambda _scope: _state()),
        )
        result = await recorder.record(_event(notice="reliability-older"))
        assert result.status is RecordStatus.NOTICE_MISMATCH
        assert (await outbox.stats()).pending_events == 0
    finally:
        await outbox.close()


async def test_scope_mismatch_is_rejected_before_consent_callback(tmp_path: Path) -> None:
    growth = await TelemetryOutbox.open(tmp_path, TelemetryScope.GROWTH)
    calls = 0

    def consent(_scope: TelemetryScope) -> ScopeConsentState:
        nonlocal calls
        calls += 1
        return _state()

    try:
        recorder = TelemetryRecorder(growth, config=_shared_config(consent))
        try:
            await recorder.record(_event())
        except ValueError as exc:
            assert "scope" in str(exc)
        else:
            raise AssertionError("scope mismatch was accepted")
        assert calls == 0
    finally:
        await growth.close()


async def test_revoke_transition_linearizes_clear_after_inflight_enqueue(
    tmp_path: Path,
) -> None:
    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    current = _state()
    config = _shared_config(lambda _scope: current)
    coordinator = scope_consent_coordinator_for(config)
    recorder = TelemetryRecorder(outbox, config=config)
    enqueue_entered = asyncio.Event()
    release_enqueue = asyncio.Event()
    original_enqueue = outbox.enqueue

    async def blocked_enqueue(event, *, priority=None):
        enqueue_entered.set()
        await release_enqueue.wait()
        return await original_enqueue(event, priority=priority)

    outbox.enqueue = blocked_enqueue  # type: ignore[method-assign]
    try:
        record_task = asyncio.create_task(recorder.record(_event()))
        await asyncio.wait_for(enqueue_entered.wait(), timeout=1)

        async def revoke() -> None:
            nonlocal current
            async with coordinator.transition(TelemetryScope.RELIABILITY):
                current = _state(decision=ConsentDecision.DECLINED)
                await outbox.clear_scope()

        revoke_task = asyncio.create_task(revoke())
        await asyncio.sleep(0)
        assert not revoke_task.done()
        release_enqueue.set()
        assert (await record_task).status is RecordStatus.RECORDED
        await revoke_task
        assert (await outbox.stats()).pending_events == 0
        assert coordinator.revision(TelemetryScope.RELIABILITY) == 1
    finally:
        await outbox.close()


async def test_record_waiting_behind_revoke_observes_decline_and_never_enqueues(
    tmp_path: Path,
) -> None:
    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    current = _state()
    config = _shared_config(lambda _scope: current)
    coordinator = scope_consent_coordinator_for(config)
    recorder = TelemetryRecorder(outbox, config=config)
    transition_entered = asyncio.Event()
    release_transition = asyncio.Event()

    async def revoke() -> None:
        nonlocal current
        async with coordinator.transition(TelemetryScope.RELIABILITY):
            transition_entered.set()
            current = _state(decision=ConsentDecision.DECLINED)
            await release_transition.wait()
            await outbox.clear_scope()

    try:
        revoke_task = asyncio.create_task(revoke())
        await asyncio.wait_for(transition_entered.wait(), timeout=1)
        record_task = asyncio.create_task(recorder.record(_event()))
        await asyncio.sleep(0)
        assert not record_task.done()
        release_transition.set()
        await revoke_task
        assert (await record_task).status is RecordStatus.CONSENT_BLOCKED
        assert (await outbox.stats()).pending_events == 0
    finally:
        await outbox.close()


async def test_global_veto_waits_for_inflight_enqueue_without_clearing_queue(
    tmp_path: Path,
) -> None:
    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    config = _live_config(tmp_path)
    scope_consent_coordinator_for(
        config,
        state_provider=lambda scope: resolve_scope_consent(scope, config=config, env={}),
    )
    recorder = TelemetryRecorder(outbox, config=config)
    enqueue_entered = asyncio.Event()
    release_enqueue = asyncio.Event()
    original_enqueue = outbox.enqueue

    async def blocked_enqueue(event, *, priority=None):
        enqueue_entered.set()
        await release_enqueue.wait()
        return await original_enqueue(event, priority=priority)

    outbox.enqueue = blocked_enqueue  # type: ignore[method-assign]
    candidate = _live_config(tmp_path, global_disabled=True)

    async def enable_global_veto() -> None:
        async with global_network_observability_transition(config, candidate):
            config.privacy.disable_network_observability = True

    try:
        record_task = asyncio.create_task(recorder.record(_event()))
        await asyncio.wait_for(enqueue_entered.wait(), timeout=1)
        veto_task = asyncio.create_task(enable_global_veto())
        await asyncio.sleep(0)
        assert not veto_task.done()

        release_enqueue.set()
        assert (await record_task).status is RecordStatus.RECORDED
        await veto_task

        assert (await outbox.stats()).pending_events == 1
        assert (await recorder.record(_event())).status is RecordStatus.CONSENT_BLOCKED
        assert (await outbox.stats()).pending_events == 1
    finally:
        await outbox.close()


def test_live_config_uses_one_process_wide_coordinator() -> None:
    class Config:
        config_path = "/tmp/synthetic-telemetry-coordinator.toml"

    config = Config()
    first = scope_consent_coordinator_for(config)
    second = scope_consent_coordinator_for(config)
    assert first is second


def test_recorder_constructor_has_no_private_consent_provider_escape_hatch() -> None:
    parameters = inspect.signature(TelemetryRecorder).parameters

    assert "config" in parameters
    assert "consent_state" not in parameters
    assert "coordinator" not in parameters
