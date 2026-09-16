"""SchedulerOps strict-schedule contract.

The structured ``schedule_kind`` + ``schedule_value`` pair must validate per
kind, persist the canonical value into both ``cron_expr`` and ``schedule_raw``,
and reject anything that is not a valid expression for the declared kind.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from opensquilla.gateway.boot import (
    _task_runtime_envelope_host_execute,
    _task_runtime_envelope_owner,
)
from opensquilla.gateway.routing import (
    PRINCIPAL_HOST_EXECUTE_METADATA_KEY,
    build_cron_route_envelope,
    tool_context_from_envelope,
)
from opensquilla.scheduler.ops import SchedulerOps
from opensquilla.scheduler.parser import CronParseError
from opensquilla.scheduler.payloads import make_agent_turn_payload
from opensquilla.scheduler.persistence import JobStore
from opensquilla.scheduler.types import ScheduleKind, SessionTarget
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolSpec


async def _open_ops(tmp_path: Path) -> tuple[JobStore, SchedulerOps]:
    store = JobStore(str(tmp_path / "cron.db"))
    await store.open()
    return store, SchedulerOps(store)


async def test_ops_add_cron_persists_canonical_expression(tmp_path: Path) -> None:
    store, ops = await _open_ops(tmp_path)
    try:
        job = await ops.add(
            name="five",
            handler_key="agent_run",
            payload=make_agent_turn_payload("ping"),
            session_target=SessionTarget.ISOLATED,
            schedule_kind=ScheduleKind.CRON,
            schedule_value="*/5 * * * *",
        )
        assert job.schedule_kind == ScheduleKind.CRON
        assert job.cron_expr == "*/5 * * * *"
        assert job.schedule_raw == "*/5 * * * *"
        assert job.next_run_at is not None
    finally:
        await store.close()


async def test_ops_add_persists_creator_owner_boundary(tmp_path: Path) -> None:
    store, ops = await _open_ops(tmp_path)
    try:
        job = await ops.add(
            name="owner-job",
            handler_key="agent_run",
            payload=make_agent_turn_payload("ping"),
            session_target=SessionTarget.ISOLATED,
            schedule_kind=ScheduleKind.CRON,
            schedule_value="*/5 * * * *",
            creator_is_owner=True,
        )

        reloaded = await store.get(job.id)

        assert reloaded is not None
        assert reloaded.creator_is_owner is True
        assert reloaded.run_mode == "full"
        assert reloaded.elevated == "full"
        assert reloaded.execution_target == "host"
    finally:
        await store.close()


@pytest.mark.parametrize(
    (
        "creator_is_owner",
        "creator_host_execute",
        "expected_persisted_host_execute",
        "expected_run_mode",
        "expected_cron_restricted",
        "expected_owner_tool_visible",
    ),
    [
        pytest.param(False, False, False, "safe", True, False, id="non-host-admin"),
        pytest.param(False, True, True, "full", False, False, id="host-token"),
        pytest.param(True, False, True, "full", False, True, id="owner"),
    ],
)
async def test_cron_creator_authority_survives_persistence_without_widening_owner(
    tmp_path: Path,
    creator_is_owner: bool,
    creator_host_execute: bool,
    expected_persisted_host_execute: bool,
    expected_run_mode: str,
    expected_cron_restricted: bool,
    expected_owner_tool_visible: bool,
) -> None:
    store, ops = await _open_ops(tmp_path)
    try:
        job = await ops.add(
            name="authority",
            handler_key="agent_run",
            payload=make_agent_turn_payload("ping"),
            session_target=SessionTarget.ISOLATED,
            schedule_kind=ScheduleKind.CRON,
            schedule_value="*/5 * * * *",
            creator_is_owner=creator_is_owner,
            creator_host_execute=creator_host_execute,
            run_mode="full",
        )
        reloaded = await store.get(job.id)
    finally:
        await store.close()

    assert reloaded is not None
    assert reloaded.creator_is_owner is creator_is_owner
    assert reloaded.creator_host_execute is expected_persisted_host_execute
    assert reloaded.run_mode == expected_run_mode

    envelope = build_cron_route_envelope(reloaded, session_key=f"cron:{reloaded.id}")
    assert _task_runtime_envelope_owner(envelope) is creator_is_owner
    assert _task_runtime_envelope_host_execute(envelope) is (
        creator_is_owner or expected_persisted_host_execute
    )
    assert bool(envelope.metadata.get("cron_trusted_owner")) is creator_is_owner
    assert bool(envelope.metadata.get("cron_trusted_host")) is (expected_persisted_host_execute)
    assert bool(envelope.metadata.get(PRINCIPAL_HOST_EXECUTE_METADATA_KEY)) is (
        expected_persisted_host_execute
    )
    ctx = tool_context_from_envelope(
        envelope,
        is_owner=_task_runtime_envelope_owner(envelope),
        host_execute_allowed=_task_runtime_envelope_host_execute(envelope),
    )

    assert ctx.is_owner is creator_is_owner
    assert ctx.run_mode == expected_run_mode
    assert (ctx.allowed_tools is not None) is expected_cron_restricted
    assert ("exec_command" in ctx.denied_tools) is expected_cron_restricted

    registry = ToolRegistry()

    async def owner_tool() -> str:
        return "owner"

    registry.register(
        ToolSpec(
            name="owner_tool",
            description="owner only",
            parameters={},
            owner_only=True,
        ),
        owner_tool,
    )
    visible_names = {definition.name for definition in registry.to_tool_definitions(ctx)}
    assert ("owner_tool" in visible_names) is expected_owner_tool_visible


@pytest.mark.parametrize("legacy_mode", ["standard", "trusted", "managed"])
async def test_ops_add_decodes_legacy_safe_modes_to_canonical_value(
    tmp_path: Path,
    legacy_mode: str,
) -> None:
    store, ops = await _open_ops(tmp_path)
    try:
        job = await ops.add(
            name=f"legacy-{legacy_mode}",
            handler_key="agent_run",
            payload=make_agent_turn_payload("ping"),
            session_target=SessionTarget.ISOLATED,
            schedule_kind=ScheduleKind.CRON,
            schedule_value="*/5 * * * *",
            creator_is_owner=True,
            run_mode=legacy_mode,
        )

        reloaded = await store.get(job.id)

        assert reloaded is not None
        assert reloaded.run_mode == "safe"
        assert reloaded.elevated == ""
        assert reloaded.execution_target == "sandbox"
    finally:
        await store.close()


async def test_ops_add_is_atomic_and_idempotent_under_concurrency(tmp_path: Path) -> None:
    store, ops = await _open_ops(tmp_path)
    try:

        async def add_once():
            return await ops.add(
                name="deduplicated",
                handler_key="agent_run",
                payload=make_agent_turn_payload("ping"),
                session_target=SessionTarget.ISOLATED,
                schedule_kind=ScheduleKind.CRON,
                schedule_value="*/5 * * * *",
                creator_is_owner=True,
                idempotency_key="cron-tool:task-1:same",
            )

        jobs = await asyncio.gather(*(add_once() for _ in range(30)))
        active = await store.list_active()
    finally:
        await store.close()

    assert len({job.id for job in jobs}) == 1
    assert len(active) == 1
    assert sum(job.deduplicated for job in jobs) == 29


async def test_ops_add_keeps_different_idempotency_keys_distinct(tmp_path: Path) -> None:
    store, ops = await _open_ops(tmp_path)
    try:
        first = await ops.add(
            name="first",
            handler_key="agent_run",
            payload=make_agent_turn_payload("ping"),
            session_target=SessionTarget.ISOLATED,
            schedule_kind=ScheduleKind.CRON,
            schedule_value="*/5 * * * *",
            idempotency_key="cron-tool:task-1:same",
        )
        second = await ops.add(
            name="second",
            handler_key="agent_run",
            payload=make_agent_turn_payload("ping"),
            session_target=SessionTarget.ISOLATED,
            schedule_kind=ScheduleKind.CRON,
            schedule_value="*/5 * * * *",
            idempotency_key="cron-tool:task-2:same",
        )
    finally:
        await store.close()

    assert first.id != second.id


async def test_ops_add_every_seconds_records_anchor(tmp_path: Path) -> None:
    store, ops = await _open_ops(tmp_path)
    try:
        before = datetime.now(UTC)
        job = await ops.add(
            name="tick",
            handler_key="agent_run",
            payload=make_agent_turn_payload("ping"),
            session_target=SessionTarget.ISOLATED,
            schedule_kind=ScheduleKind.EVERY,
            schedule_value="300",
        )
        assert job.schedule_kind == ScheduleKind.EVERY
        assert job.cron_expr == "300"
        assert job.schedule_raw == "300"
        assert job.anchor_at is not None
        assert job.next_run_at is not None
        # Within a tight window of 300s after the call.
        delta = job.next_run_at - before
        assert timedelta(seconds=290) <= delta <= timedelta(seconds=310)
    finally:
        await store.close()


async def test_ops_add_at_one_shot_uses_iso_value(tmp_path: Path) -> None:
    store, ops = await _open_ops(tmp_path)
    try:
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        job = await ops.add(
            name="once",
            handler_key="agent_run",
            payload=make_agent_turn_payload("ping"),
            session_target=SessionTarget.ISOLATED,
            schedule_kind=ScheduleKind.AT,
            schedule_value=future,
        )
        assert job.schedule_kind == ScheduleKind.AT
        assert job.cron_expr == future
        assert job.schedule_raw == future
        assert job.delete_after_run is True
        assert job.next_run_at is not None
    finally:
        await store.close()


async def test_ops_add_cron_rejects_natural_language_value(tmp_path: Path) -> None:
    """Regression guard: structured contract must not accept Chinese phrasing."""
    store, ops = await _open_ops(tmp_path)
    try:
        with pytest.raises(CronParseError):
            await ops.add(
                name="bad",
                handler_key="agent_run",
                payload=make_agent_turn_payload("ping"),
                session_target=SessionTarget.ISOLATED,
                schedule_kind=ScheduleKind.CRON,
                schedule_value="每5分钟",
            )
    finally:
        await store.close()


async def test_ops_add_at_rejects_naive_iso(tmp_path: Path) -> None:
    store, ops = await _open_ops(tmp_path)
    try:
        with pytest.raises(CronParseError, match="timezone"):
            await ops.add(
                name="bad",
                handler_key="agent_run",
                payload=make_agent_turn_payload("ping"),
                session_target=SessionTarget.ISOLATED,
                schedule_kind=ScheduleKind.AT,
                schedule_value="2026-05-15T09:00:00",
            )
    finally:
        await store.close()


async def test_ops_add_at_rejects_past_timestamp(tmp_path: Path) -> None:
    """A one-time ``at`` in the past would fire immediately then delete itself;
    reject it at creation time instead (issue #1516)."""
    store, ops = await _open_ops(tmp_path)
    try:
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        with pytest.raises(ValueError, match="in the past"):
            await ops.add(
                name="stale",
                handler_key="agent_run",
                payload=make_agent_turn_payload("ping"),
                session_target=SessionTarget.ISOLATED,
                schedule_kind=ScheduleKind.AT,
                schedule_value=past,
            )
        # Nothing should have been persisted.
        assert await store.list_active() == []
    finally:
        await store.close()


async def test_ops_update_at_rejects_past_timestamp(tmp_path: Path) -> None:
    """Repointing a job to a past one-time ``at`` is rejected as well."""
    store, ops = await _open_ops(tmp_path)
    try:
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        job = await ops.add(
            name="once",
            handler_key="agent_run",
            payload=make_agent_turn_payload("ping"),
            session_target=SessionTarget.ISOLATED,
            schedule_kind=ScheduleKind.AT,
            schedule_value=future,
        )
        before = await store.get(job.id)
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        with pytest.raises(ValueError, match="in the past"):
            await ops.update(
                job.id,
                schedule_kind=ScheduleKind.AT,
                schedule_value=past,
            )
        assert await store.get(job.id) == before
    finally:
        await store.close()


async def test_ops_at_idempotent_retries_after_due_time_return_existing_job(tmp_path: Path) -> None:
    store, _ = await _open_ops(tmp_path)
    now = datetime(2035, 1, 1, 12, tzinfo=UTC)
    ops = SchedulerOps(store, clock=lambda: now)
    at = (now + timedelta(seconds=1)).isoformat()
    try:
        async def add_once(key: str = "same-request"):
            return await ops.add(
                name="once",
                payload=make_agent_turn_payload("synthetic reminder"),
                schedule_kind=ScheduleKind.AT,
                schedule_value=at,
                idempotency_key=key,
            )

        original = await add_once()
        now += timedelta(seconds=2)
        retries = await asyncio.gather(*(add_once() for _ in range(8)))

        assert all(job.id == original.id and job.deduplicated for job in retries)
        with pytest.raises(ValueError, match="in the past"):
            await add_once("different-request")
        assert len(await store.list_active()) == 1
    finally:
        await store.close()


async def test_ops_at_checks_time_after_waiting_for_creation_lock(tmp_path: Path) -> None:
    store, _ = await _open_ops(tmp_path)
    now = datetime(2035, 1, 1, 12, tzinfo=UTC)
    started = asyncio.Event()

    def clock() -> datetime:
        started.set()
        return now

    ops = SchedulerOps(store, clock=clock)
    try:
        async with store._idempotent_create_lock:
            pending = asyncio.create_task(ops.add(
                name="once",
                payload=make_agent_turn_payload("synthetic reminder"),
                schedule_kind=ScheduleKind.AT,
                schedule_value=(now + timedelta(seconds=1)).isoformat(),
                idempotency_key="new-request",
            ))
            await started.wait()
            now += timedelta(seconds=2)
        with pytest.raises(ValueError, match="in the past"):
            await pending
        assert await store.list_active() == []
    finally:
        await store.close()


@pytest.mark.parametrize(
    "at", ["2035-01-01T12:00:00Z", "2035-01-01T20:00:00+08:00"],
)
async def test_ops_at_accepts_the_current_instant_with_either_offset(
    tmp_path: Path, at: str,
) -> None:
    store, _ = await _open_ops(tmp_path)
    now = datetime(2035, 1, 1, 12, tzinfo=UTC)
    ops = SchedulerOps(store, clock=lambda: now)
    try:
        job = await ops.add(
            name="once",
            payload=make_agent_turn_payload("synthetic reminder"),
            schedule_kind=ScheduleKind.AT,
            schedule_value=at,
        )
        assert job.next_run_at == now
    finally:
        await store.close()


async def test_ops_overdue_at_metadata_update_preserves_schedule(tmp_path: Path) -> None:
    store, _ = await _open_ops(tmp_path)
    now = datetime(2035, 1, 1, 12, tzinfo=UTC)
    ops = SchedulerOps(store, clock=lambda: now)
    try:
        job = await ops.add(
            name="once",
            payload=make_agent_turn_payload("synthetic reminder"),
            schedule_kind=ScheduleKind.AT,
            schedule_value="2035-01-01T21:00:00+08:00",
        )
        assert job.next_run_at == now + timedelta(hours=1)
        now += timedelta(hours=2)

        changed = await ops.update(job.id, name="renamed", enabled=False)

        assert changed is not None
        assert changed.name == "renamed"
        assert changed.next_run_at == job.next_run_at
        assert changed.enabled is False
    finally:
        await store.close()


async def test_ops_add_every_rejects_zero_seconds(tmp_path: Path) -> None:
    store, ops = await _open_ops(tmp_path)
    try:
        with pytest.raises(ValueError, match=">= 1 second"):
            await ops.add(
                name="bad",
                handler_key="agent_run",
                payload=make_agent_turn_payload("ping"),
                session_target=SessionTarget.ISOLATED,
                schedule_kind=ScheduleKind.EVERY,
                schedule_value="0",
            )
    finally:
        await store.close()
