"""Runtime status observation owns bounded I/O without delaying mutations."""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

import opensquilla.runtime_packs as runtime_packs
from opensquilla.gateway.adapters.sandbox_runtime import (
    GatewaySandboxRuntimePackAdapter,
    _runtime_status_flights,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.gateway.websocket import handle_ws_connection
from opensquilla.runtime_packs.models import RuntimeOperationState
from tests.test_gateway.test_rpc_sandbox_runtime import _operation, _status
from tests.test_gateway.test_websocket_request_concurrency import (
    _CONNECT_FRAME,
    _HistoryWebSocket,
)


@pytest.mark.parametrize("writer_queue_enabled", [False, True])
async def test_slow_status_does_not_block_same_socket_cancel(
    monkeypatch, tmp_path, writer_queue_enabled,
):
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    reads = 0

    def status(_state):
        nonlocal reads
        reads += 1
        if reads == 1:
            entered.set()
            assert release.wait(3)
            return _status(operation=_operation())
        return _status(operation=_operation(RuntimeOperationState.CANCELLING))

    def cancel(component, operation, _state):
        assert (component, operation) == ("python", "runtime-operation-1")
        assert not release.is_set()
        cancelled.set()
        return _operation(RuntimeOperationState.CANCELLING)

    monkeypatch.setattr(runtime_packs, "status_snapshot", status)
    monkeypatch.setattr(runtime_packs, "cancel_install", cancel)
    dispatcher = get_dispatcher()

    async def before_frame(frame):
        if json.loads(frame).get("id") == "cancel":
            assert await asyncio.to_thread(entered.wait, 2)

    async def finish(socket):
        try:
            await socket.wait_for_response("cancel")
            assert cancelled.is_set()
            assert not socket.has_response("status")
        finally:
            release.set()
        await socket.wait_for_response("status")

    connect = json.loads(_CONNECT_FRAME)
    connect["params"]["scopes"] = ["operator.admin"]
    socket = _HistoryWebSocket([
        json.dumps(connect),
        json.dumps({"type": "req", "id": "status", "method": "sandbox.runtime.status"}),
        json.dumps({"type": "req", "id": "cancel", "method": "sandbox.runtime.cancel",
                    "params": {"componentId": "python", "operationId": "runtime-operation-1"}}),
    ], dispatcher, before_frame=before_frame, after_frames=finish)
    try:
        await asyncio.wait_for(handle_ws_connection(
            socket, GatewayConfig(state_dir=str(tmp_path),
                                  ws_writer_queue_enabled=writer_queue_enabled),
            dispatcher=dispatcher,
        ), 4)
    finally:
        release.set()
    responses = socket.responses()
    assert [frame["id"] for frame in responses] == ["cancel", "status"]
    assert all(frame["ok"] for frame in responses)
    assert reads == 2  # The mutation invalidates the snapshot already in flight.
    assert "sandbox.runtime.status" in socket.hello()["policy"]["concurrent_optional_read_methods"]
    assert asyncio.get_running_loop() not in _runtime_status_flights


async def test_observer_cancellation_does_not_release_physical_read(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = 0

    def status(_state):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(3)
        return _status()

    monkeypatch.setattr(runtime_packs, "status_snapshot", status)
    first = GatewaySandboxRuntimePackAdapter(tmp_path)
    replacement = GatewaySandboxRuntimePackAdapter(tmp_path)
    observers = [asyncio.create_task(first.read_runtime_status()) for _ in range(2)]
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for task in observers:
            task.cancel()
        await asyncio.gather(*observers, return_exceptions=True)
        replacement_read = asyncio.create_task(replacement.read_runtime_status())
        observers.append(replacement_read)
        await asyncio.sleep(0)
        assert calls == 1
        assert len(_runtime_status_flights[asyncio.get_running_loop()]) == 1
        release.set()
        await replacement_read
        assert asyncio.get_running_loop() not in _runtime_status_flights
        await replacement.read_runtime_status()
        assert calls == 2
    finally:
        release.set()
        await asyncio.gather(*observers, return_exceptions=True)


async def test_socket_disconnect_then_reconnect_joins_same_physical_status(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    reads = 0

    def status(_state):
        nonlocal reads
        reads += 1
        entered.set()
        assert release.wait(3)
        return _status()

    monkeypatch.setattr(runtime_packs, "status_snapshot", status)
    config = GatewayConfig(state_dir=str(tmp_path))
    dispatcher = get_dispatcher()

    async def leave_while_pending(_socket):
        assert await asyncio.to_thread(entered.wait, 2)

    def socket(after_frames):
        return _HistoryWebSocket([
            _CONNECT_FRAME,
            json.dumps({"type": "req", "id": "status", "method": "sandbox.runtime.status"}),
        ], dispatcher, after_frames=after_frames)

    first = socket(leave_while_pending)
    try:
        await handle_ws_connection(first, config, dispatcher=dispatcher)
        assert not first.has_response("status")
        assert len(_runtime_status_flights[asyncio.get_running_loop()]) == 1

        async def finish_replacement(replacement):
            await asyncio.sleep(0)
            assert reads == 1
            assert not replacement.has_response("status")
            release.set()
            await replacement.wait_for_response("status")

        replacement = socket(finish_replacement)
        await handle_ws_connection(replacement, config, dispatcher=dispatcher)
        assert replacement.responses()[0]["ok"]
        assert reads == 1
        assert asyncio.get_running_loop() not in _runtime_status_flights
    finally:
        release.set()


async def test_cancelled_observer_failure_is_consumed_and_next_read_can_retry(
    monkeypatch, tmp_path,
):
    entered, release = threading.Event(), threading.Event()
    observed_errors = []
    loop = asyncio.get_running_loop()
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, detail: observed_errors.append(detail))

    def failed_status(_state):
        entered.set()
        assert release.wait(3)
        raise OSError("synthetic status read failed")

    monkeypatch.setattr(runtime_packs, "status_snapshot", failed_status)
    adapter = GatewaySandboxRuntimePackAdapter(tmp_path)
    observer = asyncio.create_task(adapter.read_runtime_status())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        flight = next(iter(_runtime_status_flights[loop].values()))
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
        release.set()
        with pytest.raises(OSError, match="synthetic"):
            await asyncio.shield(flight.future)
        await asyncio.sleep(0)
        assert loop not in _runtime_status_flights
        assert not observed_errors
        monkeypatch.setattr(runtime_packs, "status_snapshot", lambda _: _status())
        await adapter.read_runtime_status()
    finally:
        release.set()
        await asyncio.gather(observer, return_exceptions=True)
        loop.set_exception_handler(old_handler)


async def test_different_state_roots_do_not_share_status(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        runtime_packs, "status_snapshot", lambda state: calls.append(state) or _status(),
    )
    await asyncio.gather(*(
        GatewaySandboxRuntimePackAdapter(tmp_path / name).read_runtime_status()
        for name in ("one", "two")
    ))
    assert set(calls) == {tmp_path / "one", tmp_path / "two"}
    assert asyncio.get_running_loop() not in _runtime_status_flights


def test_event_loop_shutdown_drains_physical_status_and_releases_flight(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    loops = []

    def status(_state):
        entered.set()
        assert release.wait(3)
        return _status()

    monkeypatch.setattr(runtime_packs, "status_snapshot", status)

    async def observe_and_leave():
        loop = asyncio.get_running_loop()
        loops.append(loop)
        observer = asyncio.create_task(
            GatewaySandboxRuntimePackAdapter(tmp_path).read_runtime_status(),
        )
        assert await asyncio.to_thread(entered.wait, 2)
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
        assert loop in _runtime_status_flights
        # asyncio.run shuts down its default executor after this coroutine ends.
        loop.call_later(0.01, release.set)

    try:
        asyncio.run(observe_and_leave())
    finally:
        release.set()
    assert loops[0].is_closed()
    assert loops[0] not in _runtime_status_flights
