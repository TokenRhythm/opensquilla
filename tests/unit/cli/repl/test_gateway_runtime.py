from __future__ import annotations

import asyncio
import importlib
import inspect
import sys
from typing import Any, cast

import pytest

from opensquilla.cli.repl.session_state import ChatSessionState
from opensquilla.cli.repl.stream import TurnResult, UsageSummary
from opensquilla.cli.tui.contracts import TuiOutputHandle
from opensquilla.cli.tui.opentui.host_runtime import HostFailureReason, HostRuntimeError


def test_gateway_runtime_has_no_raw_prompt_application_dependency(monkeypatch) -> None:
    monkeypatch.delitem(
        sys.modules,
        "opensquilla.cli.repl.gateway_runtime",
        raising=False,
    )

    original_import = __import__
    blocked_module = "prompt" + "_toolkit"

    def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: ANN001
        if name == blocked_module or name.startswith(f"{blocked_module}."):
            raise AssertionError(f"gateway runtime imported {blocked_module} via {name}")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", _guarded_import)

    module = importlib.import_module("opensquilla.cli.repl.gateway_runtime")
    source = inspect.getsource(module)

    assert "ChatApplication" not in source


def test_gateway_session_context_mirrors_state_to_legacy_scope() -> None:
    from opensquilla.cli.repl.gateway_runtime import GatewaySessionContext

    state = ChatSessionState(session_key="agent:main:original", model="gateway/original")
    context = GatewaySessionContext.create(state)

    assert context.scope["session_key"] == "agent:main:original"
    assert context.scope["state"] is state
    assert context.scope["model"] == "gateway/original"

    state.session_key = "agent:main:slash"
    state.model = "gateway/slash-model"
    context.sync_from_state()

    assert context.session_key == "agent:main:slash"
    assert context.model == "gateway/slash-model"
    assert context.scope["session_key"] == "agent:main:slash"
    assert context.scope["model"] == "gateway/slash-model"


@pytest.mark.asyncio
async def test_gateway_runtime_connects_to_configured_gateway_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import gateway_runtime

    class _FakeGatewayClient:
        connected_url: str | None = None
        connected_token: str | None = None
        closed = False

        async def connect(self, url: str, *, token: str | None = None) -> None:
            type(self).connected_url = url
            type(self).connected_token = token

        async def create_session(self, model: str | None = None) -> str:
            return "agent:main:new"

        async def resolve_session(self, key: str) -> dict[str, str]:
            return {"model": "gateway/resolved"}

        async def bootstrap_session(self, key: str, *, limit: int = 200) -> dict[str, Any]:
            resolved = await self.resolve_session(key)
            return {
                "session": {"session_key": key, **resolved},
                "history": {
                    "messages": [],
                    "history_scope": "complete",
                    "loaded_count": 0,
                },
            }

        async def close(self) -> None:
            type(self).closed = True

    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_URL", "http://127.0.0.1:18790")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_TOKEN", "branch-token")

    async def fake_run_concurrent_repl(
        *,
        scope: gateway_runtime.GatewayRuntimeScope,
        dispatch,
        abort_active_turn=None,
    ) -> None:
        return None

    deps = gateway_runtime.GatewayRuntimeDependencies(
        stream_response=cast(Any, None),
        handle_slash_command=cast(Any, None),
        run_input_loop=fake_run_concurrent_repl,
        get_tui_output=lambda _scope: None,
        is_exit_command=lambda _value: False,
        notify=lambda _notice: None,
    )

    await gateway_runtime.run_gateway_chat(
        model=None,
        session_id=None,
        deps=deps,
    )

    assert _FakeGatewayClient.connected_url == "ws://127.0.0.1:18790/ws"
    assert _FakeGatewayClient.connected_token == "branch-token"
    assert _FakeGatewayClient.closed is True


@pytest.mark.asyncio
async def test_gateway_runtime_does_not_announce_resume_before_bootstrap_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.gateway_client import GatewayRPCError
    from opensquilla.cli.repl import gateway_runtime

    class _MissingSessionGatewayClient:
        closed = False

        async def connect(self, _url: str, *, token: str | None = None) -> None:
            return None

        async def bootstrap_session(self, key: str, *, limit: int = 200) -> dict[str, Any]:
            raise GatewayRPCError(
                "sessions.bootstrap",
                code="NOT_FOUND",
                message=f"Session not found: {key}",
            )

        async def close(self) -> None:
            type(self).closed = True

    monkeypatch.setattr(
        "opensquilla.cli.gateway_client.GatewayClient",
        _MissingSessionGatewayClient,
    )
    notices: list[gateway_runtime.GatewayRuntimeNotice] = []
    deps = gateway_runtime.GatewayRuntimeDependencies(
        stream_response=cast(Any, None),
        handle_slash_command=cast(Any, None),
        run_input_loop=cast(Any, None),
        get_tui_output=lambda _scope: None,
        is_exit_command=lambda _value: False,
        notify=notices.append,
    )

    with pytest.raises(GatewayRPCError, match="Session not found: main"):
        await gateway_runtime.run_gateway_chat(
            model=None,
            session_id="main",
            deps=deps,
        )

    assert notices == []
    assert _MissingSessionGatewayClient.closed is True


@pytest.mark.asyncio
async def test_gateway_runtime_dispatches_messages_slash_commands_and_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import gateway_runtime

    class _FakeGatewayClient:
        instances: list[_FakeGatewayClient] = []

        def __init__(self) -> None:
            self.connected = False
            self.closed = False
            self.create_calls: list[str | None] = []
            self.resolve_calls: list[str] = []
            self.abort_calls: list[str] = []
            _FakeGatewayClient.instances.append(self)

        async def connect(self, url: str, *, token: str | None = None) -> None:
            self.connected = True

        async def create_session(self, model: str | None = None) -> str:
            self.create_calls.append(model)
            return "agent:main:new"

        async def resolve_session(self, key: str) -> dict[str, str]:
            self.resolve_calls.append(key)
            return {"model": "gateway/resolved"}

        async def bootstrap_session(self, key: str, *, limit: int = 200) -> dict[str, Any]:
            resolved = await self.resolve_session(key)
            return {
                "session": {"session_key": key, **resolved},
                "history": {
                    "messages": [{"message_id": "m1", "role": "user", "text": "persisted"}],
                    "history_scope": "complete",
                    "loaded_count": 1,
                    "canonical_available": True,
                },
            }

        async def abort_session(self, key: str) -> dict[str, object]:
            self.abort_calls.append(key)
            return {"aborted": True, "key": key}

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)

    output = cast(TuiOutputHandle, object())
    captured: dict[str, Any] = {}

    async def fake_stream_response(
        client: object,
        session_key: str,
        message: str,
        elevated_state: dict[str, str | None] | None = None,
        attachments: list[dict] | None = None,
        *,
        tui_output: object | None = None,
    ) -> TurnResult:
        captured["stream"] = {
            "client": client,
            "session_key": session_key,
            "message": message,
            "elevated_state": elevated_state,
            "attachments": attachments,
            "tui_output": tui_output,
        }
        return TurnResult(
            text="assistant reply",
            usage=UsageSummary(input_tokens=2, output_tokens=3),
            model_after="gateway/after",
        )

    async def fake_handle_slash_command(
        cmd: str,
        state: ChatSessionState,
        client: object,
        elevated_state: dict[str, str | None],
        *,
        tui_output: object | None = None,
    ) -> bool:
        captured["slash"] = {
            "cmd": cmd,
            "client": client,
            "elevated_state": elevated_state,
            "tui_output": tui_output,
        }
        state.session_key = "agent:main:slash"
        state.model = "gateway/slash-model"
        return True

    async def fake_run_concurrent_repl(
        *,
        scope: gateway_runtime.GatewayRuntimeScope,
        dispatch,
        abort_active_turn=None,
    ) -> None:
        captured["initial_scope"] = dict(scope)
        captured["abort_active_turn"] = abort_active_turn

        assert await dispatch("hello") is True
        active_state = cast(ChatSessionState, scope["state"])
        captured["state_after_message"] = active_state
        assert active_state.model == "gateway/after"
        assert active_state.transcript.to_markdown()
        assert active_state.usage.input_tokens == 2
        assert active_state.usage.output_tokens == 3

        assert await dispatch("/reset") is True
        captured["scope_after_slash"] = dict(scope)

        assert await dispatch("/exit") is False

    def fake_get_tui_output(
        _scope: gateway_runtime.GatewayRuntimeScope,
    ) -> TuiOutputHandle | None:
        return output

    deps = gateway_runtime.GatewayRuntimeDependencies(
        stream_response=fake_stream_response,
        handle_slash_command=fake_handle_slash_command,
        run_input_loop=fake_run_concurrent_repl,
        get_tui_output=fake_get_tui_output,
        is_exit_command=lambda value: value.strip() == "/exit",
        notify=lambda notice: captured.setdefault("notices", []).append(notice),
    )

    summary = await gateway_runtime.run_gateway_chat(
        model="anthropic/claude-sonnet-4",
        session_id=None,
        deps=deps,
    )

    client = _FakeGatewayClient.instances[-1]
    assert client.connected is True
    assert client.closed is True
    assert client.create_calls == ["anthropic/claude-sonnet-4"]
    assert client.resolve_calls == ["agent:main:new", "agent:main:slash"]
    assert summary.session_key == "agent:main:slash"
    assert summary.model == "gateway/resolved"
    assert summary.reason == "command"
    assert captured["abort_active_turn"] is not None
    await captured["abort_active_turn"]()
    assert client.abort_calls == []
    assert captured["initial_scope"]["session_key"] == "agent:main:new"
    assert captured["initial_scope"]["model"] == "gateway/resolved"
    assert captured["stream"]["client"] is client
    assert captured["stream"]["session_key"] == "agent:main:new"
    assert captured["stream"]["message"] == "hello"
    assert captured["stream"]["elevated_state"] == {"mode": None}
    assert captured["stream"]["tui_output"] is output
    assert captured["slash"]["cmd"] == "/reset"
    assert captured["slash"]["client"] is client
    assert captured["slash"]["elevated_state"] == {"mode": None}
    assert captured["slash"]["tui_output"] is output
    assert captured["scope_after_slash"]["session_key"] == "agent:main:slash"
    assert captured["scope_after_slash"]["model"] == "gateway/slash-model"
    assert [notice.kind for notice in captured["notices"]] == [
        "created",
        "model",
        "welcome",
        "goodbye",
    ]


@pytest.mark.asyncio
async def test_gateway_abort_targets_active_turn_session_after_session_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import gateway_runtime

    class _FakeGatewayClient:
        instances: list[_FakeGatewayClient] = []

        def __init__(self) -> None:
            self.abort_calls: list[str] = []
            _FakeGatewayClient.instances.append(self)

        async def connect(self, url: str, *, token: str | None = None) -> None:
            return None

        async def create_session(self, model: str | None = None) -> str:
            return "agent:main:old"

        async def resolve_session(self, key: str) -> dict[str, str]:
            return {"model": "gateway/old"}

        async def bootstrap_session(self, key: str, *, limit: int = 200) -> dict[str, Any]:
            resolved = await self.resolve_session(key)
            return {
                "session": {"session_key": key, **resolved},
                "history": {
                    "messages": [],
                    "history_scope": "complete",
                    "loaded_count": 0,
                },
            }

        async def abort_session(self, key: str) -> dict[str, object]:
            self.abort_calls.append(key)
            return {"aborted": True, "key": key}

        async def close(self) -> None:
            return None

    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)

    stream_started = asyncio.Event()
    release_stream = asyncio.Event()

    async def fake_stream_response(
        _client: object,
        session_key: str,
        _message: str,
        _elevated_state: dict[str, str | None] | None = None,
        _attachments: list[dict] | None = None,
        *,
        tui_output: object | None = None,
    ) -> TurnResult:
        assert session_key == "agent:main:old"
        stream_started.set()
        await release_stream.wait()
        return TurnResult(
            text="assistant reply",
            usage=UsageSummary(input_tokens=1, output_tokens=1),
            model_after="gateway/old",
        )

    async def fake_handle_slash_command(
        _cmd: str,
        _state: ChatSessionState,
        _client: object,
        _elevated_state: dict[str, str | None],
        *,
        tui_output: object | None = None,
    ) -> bool:
        return True

    async def fake_run_concurrent_repl(
        *,
        scope: gateway_runtime.GatewayRuntimeScope,
        dispatch,
        abort_active_turn=None,
    ) -> None:
        assert abort_active_turn is not None

        turn = asyncio.create_task(dispatch("hello"))
        await asyncio.wait_for(stream_started.wait(), timeout=2.0)

        active_state = cast(ChatSessionState, scope["state"])
        active_state.session_key = "agent:main:new"
        active_state.model = "gateway/new"
        scope["session_key"] = active_state.session_key
        scope["model"] = active_state.model

        await abort_active_turn()
        release_stream.set()
        assert await asyncio.wait_for(turn, timeout=2.0) is True

    deps = gateway_runtime.GatewayRuntimeDependencies(
        stream_response=fake_stream_response,
        handle_slash_command=fake_handle_slash_command,
        run_input_loop=fake_run_concurrent_repl,
        get_tui_output=lambda _scope: None,
        is_exit_command=lambda _value: False,
        notify=lambda _notice: None,
    )

    await gateway_runtime.run_gateway_chat(
        model=None,
        session_id=None,
        deps=deps,
    )

    client = _FakeGatewayClient.instances[-1]
    assert client.abort_calls == ["agent:main:old"]


@pytest.mark.asyncio
async def test_gateway_runtime_projects_external_turn_and_converges_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import gateway_runtime
    from opensquilla.cli.tui.backend.input_identity import (
        current_tui_client_message_id,
        tui_turn_identity_sink_scope,
    )

    class _Subscription:
        def __init__(self) -> None:
            self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self) -> dict[str, Any]:
            item = await self.queue.get()
            if item is None:
                raise StopAsyncIteration
            return item

        async def get(self) -> dict[str, Any]:
            item = await self.queue.get()
            if item is None:
                raise StopAsyncIteration
            return item

        async def close(self) -> None:
            self.closed = True
            self.queue.put_nowait(None)

    class _Output:
        def __init__(self) -> None:
            self.messages: list[tuple[str, dict[str, object]]] = []
            self.presented: list[dict[str, object]] = []
            self.resolved: list[tuple[str, bool, str | None]] = []

        async def send_message(self, kind: str, payload: dict[str, object]) -> None:
            self.messages.append((kind, payload))

        async def present_gateway_approval(self, request: dict[str, object]) -> bool:
            self.presented.append(request)
            return True

        async def resolve_gateway_approval(
            self,
            approval_id: str,
            *,
            approved: bool,
            resolution: str | None = None,
        ) -> bool:
            self.resolved.append((approval_id, approved, resolution))
            return True

    class _Client:
        instances: list[_Client] = []

        def __init__(self) -> None:
            self.surface_id = "tui:local"
            self.session_events = _Subscription()
            self.turn_events: list[_Subscription] = []
            self.approval_events = _Subscription()
            self.closed = False
            _Client.instances.append(self)

        async def connect(self, url: str, *, token: str | None = None) -> None:
            return None

        async def create_session(self, model: str | None = None) -> str:
            return "agent:main:shared"

        async def bootstrap_session(self, key: str, *, limit: int = 200) -> dict[str, Any]:
            return {
                "session": {"session_key": key, "model": "gateway/model"},
                "history": {
                    "messages": [
                        {
                            "message_id": "durable-message-web",
                            "role": "user",
                            "text": "hello from web",
                        }
                    ],
                    "history_scope": "complete",
                    "loaded_count": 1,
                },
                "stream_cursor": 7,
            }

        async def subscribe_session_events(
            self,
            key: str,
            *,
            since_stream_seq: int | None = None,
        ) -> _Subscription:
            assert key == "agent:main:shared"
            if not self.turn_events and since_stream_seq == 7:
                return self.session_events
            turn_events = _Subscription()
            self.turn_events.append(turn_events)
            return turn_events

        def subscribe_global_events(self, event_names: object) -> _Subscription:
            assert "exec.approval.requested" in event_names
            return self.approval_events

        async def resolve_approval(
            self,
            approval_id: str,
            approved: bool,
            *,
            choice: str | None = None,
        ) -> dict[str, object]:
            return {"resolved": True}

        async def resolve_session(self, key: str) -> dict[str, str]:
            return {"session_key": key, "model": "gateway/model"}

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _Client)
    output = _Output()
    projected = asyncio.Event()
    captured_events: list[dict[str, Any]] = []
    bound_identities: list[tuple[str, str]] = []

    async def stream_response(
        client: object,
        session_key: str,
        message: str,
        elevated_state: dict[str, str | None] | None = None,
        attachments: list[dict] | None = None,
        *,
        tui_output: object | None = None,
    ) -> TurnResult:
        assert client is not _Client.instances[-1]
        assert session_key == "agent:main:shared"
        assert message == "hello from web"
        assert current_tui_client_message_id() == "client-message-web"

        async def _bind(turn_id: str, client_message_id: str) -> None:
            bound_identities.append((turn_id, client_message_id))

        with tui_turn_identity_sink_scope(_bind):
            captured_events.extend(
                [event async for event in client.send_message(session_key, message)]
            )
        projected.set()
        return TurnResult(text="web answer", model_after="gateway/model")

    async def input_loop(*, scope, dispatch, abort_active_turn=None) -> None:
        client = _Client.instances[-1]
        await client.approval_events.queue.put(
            {
                "event": "exec.approval.requested",
                "payload": {
                    "approval_id": "approval-web",
                    "session_key": "agent:main:shared",
                    "tool_name": "exec_command",
                    "command": "echo hello",
                },
            }
        )
        await client.approval_events.queue.put(
            {
                "event": "exec.approval.resolved",
                "payload": {
                    "approval_id": "approval-web",
                    "session_key": "agent:main:shared",
                    "approved": True,
                    "resolution": "approved",
                },
            }
        )
        identity = {
            "session_key": "agent:main:shared",
            "turn_id": "turn-web",
            "client_message_id": "client-message-web",
            "user_message_id": "durable-message-web",
            "surface_id": "web:browser",
        }
        await client.session_events.queue.put(
            {
                "event": "session.event.text_delta",
                "payload": {**identity, "text": "web answer"},
            }
        )
        while not client.turn_events:
            await asyncio.sleep(0)
        terminal_frame = {"event": "session.event.done", "payload": identity}
        await client.session_events.queue.put(terminal_frame)
        await client.turn_events[0].queue.put(terminal_frame)
        await asyncio.wait_for(projected.wait(), timeout=2.0)
        while not output.resolved:
            await asyncio.sleep(0)

    deps = gateway_runtime.GatewayRuntimeDependencies(
        stream_response=stream_response,
        handle_slash_command=cast(Any, None),
        run_input_loop=input_loop,
        get_tui_output=lambda _scope: cast(Any, output),
        is_exit_command=lambda _value: False,
        notify=lambda _notice: None,
    )

    await gateway_runtime.run_gateway_chat(model=None, session_id=None, deps=deps)

    assert [event["event"] for event in captured_events] == [
        "session.event.text_delta",
        "session.event.done",
    ]
    assert output.messages[0] == (
        "prompt.echo",
        {
            "text": "hello from web",
            "client_message_id": "client-message-web",
        },
    )
    assert bound_identities == [("turn-web", "client-message-web")]
    assert output.presented[0]["id"] == "approval-web"
    assert output.resolved == [("approval-web", True, "approved")]


@pytest.mark.asyncio
async def test_local_dispatch_parked_behind_external_turn_notifies_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import gateway_runtime

    class _Subscription:
        def __init__(self) -> None:
            self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self) -> dict[str, Any]:
            item = await self.queue.get()
            if item is None:
                raise StopAsyncIteration
            return item

        async def close(self) -> None:
            self.closed = True
            self.queue.put_nowait(None)

    class _Output:
        async def send_message(self, kind: str, payload: dict[str, object]) -> None:
            return None

    class _Client:
        instances: list[_Client] = []

        def __init__(self) -> None:
            self.surface_id = "tui:local"
            self.session_events = _Subscription()
            self.turn_events: list[_Subscription] = []
            _Client.instances.append(self)

        async def connect(self, url: str, *, token: str | None = None) -> None:
            return None

        async def create_session(self, model: str | None = None) -> str:
            return "agent:main:shared"

        async def bootstrap_session(self, key: str, *, limit: int = 200) -> dict[str, Any]:
            return {
                "session": {"session_key": key, "model": "gateway/model"},
                "history": {
                    "messages": [
                        {
                            "message_id": "durable-message-web",
                            "role": "user",
                            "text": "hello from web",
                        }
                    ],
                    "history_scope": "complete",
                    "loaded_count": 1,
                },
                "stream_cursor": 7,
            }

        async def subscribe_session_events(
            self,
            key: str,
            *,
            since_stream_seq: int | None = None,
        ) -> _Subscription:
            assert key == "agent:main:shared"
            if not self.turn_events and since_stream_seq == 7:
                return self.session_events
            turn_events = _Subscription()
            self.turn_events.append(turn_events)
            return turn_events

        async def resolve_session(self, key: str) -> dict[str, str]:
            return {"session_key": key, "model": "gateway/model"}

        async def close(self) -> None:
            return None

    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _Client)

    external_started = asyncio.Event()
    release_external = asyncio.Event()
    streamed: list[str] = []
    notices: list[gateway_runtime.GatewayRuntimeNotice] = []

    async def stream_response(
        client: object,
        session_key: str,
        message: str,
        elevated_state: dict[str, str | None] | None = None,
        attachments: list[dict] | None = None,
        *,
        tui_output: object | None = None,
    ) -> TurnResult:
        streamed.append(message)
        if message == "hello from web":
            external_started.set()
            await release_external.wait()
            return TurnResult(text="web answer", model_after="gateway/model")
        return TurnResult(text="local answer", model_after="gateway/model")

    async def input_loop(*, scope, dispatch, abort_active_turn=None) -> None:
        client = _Client.instances[-1]
        await client.session_events.queue.put(
            {
                "event": "session.event.text_delta",
                "payload": {
                    "session_key": "agent:main:shared",
                    "turn_id": "turn-web",
                    "client_message_id": "client-message-web",
                    "user_message_id": "durable-message-web",
                    "surface_id": "web:browser",
                    "text": "web answer",
                },
            }
        )
        await asyncio.wait_for(external_started.wait(), timeout=2.0)

        dispatch_task = asyncio.create_task(dispatch("typed locally"))

        async def _queued_notice_seen() -> None:
            while not any(
                notice.kind == "queued_behind_external" for notice in notices
            ):
                await asyncio.sleep(0)

        await asyncio.wait_for(_queued_notice_seen(), timeout=2.0)
        # The park is announced while the send is still held back: the local
        # message must not stream before the mirrored external turn finishes.
        assert not dispatch_task.done()
        assert streamed == ["hello from web"]

        release_external.set()
        assert await asyncio.wait_for(dispatch_task, timeout=2.0) is True
        assert streamed == ["hello from web", "typed locally"]

    deps = gateway_runtime.GatewayRuntimeDependencies(
        stream_response=stream_response,
        handle_slash_command=cast(Any, None),
        run_input_loop=input_loop,
        get_tui_output=lambda _scope: cast(Any, _Output()),
        is_exit_command=lambda _value: False,
        notify=notices.append,
    )

    await gateway_runtime.run_gateway_chat(model=None, session_id=None, deps=deps)

    queued = [notice for notice in notices if notice.kind == "queued_behind_external"]
    assert len(queued) == 1


@pytest.mark.asyncio
async def test_local_dispatch_with_idle_external_turns_emits_no_queued_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import gateway_runtime

    class _Client:
        instances: list[_Client] = []

        def __init__(self) -> None:
            self.surface_id = "tui:local"
            _Client.instances.append(self)

        async def connect(self, url: str, *, token: str | None = None) -> None:
            return None

        async def create_session(self, model: str | None = None) -> str:
            return "agent:main:new"

        async def bootstrap_session(self, key: str, *, limit: int = 200) -> dict[str, Any]:
            return {
                "session": {"session_key": key, "model": "gateway/model"},
                "history": {
                    "messages": [],
                    "history_scope": "complete",
                    "loaded_count": 0,
                },
            }

        async def close(self) -> None:
            return None

    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _Client)
    notices: list[gateway_runtime.GatewayRuntimeNotice] = []

    async def stream_response(
        client: object,
        session_key: str,
        message: str,
        elevated_state: dict[str, str | None] | None = None,
        attachments: list[dict] | None = None,
        *,
        tui_output: object | None = None,
    ) -> TurnResult:
        return TurnResult(text="local answer", model_after="gateway/model")

    async def input_loop(*, scope, dispatch, abort_active_turn=None) -> None:
        assert await dispatch("typed locally") is True

    await gateway_runtime.run_gateway_chat(
        model=None,
        session_id=None,
        deps=gateway_runtime.GatewayRuntimeDependencies(
            stream_response=stream_response,
            handle_slash_command=cast(Any, None),
            run_input_loop=input_loop,
            get_tui_output=lambda _scope: None,
            is_exit_command=lambda _value: False,
            notify=notices.append,
        ),
    )

    assert not any(notice.kind == "queued_behind_external" for notice in notices)


@pytest.mark.asyncio
async def test_external_projection_cancellation_never_aborts_originating_turn() -> None:
    from opensquilla.cli.repl import gateway_runtime

    class _RealClient:
        def __init__(self) -> None:
            self.abort_calls: list[str] = []

        async def abort_session(self, key: str) -> dict[str, object]:
            self.abort_calls.append(key)
            return {"aborted": True}

    real = _RealClient()
    adapter = gateway_runtime._ExternalTurnClient(
        real,
        object(),
        {"event": "session.event.done", "payload": {}},
        turn_id="turn-web",
        client_message_id="message-web",
    )

    result = await adapter.abort_session("agent:main:shared")

    assert result == {"aborted": False, "key": "agent:main:shared"}
    assert real.abort_calls == []


@pytest.mark.asyncio
async def test_external_projection_normalizes_legacy_task_failure_and_stops() -> None:
    from opensquilla.cli.repl import gateway_runtime

    class _NoMoreFrames:
        async def get(self) -> dict[str, Any]:
            raise AssertionError("external projection read past a terminal task event")

    adapter = gateway_runtime._ExternalTurnClient(
        object(),
        _NoMoreFrames(),
        {
            "event": "task.failed",
            "payload": {
                "turn_id": "turn-web",
                "client_message_id": "message-web",
                "terminal_reason": "error",
            },
        },
        turn_id="turn-web",
        client_message_id="message-web",
    )

    events = [
        event
        async for event in adapter.send_message(
            "agent:main:shared",
            "ignored",
        )
    ]

    assert [event["event"] for event in events] == ["session.event.error"]
    assert events[0]["code"] == "failed"


@pytest.mark.asyncio
async def test_external_projection_does_not_end_on_untracked_task_group_terminal() -> None:
    from opensquilla.cli.repl import gateway_runtime

    class _RemainingFrames:
        def __init__(self) -> None:
            self.frames = [
                {
                    "event": "session.event.done",
                    "payload": {
                        "turn_id": "turn-web",
                        "client_message_id": "message-web",
                    },
                }
            ]

        async def get(self) -> dict[str, Any]:
            if not self.frames:
                raise AssertionError("external projection read past session completion")
            return self.frames.pop(0)

    adapter = gateway_runtime._ExternalTurnClient(
        object(),
        _RemainingFrames(),
        {
            "event": "session.event.task_group.done",
            "payload": {
                "turn_id": "turn-web",
                "client_message_id": "message-web",
                "group_id": "untracked-group",
            },
        },
        turn_id="turn-web",
        client_message_id="message-web",
    )

    events = [
        event
        async for event in adapter.send_message(
            "agent:main:shared",
            "ignored",
        )
    ]

    assert [event["event"] for event in events] == [
        "session.event.task_group.done",
        "session.event.done",
    ]


@pytest.mark.asyncio
async def test_approval_watcher_fails_pending_overlays_closed_on_disconnect() -> None:
    from opensquilla.cli.repl import gateway_runtime

    class _Disconnected:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise ConnectionError("gateway gone")

    class _Output:
        failed = False

        async def fail_pending_gateway_approvals(self) -> None:
            self.failed = True

    output = _Output()
    deps = gateway_runtime.GatewayRuntimeDependencies(
        stream_response=cast(Any, None),
        handle_slash_command=cast(Any, None),
        run_input_loop=cast(Any, None),
        get_tui_output=lambda _scope: cast(Any, output),
        is_exit_command=lambda _value: False,
        notify=lambda _notice: None,
    )

    await gateway_runtime._watch_approval_events(
        _Disconnected(),
        deps=deps,
        scope={"session_key": "agent:main:shared"},
    )

    assert output.failed is True


@pytest.mark.asyncio
async def test_external_turn_discovery_routes_interleaved_turns_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.gateway_client import GatewayClient
    from opensquilla.cli.repl import gateway_runtime

    session_key = "agent:main:shared"
    client = GatewayClient()

    async def call(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if method == "sessions.messages.subscribe":
            return {"replay_complete": True, "current_stream_seq": 0}
        if method == "sessions.bootstrap":
            return {
                "session": {"session_key": session_key, "model": "gateway/model"},
                "history": {
                    "messages": [
                        {"message_id": "user-a", "role": "user", "text": "prompt A"},
                        {"message_id": "user-b", "role": "user", "text": "prompt B"},
                    ]
                },
            }
        return {}

    monkeypatch.setattr(client, "_call", call)
    discovery = await client.subscribe_session_events(session_key)
    state = ChatSessionState(session_key=session_key, model="gateway/model")
    context = gateway_runtime.GatewaySessionContext.create(state)

    class _Output:
        async def send_message(self, kind: str, payload: dict[str, object]) -> None:
            return None

    captured: dict[str, list[dict[str, Any]]] = {}
    rendered = asyncio.Event()

    async def stream_response(
        turn_client: object,
        key: str,
        message: str,
        elevated_state: dict[str, str | None] | None = None,
        attachments: list[dict] | None = None,
        *,
        tui_output: object | None = None,
    ) -> TurnResult:
        captured[message] = [event async for event in turn_client.send_message(key, message)]
        if len(captured) == 2:
            rendered.set()
        return TurnResult(text=f"answer for {message}", model_after="gateway/model")

    idle = asyncio.Event()
    idle.set()
    external_idle = asyncio.Event()
    external_idle.set()
    deps = gateway_runtime.GatewayRuntimeDependencies(
        stream_response=stream_response,
        handle_slash_command=cast(Any, None),
        run_input_loop=cast(Any, None),
        get_tui_output=lambda _scope: cast(Any, _Output()),
        is_exit_command=lambda _value: False,
        notify=lambda _notice: None,
    )
    mirror = asyncio.create_task(
        gateway_runtime._mirror_external_turns(
            discovery,
            client=client,
            session_key=session_key,
            session_context=context,
            deps=deps,
            elevated_state={"mode": None},
            local_turn_idle=idle,
            external_turn_idle=external_idle,
        )
    )

    def publish(seq: int, turn: str, user_message: str, event: str) -> None:
        client._publish_event(  # noqa: SLF001
            {
                "type": "event",
                "event": event,
                "payload": {
                    "session_key": session_key,
                    "stream_seq": seq,
                    "turn_id": turn,
                    "client_message_id": f"client-{turn}",
                    "user_message_id": user_message,
                    "surface_id": "web:browser",
                    "text": turn,
                },
            }
        )

    publish(1, "turn-a", "user-a", "session.event.text_delta")
    publish(2, "turn-b", "user-b", "session.event.text_delta")
    publish(3, "turn-a", "user-a", "session.event.done")
    publish(4, "turn-b", "user-b", "session.event.done")
    await asyncio.wait_for(rendered.wait(), timeout=2.0)
    await asyncio.sleep(0)

    assert set(captured) == {"prompt A", "prompt B"}
    assert [event["event"] for event in captured["prompt A"]] == [
        "session.event.text_delta",
        "session.event.done",
    ]
    assert [event["event"] for event in captured["prompt B"]] == [
        "session.event.text_delta",
        "session.event.done",
    ]
    assert {event["turn_id"] for events in captured.values() for event in events} == {
        "turn-a",
        "turn-b",
    }

    mirror.cancel()
    with pytest.raises(asyncio.CancelledError):
        await mirror
    await discovery.close()


@pytest.mark.asyncio
async def test_external_turn_remaining_discovery_frames_do_not_duplicate_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.gateway_client import GatewayClient
    from opensquilla.cli.repl import gateway_runtime

    session_key = "agent:main:shared"
    client = GatewayClient()

    async def call(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if method == "sessions.messages.subscribe":
            return {"replay_complete": True, "current_stream_seq": 0}
        if method == "sessions.bootstrap":
            return {
                "history": {"messages": [{"message_id": "user-a", "role": "user", "text": "once"}]}
            }
        return {}

    monkeypatch.setattr(client, "_call", call)
    discovery = await client.subscribe_session_events(session_key)
    context = gateway_runtime.GatewaySessionContext.create(
        ChatSessionState(session_key=session_key)
    )
    calls = 0
    rendered = asyncio.Event()

    async def stream_response(
        turn_client: object,
        key: str,
        message: str,
        elevated_state: dict[str, str | None] | None = None,
        attachments: list[dict] | None = None,
        *,
        tui_output: object | None = None,
    ) -> TurnResult:
        nonlocal calls
        calls += 1
        _ = [event async for event in turn_client.send_message(key, message)]
        rendered.set()
        return TurnResult(text="done")

    class _Output:
        async def send_message(self, kind: str, payload: dict[str, object]) -> None:
            return None

    idle = asyncio.Event()
    idle.set()
    external_idle = asyncio.Event()
    external_idle.set()
    mirror = asyncio.create_task(
        gateway_runtime._mirror_external_turns(
            discovery,
            client=client,
            session_key=session_key,
            session_context=context,
            deps=gateway_runtime.GatewayRuntimeDependencies(
                stream_response=stream_response,
                handle_slash_command=cast(Any, None),
                run_input_loop=cast(Any, None),
                get_tui_output=lambda _scope: cast(Any, _Output()),
                is_exit_command=lambda _value: False,
                notify=lambda _notice: None,
            ),
            elevated_state={"mode": None},
            local_turn_idle=idle,
            external_turn_idle=external_idle,
        )
    )
    identity = {
        "session_key": session_key,
        "turn_id": "turn-a",
        "client_message_id": "client-a",
        "user_message_id": "user-a",
        "surface_id": "web:browser",
    }
    for seq, event in (
        (1, "session.event.text_delta"),
        (2, "session.event.tool_use_start"),
        (3, "session.event.done"),
    ):
        client._publish_event(  # noqa: SLF001
            {
                "type": "event",
                "event": event,
                "payload": {**identity, "stream_seq": seq},
            }
        )
    await asyncio.wait_for(rendered.wait(), timeout=2.0)
    await asyncio.sleep(0.02)
    assert calls == 1

    mirror.cancel()
    with pytest.raises(asyncio.CancelledError):
        await mirror
    await discovery.close()


@pytest.mark.asyncio
async def test_replay_gap_bootstraps_and_hydrates_existing_surface_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import gateway_runtime

    class _GapSubscription:
        needs_resync = True
        gap_reason = "buffer_window_missed"

        def __init__(self) -> None:
            self.closed = False
            self._closed = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self) -> dict[str, Any]:
            await self._closed.wait()
            raise StopAsyncIteration

        async def close(self) -> None:
            self.closed = True
            self._closed.set()

    class _Client:
        instances: list[_Client] = []

        def __init__(self) -> None:
            self.surface_id = "tui:local"
            self.bootstrap_calls = 0
            self.subscribe_calls = 0
            self.subscription = _GapSubscription()
            _Client.instances.append(self)

        async def connect(self, url: str, *, token: str | None = None) -> None:
            return None

        async def create_session(self, model: str | None = None) -> str:
            return "agent:main:gap"

        async def bootstrap_session(self, key: str, *, limit: int = 200) -> dict[str, Any]:
            self.bootstrap_calls += 1
            text = "stale" if self.bootstrap_calls == 1 else "canonical after gap"
            return {
                "session": {
                    "session_key": key,
                    "model": None,
                    "effective_model": "gateway/effective",
                    "workspace": "/workspace/gap",
                },
                "history": {
                    "messages": [
                        {"message_id": f"m-{self.bootstrap_calls}", "role": "user", "text": text}
                    ],
                    "history_scope": "complete",
                    "loaded_count": 1,
                },
                "stream_cursor": 9,
                "queue": {"running_count": 0, "queued_count": 0},
            }

        async def subscribe_session_events(
            self, key: str, *, since_stream_seq: int | None = None
        ) -> _GapSubscription:
            self.subscribe_calls += 1
            return self.subscription

        async def resolve_session(self, key: str) -> dict[str, Any]:
            return {"session_key": key}

        async def close(self) -> None:
            return None

    class _Output:
        def __init__(self) -> None:
            self.messages: list[tuple[str, dict[str, Any]]] = []

        async def send_message(self, kind: str, payload: dict[str, Any]) -> None:
            self.messages.append((kind, payload))

    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _Client)
    output = _Output()

    async def input_loop(*, scope, dispatch, abort_active_turn=None) -> None:
        assert scope["state"].transcript.turns[-1].content == "canonical after gap"
        assert scope["state"].model == "gateway/effective"
        assert scope["workspace_label"] == "/workspace/gap"
        assert scope["replay_gap_reason"] == "buffer_window_missed"

    summary = await gateway_runtime.run_gateway_chat(
        model=None,
        session_id=None,
        deps=gateway_runtime.GatewayRuntimeDependencies(
            stream_response=cast(Any, None),
            handle_slash_command=cast(Any, None),
            run_input_loop=input_loop,
            get_tui_output=lambda _scope: cast(Any, output),
            is_exit_command=lambda _value: False,
            notify=lambda _notice: None,
        ),
    )

    client = _Client.instances[-1]
    assert client.bootstrap_calls == 3  # initial, one gap resync, exit receipt
    assert client.subscribe_calls == 1
    assert [kind for kind, _payload in output.messages] == [
        "composer.set",
        "history.replace",
        "composer.set",
        "context.update",
    ]
    assert summary.session_key == "agent:main:gap"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        (ConnectionError("gateway gone"), "gateway_disconnect"),
        (
            HostRuntimeError(
                "host exited",
                reason=HostFailureReason.RUNTIME_CRASH,
            ),
            "host_crash",
        ),
        (RuntimeError("surface failed"), "runtime_error"),
    ],
)
async def test_runtime_failure_returns_queue_aware_exit_summary(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_reason: str,
) -> None:
    from opensquilla.cli.repl import gateway_runtime

    class _Client:
        async def connect(self, url: str, *, token: str | None = None) -> None:
            return None

        async def create_session(self, model: str | None = None) -> str:
            return "agent:main:receipt"

        async def bootstrap_session(self, key: str, *, limit: int = 200) -> dict[str, Any]:
            return {
                "session": {
                    "session_key": key,
                    "display_name": "Receipt session",
                    "model": "gateway/model",
                },
                "history": {"messages": [], "history_scope": "complete"},
                "queue": {"running_count": 1, "queued_count": 2},
            }

        async def resolve_session(self, key: str) -> dict[str, Any]:
            return {"session_key": key}

        async def close(self) -> None:
            return None

    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _Client)

    async def input_loop(*, scope, dispatch, abort_active_turn=None) -> None:
        raise failure

    with pytest.raises(gateway_runtime.SessionExitError) as caught:
        await gateway_runtime.run_gateway_chat(
            model=None,
            session_id=None,
            deps=gateway_runtime.GatewayRuntimeDependencies(
                stream_response=cast(Any, None),
                handle_slash_command=cast(Any, None),
                run_input_loop=input_loop,
                get_tui_output=lambda _scope: None,
                is_exit_command=lambda _value: False,
                notify=lambda _notice: None,
            ),
        )

    assert caught.value.summary.reason == expected_reason
    assert caught.value.summary.active is True
    assert caught.value.summary.queued == 2


@pytest.mark.asyncio
async def test_gateway_runtime_preserves_pre_session_startup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.cli.repl import gateway_runtime

    class _Client:
        async def connect(self, url: str, *, token: str | None = None) -> None:
            raise RuntimeError("startup handshake failed")

    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _Client)

    with pytest.raises(RuntimeError, match="startup handshake failed"):
        await gateway_runtime.run_gateway_chat(
            model=None,
            session_id=None,
            deps=gateway_runtime.GatewayRuntimeDependencies(
                stream_response=cast(Any, None),
                handle_slash_command=cast(Any, None),
                run_input_loop=cast(Any, None),
                get_tui_output=lambda _scope: None,
                is_exit_command=lambda _value: False,
                notify=lambda _notice: None,
            ),
        )
