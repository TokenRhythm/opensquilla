"""Multi-client remote-control tests: same-session broadcast, cross-client
approval closure, and reconnect snapshot catch-up.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.event_bridge import EventBridge
from opensquilla.gateway.websocket import SubscriptionManager, get_registry

_OWNER_PEER = ("127.0.0.1", 51000)
_KEY = "agent:main:webchat:guest:abcd:multi-client"


@pytest.fixture(autouse=True)
def _hermetic_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "workspace").mkdir(parents=True)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(home / "logs"))
    monkeypatch.setenv("OPENSQUILLA_WORKSPACE_DIR", str(home / "workspace"))
    config_path = tmp_path / "synthetic-config.toml"
    config_path.write_text("# synthetic multi-client test config\n", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(config_path))


_SUBSCRIPTION_MANAGER: SubscriptionManager | None = None


def _app() -> TestClient:
    global _SUBSCRIPTION_MANAGER
    _SUBSCRIPTION_MANAGER = SubscriptionManager()
    return TestClient(
        create_gateway_app(
            GatewayConfig(),
            subscription_manager=_SUBSCRIPTION_MANAGER,
        ),
        base_url="http://127.0.0.1:18791",
        client=_OWNER_PEER,
    )


def _bridge() -> EventBridge:
    return EventBridge(
        subscription_manager=_SUBSCRIPTION_MANAGER,
        connection_registry=get_registry(),
    )


def _ws_connect(client: TestClient):
    ws = client.websocket_connect("/ws")
    ws.__enter__()
    ws.receive_json()  # challenge
    ws.send_json({
        "type": "req", "id": "1", "method": "connect",
        "params": {
            "minProtocol": 3, "maxProtocol": 4,
            "client": {"name": "test", "version": "1"},
        },
    })
    hello = ws.receive_json()
    assert hello.get("type") == "hello-ok", hello
    return ws


def _subscribe(ws, key: str, req_id: str) -> None:
    ws.send_json({
        "type": "req", "id": req_id, "method": "sessions.messages.subscribe",
        "params": {"key": key, "fast_ack": True},
    })
    ws.receive_json()  # subscription ack/res


def _drain_until(ws, predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = ws.receive_json()
        if predicate(frame):
            return frame
    raise AssertionError("drain timeout waiting for frame")


def test_two_clients_receive_same_session_stream() -> None:
    client = _app()
    ws1 = _ws_connect(client)
    ws2 = _ws_connect(client)
    _subscribe(ws1, _KEY, "2")
    _subscribe(ws2, _KEY, "2")

    bridge = _bridge()
    import asyncio
    asyncio.run(bridge.emit(_KEY, "session.event.text_delta", {"text": "hello"}))

    def _is_delta(frame) -> bool:
        return (
            frame.get("type") == "event"
            and frame.get("event") == "session.event.text_delta"
        )

    f1 = _drain_until(ws1, _is_delta)
    f2 = _drain_until(ws2, _is_delta)
    assert f1["payload"]["text"] == "hello"
    assert f2["payload"]["text"] == "hello"
    ws1.__exit__(None, None, None)
    ws2.__exit__(None, None, None)


def test_approval_resolution_closes_both_clients() -> None:
    import asyncio

    from opensquilla.application.approval_queue import get_approval_queue, reset_approval_queue
    from opensquilla.gateway.approval_events import register_approval_event_bridge

    reset_approval_queue()
    queue = get_approval_queue()
    client = _app()
    ws1 = _ws_connect(client)
    ws2 = _ws_connect(client)

    bridge = _bridge()
    scheduled: list = []
    remove = register_approval_event_bridge(queue, bridge, schedule=scheduled.append)
    try:
        approval_id = queue.request(
            namespace="exec",
            params={"toolName": "exec_command", "command": "echo hi"},
        )
        while scheduled:
            asyncio.run(scheduled.pop())

        def _requested(frame) -> bool:
            return frame.get("type") == "event" and frame.get("event") == "exec.approval.requested"

        _drain_until(ws1, _requested)
        _drain_until(ws2, _requested)

        # One client resolves; both clients must observe the resolved event.
        ws1.send_json({
            "type": "req", "id": "9",
            "method": "exec.approval.resolve",
            "params": {"id": approval_id, "approved": True},
        })
        ws1.receive_json()  # resolve res
        while scheduled:
            asyncio.run(scheduled.pop())

        def _resolved(frame) -> bool:
            return frame.get("type") == "event" and frame.get("event") == "exec.approval.resolved"

        _drain_until(ws1, _resolved)
        _drain_until(ws2, _resolved)
    finally:
        remove()
        reset_approval_queue()
        ws1.__exit__(None, None, None)
        ws2.__exit__(None, None, None)


def test_reconnect_snapshot_catches_up_missed_events() -> None:
    import asyncio

    client = _app()
    ws1 = _ws_connect(client)
    _subscribe(ws1, _KEY, "2")

    bridge = _bridge()
    # Emit while client 1 is connected so the events land in the stream buffer.
    asyncio.run(bridge.emit(_KEY, "session.event.text_delta", {"text": "one"}))
    asyncio.run(bridge.emit(_KEY, "session.event.text_delta", {"text": "two"}))
    _drain_until(ws1, lambda f: f.get("event") == "session.event.text_delta")
    _drain_until(ws1, lambda f: f.get("event") == "session.event.text_delta")
    ws1.__exit__(None, None, None)

    # A fresh client subscribes and reads the snapshot: the buffered deltas
    # must be present so a phone that reconnects catches up.
    ws2 = _ws_connect(client)
    ws2.send_json({
        "type": "req", "id": "2", "method": "sessions.messages.snapshot",
        "params": {"key": _KEY},
    })
    snapshot = ws2.receive_json()
    assert snapshot.get("ok") is True, snapshot
    events = snapshot["payload"].get("events") or []
    texts = [
        event.get("payload", {}).get("text")
        for event in events
        if event.get("event") == "session.event.text_delta"
    ]
    merged = "".join(texts)
    assert "one" in merged
    assert "two" in merged
    ws2.__exit__(None, None, None)
