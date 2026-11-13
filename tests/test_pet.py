"""Unit + fake-bridge contract tests for the in-process pet service (PR-2/3).

All offline: the native bridge is replaced by an httpx.MockTransport fake, the
session-event funnel and RPC invoker by in-memory fakes. Covers the event
mapping table, identity dedup + turn reset, spool cap, dispatch guard/idempotent
cmid/ack, and the service notify/spool/pause flow.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from opensquilla.gateway.config import PetConfig
from opensquilla.pet import events
from opensquilla.pet.bridge_client import BridgeClient
from opensquilla.pet.dispatch import DispatchProcessor, client_message_id
from opensquilla.pet.service import PetReminderService
from opensquilla.pet.spool import Spool

# ---------------------------------------------------------------- mapping --


def test_event_mapping_table() -> None:
    assert events.map_event("session.event.done") == events.KIND_STOP
    assert events.map_event("chat.done") == events.KIND_STOP
    assert events.map_event("task.succeeded") == events.KIND_STOP
    assert events.map_event("session.event.approval_required") == events.KIND_PERMISSION
    assert events.map_event("session.event.plan_presentation") == events.KIND_QUESTION
    assert events.map_event("session.event.turn_committed") == events.KIND_TURN_START
    assert events.map_event("session.event.text_delta") is None
    assert events.map_event("sessions.changed") is None


def test_raises_foreground_only_for_permission_and_question() -> None:
    assert events.raises_foreground(events.KIND_PERMISSION)
    assert events.raises_foreground(events.KIND_QUESTION)
    assert not events.raises_foreground(events.KIND_STOP)
    assert not events.raises_foreground(events.KIND_TURN_START)


def test_event_identity_prefers_ids_then_seq() -> None:
    assert events.event_identity({"approval_id": "a1"}) == "a1"
    assert events.event_identity({"turn_id": "t1", "approval_id": "a1"}) == "a1"
    assert events.event_identity({"stream_seq": 7}) == "seq:7"
    assert events.event_identity({}) == ""
    assert events.event_identity({"turn_id": "  "}) == ""


def test_build_notify_payload_fields() -> None:
    body = events.build_notify_payload(
        kind="permission",
        message="hi",
        title="OpenSquilla",
        session_key="agent:main:webchat:x",
        event_id="a1",
        raise_window="OpenSquilla.exe",
        project="Demo",
    )
    assert body == {
        "event": "permission",
        "title": "OpenSquilla",
        "message": "hi",
        "raise": "OpenSquilla.exe",
        "session_id": "agent:main:webchat:x",
        "event_id": "a1",
        "project": "Demo",
    }
    minimal = events.build_notify_payload(kind="stop", message="m", title="T")
    assert "raise" not in minimal and "session_id" not in minimal


# ----------------------------------------------------------------- dedup --


def test_dedupe_identity_and_echo_and_turn_reset() -> None:
    led = events.DedupeLedger(15000)
    assert led.allow("s", "stop", {"turn_id": "t1"}) is True
    assert led.allow("s", "stop", {"turn_id": "t1"}) is False  # same identity
    assert led.allow("s", "stop", {"turn_id": "t2"}) is True  # new turn
    assert led.allow("s", "stop", {}) is False  # identity-less echo suppressed
    led.reset_session("s")
    assert led.allow("s", "stop", {"turn_id": "t1"}) is True  # ledger cleared


def test_dedupe_window_expiry() -> None:
    now = [0.0]
    led = events.DedupeLedger(1000, clock=lambda: now[0])
    assert led.allow("s", "stop", {"turn_id": "t1"}) is True
    now[0] = 500.0
    assert led.allow("s", "stop", {"turn_id": "t1"}) is False
    now[0] = 1500.0
    assert led.allow("s", "stop", {"turn_id": "t1"}) is True


def test_dedupe_isolates_sessions_and_kinds() -> None:
    led = events.DedupeLedger(15000)
    assert led.allow("s1", "stop", {"turn_id": "t"}) is True
    assert led.allow("s2", "stop", {"turn_id": "t"}) is True
    assert led.allow("s1", "permission", {"turn_id": "t"}) is True


# ----------------------------------------------------------------- spool --


def test_spool_append_and_read(tmp_path) -> None:
    spool = Spool(tmp_path)
    assert spool.append({"event": "stop", "message": "m1"}) is True
    assert spool.append({"event": "stop", "message": "m2"}) is True
    items = spool.read_pending()
    assert [i["message"] for i in items] == ["m1", "m2"]


def test_spool_cap_keeps_newest(tmp_path) -> None:
    spool = Spool(tmp_path, cap_bytes=4096)
    for i in range(400):
        spool.append({"event": "stop", "message": f"m{i}", "pad": "x" * 40})
    # File exceeds cap; trimming keeps at most 500 newest lines.
    items = spool.read_pending()
    assert len(items) <= 500
    assert items[-1]["message"] == "m399"


def test_spool_read_tolerates_bad_lines(tmp_path) -> None:
    spool = Spool(tmp_path)
    spool.path.write_text('{"event":"stop"}\nnot-json\n\n', encoding="utf-8")
    assert spool.read_pending() == [{"event": "stop"}]


# ----------------------------------------------------------- bridge client --


def _bridge(handler) -> BridgeClient:
    return BridgeClient(
        "http://127.0.0.1:45915", transport=httpx.MockTransport(handler)
    )


@pytest.mark.asyncio
async def test_bridge_notify_succeeds_on_204() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(204)

    bridge = _bridge(handler)
    assert await bridge.notify({"event": "stop", "message": "m"}) is True
    assert seen["path"] == "/notify"
    assert seen["body"]["event"] == "stop"
    await bridge.aclose()


@pytest.mark.asyncio
async def test_bridge_notify_false_on_error() -> None:
    bridge = _bridge(lambda request: httpx.Response(500))
    assert await bridge.notify({"event": "stop"}) is False
    await bridge.aclose()


@pytest.mark.asyncio
async def test_bridge_dispatch_pending_and_ack() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/dispatch/pending":
            assert request.url.params["exe"] == "OpenSquilla.exe"
            return httpx.Response(200, json=[{"dispatch_id": "d1", "session_id": "s", "text": "t"}])
        if request.url.path == "/dispatch/ack":
            body = json.loads(request.content)
            assert body == {"dispatch_id": "d1", "ok": True}
            return httpx.Response(204)
        return httpx.Response(404)

    bridge = _bridge(handler)
    items = await bridge.dispatch_pending("OpenSquilla.exe")
    assert items == [{"dispatch_id": "d1", "session_id": "s", "text": "t"}]
    assert await bridge.dispatch_ack("d1", ok=True) is True
    await bridge.aclose()


@pytest.mark.asyncio
async def test_bridge_dispatch_pending_empty_on_404() -> None:
    bridge = _bridge(lambda request: httpx.Response(404))
    assert await bridge.dispatch_pending("x") == []
    await bridge.aclose()


# -------------------------------------------------------------- dispatch --


class _FakeRpc:
    def __init__(self, *, resolve_ok: bool = True, send_result: dict | None = None,
                 send_error: str = "") -> None:
        self.resolve_ok = resolve_ok
        self.send_result = send_result if send_result is not None else {"turn_id": "turn-1"}
        self.send_error = send_error
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if method == "sessions.resolve":
            if not self.resolve_ok:
                raise RuntimeError("NOT_FOUND: session not in live table")
            return {"session_key": params["key"]}
        if method == "sessions.send":
            if self.send_error:
                raise RuntimeError(self.send_error)
            return self.send_result
        raise AssertionError(f"unexpected method {method}")


def _dispatch_bridge(pending: list[dict], acks: list[dict]) -> BridgeClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/dispatch/pending":
            return httpx.Response(200, json=pending)
        if request.url.path == "/dispatch/ack":
            acks.append(json.loads(request.content))
            return httpx.Response(204)
        return httpx.Response(404)

    return _bridge(handler)


@pytest.mark.asyncio
async def test_dispatch_injects_with_idempotent_cmid_and_acks_ok() -> None:
    acks: list[dict] = []
    bridge = _dispatch_bridge(
        [{"dispatch_id": "ab-cd-12", "session_id": "sess", "text": "hello"}], acks
    )
    rpc = _FakeRpc()
    proc = DispatchProcessor(bridge, rpc, exe="OpenSquilla.exe")
    assert await proc.process_once() == 1
    # resolve guard then send with derived cmid
    assert rpc.calls[0][0] == "sessions.resolve"
    send_method, send_params = rpc.calls[1]
    assert send_method == "sessions.send"
    assert send_params["client_message_id"] == "agentrmc-abcd12"
    assert send_params["_source"]["client_message_id"] == "agentrmc-abcd12"
    assert acks == [{"dispatch_id": "ab-cd-12", "ok": True}]
    await bridge.aclose()


@pytest.mark.asyncio
async def test_dispatch_resolve_reject_acks_false_no_send() -> None:
    acks: list[dict] = []
    bridge = _dispatch_bridge(
        [{"dispatch_id": "d1", "session_id": "gone", "text": "t"}], acks
    )
    rpc = _FakeRpc(resolve_ok=False)
    proc = DispatchProcessor(bridge, rpc, exe="x")
    await proc.process_once()
    assert [m for m, _ in rpc.calls] == ["sessions.resolve"]  # never sent
    assert acks[0]["ok"] is False
    assert "不在活表" in acks[0]["error"]
    await bridge.aclose()


@pytest.mark.asyncio
async def test_dispatch_missing_fields_acks_false() -> None:
    acks: list[dict] = []
    bridge = _dispatch_bridge([{"dispatch_id": "d1"}], acks)
    rpc = _FakeRpc()
    proc = DispatchProcessor(bridge, rpc, exe="x")
    await proc.process_once()
    assert rpc.calls == []
    assert acks[0]["ok"] is False
    await bridge.aclose()


@pytest.mark.asyncio
async def test_dispatch_send_without_turn_id_is_uncertain() -> None:
    acks: list[dict] = []
    bridge = _dispatch_bridge(
        [{"dispatch_id": "d1", "session_id": "s", "text": "t"}], acks
    )
    rpc = _FakeRpc(send_result={})  # no turn_id/task_id
    proc = DispatchProcessor(bridge, rpc, exe="x")
    await proc.process_once()
    assert acks[0]["ok"] is False
    assert "送达未知" in acks[0]["error"]
    await bridge.aclose()


@pytest.mark.asyncio
async def test_dispatch_timeout_is_uncertain_not_rejected() -> None:
    acks: list[dict] = []
    bridge = _dispatch_bridge(
        [{"dispatch_id": "d1", "session_id": "s", "text": "t"}], acks
    )
    rpc = _FakeRpc(send_error="request timeout after 30s")
    proc = DispatchProcessor(bridge, rpc, exe="x")
    await proc.process_once()
    assert acks[0]["ok"] is False
    assert "送达未知" in acks[0]["error"]
    await bridge.aclose()


def test_cmid_strips_dashes() -> None:
    assert client_message_id("a-b-c") == "agentrmc-abc"


# --------------------------------------------------------------- service --


class _FakeEventSource:
    def __init__(self) -> None:
        self.observer: Any = None

    def add_local_observer(self, callback) -> Any:
        self.observer = callback

        def remove() -> None:
            self.observer = None

        return remove


def _service(tmp_path, *, bridge_handler, config: PetConfig | None = None):
    cfg = config or PetConfig(enabled=True, spool_dir=str(tmp_path / "spool"),
                              pause_file=str(tmp_path / "pause"))
    source = _FakeEventSource()
    bridge = _bridge(bridge_handler)
    rpc = _FakeRpc()
    service = PetReminderService(cfg, event_source=source, rpc_invoke=rpc, bridge=bridge)
    return service, source, bridge


@pytest.mark.asyncio
async def test_service_notifies_on_done_and_dedups(tmp_path) -> None:
    notifications: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/notify":
            notifications.append(json.loads(request.content))
            return httpx.Response(204)
        return httpx.Response(404)

    service, source, bridge = _service(tmp_path, bridge_handler=handler)
    await service.start()
    source.observer("s", "session.event.done", {"turn_id": "t1"})
    source.observer("s", "session.event.done", {"turn_id": "t1"})  # dup
    await asyncio.sleep(0.05)
    await service.stop()
    assert len(notifications) == 1
    assert notifications[0]["event"] == "stop"
    assert notifications[0]["session_id"] == "s"


@pytest.mark.asyncio
async def test_service_spools_when_pet_offline(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)  # pet unreachable

    service, source, bridge = _service(tmp_path, bridge_handler=handler)
    await service.start()
    source.observer("s", "session.event.done", {"turn_id": "t1"})
    await asyncio.sleep(0.05)
    await service.stop()
    spooled = service._spool.read_pending()
    assert len(spooled) == 1
    assert spooled[0]["event"] == "stop"


@pytest.mark.asyncio
async def test_service_pause_file_suppresses(tmp_path) -> None:
    notifications: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        notifications.append(json.loads(request.content))
        return httpx.Response(204)

    service, source, bridge = _service(tmp_path, bridge_handler=handler)
    service._pause_file.write_text("", encoding="utf-8")  # paused
    await service.start()
    source.observer("s", "session.event.done", {"turn_id": "t1"})
    await asyncio.sleep(0.05)
    await service.stop()
    assert notifications == []


@pytest.mark.asyncio
async def test_service_permission_raises_foreground(tmp_path) -> None:
    notifications: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        notifications.append(json.loads(request.content))
        return httpx.Response(204)

    service, source, bridge = _service(tmp_path, bridge_handler=handler)
    await service.start()
    source.observer("s", "session.event.approval_required", {"approval_id": "a1"})
    await asyncio.sleep(0.05)
    await service.stop()
    assert notifications[0]["event"] == "permission"
    assert notifications[0]["raise"] == "OpenSquilla.exe"


@pytest.mark.asyncio
async def test_service_ignores_cron_by_default(tmp_path) -> None:
    notifications: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        notifications.append(json.loads(request.content))
        return httpx.Response(204)

    service, source, bridge = _service(tmp_path, bridge_handler=handler)
    await service.start()
    source.observer("cron:job1", "session.event.done", {"turn_id": "t1"})
    await asyncio.sleep(0.05)
    await service.stop()
    assert notifications == []


@pytest.mark.asyncio
async def test_service_turn_start_resets_ledger(tmp_path) -> None:
    notifications: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        notifications.append(json.loads(request.content))
        return httpx.Response(204)

    service, source, bridge = _service(tmp_path, bridge_handler=handler)
    await service.start()
    source.observer("s", "session.event.done", {"turn_id": "t1"})
    source.observer("s", "session.event.turn_committed", {"turn_id": "t2"})
    source.observer("s", "session.event.done", {"turn_id": "t2"})
    await asyncio.sleep(0.05)
    await service.stop()
    kinds = [n["event"] for n in notifications]
    assert kinds == ["stop", "turn-start", "stop"]


@pytest.mark.asyncio
async def test_service_speak_redacts_and_truncates(tmp_path) -> None:
    spoken: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/speak":
            spoken.append(json.loads(request.content))
            return httpx.Response(204)
        return httpx.Response(404)

    service, source, bridge = _service(tmp_path, bridge_handler=handler)
    ok = await service.speak("key=sk-supersecretvalue123456 done")
    assert ok is True
    assert "sk-supersecretvalue" not in spoken[0]["message"]
    assert len(spoken[0]["message"]) <= 120
    await bridge.aclose()


# ------------------------------------------------- clarify/choice cards --


def _clarify_result(request_id: str = "req-1", *, paused: bool = True,
                    status: str = "input_required") -> dict:
    return {
        "kind": "user_input",
        "paused": paused,
        "status": status,
        "request_id": request_id,
        "clarify_schema": {
            "fields": [{"name": "choice", "type": "choice", "prompt": "Pick one"}]
        },
    }


def test_pending_clarify_request_detection() -> None:
    # Pending form (object result) → request id.
    assert events.pending_clarify_request({"result": _clarify_result()}) == "req-1"
    # JSON-string result is parsed too.
    assert events.pending_clarify_request(
        {"result": json.dumps(_clarify_result("req-2"))}
    ) == "req-2"
    # Lifted top-level user_input_request field also works.
    assert events.pending_clarify_request(
        {"user_input_request": _clarify_result("req-3")}
    ) == "req-3"


def test_pending_clarify_request_ignores_non_forms() -> None:
    # Answered/cancelled/expired (paused False) → no notification.
    assert events.pending_clarify_request(
        {"result": _clarify_result(paused=False, status="answered")}
    ) is None
    # Not a user_input kind.
    assert events.pending_clarify_request({"result": {"kind": "text", "paused": True}}) is None
    # Missing request id.
    no_id = _clarify_result()
    del no_id["request_id"]
    assert events.pending_clarify_request({"result": no_id}) is None
    # Empty schema fields.
    empty = _clarify_result()
    empty["clarify_schema"] = {"fields": []}
    assert events.pending_clarify_request({"result": empty}) is None
    # Ordinary tool result.
    assert events.pending_clarify_request({"result": "some text output"}) is None
    assert events.pending_clarify_request({}) is None


@pytest.mark.asyncio
async def test_service_notifies_question_on_clarify_card(tmp_path) -> None:
    notifications: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/notify":
            notifications.append(json.loads(request.content))
            return httpx.Response(204)
        return httpx.Response(404)

    service, source, bridge = _service(tmp_path, bridge_handler=handler)
    await service.start()
    source.observer("s", "session.event.tool_result", {"result": _clarify_result("req-1")})
    await asyncio.sleep(0.05)
    await service.stop()
    assert len(notifications) == 1
    assert notifications[0]["event"] == "question"
    assert notifications[0]["event_id"] == "req-1"
    assert notifications[0]["raise"] == "OpenSquilla.exe"


@pytest.mark.asyncio
async def test_service_clarify_dedup_by_request_id(tmp_path) -> None:
    notifications: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        notifications.append(json.loads(request.content))
        return httpx.Response(204)

    service, source, bridge = _service(tmp_path, bridge_handler=handler)
    await service.start()
    # Same form echoed twice → one notification; a different form → another.
    source.observer("s", "session.event.tool_result", {"result": _clarify_result("req-1")})
    source.observer("s", "session.event.tool_result", {"result": _clarify_result("req-1")})
    source.observer("s", "session.event.tool_result", {"result": _clarify_result("req-2")})
    # An ordinary tool result must not notify.
    source.observer("s", "session.event.tool_result", {"result": "plain output"})
    await asyncio.sleep(0.05)
    await service.stop()
    assert [n["event_id"] for n in notifications] == ["req-1", "req-2"]


@pytest.mark.asyncio
async def test_service_ignores_answered_clarify(tmp_path) -> None:
    notifications: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        notifications.append(json.loads(request.content))
        return httpx.Response(204)

    service, source, bridge = _service(tmp_path, bridge_handler=handler)
    await service.start()
    source.observer(
        "s", "session.event.tool_result",
        {"result": _clarify_result("req-1", paused=False, status="answered")},
    )
    await asyncio.sleep(0.05)
    await service.stop()
    assert notifications == []


# ------------------------------------------------- session label/project --


def test_session_label_prefers_title_then_project_dir() -> None:
    assert events.session_label("修复登录", "D:\\repo\\Opensquilla") == "修复登录"
    # Generic WebChat title is not spoken → project directory name.
    assert events.session_label("WebChat", "D:\\repo\\Opensquilla") == "Opensquilla"
    assert events.session_label("", "D:\\repo\\Agentrmc") == "Agentrmc"
    assert events.session_label("", "") == "这个会话"
    assert events.session_label("webchat", "/home/user/proj/") == "proj"


@pytest.mark.asyncio
async def test_service_includes_project_and_renders_session(tmp_path) -> None:
    notifications: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        notifications.append(json.loads(request.content))
        return httpx.Response(204)

    cfg = PetConfig(
        enabled=True,
        spool_dir=str(tmp_path / "spool"),
        pause_file=str(tmp_path / "pause"),
        msg_stop="OpenSquilla：{session} 任务完成",
    )
    source = _FakeEventSource()
    bridge = _bridge(handler)

    async def resolver(session_key: str) -> tuple[str, str]:
        return ("WebChat", "D:\\repo\\Opensquilla")

    service = PetReminderService(
        cfg, event_source=source, rpc_invoke=_FakeRpc(),
        bridge=bridge, metadata_resolver=resolver,
    )
    await service.start()
    source.observer("s", "session.event.done", {"turn_id": "t1"})
    await asyncio.sleep(0.05)
    await service.stop()
    assert notifications[0]["project"] == "D:\\repo\\Opensquilla"
    # {session} rendered to the project dir name (WebChat title suppressed).
    assert notifications[0]["message"] == "OpenSquilla：Opensquilla 任务完成"


@pytest.mark.asyncio
async def test_service_clarify_includes_project(tmp_path) -> None:
    notifications: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        notifications.append(json.loads(request.content))
        return httpx.Response(204)

    source = _FakeEventSource()
    bridge = _bridge(handler)

    async def resolver(session_key: str) -> tuple[str, str]:
        return ("某个任务", "D:\\repo\\Agentrmc")

    cfg = PetConfig(enabled=True, spool_dir=str(tmp_path / "spool"),
                    pause_file=str(tmp_path / "pause"))
    service = PetReminderService(
        cfg, event_source=source, rpc_invoke=_FakeRpc(),
        bridge=bridge, metadata_resolver=resolver,
    )
    await service.start()
    source.observer("s", "session.event.tool_result", {"result": _clarify_result("req-9")})
    await asyncio.sleep(0.05)
    await service.stop()
    assert notifications[0]["project"] == "D:\\repo\\Agentrmc"
    assert notifications[0]["event"] == "question"
