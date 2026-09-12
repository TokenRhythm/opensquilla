from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from opensquilla.telemetry.consent import (
    CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
    TelemetryScope,
    resolve_scope_consent,
)
from opensquilla.telemetry.contracts.common import (
    ClientEntrypoint,
    ClientSurface,
    ExecutionMode,
    Platform,
)
from opensquilla.telemetry.coordination import scope_consent_coordinator_for
from opensquilla.telemetry.growth.state import (
    delete_growth_cohort_state,
    growth_cohort_state_path,
    write_active_growth_cohort,
)
from opensquilla.telemetry.growth_sink import (
    GrowthEventSink,
    GrowthMilestoneStatus,
    read_client_launch_state,
    read_coding_mode_usage_state,
    read_gateway_growth_milestone_state,
    read_metaskill_usage_state,
)
from opensquilla.telemetry.identity import (
    TelemetryIdentityKind,
    identity_state_path,
    load_or_create_identity,
)
from opensquilla.telemetry.recorder import RecordResult, RecordStatus

ANALYTICS_ID = uuid.UUID("123e4567-e89b-42d3-a456-426614174000")
STARTED_AT = datetime(2026, 9, 2, 1, 2, 3, tzinfo=UTC)
SUCCEEDED_AT = datetime(2026, 9, 2, 1, 2, 9, tzinfo=UTC)


class CapturingRuntime:
    def __init__(self, statuses: list[RecordStatus] | None = None) -> None:
        self.events = []
        self.statuses = list(statuses or [])
        self.consent_revisions: list[int | None] = []

    async def record(
        self,
        event,
        *,
        priority=None,
        expected_consent_revision: int | None = None,
    ) -> RecordResult:
        self.events.append(event)
        self.consent_revisions.append(expected_consent_revision)
        status = self.statuses.pop(0) if self.statuses else RecordStatus.RECORDED
        return RecordResult(status)


def _config(tmp_path, *, enabled: bool | None = True):
    return SimpleNamespace(
        state_dir=str(tmp_path),
        privacy=SimpleNamespace(
            disable_network_observability=False,
            product_analytics_enabled=enabled,
            product_analytics_notice_version=(
                CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION if enabled is True else None
            ),
            product_analytics_consented_at_utc=(
                "2026-09-02T01:00:00Z" if enabled is True else None
            ),
        ),
    )


def _activate(config) -> None:
    load_or_create_identity(
        identity_state_path(TelemetryIdentityKind.ANALYTICS_USER, config=config),
        TelemetryIdentityKind.ANALYTICS_USER,
        now=STARTED_AT,
        uuid_factory=lambda: ANALYTICS_ID,
    )
    write_active_growth_cohort(
        growth_cohort_state_path(config=config),
        activated_at_utc="2026-09-02T01:00:00.000Z",
    )


def _sink(runtime: CapturingRuntime, config) -> GrowthEventSink:
    scope_consent_coordinator_for(
        config,
        state_provider=lambda scope: resolve_scope_consent(scope, config=config, env={}),
    )
    return GrowthEventSink(
        runtime,  # type: ignore[arg-type]
        config=config,
        app_version="1.2.3",
        platform=Platform.LINUX,
        clock=lambda: STARTED_AT,
    )


async def test_no_consent_creates_no_growth_files_or_event(tmp_path) -> None:
    config = _config(tmp_path, enabled=None)
    runtime = CapturingRuntime()
    sink = _sink(runtime, config)

    await sink.start()
    await sink.record_turn_started(STARTED_AT)
    await asyncio.sleep(0)
    await sink.close()

    assert runtime.events == []
    assert not (tmp_path / "telemetry").exists()


async def test_active_consent_without_fresh_cohort_proof_does_not_backfill(tmp_path) -> None:
    config = _config(tmp_path)
    runtime = CapturingRuntime()
    sink = _sink(runtime, config)

    await sink.record_turn_succeeded(SUCCEEDED_AT)

    assert runtime.events == []
    assert not sink.marker_path.exists()


async def test_started_and_success_are_enqueued_once_in_funnel_order(tmp_path) -> None:
    config = _config(tmp_path)
    _activate(config)
    runtime = CapturingRuntime()
    sink = _sink(runtime, config)

    await sink.record_turn_started(STARTED_AT)
    await sink.record_turn_succeeded(SUCCEEDED_AT)
    await sink.record_turn_started(SUCCEEDED_AT)
    await sink.record_turn_succeeded(SUCCEEDED_AT)

    assert [event.event_name for event in runtime.events] == [
        "first_turn_started",
        "first_turn_result",
    ]
    assert [event.source.value for event in runtime.events] == ["gateway", "runtime"]
    assert all(str(event.analytics_user_id) == str(ANALYTICS_ID) for event in runtime.events)
    assert runtime.events[0].occurred_at_utc == STARTED_AT
    state = read_gateway_growth_milestone_state(sink.marker_path)
    assert state.first_turn_started is not None
    assert state.first_turn_started.status is GrowthMilestoneStatus.ENQUEUED
    assert state.first_turn_result is not None
    assert state.first_turn_result.status is GrowthMilestoneStatus.ENQUEUED


async def test_pending_first_turn_retries_without_another_turn(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("opensquilla.telemetry.growth_sink._RETRY_INITIAL_SECONDS", 0.01)
    config = _config(tmp_path)
    _activate(config)
    runtime = CapturingRuntime([RecordStatus.EVICTED, RecordStatus.EVICTED])
    sink = _sink(runtime, config)

    await sink.start()
    await sink.record_turn_started(STARTED_AT)
    pending = read_gateway_growth_milestone_state(sink.marker_path)
    assert pending.first_turn_started is not None
    assert pending.first_turn_started.status is GrowthMilestoneStatus.PENDING
    pending_id = pending.first_turn_started.event.event_id

    async with asyncio.timeout(1):
        while len(runtime.events) < 3:
            await asyncio.sleep(0.001)
    await sink.close()

    assert [event.event_name for event in runtime.events] == [
        "first_turn_started",
        "first_turn_started",
        "first_turn_started",
    ]
    assert all(event.event_id == pending_id for event in runtime.events)
    assert all(event.occurred_at_utc == STARTED_AT for event in runtime.events)
    replayed = read_gateway_growth_milestone_state(sink.marker_path)
    assert replayed.first_turn_started is not None
    assert replayed.first_turn_started.status is GrowthMilestoneStatus.ENQUEUED


async def test_pending_start_blocks_success_until_startup_recovery(tmp_path) -> None:
    config = _config(tmp_path)
    _activate(config)
    runtime = CapturingRuntime([RecordStatus.EVICTED])
    sink = _sink(runtime, config)

    await sink.record_turn_started(STARTED_AT)
    await sink.record_turn_succeeded(SUCCEEDED_AT)
    await sink.close()

    pending = read_gateway_growth_milestone_state(sink.marker_path)
    assert pending.first_turn_started is not None
    assert pending.first_turn_result is not None
    assert pending.first_turn_started.status is GrowthMilestoneStatus.PENDING
    assert pending.first_turn_result.status is GrowthMilestoneStatus.PENDING
    assert [event.event_name for event in runtime.events] == ["first_turn_started"]

    recovered_runtime = CapturingRuntime()
    recovered = _sink(recovered_runtime, _config(tmp_path))
    await recovered.start()
    await recovered.start()
    async with asyncio.timeout(1):
        while len(recovered_runtime.events) < 2:
            await asyncio.sleep(0.001)
    await recovered.close()

    assert recovered_runtime.events == [
        pending.first_turn_started.event,
        pending.first_turn_result.event,
    ]
    complete = read_gateway_growth_milestone_state(recovered.marker_path)
    assert complete.first_turn_started is not None
    assert complete.first_turn_result is not None
    assert complete.first_turn_started.status is GrowthMilestoneStatus.ENQUEUED
    assert complete.first_turn_result.status is GrowthMilestoneStatus.ENQUEUED


async def test_success_does_not_invent_a_missing_start(tmp_path) -> None:
    config = _config(tmp_path)
    _activate(config)
    runtime = CapturingRuntime()
    sink = _sink(runtime, config)

    await sink.record_turn_succeeded(SUCCEEDED_AT)

    assert runtime.events == []
    assert not sink.marker_path.exists()


async def test_retry_stops_after_revocation_without_backfill(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("opensquilla.telemetry.growth_sink._RETRY_INITIAL_SECONDS", 0.01)
    config = _config(tmp_path)
    _activate(config)
    runtime = CapturingRuntime([RecordStatus.EVICTED, RecordStatus.EVICTED])
    sink = _sink(runtime, config)
    await sink.start()
    await sink.record_turn_started(STARTED_AT)
    await sink.record_turn_succeeded(SUCCEEDED_AT)
    async with asyncio.timeout(1):
        while len(runtime.events) < 2:
            await asyncio.sleep(0.001)

    coordinator = scope_consent_coordinator_for(config)
    async with coordinator.transition(TelemetryScope.GROWTH):
        config.privacy.product_analytics_enabled = False
        delete_growth_cohort_state(config=config)
    await asyncio.sleep(0.03)
    assert len(runtime.events) == 2

    config.privacy.product_analytics_enabled = True
    sink.observe_turn_started()
    sink.observe_turn_succeeded()
    await sink.close()

    assert len(runtime.events) == 2
    assert not sink.marker_path.exists()


async def test_startup_replay_requires_current_consent(tmp_path) -> None:
    config = _config(tmp_path)
    _activate(config)
    runtime = CapturingRuntime([RecordStatus.EVICTED])
    sink = _sink(runtime, config)
    await sink.record_turn_started(STARTED_AT)
    await sink.close()

    disabled_runtime = CapturingRuntime()
    disabled = _sink(disabled_runtime, _config(tmp_path, enabled=False))
    await disabled.start()
    await asyncio.sleep(0)
    await disabled.close()

    assert disabled_runtime.events == []


async def test_replay_does_not_recreate_a_marker_deleted_after_read(tmp_path, monkeypatch) -> None:
    config = _config(tmp_path)
    _activate(config)
    runtime = CapturingRuntime([RecordStatus.EVICTED])
    sink = _sink(runtime, config)
    await sink.record_turn_started(STARTED_AT)

    def read_then_remove(path):
        state = read_gateway_growth_milestone_state(path)
        path.unlink(missing_ok=True)
        return state

    monkeypatch.setattr(
        "opensquilla.telemetry.growth_sink.read_gateway_growth_milestone_state",
        read_then_remove,
    )
    await sink.replay_pending()

    assert len(runtime.events) == 1
    assert not sink.marker_path.exists()


async def test_close_cancels_retry_backoff(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("opensquilla.telemetry.growth_sink._RETRY_INITIAL_SECONDS", 60)
    config = _config(tmp_path)
    _activate(config)
    runtime = CapturingRuntime([RecordStatus.EVICTED, RecordStatus.EVICTED])
    sink = _sink(runtime, config)
    await sink.start()
    await sink.record_turn_started(STARTED_AT)
    async with asyncio.timeout(1):
        while len(runtime.events) < 2:
            await asyncio.sleep(0.001)
        await sink.close()

    assert len(runtime.events) == 2
    pending = read_gateway_growth_milestone_state(sink.marker_path)
    assert pending.first_turn_started is not None
    assert pending.first_turn_started.status is GrowthMilestoneStatus.PENDING


async def test_evicted_event_keeps_stable_pending_payload_for_retry(tmp_path) -> None:
    config = _config(tmp_path)
    _activate(config)
    runtime = CapturingRuntime([RecordStatus.EVICTED, RecordStatus.RECORDED])
    sink = _sink(runtime, config)

    await sink.record_turn_started(STARTED_AT)
    pending = read_gateway_growth_milestone_state(sink.marker_path)
    await sink.record_turn_started(SUCCEEDED_AT)

    assert len(runtime.events) == 2
    assert runtime.events[0] == runtime.events[1]
    assert pending.first_turn_started is not None
    assert pending.first_turn_started.status is GrowthMilestoneStatus.PENDING
    complete = read_gateway_growth_milestone_state(sink.marker_path)
    assert complete.first_turn_started is not None
    assert complete.first_turn_started.status is GrowthMilestoneStatus.ENQUEUED


async def test_corrupt_marker_fails_closed_without_overwrite(tmp_path) -> None:
    config = _config(tmp_path)
    _activate(config)
    runtime = CapturingRuntime()
    sink = _sink(runtime, config)
    sink.marker_path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")

    await sink.record_turn_started(STARTED_AT)

    assert runtime.events == []
    assert json.loads(sink.marker_path.read_text(encoding="utf-8")) == {"schema_version": 1}


async def test_callback_tasks_deduplicate_concurrent_turns(tmp_path) -> None:
    config = _config(tmp_path)
    _activate(config)
    runtime = CapturingRuntime()
    sink = _sink(runtime, config)

    sink.observe_turn_started()
    sink.observe_turn_started()
    sink.observe_turn_succeeded()
    await sink.close()

    assert [event.event_name for event in runtime.events] == [
        "first_turn_started",
        "first_turn_result",
    ]


async def test_client_launch_is_once_per_identity_surface_utc_day(tmp_path) -> None:
    config = _config(tmp_path)
    _activate(config)
    runtime = CapturingRuntime()
    sink = _sink(runtime, config)

    first = await sink.record_client_launch(
        surface=ClientSurface.TUI,
        entrypoint=ClientEntrypoint.CHAT,
        execution_mode=ExecutionMode.GATEWAY,
    )
    duplicate = await sink.record_client_launch(
        surface=ClientSurface.TUI,
        entrypoint=ClientEntrypoint.CHAT,
        execution_mode=ExecutionMode.STANDALONE,
    )
    other_surface = await sink.record_client_launch(
        surface=ClientSurface.CLI,
        entrypoint=ClientEntrypoint.AGENT,
        execution_mode=ExecutionMode.ONE_SHOT,
    )

    assert first is True
    assert duplicate is False
    assert other_surface is True
    assert [(event.surface.value, event.entrypoint.value) for event in runtime.events] == [
        ("tui", "chat"),
        ("cli", "agent"),
    ]
    records = read_client_launch_state(tmp_path / "telemetry" / "growth_client_launches.json")
    assert len(records) == 2
    assert all(record.status is GrowthMilestoneStatus.ENQUEUED for record in records.values())


async def test_client_launch_retry_reuses_event_id(tmp_path) -> None:
    config = _config(tmp_path)
    _activate(config)
    runtime = CapturingRuntime([RecordStatus.EVICTED, RecordStatus.RECORDED])
    sink = _sink(runtime, config)

    first = await sink.record_client_launch(
        surface=ClientSurface.CLI,
        entrypoint=ClientEntrypoint.GATEWAY_RUN,
        execution_mode=ExecutionMode.GATEWAY,
    )
    second = await sink.record_client_launch(
        surface=ClientSurface.CLI,
        entrypoint=ClientEntrypoint.GATEWAY_RUN,
        execution_mode=ExecutionMode.GATEWAY,
    )

    assert first is False
    assert second is True
    assert runtime.events[0].event_id == runtime.events[1].event_id


async def test_metaskill_usage_counts_new_runs_once_without_payload_details(tmp_path) -> None:
    config = _config(tmp_path)
    _activate(config)
    runtime = CapturingRuntime()
    sink = _sink(runtime, config)

    first = await sink.record_metaskill_usage("run-001", STARTED_AT)
    duplicate = await sink.record_metaskill_usage("run-001", SUCCEEDED_AT)
    second = await sink.record_metaskill_usage("run-002", SUCCEEDED_AT)

    assert first is True
    assert duplicate is False
    assert second is True
    assert [event.event_name for event in runtime.events] == [
        "metaskill_usage",
        "metaskill_usage",
    ]
    assert all(
        set(event.model_dump(mode="json"))
        == {
            "occurred_at_utc",
            "event_name",
            "event_version",
            "event_id",
            "source",
            "app_version",
            "platform",
            "outcome",
            "error_code",
            "duration_ms",
            "consent_scope",
            "notice_version",
            "sample_rate",
            "analytics_user_id",
        }
        for event in runtime.events
    )
    records = read_metaskill_usage_state(sink.metaskill_usage_path)
    assert set(records) == {"run-001", "run-002"}
    assert all(record.status is GrowthMilestoneStatus.ENQUEUED for record in records.values())


async def test_metaskill_usage_retry_reuses_event_id_after_eviction(tmp_path) -> None:
    config = _config(tmp_path)
    _activate(config)
    runtime = CapturingRuntime([RecordStatus.EVICTED, RecordStatus.RECORDED])
    sink = _sink(runtime, config)

    first = await sink.record_metaskill_usage("run-retry", STARTED_AT)
    second = await sink.record_metaskill_usage("run-retry", SUCCEEDED_AT)

    assert first is False
    assert second is True
    assert runtime.events[0] == runtime.events[1]


async def test_accepted_metaskill_observation_is_drained_during_close(tmp_path) -> None:
    config = _config(tmp_path)
    _activate(config)
    runtime = CapturingRuntime()
    sink = _sink(runtime, config)

    sink.observe_metaskill_usage("run-before-close")
    await sink.close()

    assert [event.event_name for event in runtime.events] == ["metaskill_usage"]
    records = read_metaskill_usage_state(sink.metaskill_usage_path)
    assert records["run-before-close"].status is GrowthMilestoneStatus.ENQUEUED


async def test_coding_mode_usage_counts_started_runs_once_without_payload_details(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    _activate(config)
    runtime = CapturingRuntime()
    sink = _sink(runtime, config)

    first = await sink.record_coding_mode_usage("codetask-001", STARTED_AT)
    duplicate = await sink.record_coding_mode_usage("codetask-001", SUCCEEDED_AT)
    second = await sink.record_coding_mode_usage("codetask-002", SUCCEEDED_AT)

    assert first is True
    assert duplicate is False
    assert second is True
    assert [event.event_name for event in runtime.events] == [
        "coding_mode_usage",
        "coding_mode_usage",
    ]
    assert all(
        set(event.model_dump(mode="json"))
        == {
            "occurred_at_utc",
            "event_name",
            "event_version",
            "event_id",
            "source",
            "app_version",
            "platform",
            "outcome",
            "error_code",
            "duration_ms",
            "consent_scope",
            "notice_version",
            "sample_rate",
            "analytics_user_id",
        }
        for event in runtime.events
    )
    records = read_coding_mode_usage_state(sink.coding_mode_usage_path)
    assert set(records) == {"codetask-001", "codetask-002"}
    assert all(record.status is GrowthMilestoneStatus.ENQUEUED for record in records.values())


async def test_later_turn_retries_pending_feature_usage_with_same_event_id(tmp_path) -> None:
    config = _config(tmp_path)
    _activate(config)
    runtime = CapturingRuntime([RecordStatus.EVICTED, RecordStatus.RECORDED])
    sink = _sink(runtime, config)

    assert await sink.record_metaskill_usage("run-pending", STARTED_AT) is False
    pending_event = runtime.events[0]

    await sink.record_turn_started(SUCCEEDED_AT)

    assert runtime.events[1] == pending_event
    records = read_metaskill_usage_state(sink.metaskill_usage_path)
    assert records["run-pending"].status is GrowthMilestoneStatus.ENQUEUED


async def test_client_launch_retries_feature_usage_left_pending_before_restart(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    _activate(config)
    first_runtime = CapturingRuntime([RecordStatus.EVICTED])
    first_sink = _sink(first_runtime, config)

    assert await first_sink.record_metaskill_usage("run-before-restart", STARTED_AT) is False
    pending_event = first_runtime.events[0]
    await first_sink.close()

    config = _config(tmp_path)
    resumed_runtime = CapturingRuntime()
    resumed_sink = _sink(resumed_runtime, config)
    recorded = await resumed_sink.record_client_launch(
        surface=ClientSurface.CLI,
        entrypoint=ClientEntrypoint.CHAT,
        execution_mode=ExecutionMode.STANDALONE,
    )

    assert recorded is True
    assert resumed_runtime.events[0] == pending_event
    assert resumed_runtime.events[1].event_name == "client_launch"
    records = read_metaskill_usage_state(resumed_sink.metaskill_usage_path)
    assert records["run-before-restart"].status is GrowthMilestoneStatus.ENQUEUED


async def test_corrupt_coding_ledger_does_not_block_other_growth_events(tmp_path) -> None:
    config = _config(tmp_path)
    _activate(config)
    runtime = CapturingRuntime()
    sink = _sink(runtime, config)
    sink.coding_mode_usage_path.parent.mkdir(parents=True, exist_ok=True)
    sink.coding_mode_usage_path.write_text("{not-json", encoding="utf-8")

    assert await sink.record_metaskill_usage("run-valid", STARTED_AT) is True
    await sink.record_turn_started(SUCCEEDED_AT)

    assert [event.event_name for event in runtime.events] == [
        "metaskill_usage",
        "first_turn_started",
    ]
    assert sink.coding_mode_usage_path.read_text(encoding="utf-8") == "{not-json"


async def test_pending_feature_usage_is_never_removed_by_dedupe_history_limit(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    _activate(config)
    # Every new observation retries every earlier pending record. Keep every
    # attempt evicted so all 25 records remain pending at once.
    runtime = CapturingRuntime([RecordStatus.EVICTED] * 400)
    sink = _sink(runtime, config)

    for index in range(25):
        recorded = await sink.record_metaskill_usage(
            f"run-pending-{index:02d}",
            STARTED_AT,
        )
        assert recorded is False

    records = read_metaskill_usage_state(sink.metaskill_usage_path)
    assert len(records) == 25
    assert "run-pending-00" in records
    assert all(record.status is GrowthMilestoneStatus.PENDING for record in records.values())
    assert all(revision == 0 for revision in runtime.consent_revisions)


async def test_pre_disclosure_feature_ledger_is_not_retried_or_allowed_to_block(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    _activate(config)
    old_runtime = CapturingRuntime()
    old_sink = _sink(old_runtime, config)
    assert await old_sink.record_metaskill_usage("old-run", STARTED_AT) is True
    await old_sink.close()

    payload = json.loads(old_sink.metaskill_usage_path.read_text(encoding="utf-8"))
    payload["records"][0]["event"]["notice_version"] = "growth-v1"
    old_sink.metaskill_usage_path.write_text(json.dumps(payload), encoding="utf-8")

    # Simulate a later process loading the profile after the updated disclosure.
    config = _config(tmp_path)
    runtime = CapturingRuntime()
    sink = _sink(runtime, config)
    assert await sink.record_metaskill_usage("new-run", SUCCEEDED_AT) is True

    assert [event.event_name for event in runtime.events] == ["metaskill_usage"]
    assert runtime.events[0].notice_version == "growth-v2"
    records = read_metaskill_usage_state(sink.metaskill_usage_path)
    assert set(records) == {"new-run"}
