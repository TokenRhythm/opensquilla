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

from opensquilla.scheduler.ops import SchedulerOps
from opensquilla.scheduler.parser import CronParseError
from opensquilla.scheduler.payloads import make_agent_turn_payload
from opensquilla.scheduler.persistence import JobStore
from opensquilla.scheduler.types import ScheduleKind, SessionTarget


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
