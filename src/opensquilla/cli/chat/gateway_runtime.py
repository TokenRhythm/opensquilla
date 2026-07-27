"""Gateway chat runtime for the WebSocket-backed chat path.

This module owns gateway session setup and input dispatch. It is kept separate
from the concrete terminal app so `chat_cmd.py` can stay as CLI entrypoint and
compatibility wiring.
"""

from __future__ import annotations

import asyncio
import inspect
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from opensquilla.cli.chat.output import ChatOutputHandle
from opensquilla.cli.chat.session_context import GatewayRuntimeScope, GatewaySessionContext
from opensquilla.cli.chat.session_state import ChatSessionState
from opensquilla.cli.chat.turn import TurnResult
from opensquilla.cli.tui.opentui.context import (
    send_context_patch,
    send_context_update,
    send_model_routing_state,
)

GatewayRuntimeNoticeKind = Literal[
    "created",
    "resumed",
    "resume_model_ignored",
    "model",
    "welcome",
    "goodbye",
    "unknown_command",
    "queued_behind_external",
    "error",
]

GatewayExitReason = Literal[
    "command",
    "surface_closed",
    "host_crash",
    "gateway_disconnect",
    "runtime_error",
]


@dataclass(frozen=True)
class GatewayRuntimeNotice:
    kind: GatewayRuntimeNoticeKind
    session_key: str | None = None
    model: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class SessionExitSummary:
    """Durable information printed after the terminal surface is restored."""

    session_key: str
    title: str | None
    model: str | None
    reason: GatewayExitReason
    active: bool = False
    queued: int = 0


class SessionExitError(RuntimeError):
    """Surface failure carrying the one receipt that must still be printed."""

    def __init__(self, summary: SessionExitSummary) -> None:
        self.summary = summary
        super().__init__(f"chat session exited: {summary.reason}")


class GatewayClientLike(Protocol):
    async def call(self, method: str, params: dict | None = None) -> Any: ...

    async def create_session(
        self,
        agent_id: str = "main",
        model: str | None = None,
        display_name: str | None = None,
    ) -> str: ...

    async def list_sessions(self, limit: int = 50) -> dict[str, Any]: ...

    async def resolve_session(self, key: str) -> dict[str, Any]: ...

    async def bootstrap_session(
        self,
        key: str,
        *,
        limit: int = 200,
    ) -> dict[str, Any]: ...

    async def delete_sessions(self, keys: list[str]) -> dict[str, Any]: ...

    async def reset_session(self, key: str) -> dict[str, Any]: ...

    async def compact_session(self, key: str) -> dict[str, Any]: ...

    async def list_models(
        self,
        provider: str | None = None,
        capabilities: list[str] | None = None,
    ) -> list[dict[str, Any]]: ...

    async def patch_session(self, key: str, **fields: Any) -> dict[str, Any]: ...

    async def usage_status(self) -> dict[str, Any]: ...

    async def upload_file(self, path: Path, mime: str, name: str) -> str: ...

    def send_message(
        self,
        session_key: str,
        message: str,
        attachments: list[dict] | None = None,
        elevated: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]: ...

    async def resolve_approval(
        self,
        approval_id: str,
        approved: bool,
        *,
        choice: str | None = None,
    ) -> Any: ...

    async def abort_session(self, key: str) -> dict[str, Any]: ...

    async def steer_session(self, key: str, message: str) -> dict[str, Any]: ...


class GatewayRunInputLoop(Protocol):
    async def __call__(
        self,
        *,
        scope: GatewayRuntimeScope,
        dispatch: Callable[[str], Coroutine[Any, Any, bool]],
        abort_active_turn: Callable[[], Awaitable[None]] | None = None,
        steer_active_turn: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None: ...


class GatewayStreamResponse(Protocol):
    async def __call__(
        self,
        client: GatewayClientLike,
        session_key: str,
        message: str,
        elevated_state: dict[str, str | None] | None = None,
        attachments: list[dict] | None = None,
        *,
        tui_output: ChatOutputHandle | None = None,
    ) -> TurnResult: ...


class GatewayHandleSlashCommand(Protocol):
    async def __call__(
        self,
        cmd: str,
        state: ChatSessionState,
        client: GatewayClientLike,
        elevated_state: dict[str, str | None],
        *,
        tui_output: ChatOutputHandle | None = None,
    ) -> bool: ...


@dataclass(frozen=True)
class GatewayRuntimeDependencies:
    stream_response: GatewayStreamResponse
    handle_slash_command: GatewayHandleSlashCommand
    run_input_loop: GatewayRunInputLoop
    get_tui_output: Callable[[GatewayRuntimeScope], ChatOutputHandle | None]
    is_exit_command: Callable[[str], bool]
    notify: Callable[[GatewayRuntimeNotice], None]


_APPROVAL_EVENTS = frozenset(
    {
        "exec.approval.requested",
        "exec.approval.resolved",
        "plugin.approval.requested",
        "plugin.approval.resolved",
    }
)
_MODEL_ROUTING_EVENTS = frozenset({"models.routing.changed"})


def _flatten_event_frame(frame: dict[str, Any]) -> dict[str, Any]:
    payload = frame.get("payload")
    values = payload if isinstance(payload, dict) else {}
    return {"event": str(frame.get("event") or ""), **values}


def _event_identity(event: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    def _value(key: str) -> str | None:
        value = event.get(key)
        return value if isinstance(value, str) and value else None

    return _value("turn_id"), _value("client_message_id"), _value("surface_id")


def _event_user_message_id(event: dict[str, Any]) -> str | None:
    value = event.get("user_message_id") or event.get("userMessageId")
    return value if isinstance(value, str) and value else None


def _event_stream_seq(frame: dict[str, Any]) -> int | None:
    payload = frame.get("payload")
    if not isinstance(payload, dict):
        return None
    value = payload.get("stream_seq")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class _BoundedTurnIds:
    """Remember recent turn identities without growing for a long-lived TUI."""

    def __init__(self, limit: int = 512) -> None:
        self._limit = limit
        self._order: deque[str] = deque()
        self._values: set[str] = set()

    def __contains__(self, turn_id: str) -> bool:
        return turn_id in self._values

    def add(self, turn_id: str) -> bool:
        if turn_id in self._values:
            return False
        self._values.add(turn_id)
        self._order.append(turn_id)
        while len(self._order) > self._limit:
            self._values.discard(self._order.popleft())
        return True


@dataclass(frozen=True)
class _ExternalTurn:
    turn_id: str
    client_message_id: str | None
    user_message_id: str | None
    surface_id: str | None
    first_frame: dict[str, Any]
    subscription: Any


_EXTERNAL_TURN_DISCOVERY_CLOSED = object()


class _ExternalTurnClient:
    """Feed one already-accepted external turn through the existing renderer."""

    def __init__(
        self,
        client: Any,
        subscription: Any,
        first_frame: dict[str, Any],
        *,
        turn_id: str,
        client_message_id: str | None,
    ) -> None:
        self._client = client
        self._subscription = subscription
        self._first_frame = first_frame
        self._turn_id = turn_id
        self._client_message_id = client_message_id

    async def send_message(
        self,
        session_key: str,
        message: str,
        attachments: list[dict] | None = None,
        elevated: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        del session_key, message, attachments, elevated
        if self._client_message_id is not None:
            from opensquilla.cli.tui.backend.input_identity import (
                notify_tui_turn_identity,
            )

            await notify_tui_turn_identity(
                self._turn_id,
                self._client_message_id,
            )
        from opensquilla.cli.gateway_client import _advance_gateway_turn_event

        active_groups: set[str] = set()
        first: dict[str, Any] | None = self._first_frame
        while True:
            frame = first if first is not None else await self._subscription.get()
            first = None
            event = _flatten_event_frame(frame)
            turn_id, client_message_id, _surface_id = _event_identity(event)
            if turn_id is not None and turn_id != self._turn_id:
                continue
            if (
                self._client_message_id is not None
                and client_message_id is not None
                and client_message_id != self._client_message_id
            ):
                continue
            event_name = str(event.get("event") or "")
            payload = {key: value for key, value in event.items() if key != "event"}
            normalized, terminal = _advance_gateway_turn_event(
                event_name,
                payload,
                active_groups,
            )
            yield normalized
            if terminal:
                return

    async def resolve_approval(
        self,
        approval_id: str,
        approved: bool,
        *,
        choice: str | None = None,
    ) -> Any:
        return await self._client.resolve_approval(approval_id, approved, choice=choice)

    async def abort_session(self, key: str) -> dict[str, Any]:
        # Cancelling the local projection (surface exit/session switch) must not
        # cancel a turn initiated by WebUI or another TUI.
        return {"aborted": False, "key": key}


async def _external_prompt(
    client: Any,
    session_key: str,
    user_message_id: str | None,
    surface_id: str | None,
) -> str:
    if user_message_id:
        with suppress(Exception):
            snapshot = await client.bootstrap_session(session_key, limit=200)
            history = snapshot.get("history") if isinstance(snapshot, dict) else None
            messages = history.get("messages") if isinstance(history, dict) else None
            if isinstance(messages, list):
                for row in reversed(messages):
                    if not isinstance(row, dict):
                        continue
                    row_id = row.get("message_id") or row.get("messageId") or row.get("id")
                    if row_id == user_message_id:
                        text = row.get("text") or row.get("content")
                        if isinstance(text, str) and text:
                            return text
    label = surface_id or "another client"
    return f"Message from {label}"


async def _wait_for_output(
    deps: GatewayRuntimeDependencies,
    scope: GatewayRuntimeScope,
) -> ChatOutputHandle | None:
    while True:
        output = deps.get_tui_output(scope)
        if output is not None:
            return output
        await asyncio.sleep(0.01)


async def _watch_approval_events(
    subscription: Any,
    *,
    deps: GatewayRuntimeDependencies,
    scope: GatewayRuntimeScope,
) -> None:
    output: ChatOutputHandle | None = None
    try:
        async for frame in subscription:
            payload = frame.get("payload")
            event = payload if isinstance(payload, dict) else {}
            session_key = str(event.get("session_key") or "")
            current_key = str(scope.get("session_key") or "")
            if session_key and session_key != current_key:
                continue
            output = await _wait_for_output(deps, scope)
            event_name = str(frame.get("event") or "")
            approval_id = str(event.get("approval_id") or event.get("id") or "")
            if not approval_id:
                continue
            if event_name.endswith(".requested"):
                present = getattr(output, "present_gateway_approval", None)
                if callable(present):
                    await present(
                        {
                            "id": approval_id,
                            "tool": str(event.get("tool_name") or event.get("namespace") or "tool"),
                            "summary": str(event.get("command") or ""),
                            "choices": [],
                        }
                    )
            elif event_name.endswith(".resolved"):
                resolve = getattr(output, "resolve_gateway_approval", None)
                if callable(resolve):
                    await resolve(
                        approval_id,
                        approved=bool(event.get("approved")),
                        resolution=(str(event["resolution"]) if event.get("resolution") else None),
                    )
    except asyncio.CancelledError:
        raise
    except (ConnectionError, StopAsyncIteration):
        if output is None:
            output = deps.get_tui_output(scope)
        fail_pending = getattr(output, "fail_pending_gateway_approvals", None)
        if callable(fail_pending):
            await fail_pending()
            return
        cancel_pending = getattr(output, "cancel_pending_approvals", None)
        if callable(cancel_pending):
            cancel_pending()


async def _watch_model_routing_events(
    subscription: Any,
    *,
    deps: GatewayRuntimeDependencies,
    scope: GatewayRuntimeScope,
) -> None:
    """Mirror WebUI or another TUI's global strategy write immediately."""

    async for frame in subscription:
        if str(frame.get("event") or "") != "models.routing.changed":
            continue
        payload = frame.get("payload")
        snapshot = payload if isinstance(payload, dict) else {}
        output = await _wait_for_output(deps, scope)
        await send_model_routing_state(output, snapshot)


async def _mirror_external_turns(
    subscription: Any,
    *,
    client: Any,
    session_key: str,
    session_context: GatewaySessionContext,
    deps: GatewayRuntimeDependencies,
    elevated_state: dict[str, str | None],
    local_turn_idle: asyncio.Event,
    external_turn_idle: asyncio.Event,
) -> None:
    discovered = _BoundedTurnIds()
    completed = _BoundedTurnIds()
    turn_queue: asyncio.Queue[_ExternalTurn | BaseException | object] = asyncio.Queue(maxsize=64)
    open_turn_subscriptions: dict[str, Any] = {}

    async def _discover() -> None:
        try:
            async for frame in subscription:
                event = _flatten_event_frame(frame)
                event_name = str(event.get("event") or "")
                if not event_name.startswith("session.event."):
                    continue
                turn_id, client_message_id, surface_id = _event_identity(event)
                user_message_id = _event_user_message_id(event)
                if (
                    turn_id is None
                    or surface_id in {None, client.surface_id}
                    or turn_id in discovered
                    or turn_id in completed
                ):
                    continue
                discovered.add(turn_id)
                turn_subscription = await client.subscribe_session_events(
                    session_key,
                    since_stream_seq=_event_stream_seq(frame),
                )
                bind_turn = getattr(turn_subscription, "bind_turn", None)
                if callable(bind_turn):
                    bind_turn(
                        turn_id=turn_id,
                        client_message_id=client_message_id,
                    )
                open_turn_subscriptions[turn_id] = turn_subscription
                await turn_queue.put(
                    _ExternalTurn(
                        turn_id=turn_id,
                        client_message_id=client_message_id,
                        user_message_id=user_message_id,
                        surface_id=surface_id,
                        first_frame=frame,
                        subscription=turn_subscription,
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await turn_queue.put(exc)
        finally:
            await turn_queue.put(_EXTERNAL_TURN_DISCOVERY_CLOSED)

    discovery_task = asyncio.create_task(_discover())
    try:
        while True:
            item = await turn_queue.get()
            if item is _EXTERNAL_TURN_DISCOVERY_CLOSED:
                return
            if isinstance(item, BaseException):
                raise item
            assert isinstance(item, _ExternalTurn)
            await local_turn_idle.wait()
            external_turn_idle.clear()
            try:
                output = await _wait_for_output(deps, session_context.scope)
                prompt = await _external_prompt(
                    client,
                    session_key,
                    item.user_message_id or item.client_message_id,
                    item.surface_id,
                )
                send = getattr(output, "send_message", None)
                if callable(send):
                    await send(
                        "prompt.echo",
                        {
                            "text": prompt,
                            "client_message_id": item.client_message_id,
                        },
                    )
                external_client = _ExternalTurnClient(
                    client,
                    item.subscription,
                    item.first_frame,
                    turn_id=item.turn_id,
                    client_message_id=item.client_message_id,
                )
                from opensquilla.cli.tui.backend.input_identity import (
                    tui_input_identity_scope,
                )

                with tui_input_identity_scope(item.client_message_id):
                    result = await deps.stream_response(
                        cast(GatewayClientLike, external_client),
                        session_key,
                        prompt,
                        elevated_state,
                        tui_output=output,
                    )
                if session_context.session_key == session_key:
                    session_context.state.model = result.model_after or session_context.model
                    session_context.state.transcript.add("user", prompt)
                    session_context.state.transcript.add("assistant", result.text)
                    session_context.state.usage.apply(result.usage)
                    session_context.sync_from_state()
                    await send_context_patch(output, model=session_context.model or "default")
            finally:
                completed.add(item.turn_id)
                turn_subscription = open_turn_subscriptions.pop(item.turn_id, None)
                close = getattr(turn_subscription, "close", None)
                if callable(close):
                    with suppress(Exception):
                        await close()
                external_turn_idle.set()
    finally:
        discovery_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await discovery_task
        for turn_subscription in tuple(open_turn_subscriptions.values()):
            close = getattr(turn_subscription, "close", None)
            if callable(close):
                with suppress(Exception):
                    await close()
        external_turn_idle.set()


async def run_gateway_chat(
    *,
    model: str | None,
    session_id: str | None,
    deps: GatewayRuntimeDependencies,
) -> SessionExitSummary:
    """Run gateway chat without owning a concrete terminal application."""
    from opensquilla.cli.gateway_client import GatewayClient, GatewayRPCError
    from opensquilla.cli.gateway_rpc import default_gateway_token, default_gateway_url
    from opensquilla.cli.tui.opentui.history import (
        HISTORY_BOOTSTRAP_LIMIT,
        apply_bootstrap_to_state,
        history_replace_from_bootstrap,
        replace_tui_history,
    )

    client = GatewayClient()
    await client.connect(default_gateway_url(), token=default_gateway_token())

    elevated_state: dict[str, str | None] = {"mode": None}

    exit_reason: GatewayExitReason = "surface_closed"
    final_title: str | None = None
    final_model: str | None = model
    final_session_key: str | None = session_id
    final_active = False
    final_queued = 0
    session_subscription: Any | None = None
    session_observer_task: asyncio.Task[None] | None = None
    approval_subscription: Any | None = None
    approval_observer_task: asyncio.Task[None] | None = None
    routing_subscription: Any | None = None
    routing_observer_task: asyncio.Task[None] | None = None
    local_turn_idle = asyncio.Event()
    local_turn_idle.set()
    external_turn_idle = asyncio.Event()
    external_turn_idle.set()
    try:
        if session_id:
            session_key = session_id
            final_session_key = session_key
        else:
            session_key = await client.create_session(model=model)
            final_session_key = session_key
        # Bootstrap before the alternate-screen surface opens so the first
        # input frame can never race ahead of canonical durable history.
        snapshot = await client.bootstrap_session(
            session_key,
            limit=HISTORY_BOOTSTRAP_LIMIT,
        )
        # Announce a created/resumed session only after Gateway canonical state
        # proves that the key exists. An explicit missing ``--session`` must not
        # first claim that it is being resumed and then fail.
        if session_id:
            deps.notify(GatewayRuntimeNotice(kind="resumed", session_key=session_key))
            if model:
                deps.notify(GatewayRuntimeNotice(kind="resume_model_ignored"))
        else:
            deps.notify(GatewayRuntimeNotice(kind="created", session_key=session_key))
            if model:
                deps.notify(GatewayRuntimeNotice(kind="model", model=model))
        history_replace = history_replace_from_bootstrap(
            snapshot,
            fallback_session_key=session_key,
        )
        state = ChatSessionState(
            session_key=session_key,
            model=None if session_id else model,
        )
        apply_bootstrap_to_state(state, snapshot, history_replace)
        raw_session = snapshot.get("session")
        bootstrap_session = raw_session if isinstance(raw_session, dict) else {}
        final_title = (
            bootstrap_session.get("displayName")
            or bootstrap_session.get("display_name")
            or bootstrap_session.get("title")
        )
        final_model = state.model

        session_context = GatewaySessionContext.create(state)
        session_context.scope["history_replace"] = history_replace
        session_context.scope["bootstrap"] = snapshot
        workspace = bootstrap_session.get("workspace")
        if isinstance(workspace, str) and workspace:
            session_context.scope["workspace_label"] = workspace
        active_turn_session_key: str | None = None

        async def _stop_session_observer() -> None:
            nonlocal session_subscription, session_observer_task
            task = session_observer_task
            session_observer_task = None
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
            subscription = session_subscription
            session_subscription = None
            close = getattr(subscription, "close", None)
            if callable(close):
                with suppress(Exception):
                    await close()

        async def _start_session_observer(
            observed_key: str,
            *,
            stream_cursor: int | None,
        ) -> None:
            nonlocal session_subscription, session_observer_task
            subscribe = getattr(client, "subscribe_session_events", None)
            if not callable(subscribe):
                return
            await _stop_session_observer()
            session_subscription = await subscribe(
                observed_key,
                since_stream_seq=stream_cursor,
            )
            if bool(getattr(session_subscription, "needs_resync", False)):
                gap_snapshot = await client.bootstrap_session(
                    observed_key,
                    limit=HISTORY_BOOTSTRAP_LIMIT,
                )
                gap_history = history_replace_from_bootstrap(
                    gap_snapshot,
                    fallback_session_key=observed_key,
                )
                if session_context.session_key == observed_key:
                    apply_bootstrap_to_state(
                        session_context.state,
                        gap_snapshot,
                        gap_history,
                    )
                    session_context.sync_from_state()
                    session_context.scope["bootstrap"] = gap_snapshot
                    session_context.scope["history_replace"] = gap_history
                    raw_gap_session = gap_snapshot.get("session")
                    gap_session = raw_gap_session if isinstance(raw_gap_session, dict) else {}
                    gap_workspace = gap_session.get("workspace")
                    if isinstance(gap_workspace, str) and gap_workspace:
                        session_context.scope["workspace_label"] = gap_workspace
                    gap_reason = getattr(session_subscription, "gap_reason", None)
                    if isinstance(gap_reason, str) and gap_reason:
                        session_context.scope["replay_gap_reason"] = gap_reason
                    output = deps.get_tui_output(session_context.scope)
                    if output is not None:
                        await replace_tui_history(output, gap_history)
                        await send_context_update(
                            output,
                            gap_snapshot,
                            model=session_context.model,
                            session_id=session_context.session_key,
                            permission=elevated_state.get("mode"),
                        )
            session_observer_task = asyncio.create_task(
                _mirror_external_turns(
                    session_subscription,
                    client=client,
                    session_key=observed_key,
                    session_context=session_context,
                    deps=deps,
                    elevated_state=elevated_state,
                    local_turn_idle=local_turn_idle,
                    external_turn_idle=external_turn_idle,
                )
            )

        stream_cursor = snapshot.get("stream_cursor")
        await _start_session_observer(
            session_key,
            stream_cursor=stream_cursor if isinstance(stream_cursor, int) else None,
        )
        subscribe_global = getattr(client, "subscribe_global_events", None)
        if callable(subscribe_global):
            approval_subscription = subscribe_global(_APPROVAL_EVENTS)
            approval_observer_task = asyncio.create_task(
                _watch_approval_events(
                    approval_subscription,
                    deps=deps,
                    scope=session_context.scope,
                )
            )
            # Routing control is additive. Older clients and narrow test
            # doubles may expose approval subscriptions only.
            try:
                routing_subscription = subscribe_global(_MODEL_ROUTING_EVENTS)
            except Exception:
                routing_subscription = None
            if routing_subscription is not None:
                routing_observer_task = asyncio.create_task(
                    _watch_model_routing_events(
                        routing_subscription,
                        deps=deps,
                        scope=session_context.scope,
                    )
                )

        deps.notify(GatewayRuntimeNotice(kind="welcome"))

        async def _dispatch_input(user_input: str) -> bool:
            nonlocal active_turn_session_key, exit_reason

            if user_input is None or deps.is_exit_command(user_input):
                exit_reason = "command"
                deps.notify(GatewayRuntimeNotice(kind="goodbye"))
                return False

            stripped = user_input.strip()
            if not stripped:
                return True

            if stripped.startswith("/"):
                slash_session_key = session_context.session_key
                try:
                    handled = await deps.handle_slash_command(
                        stripped,
                        session_context.state,
                        client,
                        elevated_state,
                        tui_output=deps.get_tui_output(session_context.scope),
                    )
                except GatewayRPCError as exc:
                    deps.notify(GatewayRuntimeNotice(kind="error", message=str(exc)))
                    return True
                if handled:
                    session_context.sync_from_state()
                    if session_context.session_key != slash_session_key and callable(
                        getattr(client, "subscribe_session_events", None)
                    ):
                        switch_snapshot = await client.bootstrap_session(
                            session_context.session_key,
                            limit=1,
                        )
                        session_context.scope["bootstrap"] = switch_snapshot
                        raw_switch_session = switch_snapshot.get("session")
                        switch_session = (
                            raw_switch_session if isinstance(raw_switch_session, dict) else {}
                        )
                        switch_workspace = switch_session.get("workspace")
                        if isinstance(switch_workspace, str) and switch_workspace:
                            session_context.scope["workspace_label"] = switch_workspace
                        else:
                            session_context.scope.pop("workspace_label", None)
                        await send_context_update(
                            deps.get_tui_output(session_context.scope),
                            switch_snapshot,
                            model=session_context.model,
                            session_id=session_context.session_key,
                            workspace_label=(
                                switch_workspace
                                if isinstance(switch_workspace, str)
                                else None
                            ),
                            permission=elevated_state.get("mode"),
                        )
                        switch_cursor = switch_snapshot.get("stream_cursor")
                        await _start_session_observer(
                            session_context.session_key,
                            stream_cursor=(
                                switch_cursor if isinstance(switch_cursor, int) else None
                            ),
                        )
                    return True
                deps.notify(GatewayRuntimeNotice(kind="unknown_command"))
                return True

            turn_session_key = session_context.session_key
            active_turn_session_key = turn_session_key
            if not external_turn_idle.is_set():
                # A mirrored turn from another surface (e.g. the Web UI) is
                # still streaming into this session. The submitted prompt was
                # already echoed, so without feedback the parked send is
                # indistinguishable from a hang.
                deps.notify(GatewayRuntimeNotice(kind="queued_behind_external"))
            await external_turn_idle.wait()
            local_turn_idle.clear()
            try:
                result = await deps.stream_response(
                    client,
                    turn_session_key,
                    user_input,
                    elevated_state,
                    tui_output=deps.get_tui_output(session_context.scope),
                )
            except GatewayRPCError as exc:
                deps.notify(GatewayRuntimeNotice(kind="error", message=str(exc)))
                return True
            finally:
                local_turn_idle.set()
                if active_turn_session_key == turn_session_key:
                    active_turn_session_key = None
            session_context.state.model = result.model_after or session_context.model
            session_context.state.transcript.add("user", user_input)
            session_context.state.transcript.add("assistant", result.text)
            session_context.state.usage.apply(result.usage)
            session_context.sync_from_state()
            await send_context_patch(
                deps.get_tui_output(session_context.scope),
                model=session_context.model or "default",
            )
            return True

        def _abort_active_turn() -> Awaitable[None]:
            nonlocal active_turn_session_key
            turn_session_key = active_turn_session_key
            active_turn_session_key = None

            async def _abort_captured_turn() -> None:
                if turn_session_key is None:
                    return
                await client.abort_session(turn_session_key)

            return _abort_captured_turn()

        async def _steer_active_turn(text: str) -> bool:
            turn_session_key = active_turn_session_key
            if turn_session_key is None:
                return False
            result = await client.steer_session(turn_session_key, text)
            return bool(result.get("accepted"))

        from opensquilla.cli.tui.opentui.host_runtime import HostRuntimeError

        try:
            input_loop_kwargs: dict[str, Any] = {
                "scope": session_context.scope,
                "dispatch": _dispatch_input,
                "abort_active_turn": _abort_active_turn,
            }
            # Additive adapter compatibility: third-party/native input loops
            # compiled against the older callback contract keep working; the
            # OpenTUI bridge advertises the new keyword explicitly.
            with suppress(TypeError, ValueError):
                parameters = inspect.signature(deps.run_input_loop).parameters.values()
                if any(
                    parameter.name == "steer_active_turn"
                    or parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters
                ):
                    input_loop_kwargs["steer_active_turn"] = _steer_active_turn
            await deps.run_input_loop(**input_loop_kwargs)
        except ConnectionError:
            exit_reason = "gateway_disconnect"
        except HostRuntimeError:
            exit_reason = "host_crash"
        except Exception:  # noqa: BLE001 - return one receipt after surface teardown
            exit_reason = "runtime_error"
        final_session_key = session_context.session_key
        final_model = session_context.model
        try:
            receipt_snapshot = await asyncio.wait_for(
                client.bootstrap_session(final_session_key, limit=1),
                timeout=2.0,
            )
            raw_receipt_session = receipt_snapshot.get("session")
            resolved = raw_receipt_session if isinstance(raw_receipt_session, dict) else {}
            final_title = (
                resolved.get("displayName")
                or resolved.get("display_name")
                or resolved.get("title")
                or final_title
            )
            final_model = resolved.get("effective_model") or resolved.get("model") or final_model
            raw_queue = receipt_snapshot.get("queue")
            queue = raw_queue if isinstance(raw_queue, dict) else {}
            running_count = queue.get("running_count", queue.get("runningCount", 0))
            queued_count = queue.get("queued_count", queue.get("queuedCount", 0))
            final_active = (
                isinstance(running_count, int)
                and not isinstance(running_count, bool)
                and running_count > 0
            )
            final_queued = (
                queued_count
                if isinstance(queued_count, int)
                and not isinstance(queued_count, bool)
                and queued_count > 0
                else 0
            )
        except Exception:  # noqa: BLE001 - receipt must survive disconnects
            try:
                resolved = await asyncio.wait_for(
                    client.resolve_session(final_session_key),
                    timeout=2.0,
                )
                final_title = (
                    resolved.get("displayName")
                    or resolved.get("display_name")
                    or resolved.get("title")
                    or final_title
                )
                final_model = resolved.get("model") or final_model
            except Exception:  # noqa: BLE001 - retain the local receipt snapshot
                pass
        summary = SessionExitSummary(
            session_key=final_session_key,
            title=final_title,
            model=final_model,
            reason=exit_reason,
            active=final_active,
            queued=final_queued,
        )
        if exit_reason not in {"command", "surface_closed"}:
            raise SessionExitError(summary)
        return summary
    finally:
        if session_observer_task is not None:
            session_observer_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await session_observer_task
        close_session_subscription = getattr(session_subscription, "close", None)
        if callable(close_session_subscription):
            with suppress(Exception):
                await close_session_subscription()
        if approval_observer_task is not None:
            approval_observer_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await approval_observer_task
        close_approval_subscription = getattr(approval_subscription, "close", None)
        if callable(close_approval_subscription):
            with suppress(Exception):
                await close_approval_subscription()
        if routing_observer_task is not None:
            routing_observer_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await routing_observer_task
        close_routing_subscription = getattr(routing_subscription, "close", None)
        if callable(close_routing_subscription):
            with suppress(Exception):
                await close_routing_subscription()
        await client.close()
