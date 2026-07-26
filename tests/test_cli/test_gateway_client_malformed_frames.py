"""Regression tests: malformed gateway frames must not abort the client.

The original ``GatewayClient`` decoded every WebSocket frame with a
bare ``json.loads``. A single malformed frame from the server or an
intervening proxy raised ``JSONDecodeError`` that aborted the
connection with no retry. The fix wraps each ``json.loads`` in a
``try/except JSONDecodeError`` so:

* during handshake the client fails fast with a clean SystemExit
  message naming the bad frame instead of a raw traceback;
* during the listener loop the bad frame is reported as a clean
  ``ConnectionError`` rather than crashing the listener task.
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.cli.gateway_client import GatewayClient


class _FakeWebSocket:
    def __init__(self, recv_frames: list[Any]) -> None:
        self._recv_frames = list(recv_frames)
        self.sent: list[str] = []
        self.closed = False

    async def recv(self) -> str:
        if not self._recv_frames:
            raise AssertionError("unexpected recv() call")
        return self._recv_frames.pop(0)

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> _FakeWebSocket:
        return self

    async def __anext__(self) -> str:
        # pop from the front until exhausted
        if not self._recv_frames:
            raise StopAsyncIteration
        return self._recv_frames.pop(0)


def _install_fake_websockets(
    monkeypatch: pytest.MonkeyPatch, ws: _FakeWebSocket
) -> None:
    async def _connect(url: str) -> _FakeWebSocket:
        return ws

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=_connect))


def test_connect_challenge_handshake_malformed_json_raises_systemexit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed challenge frame must surface as a clean SystemExit."""

    ws = _FakeWebSocket(["{not valid json"])
    _install_fake_websockets(monkeypatch, ws)

    client = GatewayClient()
    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(client.connect())
    assert "Malformed handshake frame" in str(excinfo.value)


def test_connect_hello_handshake_malformed_json_raises_systemexit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed hello-ok frame must surface as a clean SystemExit."""

    ws = _FakeWebSocket(
        [
            json.dumps(
                {"type": "event", "event": "connect.challenge", "payload": {}}
            ),
            "{still not json",
        ]
    )
    _install_fake_websockets(monkeypatch, ws)

    client = GatewayClient()
    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(client.connect())
    assert "Malformed hello frame" in str(excinfo.value)


@pytest.mark.asyncio
async def test_listener_skips_malformed_frame_and_fails_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad frame mid-stream must not crash the listener — it fails
    any pending RPCs and surfaces a clean connection error."""

    ws = _FakeWebSocket([b"not-json-at-all".decode("utf-8")])
    client = GatewayClient()
    client._ws = ws  # noqa: SLF001
    # No pending futures, but a connection error should still be set.
    await client._listen()  # noqa: SLF001
    assert client._connection_error is not None  # noqa: SLF001
    msg = str(client._connection_error)  # noqa: SLF001
    assert "malformed" in msg.lower()


@pytest.mark.asyncio
async def test_listener_fails_pending_on_non_dict_frame() -> None:
    """A valid-JSON non-object frame is still a protocol error.

    It cannot carry a response id, so ignoring it while the WebSocket
    remains open could leave an in-flight RPC waiting forever.
    """

    ws = _FakeWebSocket(["[1, 2, 3]"])
    client = GatewayClient()
    client._ws = ws  # noqa: SLF001

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict] = loop.create_future()
    client._pending["req-1"] = fut  # noqa: SLF001

    await client._listen()  # noqa: SLF001

    assert client._connection_error is not None  # noqa: SLF001
    assert "non-object" in str(client._connection_error).lower()  # noqa: SLF001
    assert fut.done(), "pending RPC future must not be left hanging"
    with pytest.raises(ConnectionError):
        fut.result()
    assert not client._pending  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "res_frame",
    [
        {"type": "res", "ok": True},  # id missing entirely
        {"type": "res", "id": 42, "ok": True},  # non-string id
        {"type": "res", "id": None, "ok": True},  # null id
    ],
)
async def test_listener_fails_pending_on_res_frame_without_valid_id(
    res_frame: dict,
) -> None:
    """A ``res`` frame with a missing/non-string id can never be matched
    to its pending request. Silently skipping it would leave the
    matching ``_call()`` future pending forever — the listener must
    treat it as a protocol error and fail all in-flight RPCs."""

    ws = _FakeWebSocket([json.dumps(res_frame)])
    client = GatewayClient()
    client._ws = ws  # noqa: SLF001

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict] = loop.create_future()
    client._pending["req-1"] = fut  # noqa: SLF001

    await client._listen()  # noqa: SLF001

    assert client._connection_error is not None  # noqa: SLF001
    assert fut.done(), "pending RPC future must not be left hanging"
    with pytest.raises(ConnectionError):
        fut.result()
    assert not client._pending  # noqa: SLF001
