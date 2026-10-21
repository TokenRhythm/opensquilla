"""Background metadata maintenance invalidates the directory once, after work."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway import content_length_backfill
from opensquilla.gateway.event_bridge import EventBridge
from opensquilla.gateway.session_events import (
    SESSION_DIRECTORY_KEY,
    build_session_directory_metadata_changed_payload,
)
from opensquilla.gateway.websocket import SubscriptionManager


@pytest.mark.asyncio
async def test_default_delay_and_small_batches_precede_one_notification(monkeypatch) -> None:
    order: list[object] = []

    async def sleep(delay: float) -> None:
        order.append(("sleep", delay))

    counts = iter([4, 4, 1, 0])

    async def backfill(**kwargs) -> int:
        order.append(("batch", kwargs))
        return next(counts)

    async def notify() -> None:
        order.append("notify")

    monkeypatch.setattr(content_length_backfill.asyncio, "sleep", sleep)
    await content_length_backfill.run_content_length_backfill(
        SimpleNamespace(backfill_transcript_content_lengths=backfill),
        notify_directory_changed=notify,
    )

    assert order[0] == ("sleep", 60.0)
    assert [item for item in order if isinstance(item, tuple) and item[0] == "batch"] == [
        ("batch", {"batch_size": 4, "max_batches": 1}),
    ] * 4
    assert order.count(("sleep", 0)) == 3
    assert order.count("notify") == 1
    assert order[-1] == "notify"


@pytest.mark.asyncio
async def test_no_pending_rows_does_not_notify() -> None:
    storage = SimpleNamespace(backfill_transcript_content_lengths=AsyncMock(return_value=0))
    notify = AsyncMock()
    await content_length_backfill.run_content_length_backfill(
        storage, initial_delay_s=0, notify_directory_changed=notify,
    )
    storage.backfill_transcript_content_lengths.assert_awaited_once()
    notify.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "counts", [[RuntimeError("cleanup after commit")], [4, RuntimeError("busy")]],
)
async def test_failed_attempt_notifies_once_even_without_a_returned_count(counts) -> None:
    storage = SimpleNamespace(backfill_transcript_content_lengths=AsyncMock(side_effect=counts))
    notify = AsyncMock()
    await content_length_backfill.run_content_length_backfill(
        storage, initial_delay_s=0, notify_directory_changed=notify,
    )
    notify.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_during", ["initial_delay", "first_batch", "later_batch"])
async def test_cancellation_never_starts_notification(monkeypatch, cancel_during: str) -> None:
    blocked = asyncio.Event()
    release = asyncio.Event()
    real_sleep = asyncio.sleep
    calls = 0

    async def sleep(delay: float) -> None:
        if delay > 0:
            blocked.set()
            await release.wait()
        else:
            await real_sleep(0)

    async def backfill(**_kwargs) -> int:
        nonlocal calls
        calls += 1
        if cancel_during == "later_batch" and calls == 1:
            return 4
        blocked.set()
        await release.wait()
        return 0

    monkeypatch.setattr(content_length_backfill.asyncio, "sleep", sleep)
    notify = AsyncMock()
    task = asyncio.create_task(content_length_backfill.run_content_length_backfill(
        SimpleNamespace(backfill_transcript_content_lengths=backfill),
        initial_delay_s=60 if cancel_during == "initial_delay" else 0,
        notify_directory_changed=notify,
    ))
    await blocked.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    notify.assert_not_awaited()
    if cancel_during == "initial_delay":
        assert calls == 0


@pytest.mark.asyncio
async def test_notification_failure_does_not_restart_completed_maintenance() -> None:
    storage = SimpleNamespace(backfill_transcript_content_lengths=AsyncMock(side_effect=[4, 0]))
    notify = AsyncMock(side_effect=RuntimeError("socket closed"))
    await content_length_backfill.run_content_length_backfill(
        storage, initial_delay_s=0, notify_directory_changed=notify,
    )
    assert storage.backfill_transcript_content_lengths.await_count == 2
    notify.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_restart_uses_remaining_work_without_replaying_previous_notification() -> None:
    storage = SimpleNamespace(backfill_transcript_content_lengths=AsyncMock(
        side_effect=[4, RuntimeError("busy"), 4, 0, 0],
    ))
    notify = AsyncMock()
    for expected_notifications in [1, 2, 2]:
        await content_length_backfill.run_content_length_backfill(
            storage, initial_delay_s=0, notify_directory_changed=notify,
        )
        assert notify.await_count == expected_notifications


@pytest.mark.asyncio
@pytest.mark.parametrize("subscribed", [False, True])
async def test_directory_notification_uses_live_subscribers_without_replay_or_epoch_reads(
    monkeypatch, subscribed: bool,
) -> None:
    import opensquilla.gateway.event_bridge as bridge_module

    def no_replay():
        pytest.fail("Directory metadata invalidation must not enter session replay")

    monkeypatch.setattr(bridge_module, "get_session_streams", no_replay)
    subscriptions = SubscriptionManager()
    directory_conn = SimpleNamespace(send_event=AsyncMock())
    message_conn = SimpleNamespace(send_event=AsyncMock())
    connections = {"directory": directory_conn, "message": message_conn}
    if subscribed:
        subscriptions.subscribe_sessions("directory")
    subscriptions.subscribe_messages("message", "agent:main:webchat:one")
    bridge = EventBridge(subscriptions, SimpleNamespace(get=connections.get))
    payload = build_session_directory_metadata_changed_payload()
    assert payload == {"schema_version": 1, "key": "*", "reason": "metadata_backfilled"}

    async def notify() -> None:
        await bridge.emit(SESSION_DIRECTORY_KEY, "sessions.changed", payload)

    await content_length_backfill.run_content_length_backfill(
        SimpleNamespace(backfill_transcript_content_lengths=AsyncMock(side_effect=[4, 4, 0])),
        initial_delay_s=0,
        notify_directory_changed=notify,
    )
    if subscribed:
        directory_conn.send_event.assert_awaited_once_with("sessions.changed", payload)
    else:
        directory_conn.send_event.assert_not_awaited()
    message_conn.send_event.assert_not_awaited()
