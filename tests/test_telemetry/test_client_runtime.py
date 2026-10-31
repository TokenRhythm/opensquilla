from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from opensquilla.telemetry import runtime as runtime_module
from opensquilla.telemetry.consent import (
    CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
    TelemetryScope,
)
from opensquilla.telemetry.contracts import TELEMETRY_EVENT_ADAPTER
from opensquilla.telemetry.outbox import OutboxPriority, TelemetryOutbox
from opensquilla.telemetry.recorder import RecordStatus
from opensquilla.telemetry.runtime import ScopedTelemetryRuntime
from opensquilla.telemetry.uploader import TelemetryUploader
from tests.helpers.telemetry_runtime import runtime_config as _config
from tests.helpers.telemetry_runtime import turn_event as _turn_event
from tests.helpers.telemetry_shutdown_process import run_shutdown_probe


@pytest.fixture(autouse=True)
async def offline_uploads(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(runtime_module, "get_device_id", lambda: None)

    async def unavailable(_request):
        return httpx.Response(503)

    transport = SimpleNamespace(handler=unavailable, requests=[])

    async def handle(request):
        transport.requests.append(request)
        return await transport.handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(
            runtime_module,
            "TelemetryUploader",
            lambda *args, **kwargs: TelemetryUploader(*args, **kwargs, http_client=client),
        )
        yield transport


@pytest.fixture
async def expire_shutdown_deadline(monkeypatch: pytest.MonkeyPatch):
    # This fixture tests cancellation consequences, NOT deadline arithmetic or
    # real timer delivery. Those remain covered by test_shutdown_deadlines and
    # the isolated real-timer/SQLite probes below.
    contexts: list[tuple[float, asyncio.Timeout]] = []
    installed = asyncio.Event()
    real_loop = asyncio.get_running_loop()
    shutdown_now = real_loop.time()

    class ShutdownClockLoop:
        def time(self) -> float:
            return shutdown_now

        def __getattr__(self, name):
            return getattr(real_loop, name)

    # close() also checks time before entering timeout_at. Control both views
    # of the deadline so preemption cannot skip the cancellation under test.
    # The real event loop, SQLite workers and wait_for watchdogs keep running.
    shutdown_loop = ShutdownClockLoop()

    def controlled_get_running_loop():
        running_loop = asyncio.get_running_loop()
        return shutdown_loop if running_loop is real_loop else running_loop

    def controlled_timeout_at(deadline: float) -> asyncio.Timeout:
        assert math.isfinite(deadline)
        assert deadline <= (
            shutdown_now + runtime_module.SHUTDOWN_UPLOAD_TIMEOUT_SECONDS
        )
        # Drive the real asyncio cancellation after the intended upload state
        # is reached, independently of SQLite or worker scheduling latency.
        timeout = asyncio.timeout(None)
        contexts.append((deadline, timeout))
        installed.set()
        return timeout

    monkeypatch.setattr(
        runtime_module,
        "asyncio",
        SimpleNamespace(**(vars(asyncio) | {
            "get_running_loop": controlled_get_running_loop,
            "timeout_at": controlled_timeout_at,
        })),
    )

    async def expire(
        runtime: ScopedTelemetryRuntime, *, cancel: bool = True,
    ) -> tuple[asyncio.Timeout, ...]:
        await asyncio.wait_for(installed.wait(), timeout=10)
        assert contexts, "shutdown must install a bounded upload timeout"
        assert all(deadline == runtime._shutdown_deadline for deadline, _ in contexts)
        timeouts = tuple(timeout for _, timeout in contexts)
        if cancel:
            for timeout in timeouts:
                timeout.reschedule(asyncio.get_running_loop().time())
        return timeouts

    return expire


def _accepted_response(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    return httpx.Response(
        202,
        json={
            "ok": True,
            "batch_id": payload["batch_id"],
            "accepted": len(payload["events"]),
            "duplicates": 0,
        },
    )


@pytest.mark.parametrize(
    ("environment", "explicit_url", "expected"),
    [
        ({}, None, runtime_module.DEFAULT_TELEMETRY_V2_BASE_URL),
        (
            {"OPENSQUILLA_TELEMETRY_BASE_URL": "https://telemetry.invalid/test"},
            None,
            "https://telemetry.invalid/test",
        ),
        (
            {"OPENSQUILLA_TELEMETRY_BASE_URL": "https://telemetry.invalid/test"},
            "https://explicit.invalid/preview",
            "https://explicit.invalid/preview",
        ),
        ({"OPENSQUILLA_TELEMETRY_BASE_URL": ""}, None, ""),
    ],
)
def test_destination_override_is_explicit_and_never_falls_back(
    tmp_path: Path,
    environment: dict[str, str],
    explicit_url: str | None,
    expected: str,
) -> None:
    runtime = ScopedTelemetryRuntime(
        config=_config(tmp_path),
        env=environment,
        base_url=explicit_url,
    )
    assert runtime._base_url == expected


def test_destination_override_reads_process_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_TELEMETRY_BASE_URL", "https://telemetry.invalid/test")
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path))
    assert runtime._base_url == "https://telemetry.invalid/test"


async def test_start_does_not_create_state_or_network_when_uploads_disabled(
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
        config=_config(state_dir, disabled=True),
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


async def test_default_upload_policy_lazily_opens_only_the_recorded_scope(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    runtime = ScopedTelemetryRuntime(
        config=_config(state_dir),
    )
    try:
        result = await runtime.record(_turn_event())
        assert result.status is RecordStatus.RECORDED
        assert runtime.opened_scopes == frozenset({TelemetryScope.RELIABILITY})
        assert (state_dir / "telemetry" / "reliability-outbox.sqlite3").is_file()
        assert not (state_dir / "telemetry" / "growth-outbox.sqlite3").exists()
    finally:
        await runtime.close()


async def test_live_disable_preserves_queue_and_reenable_resumes_recording(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    config = _config(state_dir)
    runtime = ScopedTelemetryRuntime(config=config)
    try:
        assert (await runtime.record(_turn_event(1))).status is RecordStatus.RECORDED
        config.privacy.disable_network_observability = True
        assert (await runtime.record(_turn_event(2))).status is RecordStatus.CONSENT_BLOCKED
        outbox = runtime._scopes[TelemetryScope.RELIABILITY].outbox
        assert (await outbox.stats()).pending_events == 1
        config.privacy.disable_network_observability = False
        assert (await runtime.record(_turn_event(3))).status is RecordStatus.RECORDED
        assert (await outbox.stats()).pending_events == 2
    finally:
        await runtime.close()


async def test_background_record_failure_does_not_escape(tmp_path: Path) -> None:
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path / "state"))
    await runtime.close()
    runtime.record_background(_turn_event())


async def test_background_burst_bounds_pending_work_and_preserves_higher_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "BACKGROUND_RECORD_QUEUE_LIMIT", 4)
    monkeypatch.setattr(runtime_module, "BACKGROUND_RECORD_BATCH_SIZE", 2)
    monkeypatch.setattr(runtime_module, "get_device_id", lambda: None)
    batches = []
    enqueue_many = TelemetryOutbox.enqueue_many

    async def capture_batch(outbox, events):
        batches.append(len(events))
        return await enqueue_many(outbox, events)

    monkeypatch.setattr(TelemetryOutbox, "enqueue_many", capture_batch)
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path), env={})
    for number in range(1, 9):
        runtime.record_background(_turn_event(number))
    runtime.record_background(_turn_event(9), priority=OutboxPriority.HIGH)
    assert len(runtime._record_queue) == 4
    assert len(runtime._record_tasks) == 1
    await runtime.close(flush=False)
    assert batches == [2, 2]
    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        batch = await outbox.claim_batch()
        assert batch is not None
        assert {event.event_id for event in batch.events} == {
            str(_turn_event(number).event_id) for number in (2, 3, 4, 9)
        }
    finally:
        await outbox.close()


async def test_background_coalesces_across_callbacks_and_close_wakes_pending_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "BACKGROUND_RECORD_INTERVAL_SECONDS", 60)
    monkeypatch.setattr(runtime_module, "get_device_id", lambda: None)
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path), env={})
    waiting = asyncio.Event()
    flush_wait = runtime._record_flush.wait
    batches = []
    enqueue_many = TelemetryOutbox.enqueue_many

    async def capture_wait():
        waiting.set()
        await flush_wait()

    async def capture_batch(outbox, events):
        batches.append(len(events))
        return await enqueue_many(outbox, events)

    monkeypatch.setattr(runtime._record_flush, "wait", capture_wait)
    monkeypatch.setattr(TelemetryOutbox, "enqueue_many", capture_batch)
    runtime.record_background(_turn_event(1))
    await asyncio.wait_for(waiting.wait(), timeout=5)
    assert runtime.opened_scopes == frozenset()
    runtime.record_background(_turn_event(2))
    await asyncio.wait_for(runtime.close(flush=False), timeout=5)
    assert batches == [2]


async def test_hundred_background_events_use_four_commits_and_one_spool_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "get_device_id", lambda: None)
    commits = 0
    scans = 0
    open_outbox = TelemetryOutbox.open.__func__

    async def capture_open(cls, *args, **kwargs):
        outbox = await open_outbox(cls, *args, **kwargs)
        commit = outbox._connection.commit

        async def capture_commit():
            nonlocal commits
            commits += 1
            await commit()

        monkeypatch.setattr(outbox._connection, "commit", capture_commit)
        return outbox

    async def scan_spool():
        nonlocal scans
        scans += 1

    async def no_upload(_scope):
        return None

    monkeypatch.setattr(TelemetryOutbox, "open", classmethod(capture_open))
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path), env={})
    monkeypatch.setattr(runtime, "_drain_desktop_spool", scan_spool)
    monkeypatch.setattr(runtime, "upload_once", no_upload)
    for number in range(1, 101):
        runtime.record_background(_turn_event(number))
    await asyncio.wait_for(asyncio.gather(*tuple(runtime._record_tasks)), timeout=10)
    assert commits == 4
    assert scans == 1
    await runtime.close(flush=False)
    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        assert (await outbox.stats()).pending_events == 100
    finally:
        await outbox.close()


async def test_awaited_gateway_start_preserves_device_attribution_off_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.telemetry.contracts.common import ResultOutcome
    from opensquilla.telemetry.reliability_sink import ReliabilityEventSink

    main_thread = threading.get_ident()
    resolver_threads = []

    def resolve_identity():
        resolver_threads.append(threading.get_ident())
        return "b" * 64

    monkeypatch.setattr(runtime_module, "get_device_id", resolve_identity)
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path), env={})
    sink = ReliabilityEventSink(runtime)
    await sink.record_gateway_start(
        outcome=ResultOutcome.SUCCESS, error_code=None, failure_stage=None, duration_ms=1,
    )
    assert len(resolver_threads) == 1 and resolver_threads[0] != main_thread
    await runtime.close(flush=False)
    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        batch = await outbox.claim_batch()
        assert batch is not None
        assert json.loads(batch.events[0].payload)["device_id"] == "b" * 64
    finally:
        await outbox.close()


async def test_background_records_captured_before_disable_are_not_reauthorized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.telemetry.coordination import scope_consent_coordinator_for

    def forbidden_identity():
        raise AssertionError("obsolete observations must not resolve device identity")

    monkeypatch.setattr(runtime_module, "get_device_id", forbidden_identity)
    config = _config(tmp_path)
    runtime = ScopedTelemetryRuntime(config=config, env={})
    coordinator = scope_consent_coordinator_for(config)
    runtime.record_background(_turn_event(1))
    runtime.record_background(_turn_event(2))
    async with coordinator.transition(TelemetryScope.RELIABILITY):
        config.privacy.disable_network_observability = True
    async with coordinator.transition(TelemetryScope.RELIABILITY):
        config.privacy.disable_network_observability = False
    await runtime.close(flush=False)
    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        assert (await outbox.stats()).pending_events == 0
    finally:
        await outbox.close()


async def test_background_identity_resolution_leaves_application_loop_responsive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    resolver_threads = []
    synthetic_device_id = "a" * 64

    def delayed_identity():
        resolver_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return synthetic_device_id

    monkeypatch.setattr(runtime_module, "get_device_id", delayed_identity)
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path), env={})
    try:
        assert runtime.device_id_for(TelemetryScope.RELIABILITY) is None
        runtime.record_background(_turn_event())
        assert resolver_threads == []
        assert await asyncio.to_thread(entered.wait, 5)
        # Resolving identity in a worker must allow unrelated callbacks to run.
        await asyncio.wait_for(asyncio.sleep(0), timeout=1)
        assert resolver_threads != [threading.get_ident()]
    finally:
        release.set()
        await runtime.close(flush=False)
    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        batch = await outbox.claim_batch()
        assert batch is not None
        assert json.loads(batch.events[0].payload)["device_id"] == synthetic_device_id
    finally:
        await outbox.close()


async def test_background_batch_conflict_does_not_discard_valid_peers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "get_device_id", lambda: None)
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path), env={})
    await runtime.record(_turn_event(1))
    runtime.record_background(_turn_event(1).model_copy(update={"duration_ms": 999}))
    runtime.record_background(_turn_event(2))
    await runtime.close(flush=False)
    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        assert (await outbox.stats()).pending_events == 2
    finally:
        await outbox.close()


async def test_desktop_checkpoint_failure_does_not_skip_remaining_batch_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpointed = []

    def checkpoint(_state_dir, event):
        checkpointed.append(str(event.event_id))
        if len(checkpointed) == 1:
            raise OSError("Synthetic first checkpoint failure")

    monkeypatch.setattr(runtime_module, "record_desktop_turn", checkpoint)
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path), env={})
    runtime.record_background(_turn_event(1))
    runtime.record_background(_turn_event(2))
    await runtime.close(flush=False)
    assert checkpointed == [str(_turn_event(number).event_id) for number in (1, 2)]


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
        config=_config(state_dir),
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
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path / "state"))
    await runtime.start()
    await runtime.close()
    await runtime.close()
    with pytest.raises(RuntimeError, match="closed"):
        await runtime.record(_turn_event())


async def test_default_upload_policy_drains_desktop_growth_into_isolated_outbox(
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
        config=_config(state_dir),
        upload_interval_seconds=60,
        env={},
    )
    imported = asyncio.Event()
    run_cycle = runtime._run_upload_cycle

    async def import_cycle():
        await run_cycle()
        imported.set()

    monkeypatch.setattr(runtime, "_run_upload_cycle", import_cycle)

    await runtime.start()
    try:
        await asyncio.wait_for(imported.wait(), timeout=5)
        scoped = runtime._scopes[TelemetryScope.GROWTH]
        assert (await scoped.outbox.stats()).pending_events == 1
        assert not ready.exists()
    finally:
        await runtime.close()

    reliability = await TelemetryOutbox.open(state_dir, TelemetryScope.RELIABILITY)
    try:
        assert (await reliability.stats()).pending_events == 0
    finally:
        await reliability.close()


async def test_upload_loop_recovers_scope_initialization_on_next_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path))
    attempts = {scope: 0 for scope in TelemetryScope}
    uploaded: list[TelemetryScope] = []
    sleeping = asyncio.Event()
    tick = asyncio.Event()
    delays: list[float] = []

    async def interval_sleep():
        delays.append(runtime._upload_interval_seconds)
        sleeping.set()
        tick_task = asyncio.create_task(tick.wait())
        stop_task = asyncio.create_task(runtime._upload_stop.wait())
        try:
            await asyncio.wait({tick_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            tick_task.cancel()
            stop_task.cancel()
            await asyncio.gather(tick_task, stop_task, return_exceptions=True)
        tick.clear()

    async def scope_runtime(scope):
        attempts[scope] += 1
        if scope is TelemetryScope.RELIABILITY and attempts[scope] == 1:
            raise OSError("synthetic scope initialization failure")

        async def upload_once():
            uploaded.append(scope)

        return SimpleNamespace(uploader=SimpleNamespace(upload_once=upload_once))

    monkeypatch.setattr(runtime, "_scope_runtime", scope_runtime)
    monkeypatch.setattr(runtime, "_wait_for_upload_interval", interval_sleep)
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
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path), env={})
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
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path))
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
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path))
    # Keep SQLite initialization outside the shutdown race's watchdog.
    assert await runtime._scope_runtime(TelemetryScope.RELIABILITY) is not None
    entered = asyncio.Event()
    release = asyncio.Event()
    recorded = asyncio.Event()
    starts = 0
    record = runtime.record

    async def slow_record(event, **kwargs):
        entered.set()
        await release.wait()
        result = await record(event, **kwargs)
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
    await asyncio.wait_for(closing, timeout=10)

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
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path))
    # This race starts after recording, independently of outbox setup latency.
    assert await runtime._scope_runtime(TelemetryScope.RELIABILITY) is not None
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


async def test_start_returns_before_desktop_spool_io_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_spool():
        entered.set()
        await release.wait()

    monkeypatch.setattr(runtime, "_drain_desktop_spool", slow_spool)
    try:
        await asyncio.wait_for(runtime.start(), timeout=1)
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert runtime._upload_task is not None
        assert not release.is_set()
        await asyncio.sleep(0)
    finally:
        release.set()
        await runtime.close(flush=False)


async def test_close_uploads_records_created_after_empty_startup_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline_uploads
) -> None:
    async def accepted(request):
        return _accepted_response(request)

    offline_uploads.handler = accepted
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path), upload_interval_seconds=60)
    initial_cycle_done = asyncio.Event()
    run_cycle = runtime._run_upload_cycle

    async def initial_cycle():
        await run_cycle()
        initial_cycle_done.set()

    monkeypatch.setattr(runtime, "_run_upload_cycle", initial_cycle)
    await runtime.start()
    await asyncio.wait_for(initial_cycle_done.wait(), timeout=5)
    assert offline_uploads.requests == []
    runtime.record_background(_turn_event())
    await runtime.close()

    assert len(offline_uploads.requests) == 1
    sent = json.loads(offline_uploads.requests[0].content)
    assert [event["event_name"] for event in sent["events"]] == ["turn_result"]
    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        assert (await outbox.stats()).pending_events == 0
    finally:
        await outbox.close()


@pytest.mark.parametrize("start_upload", [False, True], ids=["final-flush", "in-flight"])
@pytest.mark.parametrize("cancel_close", [False, True], ids=["deadline", "caller-cancel"])
@pytest.mark.parametrize("pause_before_guard", [False, True], ids=["ready", "preempted"])
async def test_close_cancels_stalled_upload_and_preserves_unacknowledged_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline_uploads, cancel_close: bool,
    start_upload: bool, pause_before_guard: bool, expire_shutdown_deadline,
) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def stalled(_request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    offline_uploads.handler = stalled
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path))
    closing = None
    # Reaching the upload includes SQLite claim I/O; only cancellation after
    # the explicit deadline below is subject to the one-second assertion.
    try:
        assert (await runtime.record(_turn_event())).status is RecordStatus.RECORDED
        if start_upload:
            await runtime.start()
            await asyncio.wait_for(entered.wait(), timeout=10)
        assert (runtime._upload_task is not None) is start_upload
        monkeypatch.setattr(runtime_module, "SHUTDOWN_UPLOAD_TIMEOUT_SECONDS", 0.05)
        if pause_before_guard:
            prepare_shutdown = runtime.prepare_shutdown

            def preempted_shutdown():
                prepare_shutdown()
                # Model OS preemption after creating the guard but before it
                # can run. This is fault injection, not a readiness wait.
                started = time.monotonic()
                time.sleep(0.1)
                assert time.monotonic() - started > 0.05

            monkeypatch.setattr(runtime, "prepare_shutdown", preempted_shutdown)
        closing = asyncio.create_task(runtime.close())
        if not start_upload:
            await asyncio.wait_for(entered.wait(), timeout=10)
        assert not cancelled.is_set()
        timeouts = await expire_shutdown_deadline(runtime, cancel=not cancel_close)
        if cancel_close:
            closing.cancel()
        # Bound the upload cancellation separately from durable SQLite cleanup;
        # disk-worker scheduling must not turn a completed network cancellation
        # into a second cancellation of close().
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        if cancel_close:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(closing, timeout=10)
        else:
            await asyncio.wait_for(closing, timeout=10)
        assert any(timeout.expired() for timeout in timeouts) is not cancel_close

        assert cancelled.is_set()
        assert len(offline_uploads.requests) == 1
        assert runtime.opened_scopes == frozenset()
        await runtime.close()
    finally:
        if closing is not None:
            if not closing.done():
                closing.cancel()
            await asyncio.gather(closing, return_exceptions=True)
        await runtime.close(flush=False)
    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        assert (await outbox.stats()).pending_events == 1
    finally:
        await outbox.close()


async def test_close_only_final_upload_preserves_lease_when_close_is_cancelled(
    tmp_path: Path, offline_uploads
) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def stalled(_request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    offline_uploads.handler = stalled
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path))
    closing = None
    try:
        assert (await runtime.record(_turn_event())).status is RecordStatus.RECORDED
        assert runtime._upload_task is None
        # Keep the production two-second deadline: close() itself owns the
        # final send, without a background upload loop or its shutdown guard.
        closing = asyncio.create_task(runtime.close())
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert runtime._upload_task is None
        assert runtime._shutdown_upload_guard is None
        assert len(offline_uploads.requests) == 1
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, timeout=1)

        assert cancelled.is_set()
        assert runtime.opened_scopes == frozenset()
        outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
        try:
            stats = await outbox.stats()
            assert stats.pending_events == stats.leased_events == 1
        finally:
            await outbox.close()
    finally:
        if closing is not None:
            closing.cancel()
            await asyncio.gather(closing, return_exceptions=True)
        await runtime.close(flush=False)


async def test_close_allows_inflight_receipt_to_finish_without_releasing_lease(
    tmp_path: Path, offline_uploads
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def accepted_after_release(request):
        entered.set()
        await release.wait()
        return _accepted_response(request)

    offline_uploads.handler = accepted_after_release
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path), upload_interval_seconds=60)
    await runtime.record(_turn_event())
    # The receipt is the shutdown work under test; initialize the unrelated
    # empty scope before the upload loop and its close deadline begin.
    assert await runtime._scope_runtime(TelemetryScope.GROWTH) is not None
    await runtime.start()
    await asyncio.wait_for(entered.wait(), timeout=1)
    closing = asyncio.create_task(runtime.close())
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(closing, timeout=5)

    assert len(offline_uploads.requests) == 1
    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        assert (await outbox.stats()).pending_events == 0
    finally:
        await outbox.close()


async def test_close_closes_scopes_initialized_by_an_already_running_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path), upload_interval_seconds=60)
    entered = asyncio.Event()
    release = asyncio.Event()
    initialized = []
    scope_runtime = runtime._scope_runtime
    outboxes = {}
    upload_task = None
    closing = None

    async def delayed_open(_state_dir, scope):
        if scope is TelemetryScope.RELIABILITY:
            entered.set()
            await release.wait()
        return outboxes[scope]

    async def track_scope(scope):
        scoped = await scope_runtime(scope)
        if scoped is not None:
            initialized.append(scoped)
        return scoped

    async def idle_upload(_uploader):
        return None

    # Keep real outbox ownership/close checks, but finish cold setup before
    # gating the lazy runtime publication across the shutdown boundary.
    monkeypatch.setattr(runtime_module, "TelemetryOutbox", SimpleNamespace(open=delayed_open))
    monkeypatch.setattr(TelemetryUploader, "upload_once", idle_upload)
    monkeypatch.setattr(runtime, "_scope_runtime", track_scope)
    try:
        for scope in TelemetryScope:
            outboxes[scope] = await TelemetryOutbox.open(tmp_path, scope)
        await runtime.start()
        upload_task = runtime._upload_task
        await asyncio.wait_for(entered.wait(), timeout=1)
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        assert runtime.opened_scopes == frozenset()
        release.set()
        await asyncio.wait_for(closing, timeout=5)

        assert len(initialized) == 2
        assert all(scoped.outbox._closed and scoped.uploader._closed for scoped in initialized)
        assert runtime.opened_scopes == frozenset()
    finally:
        release.set()
        tasks = tuple(task for task in (closing, upload_task) if task is not None)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(
            *(outbox.close() for outbox in outboxes.values()), return_exceptions=True,
        )


@pytest.mark.ci_serial
def test_close_releases_stalled_send_lock_before_draining_accepted_record(tmp_path: Path) -> None:
    result = run_shutdown_probe(tmp_path, mode="close")
    assert result.timed_out is None, result.output
    assert result.returncode == 0, result.output
    assert "TELEMETRY_PROBE_PASSED" in result.output


@pytest.mark.ci_serial
def test_prepare_shutdown_releases_send_lock_and_keeps_producer_records_open(
    tmp_path: Path,
) -> None:
    result = run_shutdown_probe(tmp_path, mode="prepare")
    assert result.timed_out is None, result.output
    assert result.returncode == 0, result.output
    assert "TELEMETRY_PROBE_PASSED" in result.output


@pytest.mark.ci_serial
@pytest.mark.parametrize(
    ("fault", "failure"),
    [("no-cancel", "upload-cancelled did not become ready"),
     ("drop-record", "accepted records were lost")],
)
def test_shutdown_probe_rejects_broken_contracts(tmp_path: Path, fault: str, failure: str) -> None:
    result = run_shutdown_probe(tmp_path, fault=fault)
    assert result.timed_out is None, result.output
    assert result.returncode != 0, result.output
    assert failure in result.output
    assert "TELEMETRY_PROBE_PASSED" not in result.output


@pytest.mark.ci_serial
def test_shutdown_probe_bounds_noncooperative_cleanup(tmp_path: Path) -> None:
    result = run_shutdown_probe(tmp_path, fault="cleanup-hang", execution_seconds=2)
    assert result.timed_out == "execution/cleanup", result.output
    assert result.returncode != 0
    assert "phase=cleanup-blocked" in result.output


async def test_close_uploads_other_scope_while_inflight_request_stalls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline_uploads, expire_shutdown_deadline
) -> None:
    entered = asyncio.Event()
    accepted_growth = asyncio.Event()
    acknowledged_growth = asyncio.Event()
    cancelled_reliability = asyncio.Event()

    async def stalled_reliability(request):
        if request.url.path == "/v1/reliability/events":
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled_reliability.set()
        accepted_growth.set()
        return _accepted_response(request)

    offline_uploads.handler = stalled_reliability
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path), upload_interval_seconds=60)
    await runtime.record(_turn_event())
    growth = TELEMETRY_EVENT_ADAPTER.validate_json(
        json.dumps({
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
        }),
        strict=True,
    )
    await runtime.record(growth)
    growth_runtime = await runtime._scope_runtime(TelemetryScope.GROWTH)
    assert growth_runtime is not None
    acknowledge = growth_runtime.outbox.acknowledge

    async def acknowledge_growth(lease_id):
        result = await acknowledge(lease_id)
        acknowledged_growth.set()
        return result

    monkeypatch.setattr(growth_runtime.outbox, "acknowledge", acknowledge_growth)
    await runtime.start()
    closing = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        monkeypatch.setattr(runtime_module, "SHUTDOWN_UPLOAD_TIMEOUT_SECONDS", 0.1)
        closing = asyncio.create_task(runtime.close())
        await asyncio.wait_for(acknowledged_growth.wait(), timeout=1)
        assert not cancelled_reliability.is_set()
        timeouts = await expire_shutdown_deadline(runtime)
        assert len(timeouts) == 2
        await asyncio.wait_for(closing, timeout=1)
        assert any(timeout.expired() for timeout in timeouts)
    finally:
        if closing is None:
            await runtime.close(flush=False)
        else:
            if not closing.done():
                closing.cancel()
            await asyncio.gather(closing, return_exceptions=True)

    assert accepted_growth.is_set()
    assert cancelled_reliability.is_set()
    for scope, expected_pending in ((TelemetryScope.RELIABILITY, 1), (TelemetryScope.GROWTH, 0)):
        outbox = await TelemetryOutbox.open(tmp_path, scope)
        try:
            assert (await outbox.stats()).pending_events == expected_pending
        finally:
            await outbox.close()


async def test_close_does_not_retry_a_batch_under_server_backoff(
    tmp_path: Path, offline_uploads
) -> None:
    async def throttled(_request):
        return httpx.Response(429, headers={"Retry-After": "3600"})

    offline_uploads.handler = throttled
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path))
    await runtime.record(_turn_event())
    await runtime.upload_once(TelemetryScope.RELIABILITY)
    await runtime.close()
    assert len(offline_uploads.requests) == 1
    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        assert (await outbox.stats()).pending_events == 1
    finally:
        await outbox.close()


async def test_close_rechecks_disable_and_retains_previously_queued_event(
    tmp_path: Path, offline_uploads
) -> None:
    config = _config(tmp_path)
    runtime = ScopedTelemetryRuntime(config=config)
    await runtime.record(_turn_event())
    config.privacy.disable_network_observability = True
    await runtime.close()
    assert offline_uploads.requests == []
    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        assert (await outbox.stats()).pending_events == 1
    finally:
        await outbox.close()


async def test_close_without_flush_retains_records_and_makes_no_request(
    tmp_path: Path, offline_uploads
) -> None:
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path))
    runtime.record_background(_turn_event())
    await runtime.close(flush=False)

    assert offline_uploads.requests == []
    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        assert (await outbox.stats()).pending_events == 1
    finally:
        await outbox.close()


async def test_runtime_reliability_producers_reach_strict_collector_on_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline_uploads
) -> None:
    from opensquilla.telemetry.contracts.common import ClientSurface, ExecutionMode, ResultOutcome
    from opensquilla.telemetry.contracts.reliability import ToolCategory, ToolOutcome
    from opensquilla.telemetry.contracts.wire import TelemetryWireTarget, parse_telemetry_wire
    from opensquilla.telemetry.reliability_sink import ReliabilityEventSink
    from opensquilla.telemetry.runtime_facts import (
        reset_client_runtime_dimensions,
        set_client_runtime_dimensions,
    )

    received = []

    async def accepted(request):
        batch = parse_telemetry_wire(
            request.content, target=TelemetryWireTarget.RELIABILITY_BATCH
        )
        received.extend(batch.events)
        return _accepted_response(request)

    offline_uploads.handler = accepted
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path))
    sink = ReliabilityEventSink(runtime, app_version="1.2.3")
    token = set_client_runtime_dimensions(ClientSurface.CLI, ExecutionMode.ONE_SHOT)
    try:
        sink.observe_turn(
            SimpleNamespace(
                outcome=ResultOutcome.SUCCESS,
                error_code=None,
                failure_stage=None,
                duration_ms=100,
                ttft_ms=20,
                stall_count=0,
            )
        )
        sink.observe_tool_call(
            SimpleNamespace(
                outcome=ToolOutcome.SUCCESS,
                error_code=None,
                duration_ms=10,
                tool_category=ToolCategory.SHELL,
                retry_count=0,
            )
        )
    finally:
        reset_client_runtime_dimensions(token)

    # Producer writes are setup for the close contract.  Do not start the
    # periodic uploader, so close() owns the only request asserted below and
    # its unchanged two-second deadline excludes lazy SQLite initialization.
    async def no_periodic_upload() -> None:
        return None

    monkeypatch.setattr(runtime, "start", no_periodic_upload)
    await asyncio.wait_for(asyncio.gather(*tuple(runtime._record_tasks)), timeout=5)
    scoped = runtime._scopes[TelemetryScope.RELIABILITY]
    assert (await scoped.outbox.stats()).pending_events == 2
    assert offline_uploads.requests == []
    await runtime.close()

    assert len(offline_uploads.requests) == 1
    assert {event.event_name for event in received} == {"turn_result", "tool_call_result"}
    assert {event.surface for event in received} == {ClientSurface.CLI}
    assert {event.execution_mode for event in received} == {ExecutionMode.ONE_SHOT}
    outbox = await TelemetryOutbox.open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        assert (await outbox.stats()).pending_events == 0
    finally:
        await outbox.close()


@pytest.mark.parametrize("blocked_path", ["root", "scope"])
async def test_desktop_spool_directory_checks_keep_agent_loop_responsive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocked_path: str,
) -> None:
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path), env={})
    root = runtime_module.desktop_early_spool_root(tmp_path)
    scope_directory = root / TelemetryScope.RELIABILITY.value
    scope_directory.mkdir(parents=True)
    target = root if blocked_path == "root" else scope_directory
    entered = threading.Event()
    release = threading.Event()
    owner_thread = threading.get_ident()
    real_lstat = Path.lstat

    def blocked_lstat(path: Path, *args, **kwargs):
        if path == target:
            entered.set()
            assert threading.get_ident() != owner_thread
            assert release.wait(timeout=5), "filesystem worker was not released"
        return real_lstat(path, *args, **kwargs)

    initialized_scopes = []

    async def initialize_scope(scope):
        assert threading.get_ident() == owner_thread
        initialized_scopes.append(scope)
        return None

    monkeypatch.setattr(Path, "lstat", blocked_lstat)
    monkeypatch.setattr(runtime, "_scope_runtime", initialize_scope)
    draining = asyncio.create_task(runtime._drain_desktop_spool())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        agent_steps = []
        for step in range(3):
            await asyncio.sleep(0)
            agent_steps.append(step)
        assert agent_steps == [0, 1, 2]
        assert not draining.done()
        assert initialized_scopes == []
    finally:
        release.set()
        await draining
        await runtime.close(flush=False)

    assert initialized_scopes == [TelemetryScope.RELIABILITY]
