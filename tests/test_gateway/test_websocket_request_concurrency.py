"""Regression coverage for safe concurrent WebSocket request dispatch."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from starlette.websockets import WebSocketDisconnect, WebSocketState

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.protocol import make_ok_res
from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.gateway.websocket import handle_ws_connection
from opensquilla.skills.loader import SkillLoader

_CONNECT_FRAME = json.dumps(
    {
        "type": "req",
        "id": "h",
        "method": "connect",
        "params": {"minProtocol": 1, "role": "operator", "auth": {}},
    }
)


class _HistoryDispatcher:
    def __init__(self) -> None:
        self.history_started = asyncio.Event()
        self.history_cancelled = asyncio.Event()
        self.release_history = asyncio.Event()
        self.quick_dispatched = asyncio.Event()

    def list_methods(self) -> list[str]:
        return ["chat.history", "noop"]

    async def dispatch(self, req_id: str, method: str, params: Any, ctx: Any) -> Any:
        if method == "chat.history":
            self.history_started.set()
            try:
                await self.release_history.wait()
            except asyncio.CancelledError:
                self.history_cancelled.set()
                raise
        elif method == "noop":
            self.quick_dispatched.set()
        return make_ok_res(req_id, {"method": method})


class _ConcurrentHistoryDispatcher:
    def __init__(self, held_history_ids: set[str]) -> None:
        self.held_history_ids = frozenset(held_history_ids)
        self.history_started = {
            req_id: asyncio.Event() for req_id in self.held_history_ids
        }
        self.release_history = {
            req_id: asyncio.Event() for req_id in self.held_history_ids
        }
        self.quick_dispatched = asyncio.Event()

    def list_methods(self) -> list[str]:
        return ["chat.history", "noop"]

    async def dispatch(self, req_id: str, method: str, params: Any, ctx: Any) -> Any:
        if method == "chat.history":
            started = self.history_started.setdefault(req_id, asyncio.Event())
            started.set()
            release = self.release_history.get(req_id)
            if release is not None:
                await release.wait()
        elif method == "noop":
            self.quick_dispatched.set()
        return make_ok_res(req_id, {"method": method})

    async def wait_for_histories(self, *req_ids: str) -> None:
        await asyncio.gather(
            *(self.history_started[req_id].wait() for req_id in req_ids)
        )

    def release(self, *req_ids: str) -> None:
        for req_id in req_ids:
            self.release_history[req_id].set()


class _ConcurrentOptionalReadDispatcher:
    def __init__(self, held_request_ids: set[str]) -> None:
        self.held_request_ids = frozenset(held_request_ids)
        self.request_started = {req_id: asyncio.Event() for req_id in self.held_request_ids}
        self.release_request = {req_id: asyncio.Event() for req_id in self.held_request_ids}
        self.quick_dispatched = asyncio.Event()

    def list_methods(self) -> list[str]:
        return ["sessions.list", "noop"]

    async def dispatch(self, req_id: str, method: str, params: Any, ctx: Any) -> Any:
        if req_id in self.held_request_ids:
            self.request_started[req_id].set()
            await self.release_request[req_id].wait()
        elif method == "noop":
            self.quick_dispatched.set()
        return make_ok_res(req_id, {"method": method})

    async def wait_for_requests(self, *req_ids: str) -> None:
        await asyncio.gather(*(self.request_started[req_id].wait() for req_id in req_ids))

    def release(self, *req_ids: str) -> None:
        for req_id in req_ids:
            self.release_request[req_id].set()


class _HistoryWebSocket:
    client_state = WebSocketState.CONNECTED
    client = SimpleNamespace(host="127.0.0.1", port=12345)

    def __init__(
        self,
        frames: list[str],
        dispatcher: Any,
        *,
        release_after_quick_response: bool = False,
        after_frames: Callable[[_HistoryWebSocket], Awaitable[None]] | None = None,
        before_frame: Callable[[str], Awaitable[None]] | None = None,
        fail_response_id: str | None = None,
    ) -> None:
        self._frames = list(frames)
        self.dispatcher = dispatcher
        self.release_after_quick_response = release_after_quick_response
        self.after_frames = after_frames
        self.before_frame = before_frame
        self.fail_response_id = fail_response_id
        self.sent: list[str] = []
        self.close_codes: list[int] = []
        self.close_event = asyncio.Event()
        self.quick_response_sent = asyncio.Event()
        self.history_response_sent = asyncio.Event()
        self._response_events: dict[str, asyncio.Event] = {}

    async def accept(self) -> None:
        return None

    async def send_text(self, text: str) -> None:
        frame = json.loads(text)
        if frame.get("type") == "res" and frame.get("id") == self.fail_response_id:
            raise RuntimeError("synthetic detached response send failure")
        self.sent.append(text)
        if frame.get("type") != "res":
            return
        req_id = str(frame.get("id", ""))
        self._response_events.setdefault(req_id, asyncio.Event()).set()
        if req_id == "quick":
            self.quick_response_sent.set()
        elif req_id == "history":
            self.history_response_sent.set()

    async def receive_text(self) -> str:
        if self._frames:
            frame = self._frames.pop(0)
            if self.before_frame is not None:
                await self.before_frame(frame)
            return frame
        if self.after_frames is not None:
            await self.after_frames(self)
            raise WebSocketDisconnect(code=1000)
        await asyncio.wait_for(self.dispatcher.history_started.wait(), timeout=1)
        if self.release_after_quick_response:
            await asyncio.wait_for(self.dispatcher.quick_dispatched.wait(), timeout=1)
            await asyncio.wait_for(self.quick_response_sent.wait(), timeout=1)
            self.dispatcher.release_history.set()
            await asyncio.wait_for(self.history_response_sent.wait(), timeout=1)
        raise WebSocketDisconnect(code=1000)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_codes.append(code)
        self.close_event.set()

    def responses(self) -> list[dict[str, Any]]:
        return [frame for frame in map(json.loads, self.sent) if frame.get("type") == "res"]

    def hello(self) -> dict[str, Any]:
        return next(
            frame
            for frame in map(json.loads, self.sent)
            if frame.get("type") == "hello-ok"
        )

    async def wait_for_response(self, req_id: str) -> None:
        event = self._response_events.setdefault(req_id, asyncio.Event())
        await asyncio.wait_for(event.wait(), timeout=1)

    def has_response(self, req_id: str) -> bool:
        event = self._response_events.get(req_id)
        return event is not None and event.is_set()


@pytest.mark.parametrize("writer_queue_enabled", [False, True])
async def test_blocked_skill_install_can_be_cancelled_on_same_websocket(
    tmp_path,
    writer_queue_enabled: bool,
) -> None:
    entered = asyncio.Event()
    cleaned_up = asyncio.Event()

    class _Installer:
        async def install(self, *_args, **_kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned_up.set()

    operation_id = str(uuid4())

    async def wait_until_install_is_active(frame: str) -> None:
        decoded = json.loads(frame)
        if decoded.get("id") == "cancel":
            await asyncio.wait_for(entered.wait(), timeout=1)

    async def finish_after_responses(socket: _HistoryWebSocket) -> None:
        await socket.wait_for_response("install")
        await socket.wait_for_response("cancel")

    ws = _HistoryWebSocket(
        [
            _CONNECT_FRAME,
            json.dumps({
                "type": "req",
                "id": "install",
                "method": "skills.install",
                "params": {"identifier": "demo", "operationId": operation_id},
            }),
            json.dumps({
                "type": "req",
                "id": "cancel",
                "method": "skills.install.cancel",
                "params": {"operationId": operation_id},
            }),
        ],
        get_dispatcher(),
        before_frame=wait_until_install_is_active,
        after_frames=finish_after_responses,
    )
    loader = SkillLoader(
        managed_dir=tmp_path / "managed",
        snapshot_path=tmp_path / "snapshot.json",
    )
    loader.load_all()
    skill_management_state: dict[str, Any] = {}

    await asyncio.wait_for(
        handle_ws_connection(
            ws,
            GatewayConfig(ws_writer_queue_enabled=writer_queue_enabled),
            dispatcher=get_dispatcher(),
            skill_loader=loader,
            skill_management_service=_Installer(),
            skill_management_state=skill_management_state,
        ),
        timeout=2,
    )

    responses = {frame["id"]: frame for frame in ws.responses()}
    assert responses["install"]["payload"]["cancelled"] is True
    assert responses["cancel"]["payload"]["cancelled"] is True
    assert responses["cancel"]["payload"]["pending"] is False
    assert cleaned_up.is_set()
    assert skill_management_state["_active_skill_installs"] == {}
    assert ws.close_codes == []


@pytest.mark.parametrize("writer_queue_enabled", [False, True])
async def test_legacy_skill_install_without_operation_id_remains_serialized(
    tmp_path,
    writer_queue_enabled: bool,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    observed: dict[str, bool] = {}

    class _Installer:
        async def install(self, *_args, **_kwargs):
            entered.set()
            await release.wait()
            return SimpleNamespace(
                success=True,
                name="demo",
                message="installed",
                path=None,
                scan=None,
            )

    async def finish_after_responses(socket: _HistoryWebSocket) -> None:
        await socket.wait_for_response("install")
        await socket.wait_for_response("quick")

    ws = _HistoryWebSocket(
        [
            _CONNECT_FRAME,
            json.dumps({
                "type": "req",
                "id": "install",
                "method": "skills.install",
                "params": {"identifier": "demo"},
            }),
            json.dumps({"type": "req", "id": "quick", "method": "health"}),
        ],
        get_dispatcher(),
        after_frames=finish_after_responses,
    )
    loader = SkillLoader(
        managed_dir=tmp_path / "managed",
        snapshot_path=tmp_path / "snapshot.json",
    )
    loader.load_all()

    async def release_after_serialization_check() -> None:
        await asyncio.wait_for(entered.wait(), timeout=1)
        try:
            await asyncio.wait_for(ws.quick_response_sent.wait(), timeout=0.05)
            observed["quick_overtook_install"] = True
        except TimeoutError:
            observed["quick_overtook_install"] = False
        finally:
            release.set()

    observer = asyncio.create_task(release_after_serialization_check())
    try:
        await asyncio.wait_for(
            handle_ws_connection(
                ws,
                GatewayConfig(ws_writer_queue_enabled=writer_queue_enabled),
                dispatcher=get_dispatcher(),
                skill_loader=loader,
                skill_management_service=_Installer(),
                skill_management_state={},
            ),
            timeout=2,
        )
    finally:
        await observer

    responses = {frame["id"]: frame for frame in ws.responses()}
    assert observed["quick_overtook_install"] is False
    assert responses["install"]["payload"]["success"] is True
    assert responses["quick"]["payload"]["status"] == "ok"
    assert ws.close_codes == []


@pytest.mark.parametrize("writer_queue_enabled", [False, True])
async def test_slow_chat_history_does_not_block_later_interactive_rpc(
    writer_queue_enabled: bool,
) -> None:
    dispatcher = _HistoryDispatcher()
    ws = _HistoryWebSocket(
        [
            _CONNECT_FRAME,
            json.dumps({
                "type": "req",
                "id": "history",
                "method": "chat.history",
                "params": {"sessionKey": "agent:main:webchat:slow-history"},
            }),
            json.dumps({"type": "req", "id": "quick", "method": "noop"}),
        ],
        dispatcher,
        release_after_quick_response=True,
    )

    await asyncio.wait_for(
        handle_ws_connection(
            ws,
            GatewayConfig(ws_writer_queue_enabled=writer_queue_enabled),
            dispatcher=dispatcher,
        ),
        timeout=2,
    )

    responses = ws.responses()
    quick_index = next(i for i, frame in enumerate(responses) if frame["id"] == "quick")
    history_index = next(i for i, frame in enumerate(responses) if frame["id"] == "history")
    assert quick_index < history_index
    assert ws.hello()["policy"]["concurrent_history_reads"] is True
    assert ws.close_codes == []


@pytest.mark.parametrize("writer_queue_enabled", [False, True])
async def test_slow_history_does_not_block_another_history_or_noop(
    writer_queue_enabled: bool,
) -> None:
    dispatcher = _ConcurrentHistoryDispatcher({"history-a"})
    observed: dict[str, bool] = {}

    async def finish_after_concurrent_responses(socket: _HistoryWebSocket) -> None:
        await dispatcher.wait_for_histories("history-a")
        await socket.wait_for_response("history-b")
        await socket.wait_for_response("quick")
        observed["history_a_still_pending"] = not socket.has_response("history-a")
        dispatcher.release("history-a")
        await socket.wait_for_response("history-a")

    ws = _HistoryWebSocket(
        [
            _CONNECT_FRAME,
            json.dumps({
                "type": "req",
                "id": "history-a",
                "method": "chat.history",
                "params": {"sessionKey": "agent:main:webchat:slow-history-a"},
            }),
            json.dumps({
                "type": "req",
                "id": "history-b",
                "method": "chat.history",
                "params": {"sessionKey": "agent:main:webchat:quick-history-b"},
            }),
            json.dumps({"type": "req", "id": "quick", "method": "noop"}),
        ],
        dispatcher,
        after_frames=finish_after_concurrent_responses,
    )

    await asyncio.wait_for(
        handle_ws_connection(
            ws,
            GatewayConfig(ws_writer_queue_enabled=writer_queue_enabled),
            dispatcher=dispatcher,
        ),
        timeout=2,
    )

    responses = ws.responses()
    response_indexes = {
        frame["id"]: index
        for index, frame in enumerate(responses)
        if frame["id"] in {"history-a", "history-b", "quick"}
    }
    assert observed["history_a_still_pending"]
    assert response_indexes["history-b"] < response_indexes["history-a"]
    assert response_indexes["quick"] < response_indexes["history-a"]
    assert ws.close_codes == []


@pytest.mark.parametrize("writer_queue_enabled", [False, True])
async def test_webui_bootstrap_optional_reads_do_not_reject_catalog_or_block_interactive_rpc(
    writer_queue_enabled: bool,
) -> None:
    requests = (
        ("drafts", "meta.drafts.list"),
        ("workspaces", "workspaces.list"),
        ("onboarding", "onboarding.status"),
        ("run-mode", "sandbox.run_mode.preference.get"),
        ("config", "config.get"),
        ("commands", "commands.list_for_surface"),
        ("agents", "agents.list"),
        ("sessions", "sessions.list"),
    )
    request_ids = tuple(req_id for req_id, _method in requests)
    dispatcher = _ConcurrentOptionalReadDispatcher(set(request_ids))
    observed: dict[str, bool] = {}

    async def finish_after_quick_response(socket: _HistoryWebSocket) -> None:
        await dispatcher.wait_for_requests(*request_ids)
        await socket.wait_for_response("quick")
        observed["reads_still_pending"] = all(
            not socket.has_response(req_id) for req_id in request_ids
        )
        dispatcher.release(*request_ids)
        await asyncio.gather(*(socket.wait_for_response(req_id) for req_id in request_ids))

    frames = [_CONNECT_FRAME]
    frames.extend(
        json.dumps(
            {
                "type": "req",
                "id": req_id,
                "method": method,
                "params": {},
            }
        )
        for req_id, method in requests
    )
    frames.append(json.dumps({"type": "req", "id": "quick", "method": "noop"}))
    ws = _HistoryWebSocket(
        frames,
        dispatcher,
        after_frames=finish_after_quick_response,
    )

    await asyncio.wait_for(
        handle_ws_connection(
            ws,
            GatewayConfig(ws_writer_queue_enabled=writer_queue_enabled),
            dispatcher=dispatcher,
        ),
        timeout=2,
    )

    assert observed["reads_still_pending"]
    assert dispatcher.quick_dispatched.is_set()
    assert ws.hello()["policy"]["concurrent_optional_read_methods"] == [
        "agents.list",
        "artifacts.list",
        "commands.list_for_surface",
        "config.get",
        "models.routing.get",
        "onboarding.status",
        "sandbox.run_mode.preference.get",
        "sessions.list",
        "usage.status",
        "workspaces.list",
    ]
    assert ws.close_codes == []


@pytest.mark.parametrize("writer_queue_enabled", [False, True])
async def test_detached_history_limit_rejects_fifth_without_blocking_noop(
    writer_queue_enabled: bool,
) -> None:
    held_history_ids = tuple(f"history-{index}" for index in range(1, 5))
    dispatcher = _ConcurrentHistoryDispatcher(set(held_history_ids))
    observed: dict[str, bool] = {}

    async def finish_after_busy_and_noop_responses(socket: _HistoryWebSocket) -> None:
        await dispatcher.wait_for_histories(*held_history_ids)
        await socket.wait_for_response("history-5")
        await socket.wait_for_response("quick")
        observed["held_histories_still_pending"] = all(
            not socket.has_response(req_id) for req_id in held_history_ids
        )
        dispatcher.release(*held_history_ids)
        await asyncio.gather(
            *(socket.wait_for_response(req_id) for req_id in held_history_ids)
        )

    frames = [_CONNECT_FRAME]
    frames.extend(
        json.dumps({
            "type": "req",
            "id": req_id,
            "method": "chat.history",
            "params": {"sessionKey": f"agent:main:webchat:{req_id}"},
        })
        for req_id in (*held_history_ids, "history-5")
    )
    frames.append(json.dumps({"type": "req", "id": "quick", "method": "noop"}))
    ws = _HistoryWebSocket(
        frames,
        dispatcher,
        after_frames=finish_after_busy_and_noop_responses,
    )

    await asyncio.wait_for(
        handle_ws_connection(
            ws,
            GatewayConfig(ws_writer_queue_enabled=writer_queue_enabled),
            dispatcher=dispatcher,
        ),
        timeout=2,
    )

    responses = ws.responses()
    responses_by_id = {frame["id"]: frame for frame in responses}
    busy_response = responses_by_id["history-5"]
    assert observed["held_histories_still_pending"]
    assert "history-5" not in dispatcher.history_started
    assert busy_response["ok"] is False
    assert busy_response["error"]["code"] == "STORAGE_BUSY"
    assert busy_response["error"]["retryable"] is True
    assert busy_response["error"]["retry_after_ms"] == 100
    assert responses_by_id["quick"]["ok"] is True
    assert dispatcher.quick_dispatched.is_set()
    assert ws.close_codes == []


async def test_disconnect_cancels_in_flight_detached_history() -> None:
    dispatcher = _HistoryDispatcher()
    ws = _HistoryWebSocket(
        [
            _CONNECT_FRAME,
            json.dumps({
                "type": "req",
                "id": "history",
                "method": "chat.history",
                "params": {"sessionKey": "agent:main:webchat:disconnect-history"},
            }),
        ],
        dispatcher,
        release_after_quick_response=False,
    )

    await asyncio.wait_for(
        handle_ws_connection(
            ws,
            GatewayConfig(ws_writer_queue_enabled=True),
            dispatcher=dispatcher,
        ),
        timeout=2,
    )

    assert dispatcher.history_cancelled.is_set()
    assert all(frame["id"] != "history" for frame in ws.responses())


@pytest.mark.parametrize("writer_queue_enabled", [False, True])
async def test_detached_history_send_failure_closes_connection(
    writer_queue_enabled: bool,
) -> None:
    dispatcher = _ConcurrentHistoryDispatcher(set())

    async def finish_after_close(socket: _HistoryWebSocket) -> None:
        await asyncio.wait_for(socket.close_event.wait(), timeout=1)

    ws = _HistoryWebSocket(
        [
            _CONNECT_FRAME,
            json.dumps({
                "type": "req",
                "id": "history-fail",
                "method": "chat.history",
                "params": {"sessionKey": "agent:main:webchat:history-fail"},
            }),
        ],
        dispatcher,
        after_frames=finish_after_close,
        fail_response_id="history-fail",
    )

    await asyncio.wait_for(
        handle_ws_connection(
            ws,
            GatewayConfig(ws_writer_queue_enabled=writer_queue_enabled),
            dispatcher=dispatcher,
        ),
        timeout=2,
    )

    assert ws.close_codes == [1011]
    assert all(frame["id"] != "history-fail" for frame in ws.responses())
