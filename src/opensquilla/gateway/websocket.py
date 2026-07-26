"""WebSocket connection handler: handshake, frame parsing, event loop."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from opensquilla import __version__
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import (
    GatewayConfig,
    effective_agent_stream_idle_timeout_seconds,
    effective_webui_stream_idle_grace_seconds,
)
from opensquilla.gateway.protocol import (
    PREAUTH_TIMEOUT_MS,
    PROTOCOL_VERSION,
    WS_CLOSE_SERVICE_RESTART,
    HelloOk,
    PolicyInfo,
    ResFrame,
    ServerInfo,
    SnapshotInfo,
    make_error_res,
    make_event,
)
from opensquilla.gateway.rpc import RpcContext, RpcDispatcher

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Outbound writer queue primitives
# ---------------------------------------------------------------------------
#
# When the per-connection writer queue is enabled, every outbound frame
# (events, RPC responses, ticks) is enqueued from any producer task and
# drained sequentially by a dedicated writer task. WS-frame ``seq`` is
# minted by the writer at DEQUEUE time so that lossy drops never consume
# a seq number — the Vue RPC client in ``opensquilla-webui/src/lib/rpc.ts``
# closes the socket on any seq gap.
#
# ``_LOSSY_EVENTS`` is intentionally narrow: the lossy event MUST NOT be
# routed through ``SessionStreamRegistry.record()`` upstream, otherwise a
# silent drop here would create a ``stream_seq`` gap that could be rejected by
# the Vue client's ``utils/chat/streamEvents.ts:acceptStreamSeq`` reconciliation
# on reconnect. The only event that satisfies that constraint today is the
# liveness ``tick`` emitted from ``_tick_loop`` — its name is not prefixed
# ``session.event.`` so ``EventBridge.emit`` skips ``record()`` for it.
# Any future addition to this set MUST be verified against the same
# upstream invariant.
_LOSSY_EVENTS: frozenset[str] = frozenset({"tick"})

# Sentinel pushed into the outbox by ``_stop_writer`` to wake a writer
# blocked in ``await self._outbox.get()`` and exit cleanly.
_SENTINEL_STOP: Any = object()


@dataclass(slots=True)
class _OutboundFrame:
    """A frame queued for the writer task.

    ``seq`` is deliberately absent — it is minted by ``_writer_loop`` at
    dequeue time. ``kind`` is used by same-kind eviction; for events it is
    ``f"event:{event_name}"``, for RPC responses it is ``"res"``.
    """

    kind: str
    classification: str  # "lossy" or "control"
    payload: Any
    event_name: str | None
    res_frame: ResFrame | None
    meta: dict[str, Any] | None = None


def _payload_field(payload: Any, key: str) -> Any:
    """Best-effort extraction of a field from a payload dict; tolerates non-dicts."""
    if isinstance(payload, dict):
        return payload.get(key)
    return None


@dataclass
class WsConnection:
    """Represents a connected WebSocket client."""

    conn_id: str
    ws: WebSocket
    principal: Principal = field(
        default_factory=lambda: Principal(
            role="operator",
            scopes=frozenset(["operator.admin"]),
            is_owner=True,
            authenticated=False,
        )
    )
    connected_at: int = field(default_factory=lambda: int(time.time() * 1000))
    _seq: int = field(default=0, init=False)
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    # Writer-queue state.
    # ``_queue_enabled`` mirrors the kill-switch config at registration time;
    # once a connection starts in legacy mode it stays in legacy mode for life.
    _queue_enabled: bool = field(default=False, init=False, repr=False)
    _writer_queue_maxsize: int = field(default=512, init=False, repr=False)
    _outbox: asyncio.Queue[Any] | None = field(default=None, init=False, repr=False)
    _writer_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _closing: bool = field(default=False, init=False, repr=False)

    @property
    def role(self) -> str:
        return self.principal.role

    @property
    def scopes(self) -> list[str]:
        return list(self.principal.scopes)

    @property
    def authenticated(self) -> bool:
        return self.principal.authenticated

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # ------------------------------------------------------------------
    # Public send entry points
    # ------------------------------------------------------------------

    async def send_event(
        self,
        event: str,
        payload: Any = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        # Atomic check + enqueue. The check and ``put_nowait`` are part of
        # one synchronous flow with no ``await`` between them, so
        # ``_force_close`` cannot flip ``_closing`` mid-flight (asyncio is
        # single-threaded; only awaits yield).
        if (
            self._queue_enabled
            and self._outbox is not None
            and not self._closing
        ):
            classification = "lossy" if event in _LOSSY_EVENTS else "control"
            frame = _OutboundFrame(
                kind=f"event:{event}",
                classification=classification,
                payload=payload,
                event_name=event,
                res_frame=None,
                meta=meta,
            )
            self._enqueue_frame(frame)
            return
        # Legacy direct-send path (pre-auth, kill-switch off, or post-stop).
        async with self._send_lock:
            if self.ws.client_state == WebSocketState.CONNECTED:
                wire = make_event(event, payload, seq=self.next_seq(), meta=meta)
                await self.ws.send_text(wire.model_dump_json())

    async def send_res(self, frame: ResFrame) -> None:
        # RPC responses are always CONTROL: they carry state-bearing payloads
        # and a slow-client overflow must close the connection rather than
        # silently dropping the response.
        if (
            self._queue_enabled
            and self._outbox is not None
            and not self._closing
        ):
            outbound = _OutboundFrame(
                kind="res",
                classification="control",
                payload=None,
                event_name=None,
                res_frame=frame,
            )
            self._enqueue_frame(outbound)
            return
        async with self._send_lock:
            if self.ws.client_state == WebSocketState.CONNECTED:
                await self.ws.send_text(frame.model_dump_json())

    async def close(self, code: int = WS_CLOSE_SERVICE_RESTART, reason: str = "") -> None:
        try:
            await self.ws.close(code=code)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Writer task lifecycle
    # ------------------------------------------------------------------

    def _start_writer(self, *, maxsize: int, enabled: bool) -> None:
        """Idempotently boot the per-connection writer task.

        Called from ``handle_ws_connection`` immediately after
        ``registry.register(conn)``. Pre-auth sends do NOT go through the
        queue because the writer task does not exist yet — see Step 4 of
        the plan and the comment block at the registration call site.
        """
        if self._writer_task is not None:
            return
        self._queue_enabled = bool(enabled)
        self._writer_queue_maxsize = int(maxsize)
        if not self._queue_enabled:
            return
        self._outbox = asyncio.Queue(maxsize=self._writer_queue_maxsize)
        self._writer_task = asyncio.create_task(
            self._writer_loop(), name=f"ws-writer-{self.conn_id}"
        )
        log.debug("gateway.ws_writer_started", conn_id=self.conn_id)

    async def _stop_writer(self) -> None:
        """Idempotent writer shutdown for the disconnect path.

        Unlike ``_force_close`` this does NOT call ``ws.close()`` — clean
        disconnects are already signaled by ``WebSocketDisconnect`` and the
        socket is already torn down by the time we hit the ``finally`` of
        ``handle_ws_connection``. Calling ws.close() here would race with
        starlette's own teardown.
        """
        self._closing = True
        task = self._writer_task
        if task is None:
            return
        self._writer_task = None
        # Best-effort wakeup for a writer blocked in ``outbox.get()``.
        if self._outbox is not None:
            try:
                self._outbox.put_nowait(_SENTINEL_STOP)
            except asyncio.QueueFull:
                pass
        if not task.done():
            task.cancel()
            # NOTE: ``gather(..., return_exceptions=True)`` deliberately
            # absorbs the writer's CancelledError as a result *value* so
            # it does not propagate into this teardown path. Do NOT
            # replace this with ``await task`` — that re-raises
            # CancelledError into ``_stop_writer`` and corrupts the
            # cleanup sequence.
            try:
                await asyncio.wait_for(
                    asyncio.gather(task, return_exceptions=True),
                    timeout=2.0,
                )
            except TimeoutError:
                log.warning(
                    "gateway.ws_stop_writer_timeout",
                    conn_id=self.conn_id,
                )
        log.debug("gateway.ws_writer_stopped", conn_id=self.conn_id)

    async def _force_close(self, *, reason: str, code: int = 1011) -> None:
        """Forcefully tear down the connection due to writer backpressure.

        Idempotent. The ``_writer_task is None`` marker doubles as the
        "already-completed force_close" sentinel: the first invocation
        claims the task atomically, cancels it with a bounded timeout,
        then closes the socket. Concurrent invocations no-op.
        """
        self._closing = True
        task = self._writer_task
        if task is None:
            # Either there was never a writer (legacy mode) or another
            # force_close already ran. Either way: nothing to do.
            return
        # Atomically claim ownership so concurrent calls see _writer_task=None.
        self._writer_task = None
        if not task.done():
            task.cancel()
            # NOTE: ``gather(..., return_exceptions=True)`` deliberately
            # absorbs the writer's CancelledError as a result *value* so
            # it does not propagate into this teardown path. Do NOT
            # replace this with ``await task`` — that re-raises
            # CancelledError into ``_force_close`` and corrupts the close
            # sequence.
            try:
                await asyncio.wait_for(
                    asyncio.gather(task, return_exceptions=True),
                    timeout=2.0,
                )
            except TimeoutError:
                log.warning(
                    "gateway.ws_writer_force_close_timeout",
                    conn_id=self.conn_id,
                    reason=reason,
                )
        try:
            await self.ws.close(code=code, reason=reason)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Writer loop and enqueue helper
    # ------------------------------------------------------------------

    async def _writer_loop(self) -> None:
        """Drain ``_outbox`` and serialize frames onto the wire.

        WS-frame ``seq`` is minted here, at dequeue. This guarantees a
        contiguous monotonic ``seq`` even when producers' lossy frames are
        dropped by ``_enqueue_frame`` — drops never consume a seq.
        """
        assert self._outbox is not None
        try:
            while True:
                item = await self._outbox.get()
                if item is _SENTINEL_STOP or self._closing:
                    return
                if not isinstance(item, _OutboundFrame):
                    continue
                if self.ws.client_state != WebSocketState.CONNECTED:
                    return
                try:
                    if item.event_name is not None:
                        wire = make_event(
                            item.event_name,
                            item.payload,
                            seq=self.next_seq(),
                            meta=item.meta,
                        )
                        text = wire.model_dump_json()
                    elif item.res_frame is not None:
                        text = item.res_frame.model_dump_json()
                    else:
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A frame that cannot be serialized (e.g. a lone surrogate
                    # in a payload) must not silently kill this task: the
                    # socket would stay open, requests would keep executing,
                    # and no response would ever leave. Close instead so the
                    # reader loop tears the connection down normally.
                    log.warning(
                        "gateway.ws_frame_serialize_failed",
                        conn_id=self.conn_id,
                        exc_info=True,
                    )
                    self._closing = True
                    try:
                        await self.ws.close(code=1011)
                    except Exception:  # noqa: BLE001
                        pass
                    return
                try:
                    await self.ws.send_text(text)
                except WebSocketDisconnect:
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.debug(
                        "gateway.ws_writer_send_failed",
                        conn_id=self.conn_id,
                        exc_info=True,
                    )
                    return
        except asyncio.CancelledError:
            raise

    def _enqueue_frame(self, frame: _OutboundFrame) -> None:
        """Synchronous enqueue with classification-aware overflow.

        Caller has already verified ``_queue_enabled`` and ``not _closing``
        and that ``_outbox is not None``. This method MUST NOT ``await`` —
        a yield point here would let ``_force_close`` flip ``_closing``
        between the guard check in ``send_event`` and the enqueue mutation.
        """
        if self._outbox is None:
            return
        try:
            self._outbox.put_nowait(frame)
            return
        except asyncio.QueueFull:
            pass

        if frame.classification == "lossy":
            evicted = self._evict_oldest_same_kind(frame.kind)
            if evicted:
                try:
                    self._outbox.put_nowait(frame)
                    log.warning(
                        "gateway.ws_writer_drop",
                        conn_id=self.conn_id,
                        event_name=frame.event_name,
                        session_key=_payload_field(frame.payload, "session_key"),
                        stream_seq=_payload_field(frame.payload, "stream_seq"),
                        queue_depth=self._outbox.qsize(),
                        eviction=True,
                    )
                    return
                except asyncio.QueueFull:
                    pass
            # No same-kind candidate or impossibly rare race: drop the new
            # incoming frame to keep the close path moving.
            log.warning(
                "gateway.ws_writer_drop",
                conn_id=self.conn_id,
                event_name=frame.event_name,
                session_key=_payload_field(frame.payload, "session_key"),
                stream_seq=_payload_field(frame.payload, "stream_seq"),
                queue_depth=self._outbox.qsize(),
                eviction=False,
            )
            return

        # CONTROL overflow: cannot drop, cannot block. Schedule force-close.
        # Same-kind eviction policy note: under R-B the lossy set is {tick},
        # which has no session_key, so eviction is keyed on event_name only.
        # If the lossy set is later expanded to session-bearing events, the
        # eviction key MUST become (event_name, session_key) to prevent one
        # session's overflow from evicting another session's queued frame.
        # Keep this invariant if more lossy event kinds are added later.
        self._closing = True
        log.error(
            "gateway.ws_writer_overflow_close",
            conn_id=self.conn_id,
            event_name=frame.event_name,
            session_key=_payload_field(frame.payload, "session_key"),
            stream_seq=_payload_field(frame.payload, "stream_seq"),
            queue_depth=self._outbox.qsize(),
        )
        asyncio.create_task(
            self._force_close(reason="writer_backpressure", code=1011),
            name=f"ws-force-close-{self.conn_id}",
        )

    def _evict_oldest_same_kind(self, kind: str) -> bool:
        """Evict the oldest queued frame whose ``kind`` matches.

        Manipulates ``asyncio.Queue._queue`` directly. Safe under asyncio
        because this method is fully synchronous (no await points), and the
        deque is the documented backing store. ``qsize()`` reflects
        ``len(_queue)`` so deletion alone is sufficient bookkeeping for
        our use (we do not use ``join()``/``task_done()``).
        """
        if self._outbox is None:
            return False
        backing = self._outbox._queue  # type: ignore[attr-defined]
        for index, queued in enumerate(backing):
            if isinstance(queued, _OutboundFrame) and queued.kind == kind:
                del backing[index]
                return True
        return False


class ConnectionRegistry:
    """Tracks all active WebSocket connections."""

    def __init__(self) -> None:
        self._connections: dict[str, WsConnection] = {}

    def register(self, conn: WsConnection) -> None:
        self._connections[conn.conn_id] = conn

    def unregister(self, conn_id: str) -> None:
        self._connections.pop(conn_id, None)

    def get(self, conn_id: str) -> WsConnection | None:
        return self._connections.get(conn_id)

    def all(self) -> list[WsConnection]:
        return list(self._connections.values())

    async def broadcast(self, event: str, payload: Any = None) -> None:
        for conn in self.all():
            if conn.authenticated:
                try:
                    await conn.send_event(event, payload)
                except Exception:
                    pass


class SubscriptionManager:
    """Track which connections are subscribed to session-level and message-level events."""

    def __init__(self) -> None:
        self._session_subs: set[str] = set()  # conn_ids subscribed to session lifecycle
        self._message_subs: dict[str, set[str]] = {}  # session_key -> {conn_id}
        self._topic_subs: dict[str, set[str]] = {}  # topic -> {conn_id}

    # -- session-level (sessions.subscribe / sessions.unsubscribe) --

    def subscribe_sessions(self, conn_id: str) -> None:
        self._session_subs.add(conn_id)

    def unsubscribe_sessions(self, conn_id: str) -> None:
        self._session_subs.discard(conn_id)

    def get_session_subscribers(self) -> set[str]:
        return set(self._session_subs)

    # -- message-level (sessions.messages.subscribe / unsubscribe) --

    def subscribe_messages(self, conn_id: str, session_key: str) -> None:
        self._message_subs.setdefault(session_key, set()).add(conn_id)

    def unsubscribe_messages(self, conn_id: str, session_key: str) -> None:
        if session_key in self._message_subs:
            self._message_subs[session_key].discard(conn_id)

    def get_message_subscribers(self, session_key: str) -> set[str]:
        return set(self._message_subs.get(session_key, set()))

    # -- topic-level (cron.subscribe / cron.unsubscribe) --

    def subscribe_topic(self, conn_id: str, topic: str) -> None:
        self._topic_subs.setdefault(topic, set()).add(conn_id)

    def unsubscribe_topic(self, conn_id: str, topic: str) -> None:
        if topic in self._topic_subs:
            self._topic_subs[topic].discard(conn_id)
            if not self._topic_subs[topic]:
                del self._topic_subs[topic]

    def get_topic_subscribers(self, topic: str) -> set[str]:
        return set(self._topic_subs.get(topic, set()))

    def remove_connection(self, conn_id: str) -> None:
        """Clean up all subscriptions for a disconnected connection."""
        self._session_subs.discard(conn_id)
        for subs in self._message_subs.values():
            subs.discard(conn_id)
        empty_topics = []
        for topic, subs in self._topic_subs.items():
            subs.discard(conn_id)
            if not subs:
                empty_topics.append(topic)
        for topic in empty_topics:
            del self._topic_subs[topic]


# Module-level registry shared across connections
_registry = ConnectionRegistry()


def get_registry() -> ConnectionRegistry:
    return _registry


def _is_wire_text(value: str) -> bool:
    """True when ``value`` can be re-serialized onto the wire as UTF-8.

    Valid JSON may carry lone-surrogate escapes (``"\\ud800"``); echoing one
    into a response frame makes ``model_dump_json`` raise at send time, long
    after the handler ran.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _wire_frame_id(raw_id: Any, fallback: str = "") -> str:
    """Best-effort string id for correlating a response to a client frame.

    ``ResFrame.id`` must be a string, but a client may send any JSON value.
    Scalar ids are echoed back stringified so the client can still correlate
    the error; container and non-encodable ids fall back to ``fallback``.
    """
    if isinstance(raw_id, str):
        return raw_id if _is_wire_text(raw_id) else fallback
    if isinstance(raw_id, bool | int | float):
        return str(raw_id)
    return fallback


async def handle_ws_connection(
    ws: WebSocket,
    config: GatewayConfig,
    dispatcher: RpcDispatcher,
    session_manager: Any = None,
    provider_selector: Any = None,
    tool_registry: Any = None,
    subscription_manager: Any = None,
    channel_manager: Any = None,
    usage_tracker: Any = None,
    usage_event_sink: Any = None,
    meta_run_writer: Any = None,
    skill_loader: Any = None,
    cron_scheduler: Any = None,
    turn_runner: Any = None,
    task_runtime: Any = None,
    flush_service: Any = None,
    heartbeat_service: Any = None,
    heartbeat_loop: Any = None,
    agent_registry: Any = None,
    diagnostics_state: Any = None,
    provider_stats: Any = None,
    memory_managers: dict[str, Any] | None = None,
    memory_stores: dict[str, Any] | None = None,
    memory_retrievers: dict[str, Any] | None = None,
) -> None:
    """Main WebSocket connection handler."""
    conn_id = str(uuid.uuid4())
    conn = WsConnection(conn_id=conn_id, ws=ws)
    registry = get_registry()

    await ws.accept()
    log.info("ws.connected", conn_id=conn_id, remote=str(ws.client))

    # Step 1: Send connect.challenge
    nonce = str(uuid.uuid4())
    try:
        await conn.send_event("connect.challenge", {"nonce": nonce})
    except WebSocketDisconnect:
        return

    # Step 2: Pre-auth timeout — client must send connect request
    try:
        preauth_timeout = PREAUTH_TIMEOUT_MS / 1000
        raw = await asyncio.wait_for(ws.receive_text(), timeout=preauth_timeout)
    except TimeoutError:
        log.warning("ws.preauth_timeout", conn_id=conn_id)
        await conn.close()
        return
    except WebSocketDisconnect:
        return

    # Step 3: Parse the connect request
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        # ValueError covers JSONDecodeError plus non-decode parse failures
        # (e.g. the int-digit conversion limit); RecursionError covers
        # pathological nesting depth.
        await conn.send_res(
            make_error_res("handshake", "INVALID_REQUEST", "Invalid JSON in connect frame")
        )
        await conn.close()
        return
    if not isinstance(data, dict):
        await conn.send_res(
            make_error_res("handshake", "INVALID_REQUEST", "Connect frame must be a JSON object")
        )
        await conn.close()
        return

    if data.get("type") != "req" or data.get("method") != "connect":
        await conn.send_res(
            make_error_res(
                _wire_frame_id(data.get("id"), "handshake"),
                "INVALID_REQUEST",
                "First message must be connect request",
            )
        )
        await conn.close()
        return

    req_id = _wire_frame_id(data.get("id"), "handshake")
    params_raw = data.get("params")
    if not isinstance(params_raw, dict):
        params_raw = {}

    # Step 4: Resolve auth via server-side ScopeResolver
    from opensquilla.gateway.auth import resolve_auth

    auth_params = params_raw.get("auth")
    if not isinstance(auth_params, dict):
        auth_params = {}
    role_claim = params_raw.get("role", "operator")
    if not isinstance(role_claim, str):
        role_claim = "operator"
    peer_ip = ws.client.host if ws.client is not None else None
    principal = resolve_auth(
        config,
        auth_params=auth_params,
        role_claim=role_claim,
        peer_ip=peer_ip,
    )
    if principal is None:
        await conn.send_res(make_error_res(req_id, "UNAUTHORIZED", "Authentication failed"))
        await conn.close()
        return

    from opensquilla.sandbox.run_mode_policy import hello_auth_payload

    # Step 5: Negotiate protocol version
    min_proto = params_raw.get("minProtocol", 1)
    max_proto = params_raw.get("maxProtocol", PROTOCOL_VERSION)
    if not all(
        isinstance(bound, int) and not isinstance(bound, bool)
        for bound in (min_proto, max_proto)
    ):
        await conn.send_res(
            make_error_res(
                req_id, "INVALID_REQUEST", "minProtocol and maxProtocol must be integers"
            )
        )
        await conn.close()
        return
    negotiated = min(max_proto, PROTOCOL_VERSION)
    if negotiated < min_proto:
        await conn.send_res(
            make_error_res(req_id, "INVALID_REQUEST", "Unsupported protocol version range")
        )
        await conn.close()
        return

    # Assign principal
    conn.principal = principal

    # Step 6: Send HelloOk
    hello = HelloOk(
        protocol=negotiated,
        server=ServerInfo(version=__version__, conn_id=conn_id),
        features=_build_features(dispatcher),
        snapshot=SnapshotInfo(
            uptime_ms=int(time.time() * 1000),
            config_path=config.config_path,
            state_dir=config.state_dir,
            auth_mode=config.auth.mode,
        ),
        policy=PolicyInfo(
            agent_stream_heartbeat_interval_ms=int(
                max(0.0, float(getattr(config, "agent_stream_heartbeat_interval_seconds", 15.0)))
                * 1000
            ),
            agent_stream_idle_timeout_ms=int(
                effective_agent_stream_idle_timeout_seconds(config) * 1000
            ),
            webui_stream_idle_grace_ms=int(
                effective_webui_stream_idle_grace_seconds(config) * 1000
            ),
            client_ws_keepalive_timeout_ms=int(
                max(0.0, float(getattr(config, "client_ws_keepalive_timeout_s", 120.0)))
                * 1000
            ),
        ),
        auth=hello_auth_payload(principal),
    )
    await ws.send_text(hello.model_dump_json())

    registry.register(conn)
    # Boundary: pre-auth direct-send ends here. After registry.register(conn),
    # conn._writer_task owns all post-auth sends. send_event/send_res route
    # through conn._outbox; WS-frame seq is minted at dequeue inside the
    # writer loop (NOT at enqueue), so dropped lossy frames never consume a
    # seq number.
    # Kill switch (config.ws_writer_queue_enabled) is read here at registration
    # time only — affects new connections only; existing connections retain
    # their startup-time behavior.
    conn._start_writer(
        maxsize=config.ws_writer_queue_maxsize,
        enabled=config.ws_writer_queue_enabled,
    )
    log.info("ws.authenticated", conn_id=conn_id, role=conn.role)

    # Step 7: Main message loop
    tick_task = asyncio.create_task(_tick_loop(conn, hello.policy.tick_interval_ms))
    try:
        await _message_loop(
            conn,
            config,
            dispatcher,
            session_manager,
            provider_selector,
            tool_registry,
            subscription_manager,
            channel_manager,
            usage_tracker,
            usage_event_sink,
            meta_run_writer,
            skill_loader,
            cron_scheduler,
            turn_runner,
            task_runtime,
            flush_service,
            heartbeat_service,
            heartbeat_loop,
            agent_registry,
            diagnostics_state,
            memory_managers,
            memory_stores,
            memory_retrievers,
            provider_stats=provider_stats,
        )
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.error("ws.error", conn_id=conn_id, error=str(exc))
    finally:
        # Stop the writer FIRST, before tick_task.cancel() and before
        # registry.unregister. Otherwise an EventBridge.emit on another
        # coroutine could still hold a reference to this connection while
        # the writer task is mid-cancel, producing a "zombie writer"
        # scenario.
        await conn._stop_writer()
        tick_task.cancel()
        try:
            await tick_task
        except asyncio.CancelledError:
            pass
        registry.unregister(conn_id)
        if subscription_manager is not None:
            subscription_manager.remove_connection(conn_id)
        log.info("ws.disconnected", conn_id=conn_id)


async def _tick_loop(conn: WsConnection, tick_interval_ms: int) -> None:
    interval_s = max(1.0, tick_interval_ms / 1000)
    while True:
        await asyncio.sleep(interval_s)
        try:
            await conn.send_event("tick", {"time_ms": int(time.time() * 1000)})
        except Exception:
            log.debug("ws.tick_failed", conn_id=conn.conn_id, exc_info=True)
            return


async def _message_loop(
    conn: WsConnection,
    config: GatewayConfig,
    dispatcher: RpcDispatcher,
    session_manager: Any,
    provider_selector: Any = None,
    tool_registry: Any = None,
    subscription_manager: Any = None,
    channel_manager: Any = None,
    usage_tracker: Any = None,
    usage_event_sink: Any = None,
    meta_run_writer: Any = None,
    skill_loader: Any = None,
    cron_scheduler: Any = None,
    turn_runner: Any = None,
    task_runtime: Any = None,
    flush_service: Any = None,
    heartbeat_service: Any = None,
    heartbeat_loop: Any = None,
    agent_registry: Any = None,
    diagnostics_state: Any = None,
    memory_managers: dict[str, Any] | None = None,
    memory_stores: dict[str, Any] | None = None,
    memory_retrievers: dict[str, Any] | None = None,
    provider_stats: Any = None,
) -> None:
    ws = conn.ws
    keepalive_timeout = max(0.0, float(getattr(config, "client_ws_keepalive_timeout_s", 0.0)))
    while True:
        try:
            if keepalive_timeout > 0.0:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=keepalive_timeout)
            else:
                raw = await ws.receive_text()
        except WebSocketDisconnect:
            return
        except TimeoutError:
            log.warning(
                "gateway.client_ws_keepalive_timeout",
                conn_id=conn.conn_id,
                timeout_s=keepalive_timeout,
            )
            try:
                await ws.close(code=1011)
            except Exception:  # noqa: BLE001
                pass
            return

        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            # ValueError covers JSONDecodeError plus non-decode parse failures
            # (e.g. the int-digit conversion limit); RecursionError covers
            # pathological nesting depth.
            await conn.send_res(make_error_res("", "INVALID_REQUEST", "Invalid JSON"))
            continue
        if not isinstance(data, dict):
            await conn.send_res(
                make_error_res("", "INVALID_REQUEST", "Frame must be a JSON object")
            )
            continue

        frame_type = data.get("type")

        if frame_type == "ping":
            await ws.send_text('{"type":"pong"}')
            continue

        if frame_type == "pong":
            continue

        if frame_type == "req":
            req_id = data.get("id", "")
            method = data.get("method", "")
            if (
                not isinstance(req_id, str)
                or not isinstance(method, str)
                or not _is_wire_text(req_id)
                or not _is_wire_text(method)
            ):
                # A non-string or non-encodable id/method would fail ResFrame
                # validation or serialization after the handler already ran;
                # reject the frame here so one malformed request cannot kill
                # the connection.
                await conn.send_res(
                    make_error_res(
                        _wire_frame_id(req_id),
                        "INVALID_REQUEST",
                        "Frame id and method must be UTF-8-encodable strings",
                    )
                )
                continue
            params = data.get("params")

            ctx = RpcContext(
                conn_id=conn.conn_id,
                principal=conn.principal,
                session_manager=session_manager,
                config=config,
                provider_selector=provider_selector,
                tool_registry=tool_registry,
                subscription_manager=subscription_manager,
                # Live reconcile can create the manager after this connection
                # opened; a callable is re-resolved per request so long-lived
                # console sockets see it.
                channel_manager=(
                    channel_manager() if callable(channel_manager) else channel_manager
                ),
                usage_tracker=usage_tracker,
                usage_event_sink=usage_event_sink,
                meta_run_writer=meta_run_writer,
                skill_loader=skill_loader,
                cron_scheduler=cron_scheduler,
                turn_runner=turn_runner,
                task_runtime=task_runtime,
                flush_service=flush_service,
                heartbeat_service=heartbeat_service,
                heartbeat_loop=heartbeat_loop,
                agent_registry=agent_registry,
                diagnostics_state=diagnostics_state,
                provider_stats=provider_stats,
                memory_managers=memory_managers or {},
                memory_stores=memory_stores or {},
                memory_retrievers=memory_retrievers or {},
            )
            res = await dispatcher.dispatch(req_id, method, params, ctx)
            await conn.send_res(res)
        else:
            # repr keeps the echo serializable for any client value (lone
            # surrogates escape to backslash form).
            await conn.send_res(
                make_error_res("", "INVALID_REQUEST", f"Unknown frame type: {frame_type!r}")
            )


def _build_features(dispatcher: RpcDispatcher) -> Any:
    from opensquilla.gateway.protocol import FeaturesInfo

    methods = dispatcher.list_methods()
    events = [
        "connect.challenge",
        "agent",
        "session.message",
        "sessions.changed",
        "presence",
        "tick",
        "shutdown",
        "health",
        "heartbeat",
        "cron",
    ]
    return FeaturesInfo(methods=methods, events=events)
