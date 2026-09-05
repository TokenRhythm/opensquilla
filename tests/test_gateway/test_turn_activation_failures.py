"""Activation failures preserve accepted identities across both interactive entry points."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.session.models import AgentTaskStatus
from tests.test_gateway.test_channel_turn_ingress import _accept, _open_stack
from tests.test_gateway.test_turn_ingress_rpc import (
    CLIENT_REQUEST_ID,
    SESSION_KEY,
    _open_real_stack,
)


@asynccontextmanager
async def ingress(tmp_path, surface):
    context = _open_real_stack if surface == "web" else _open_stack
    async with context(tmp_path / "activation.sqlite") as stack:
        async def send():
            if surface == "channel":
                handle, _, _, replayed = await _accept(stack, "stable accepted input")
                assert handle is not None
                return handle.task_id, handle.status, replayed
            result = await get_dispatcher().dispatch(
                "activation-test", "sessions.send",
                {"key": SESSION_KEY, "message": "stable accepted input",
                 "clientRequestId": CLIENT_REQUEST_ID}, stack.context,
            )
            assert result.ok and result.payload["accepted"] is True
            return (result.payload["task_id"], result.payload.get("task_status"),
                    result.payload["replayed"])
        yield stack, send


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["web", "channel"])
@pytest.mark.parametrize("failure", ["before_write", "after_commit", "unknown"])
async def test_unavailable_compensation_storage_keeps_accepted_identity(
    tmp_path, monkeypatch, surface, failure
):
    async with ingress(tmp_path, surface) as (stack, send):
        original_fail = stack.storage.fail_queued_agent_task_activation
        original_read = stack.storage.get_agent_task
        identities = []

        async def fail_activation(*_args, **_kwargs):
            raise RuntimeError("synthetic activation failure")

        async def fail_storage(task_id, **kwargs):
            identities.append(task_id)
            if failure == "after_commit":
                await original_fail(task_id, **kwargs)
            raise OSError("synthetic compensation storage failure")

        async def unavailable_read(_task_id):
            raise OSError("synthetic task read failure")

        with monkeypatch.context() as patch:
            patch.setattr(stack.runtime, "activate", fail_activation)
            patch.setattr(stack.storage, "fail_queued_agent_task_activation", fail_storage)
            if failure == "unknown":
                patch.setattr(stack.storage, "get_agent_task", unavailable_read)
            task_id, status, replayed = await send()
        assert identities == [task_id] and not replayed
        expected = AgentTaskStatus.FAILED if failure == "after_commit" else AgentTaskStatus.QUEUED
        task = await original_read(task_id)
        assert task is not None and task.status == expected
        if failure == "unknown" and surface == "web":
            assert status is None
        else:
            assert status == expected
        replay_id, replay_status, replayed = await send()
        assert replayed and replay_id == task_id and replay_status == expected
        assert stack.runtime._reservations_by_session == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["web", "channel"])
@pytest.mark.parametrize("activation_boundary", ["observer", "abort_race"])
async def test_driver_ownership_prevents_compensation_even_when_activation_raises(
    tmp_path, monkeypatch, surface, activation_boundary
):
    async with ingress(tmp_path, surface) as (stack, send):
        activate = stack.runtime.activate
        abort = stack.runtime.abort_reservation
        reservations = []
        compensation_calls = []

        async def failing_activation(reservation, **kwargs):
            reservations.append(reservation)
            if activation_boundary == "observer":
                await activate(reservation, **kwargs)
            raise RuntimeError("synthetic activation observer failure")

        async def raced_abort(reservation):
            await activate(reservation)
            await abort(reservation)

        async def unexpected_compensation(task_id, **_kwargs):
            compensation_calls.append(task_id)
            raise AssertionError("driver-owned task must not be compensated")

        monkeypatch.setattr(stack.runtime, "activate", failing_activation)
        if activation_boundary == "abort_race":
            monkeypatch.setattr(stack.runtime, "abort_reservation", raced_abort)
        monkeypatch.setattr(
            stack.storage, "fail_queued_agent_task_activation", unexpected_compensation
        )
        task_id, status, replayed = await send()
        await stack.wait_until_running()
        assert not replayed and status == AgentTaskStatus.QUEUED
        assert reservations[0].activated and not reservations[0].aborted
        assert compensation_calls == []
        task = await stack.storage.get_agent_task(task_id)
        assert task is not None and task.status == AgentTaskStatus.RUNNING
        stack.release_handler.set()
        terminal = await stack.runtime.wait(task_id, timeout=2.0)
        assert terminal.status == AgentTaskStatus.SUCCEEDED


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["web", "channel"])
@pytest.mark.parametrize("abort_claimed", [False, True])
async def test_abort_cleanup_failure_requires_confirmed_reservation_revocation(
    tmp_path, monkeypatch, surface, abort_claimed
):
    async with ingress(tmp_path, surface) as (stack, send):
        abort = stack.runtime.abort_reservation
        fail_task = stack.storage.fail_queued_agent_task_activation
        compensation_calls = []

        async def failing_activation(*_args, **_kwargs):
            raise RuntimeError("synthetic activation failure")

        async def failing_abort(reservation):
            if abort_claimed:
                await abort(reservation)
            raise RuntimeError("synthetic abort cleanup failure")

        async def record_compensation(task_id, **kwargs):
            compensation_calls.append(task_id)
            return await fail_task(task_id, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(stack.runtime, "activate", failing_activation)
            patch.setattr(stack.runtime, "abort_reservation", failing_abort)
            patch.setattr(stack.storage, "fail_queued_agent_task_activation", record_compensation)
            task_id, status, replayed = await send()
        assert not replayed
        expected = AgentTaskStatus.FAILED if abort_claimed else AgentTaskStatus.QUEUED
        assert status == expected
        assert compensation_calls == ([task_id] if abort_claimed else [])
        task = await stack.storage.get_agent_task(task_id)
        assert task is not None and task.status == expected
