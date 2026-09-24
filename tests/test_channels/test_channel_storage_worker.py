from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.channels._util import EventDedupeCache
from opensquilla.channels.delivery_store import (
    ChannelDeliveryStore,
    deliver_operation_with_outbox,
    deliver_with_outbox,
)
from opensquilla.channels.storage_worker import AsyncChannelDeliveryStore, ChannelStorageClosedError
from opensquilla.channels.types import (
    ChannelArtifactDeliveryRequest,
    IncomingMessage,
    OutgoingMessage,
)


def message(event: str = "one") -> IncomingMessage:
    return IncomingMessage(
        sender_id="u",
        channel_id="c",
        content="original",
        metadata={"event_id": event, "nested": {"value": 1}},
    )


async def until(predicate) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


class GateStore(ChannelDeliveryStore):
    """A real SQLite connection, paused at deterministic operation boundaries."""

    def __init__(
        self, path: Path, events: list, gate: threading.Event, entered: threading.Event, pause: str
    ):
        self.events = events
        self.gate = gate
        self.entered = entered
        self.pause = pause
        self.events.append(("open", threading.get_ident()))
        super().__init__(path)

    def _pause(self, name):
        self.events.append((name, threading.get_ident()))
        if self.pause == name:
            self.entered.set()
            assert self.gate.wait(5), "test did not release database operation"

    def diagnostics(self, name):
        self._pause(name)
        return super().diagnostics(name)

    def accept_inbound(self, name, msg):
        self._pause("before_accept")
        result = super().accept_inbound(name, msg)
        self._pause("after_accept")
        return result

    def claim_inbound(self, name, msg):
        result = super().claim_inbound(name, msg)
        self._pause("after_claim")
        return result

    def fail_inbound_snapshot(self, *args):
        self._pause("fail_inbound")
        return super().fail_inbound_snapshot(*args)

    def complete_inbound(self, *args, **kwargs):
        self._pause("settle")
        return super().complete_inbound(*args, **kwargs)

    def acquire_transport_lease(self, *args, **kwargs):
        result = super().acquire_transport_lease(*args, **kwargs)
        self._pause("after_acquire")
        return result

    def renew_transport_lease(self, lease, **kwargs):
        self._pause("renew")
        result = super().renew_transport_lease(lease, **kwargs)
        self._pause("after_renew")
        return result

    def close(self):
        self._pause("close")
        return super().close()


@pytest.fixture
async def gated(tmp_path):
    workers = []

    async def create(pause="block", **kwargs):
        events, gate, entered = [], threading.Event(), threading.Event()

        def factory(path):
            return GateStore(path, events, gate, entered, pause)

        worker = AsyncChannelDeliveryStore(
            tmp_path / f"{len(workers)}.sqlite", store_factory=factory, **kwargs
        )
        workers.append((worker, gate))
        await worker.open()
        return worker, events, gate, entered

    yield create
    for worker, gate in workers:
        gate.set()
        await worker.close()


async def test_connection_work_and_close_share_one_non_loop_thread(gated):
    worker, events, gate, entered = await gated()
    call = asyncio.create_task(worker.diagnostics("block"))
    await until(entered.is_set)
    ticks = 0
    for _ in range(10):
        await asyncio.sleep(0.002)
        ticks += 1
    gate.set()
    await call
    await worker.close()
    assert ticks == 10
    thread_ids = {tid for _, tid in events}
    assert len(thread_ids) == 1
    assert threading.get_ident() not in thread_ids
    assert events[0][0] == "open" and events[-1][0] == "close"


async def test_queue_capacity_and_cancel_before_submission(gated):
    worker, events, gate, entered = await gated(ordinary_capacity=1)
    active = asyncio.create_task(worker.diagnostics("block"))
    await until(entered.is_set)
    queued = asyncio.create_task(worker.diagnostics("queued"))
    await until(lambda: worker.metrics()["queued"] == 1)
    waiting = asyncio.create_task(worker.diagnostics("waiting"))
    await until(lambda: worker.metrics()["waiting"] == 1)
    for task in (queued, waiting):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    gate.set()
    await active
    assert worker.metrics()["queue_high_water"] == 1
    assert not {"queued", "waiting"} & {name for name, _ in events}


async def test_submitted_accept_cancellation_still_hands_off_exactly_once(gated):
    worker, _, gate, entered = await gated("after_accept")
    queue = asyncio.Queue()
    task = asyncio.create_task(worker.enqueue("channel", message(), queue))
    await until(entered.is_set)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    await until(lambda: queue.qsize() == 1)
    assert await worker.enqueue("channel", message(), queue) is False
    assert queue.qsize() == 1
    await worker.close()
    with sqlite3.connect(worker.path) as conn:
        assert conn.execute("SELECT state FROM channel_ingress").fetchone() == ("accepted",)


async def test_waiting_parameters_and_handoff_are_snapshots(gated):
    worker, _, gate, entered = await gated()
    active = asyncio.create_task(worker.diagnostics("block"))
    await until(entered.is_set)
    original = message()
    queue = asyncio.Queue()
    admission = asyncio.create_task(worker.enqueue("channel", original, queue))
    await until(lambda: worker.metrics()["queued"] == 1)
    original.content = "mutated"
    original.metadata["nested"]["value"] = 2
    gate.set()
    await active
    assert await admission is True
    handed_off = queue.get_nowait()
    assert handed_off.content == "original"
    assert handed_off.metadata["nested"] == {"value": 1}
    recovered = await worker.recover_inbound("channel")
    assert recovered[0].content == "original"


async def test_cancelled_claim_is_settled_before_close(gated):
    worker, _, gate, entered = await gated("after_claim")
    await worker.accept_inbound("channel", message())
    task = asyncio.create_task(worker.claim_inbound("channel", message()))
    await until(entered.is_set)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    closing = asyncio.create_task(worker.close())
    await asyncio.sleep(0)
    gate.set()
    await closing
    with sqlite3.connect(worker.path) as conn:
        assert conn.execute("SELECT state FROM channel_ingress").fetchone() == ("accepted",)


async def test_lease_has_reserved_capacity_and_settlement_fairness(gated):
    worker, events, gate, entered = await gated(ordinary_capacity=1, settlement_capacity=16)
    lease = await worker.acquire_transport_lease("channel", "account", "owner")
    active = asyncio.create_task(worker.diagnostics("block"))
    await until(entered.is_set)
    ordinary = asyncio.create_task(worker.diagnostics("ordinary"))
    settles = [asyncio.create_task(worker.complete_inbound(None, "done")) for _ in range(10)]
    renew = asyncio.create_task(worker.renew_transport_lease(lease))
    await until(lambda: worker.metrics()["queued"] == 12)
    gate.set()
    await asyncio.gather(active, ordinary, renew, *settles)
    order = [name for name, _ in events if name in {"renew", "settle", "ordinary"}]
    assert order[0] == "renew"
    assert order[1:9] == ["settle"] * 8
    assert order[9] == "ordinary"
    assert order[10:] == ["settle"] * 2


async def test_close_rejects_new_reads_but_drains_existing_settlement(gated):
    worker, _, gate, entered = await gated()
    active = asyncio.create_task(worker.diagnostics("block"))
    await until(entered.is_set)
    settle = asyncio.create_task(worker.complete_inbound(None, "done"))
    await until(lambda: worker.metrics()["queued"] == 1)
    closing = asyncio.create_task(worker.close())
    await asyncio.sleep(0)
    with pytest.raises(ChannelStorageClosedError):
        await worker.diagnostics("new")
    gate.set()
    await asyncio.gather(active, settle, closing)


async def test_cancelling_successful_outbox_settlement_never_overwrites_receipt(gated):
    worker, _, gate, entered = await gated()
    sent = asyncio.Event()
    blocker = None

    async def send(_message):
        nonlocal blocker
        blocker = asyncio.create_task(worker.diagnostics("block"))
        await until(entered.is_set)
        sent.set()
        return "provider-id"

    channel = SimpleNamespace(_delivery_store=worker, _delivery_channel_name="channel", send=send)
    task = asyncio.create_task(
        deliver_with_outbox(
            channel,
            OutgoingMessage(content="hello", reply_to="chat", metadata={"delivery_id": "delivery"}),
        )
    )
    await sent.wait()
    await until(lambda: worker.metrics()["queued"] == 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    await blocker
    await worker.close()
    with sqlite3.connect(worker.path) as conn:
        assert conn.execute("SELECT state FROM channel_outbox").fetchone() == ("sent",)


async def test_exception_snapshot_does_not_copy_unpicklable_transport_state(
    channel_store, tmp_path
):
    worker = await channel_store(tmp_path / "error.sqlite")
    send_id = await worker.begin_send("channel", OutgoingMessage(content="hello", reply_to="chat"))
    error = RuntimeError("authorization=secret unavailable")
    error.lock = threading.Lock()
    await worker.fail_send(send_id, error)
    record = await worker.send_record(send_id)
    assert isinstance(record, dict)
    assert record["state"] == "unknown"
    assert "secret" not in record["error_message"]
    assert "RuntimeError" in record["error_message"]


async def test_open_failure_still_shuts_down_executor(tmp_path):
    def factory(_path):
        raise OSError("cannot open")

    worker = AsyncChannelDeliveryStore(tmp_path / "failure.sqlite", store_factory=factory)
    with pytest.raises(OSError, match="cannot open"):
        await worker.open()
    with pytest.raises(OSError, match="cannot open"):
        await worker.close()
    assert worker._executor._shutdown


async def test_dedupe_waiter_can_retry_cancelled_unaccepted_owner():
    cache = EventDedupeCache()
    started = asyncio.Event()
    hold = asyncio.Event()
    calls = []

    async def first():
        started.set()
        await hold.wait()

    async def second():
        calls.append("accepted")
        return True

    owner = asyncio.create_task(cache.run_once("key", first))
    await started.wait()
    waiter = asyncio.create_task(cache.run_once("key", second))
    await asyncio.sleep(0)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert await waiter is True
    assert await cache.run_once("key", second) is False
    assert calls == ["accepted"]


async def test_pairing_transaction_rolls_back_on_failure(channel_store, tmp_path):
    worker = await channel_store(tmp_path / "pairing.sqlite")
    with pytest.raises(KeyError):
        await worker.approve_pairing_once(channel_name="channel", pairing_id="missing")
    record = await worker.request_pairing(
        channel_name="channel", provider="test", account_id="a", sender_id="u", sender_name=None
    )
    results = await asyncio.gather(
        *(
            worker.approve_pairing_once(channel_name="channel", pairing_id=record.pairing_id)
            for _ in range(2)
        )
    )
    assert sorted(changed for _, changed in results) == [False, True]


@pytest.mark.parametrize("operation", ["acquire", "renew"])
async def test_cancelled_lease_commit_is_released_before_close(gated, operation):
    worker, _, gate, entered = await gated(f"after_{operation}")
    if operation == "acquire":
        task = asyncio.create_task(worker.acquire_transport_lease("channel", "account", "owner"))
    else:
        lease = await worker.acquire_transport_lease("channel", "account", "owner")
        task = asyncio.create_task(worker.renew_transport_lease(lease))
    await until(entered.is_set)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    closing = asyncio.create_task(worker.close())
    await asyncio.sleep(0)
    gate.set()
    await closing
    with sqlite3.connect(worker.path) as conn:
        assert conn.execute("SELECT expires_at FROM channel_transport_leases").fetchone() == (0,)


async def test_close_wakes_waiters_without_waiting_for_queue_capacity(gated):
    worker, _, gate, entered = await gated(ordinary_capacity=1)
    active = asyncio.create_task(worker.diagnostics("block"))
    await until(entered.is_set)
    queued = asyncio.create_task(worker.diagnostics("queued"))
    await until(lambda: worker.metrics()["queued"] == 1)
    waiting = asyncio.create_task(worker.diagnostics("waiting"))
    await until(lambda: worker.metrics()["waiting"] == 1)
    closing = asyncio.create_task(worker.close())
    with pytest.raises(ChannelStorageClosedError):
        await asyncio.wait_for(waiting, 0.2)
    assert not active.done()
    gate.set()
    await asyncio.gather(active, queued, closing)


async def test_manager_stops_transport_when_lease_deadline_passes(monkeypatch):
    from unittest.mock import AsyncMock

    from opensquilla.channels.delivery_store import TransportLease
    from opensquilla.channels.manager import ChannelManager

    original_sleep = asyncio.sleep

    async def short_sleep(delay):
        await original_sleep(0 if delay == 30.0 else delay)

    monkeypatch.setattr("opensquilla.channels.manager.asyncio.sleep", short_sleep)
    lease = TransportLease("channel", "account", "owner", 1, time.time() + 0.02)

    async def renew(_lease):
        await original_sleep(1)
        return lease

    adapter = SimpleNamespace(stop=AsyncMock(), _connected=True)
    manager = ChannelManager(
        _channels={"channel": adapter},
        _turn_runner=None,
        _session_manager=None,
        _delivery_store=SimpleNamespace(renew_transport_lease=renew),
        _transport_leases={"channel": lease},
    )
    await manager._renew_transport_lease("channel")
    adapter.stop.assert_awaited_once()
    assert adapter._connected is False


async def test_drain_waits_for_enqueue_not_the_provider_loop(gated):
    worker, _, gate, entered = await gated("after_accept")
    queue = asyncio.Queue()
    provider_lifetime = asyncio.Event()

    async def producer():
        await worker.enqueue("channel", message(), queue)
        await provider_lifetime.wait()

    task = asyncio.create_task(producer())
    await until(entered.is_set)
    worker.stop_accepting("channel")
    draining = asyncio.create_task(worker.drain_channel("channel"))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.wait_for(draining, 1)
    assert not task.done()
    assert queue.qsize() == 1
    provider_lifetime.set()
    await task


async def test_success_receipt_ignores_uncopyable_sdk_state(channel_store, tmp_path):
    worker = await channel_store(tmp_path / "receipt.sqlite")
    send_id = await worker.begin_send("channel", OutgoingMessage(content="hello", reply_to="chat"))
    await worker.complete_send(
        send_id,
        {
            "status": "sent",
            "provider_message_id": "ack",
            "sdk_handle": threading.Lock(),
        },
    )
    receipt = await worker.send_record(send_id)
    assert receipt["state"] == "sent"
    assert receipt["provider_message_id"] == "ack"


async def test_shutdown_deadline_discards_unsubmitted_jobs_and_keeps_active_connection(gated):
    from opensquilla.channels.manager import ChannelManager

    worker, events, gate, entered = await gated()
    active = asyncio.create_task(worker.diagnostics("block"))
    await until(entered.is_set)
    queued = asyncio.create_task(worker.diagnostics("must-not-run"))
    await until(lambda: worker.metrics()["queued"] == 1)
    manager = ChannelManager(
        _channels={}, _turn_runner=None, _session_manager=None, _delivery_store=worker
    )
    with pytest.raises(TimeoutError, match="deadline"):
        await asyncio.wait_for(manager.stop_all(timeout=0.02), 0.2)
    assert not active.done()
    assert "close" not in {name for name, _ in events}
    with pytest.raises(ChannelStorageClosedError):
        await queued
    gate.set()
    await active
    await worker.close()
    await until(lambda: not manager._shutdown_tasks)
    names = [name for name, _ in events]
    assert "must-not-run" not in names
    assert names[-1] == "close"


async def test_late_lease_cleanup_cannot_release_new_owner(gated):
    worker, _, gate, entered = await gated("after_acquire")
    first = asyncio.create_task(worker.acquire_transport_lease("channel", "account", "old"))
    await until(entered.is_set)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    with sqlite3.connect(worker.path) as conn:
        conn.execute("UPDATE channel_transport_leases SET expires_at = 0")
    replacement = ChannelDeliveryStore(worker.path)
    try:
        lease = replacement.acquire_transport_lease("channel", "account", "new")
    finally:
        replacement.close()
    gate.set()
    await worker.close()
    with sqlite3.connect(worker.path) as conn:
        row = conn.execute(
            "SELECT owner_id, fencing_token, expires_at FROM channel_transport_leases"
        ).fetchone()
    assert row[0] == "new"
    assert row[1] == lease.fencing_token
    assert row[2] > 0


async def test_abort_after_commit_leaves_recoverable_row_without_stopped_handoff(gated):
    worker, _, gate, entered = await gated("after_accept")
    queue = asyncio.Queue()
    admission = asyncio.create_task(worker.enqueue("channel", message(), queue))
    await until(entered.is_set)
    await worker.abort_pending()
    gate.set()
    assert await admission is True
    await worker.close()
    assert queue.empty()
    reopened = ChannelDeliveryStore(worker.path)
    try:
        assert len(reopened.recover_inbound("channel")) == 1
    finally:
        reopened.close()


async def test_timeout_cleanup_releases_all_known_leases(gated):
    from unittest.mock import AsyncMock

    from opensquilla.channels.manager import ChannelManager

    worker, _, gate, entered = await gated()
    leases = {
        name: await worker.acquire_transport_lease(name, "account", "owner") for name in ("a", "b")
    }
    queue = asyncio.Queue()
    active = asyncio.create_task(worker.diagnostics("block"))
    await until(entered.is_set)
    admission = asyncio.create_task(worker.enqueue("a", message(), queue))
    await until(lambda: worker.metrics()["queued"] == 1)
    manager = ChannelManager(
        _channels={name: SimpleNamespace(stop=AsyncMock()) for name in leases},
        _turn_runner=None,
        _session_manager=None,
        _delivery_store=worker,
        _transport_leases=dict(leases),
    )
    with pytest.raises(TimeoutError):
        await manager.stop_all(timeout=0.02)
    with pytest.raises(ChannelStorageClosedError):
        await admission
    gate.set()
    await active
    await until(lambda: not manager._shutdown_tasks)
    with sqlite3.connect(worker.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM channel_transport_leases WHERE expires_at != 0"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "result,state",
    [(None, "sent_unconfirmed"), ({"status": "sent", "provider_message_id": "ack"}, "sent")],
)
@pytest.mark.parametrize("success_first", [False, True])
async def test_success_receipt_wins_over_late_failure_in_either_order(
    channel_store, tmp_path, result, state, success_first
):
    worker = await channel_store(tmp_path / "late-failure.sqlite")
    send_id = await worker.begin_send("channel", OutgoingMessage(content="hello", reply_to="chat"))
    if success_first:
        await worker.complete_send(send_id, result)
        await worker.fail_send(send_id, RuntimeError("late cancellation"))
    else:
        await worker.fail_send(send_id, RuntimeError("unconfirmed"))
        await worker.complete_send(send_id, result)
    record = await worker.send_record(send_id)
    assert record["state"] == state
    if result is not None:
        assert record["provider_message_id"] == "ack"


async def test_process_crash_recovers_committed_ingress_once_without_resending_unknown(
    channel_store, tmp_path
):
    path = tmp_path / "crashed.sqlite"
    inbound = message("crash-event")
    artifact = {
        "artifact_id": "crash-artifact",
        "file_path": "/synthetic/report.txt",
        "name": "report.txt",
        "mime_type": "text/plain",
        "size": 6,
        "session_id": "crash-session",
        "delivery_id": "crash-delivery",
    }
    script = textwrap.dedent("""\
        import asyncio
        import json
        import os
        import sys
        from types import SimpleNamespace
        from opensquilla.channels.delivery_store import deliver_operation_with_outbox
        from opensquilla.channels.storage_worker import AsyncChannelDeliveryStore
        from opensquilla.channels.types import ChannelArtifactDeliveryRequest, IncomingMessage

        async def main():
            payload = json.load(sys.stdin)
            store = AsyncChannelDeliveryStore(payload["path"])
            await store.open()
            inbound = IncomingMessage.model_validate(payload["inbound"])
            assert await store.accept_inbound("channel", inbound)
            channel = SimpleNamespace(_delivery_store=store, _delivery_channel_name="channel")
            request = ChannelArtifactDeliveryRequest(inbound=inbound, **payload["artifact"])

            async def ambiguous_provider(request):
                raise TimeoutError("provider response lost after upload")

            try:
                await deliver_operation_with_outbox(
                    channel, "deliver_artifact", ambiguous_provider, (request,), {}
                )
            except TimeoutError:
                pass
            else:
                raise AssertionError("provider must have an ambiguous outcome")
            record = await store.send_record(request.delivery_id)
            assert record["state"] == "unknown"
            print("accept-and-unknown-committed", flush=True)
            # No worker.close, asyncio teardown, or Python exit handlers may run.
            os._exit(31)

        asyncio.run(main())
        """)
    root = Path(__file__).resolve().parents[2]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(json.dumps({
                "path": str(path),
                "inbound": inbound.model_dump(mode="json"),
                "artifact": artifact,
            }).encode()),
            timeout=20,
        )
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
    assert process.returncode == 31, stderr.decode()
    assert b"accept-and-unknown-committed" in stdout.splitlines()

    reopened = await channel_store(path)
    queue = asyncio.Queue()
    recovered = await reopened.recover_inbound("channel")
    assert len(recovered) == 1
    assert await reopened.enqueue("channel", recovered[0], queue)
    assert not await reopened.enqueue("channel", inbound, queue)
    assert queue.qsize() == 1
    restored = queue.get_nowait()
    claim = await reopened.claim_inbound("channel", restored)
    assert claim is not None
    assert await reopened.claim_inbound("channel", restored) is None
    await reopened.complete_inbound(claim, "dispatched")
    assert await reopened.recover_inbound("channel") == []

    from unittest.mock import AsyncMock

    provider = AsyncMock(side_effect=AssertionError("unknown must not resend"))
    channel = SimpleNamespace(_delivery_store=reopened, _delivery_channel_name="channel")
    request = ChannelArtifactDeliveryRequest(inbound=inbound, **artifact)
    replay = await deliver_operation_with_outbox(
        channel, "deliver_artifact", provider, (request,), {}
    )
    assert not replay.is_delivered()
    assert replay.retryable is False
    assert "unknown" in replay.reason
    provider.assert_not_awaited()
    assert (await reopened.send_record(request.delivery_id))["state"] == "unknown"
