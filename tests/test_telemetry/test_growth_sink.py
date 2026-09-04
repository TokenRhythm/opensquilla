from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from opensquilla.telemetry.consent import (
    CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
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
    growth_cohort_state_path,
    write_active_growth_cohort,
)
from opensquilla.telemetry.growth_sink import (
    GrowthEventSink,
    GrowthMilestoneStatus,
    read_client_launch_state,
    read_gateway_growth_milestone_state,
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

    async def record(self, event, *, priority=None) -> RecordResult:
        self.events.append(event)
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

    await sink.record_turn_started(STARTED_AT)

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

    await sink.record_turn_succeeded(SUCCEEDED_AT)
    await sink.record_turn_started(STARTED_AT)
    await sink.record_turn_succeeded(SUCCEEDED_AT)

    assert [event.event_name for event in runtime.events] == [
        "first_turn_started",
        "first_turn_result",
    ]
    assert [event.source.value for event in runtime.events] == ["gateway", "runtime"]
    assert all(str(event.analytics_user_id) == str(ANALYTICS_ID) for event in runtime.events)
    assert runtime.events[0].occurred_at_utc == SUCCEEDED_AT
    state = read_gateway_growth_milestone_state(sink.marker_path)
    assert state.first_turn_started is not None
    assert state.first_turn_started.status is GrowthMilestoneStatus.ENQUEUED
    assert state.first_turn_result is not None
    assert state.first_turn_result.status is GrowthMilestoneStatus.ENQUEUED


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
