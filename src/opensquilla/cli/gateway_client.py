"""WebSocket client for connecting to OpenSquilla gateway daemon."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlparse

from opensquilla.session.terminal_reply import build_terminal_reply, sanitize_agent_error


class GatewayRPCError(Exception):
    """Operator-facing RPC failure raised by GatewayClient."""

    def __init__(
        self,
        method: str,
        *,
        code: str | None = None,
        message: str = "RPC failed",
        data: dict | None = None,
    ) -> None:
        self.method = method
        self.code = code
        self.message = message
        self.data = data
        super().__init__(self.__str__())

    def __str__(self) -> str:
        code = f"{self.code}: " if self.code else ""
        return f"{self.method} failed: {code}{self.message}"


def _history_message_identity(message: dict[str, Any]) -> tuple[str, str] | None:
    """Return the stable identity shared by paginated history responses."""

    message_id = message.get("message_id") or message.get("id")
    if message_id not in (None, ""):
        return ("message", str(message_id))
    transcript_id = message.get("transcript_id")
    if transcript_id not in (None, ""):
        return ("transcript", str(transcript_id))
    return None


def _history_cursor_key(value: object) -> tuple[int, int] | None:
    """Parse the stable numeric key returned by canonical history pages."""

    raw = str(value or "").strip()
    if not raw or "|" not in raw:
        return None
    created_at, transcript_id = raw.split("|", 1)
    try:
        return int(created_at), int(transcript_id)
    except ValueError:
        return None


async def session_history_all(
    session_history: Callable[..., Awaitable[dict[str, Any]]],
    session_key: str,
    *,
    page_size: int = 200,
) -> dict[str, Any]:
    """Read every canonical history page without silently exporting a partial session.

    ``chat.history`` returns the newest page first. Older pages are addressed by
    the response's exclusive ``oldest_cursor``. Anonymous legacy messages are
    retained; only messages with a stable gateway identity are deduplicated.
    """

    limit = max(1, min(int(page_size), 200))
    before: str | None = None
    seen_cursors: set[str] = set()
    pages: list[list[dict[str, Any]]] = []
    newest_response: dict[str, Any] | None = None
    oldest_response: dict[str, Any] | None = None

    while True:
        response = await session_history(
            session_key,
            limit=limit,
            before=before,
            include_canonical=True,
            include_summaries=False,
        )
        if not isinstance(response, dict):
            raise GatewayRPCError(
                "chat.history",
                code="INVALID_HISTORY_PAGE",
                message="gateway returned a non-object history page",
            )
        if response.get("canonical_available") is False:
            raise GatewayRPCError(
                "chat.history",
                code="CANONICAL_HISTORY_UNAVAILABLE",
                message=(
                    "complete canonical history is temporarily unavailable; "
                    "export was cancelled"
                ),
            )
        if response.get("canonical_complete") is False:
            raise GatewayRPCError(
                "chat.history",
                code="CANONICAL_HISTORY_INCOMPLETE",
                message="older original messages were not preserved; export was cancelled",
            )
        raw_messages = response.get("messages")
        if not isinstance(raw_messages, list):
            raise GatewayRPCError(
                "chat.history",
                code="INVALID_HISTORY_PAGE",
                message="gateway history page did not contain a messages list",
            )
        has_more = bool(response.get("has_more"))
        next_before: str | None = None
        if has_more:
            raw_cursor = response.get("oldest_cursor")
            next_before = str(raw_cursor).strip() if raw_cursor is not None else ""
            if not next_before or next_before == before or next_before in seen_cursors:
                raise GatewayRPCError(
                    "chat.history",
                    code="HISTORY_PAGINATION_STALLED",
                    message="gateway history cursor did not advance; export was cancelled",
                )
        if before is not None:
            requested_key = _history_cursor_key(before)
            newest_key = _history_cursor_key(response.get("newest_cursor"))
            if requested_key is None or newest_key is None or newest_key >= requested_key:
                raise GatewayRPCError(
                    "chat.history",
                    code="HISTORY_CURSOR_INVALIDATED",
                    message=(
                        "gateway history no longer precedes the requested cursor; "
                        "the session may have changed and export was cancelled"
                    ),
                )
        pages.append([message for message in raw_messages if isinstance(message, dict)])
        newest_response = newest_response or response
        oldest_response = response

        if not has_more:
            break

        assert next_before is not None
        seen_cursors.add(next_before)
        before = next_before

    merged: list[dict[str, Any]] = []
    seen_messages: set[tuple[str, str]] = set()
    for page in reversed(pages):
        for message in page:
            identity = _history_message_identity(message)
            if identity is not None:
                if identity in seen_messages:
                    continue
                seen_messages.add(identity)
            merged.append(message)

    result = dict(oldest_response or newest_response or {})
    result["messages"] = merged
    result["has_more"] = False
    result["loaded_count"] = len(merged)
    result["page_size"] = limit
    if newest_response is not None:
        result["newest_cursor"] = newest_response.get("newest_cursor")
    return result


def gateway_base_is_local(base_url: str | None) -> bool:
    """Return True for loopback/same-machine gateway origins.

    Unknown, unparsable, or non-loopback hosts fail closed so CLI `/path`
    cannot confuse a remote gateway with files on the operator's machine.
    """

    if not base_url:
        return False
    parsed = urlparse(base_url)
    host = parsed.hostname
    if not host:
        return False
    normalized = host.strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


_SUBSCRIPTION_CLOSED = object()


@dataclass
class GatewayEventSubscription:
    """Independent filtered view over the client's single WebSocket reader."""

    _client: GatewayClient
    subscription_id: int
    session_key: str | None = None
    event_names: frozenset[str] = frozenset()
    turn_id: str | None = None
    client_message_id: str | None = None
    replay: dict[str, Any] = field(default_factory=dict)
    _queue: asyncio.Queue[dict[str, Any] | BaseException | object] = field(
        default_factory=asyncio.Queue
    )
    _closed: bool = False
    _active: bool = True
    _pending_live: list[dict[str, Any]] = field(default_factory=list)
    _seen_stream_seqs: set[int] = field(default_factory=set)
    _seen_stream_seq_order: deque[int] = field(default_factory=deque)

    @property
    def gap_reason(self) -> str | None:
        """Explain why replay could not cover the requested cursor."""

        value = (
            self.replay.get("replay_gap_reason")
            or self.replay.get("replayGapReason")
            or self.replay.get("gap_reason")
        )
        return value if isinstance(value, str) and value else None

    @property
    def needs_resync(self) -> bool:
        """Return whether canonical bootstrap is required before live use."""

        replay_complete = self.replay.get("replay_complete", self.replay.get("replayComplete"))
        return replay_complete is False or self.gap_reason is not None

    def bind_turn(
        self,
        *,
        turn_id: str | None,
        client_message_id: str | None,
    ) -> None:
        """Narrow a session stream once sessions.send returns its identity."""

        self.turn_id = turn_id
        self.client_message_id = client_message_id

    def matches(self, frame: dict[str, Any]) -> bool:
        event_name = str(frame.get("event") or "")
        if self.event_names and event_name not in self.event_names:
            return False
        if (
            self.session_key is not None
            and not self.event_names
            and not (event_name.startswith("session.event.") or event_name.startswith("task."))
        ):
            return False
        payload = frame.get("payload")
        event = payload if isinstance(payload, dict) else {}
        if self.session_key is not None:
            event_session = event.get("session_key") or event.get("key")
            if event_session != self.session_key:
                return False
        for field_name, expected in (
            ("turn_id", self.turn_id),
            ("client_message_id", self.client_message_id),
        ):
            actual = event.get(field_name)
            # Current gateways stamp both identities. Missing fields remain
            # tolerated for old terminal/task events; a conflicting identity
            # is always foreign and must stay out of this local turn.
            if expected is not None and actual is not None and actual != expected:
                return False
        return True

    async def get(self) -> dict[str, Any]:
        item = await self._queue.get()
        if item is _SUBSCRIPTION_CLOSED:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, dict)
        return item

    def __aiter__(self) -> GatewayEventSubscription:
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return await self.get()
        except StopAsyncIteration:
            raise StopAsyncIteration from None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client._remove_event_subscription(self)
        self._queue.put_nowait(_SUBSCRIPTION_CLOSED)

    def _deliver(self, frame: dict[str, Any]) -> None:
        if self._closed:
            return
        if not self._active:
            self._pending_live.append(frame)
            return
        self._enqueue(frame)

    def _activate(self, replay_frames: list[dict[str, Any]] | None = None) -> None:
        frames = [*(replay_frames or ()), *self._pending_live]
        self._pending_live.clear()
        self._active = True
        indexed = list(enumerate(frames))
        indexed.sort(key=lambda item: (_frame_stream_seq(item[1]) or 2**63, item[0]))
        for _index, frame in indexed:
            self._enqueue(frame)

    def _enqueue(self, frame: dict[str, Any]) -> None:
        stream_seq = _frame_stream_seq(frame)
        if stream_seq is not None:
            if stream_seq in self._seen_stream_seqs:
                return
            self._seen_stream_seqs.add(stream_seq)
            self._seen_stream_seq_order.append(stream_seq)
            while len(self._seen_stream_seq_order) > 1024:
                self._seen_stream_seqs.discard(self._seen_stream_seq_order.popleft())
        self._queue.put_nowait(frame)

    def _fail(self, error: BaseException) -> None:
        if not self._closed:
            self._queue.put_nowait(error)

    def _close_from_client(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put_nowait(_SUBSCRIPTION_CLOSED)


class GatewayClient:
    """WebSocket client for connecting to OpenSquilla gateway daemon."""

    def __init__(self) -> None:
        self._ws: Any = None
        self._recv_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._listener_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._heartbeat_interval: float = 48.0
        self._connection_error: ConnectionError | None = None
        self._closing = False
        self._http_base: str | None = None
        self._auth_token: str | None = None
        self.surface_id = f"tui:{uuid.uuid4().hex}"
        self._event_subscriptions: dict[int, GatewayEventSubscription] = {}
        self._next_subscription_id = 1
        self._server_session_subscriptions: set[str] = set()
        self._subscription_lock = asyncio.Lock()
        self._session_event_backlog: dict[str, deque[dict[str, Any]]] = {}

    async def connect(
        self,
        url: str = "ws://localhost:18791/ws",
        *,
        token: str | None = None,
    ) -> None:
        """Connect to gateway. Raises SystemExit with friendly message on failure."""
        has_existing_connection = (
            self._ws is not None
            or self._listener_task is not None
            or self._heartbeat_task is not None
        )
        if has_existing_connection:
            await self.close()
        self._closing = False
        self._connection_error = None
        try:
            import websockets
        except ImportError:
            raise SystemExit("websockets package is required: uv pip install websockets")

        try:
            self._ws = await websockets.connect(url)
        except Exception as exc:
            raise SystemExit(
                f"Cannot connect to OpenSquilla gateway at {url}\n"
                f"Is the gateway running? Start it with: opensquilla gateway run\n"
                f"Error: {exc}"
            )

        # Cache an HTTP base derived from the WS URL for the bridge upload
        # endpoint. ws://host:port/ws -> http://host:port; same scheme swap
        # for wss:// -> https://.
        if url.startswith("ws://"):
            base = "http://" + url[len("ws://") :]
        elif url.startswith("wss://"):
            base = "https://" + url[len("wss://") :]
        else:
            base = url
        if base.endswith("/ws"):
            base = base[: -len("/ws")]
        self._http_base = base.rstrip("/")
        self._auth_token = token

        # Wait for connect.challenge. A malformed frame from the server or
        # an intercepting proxy should not abort the connection with a
        # bare ``JSONDecodeError``; surface a clean shutdown instead.
        raw = await self._ws.recv()
        try:
            challenge = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"Malformed handshake frame from gateway: {exc.msg} "
                f"(line {exc.lineno} col {exc.colno})"
            ) from exc
        if not isinstance(challenge, dict):
            raise SystemExit(f"Unexpected handshake frame: {challenge!r}")
        if challenge.get("type") != "event" or challenge.get("event") != "connect.challenge":
            raise SystemExit(f"Unexpected handshake frame: {challenge}")

        # Send connect request
        req_id = str(uuid.uuid4())
        params: dict[str, Any] = {
            "minProtocol": 1,
            "maxProtocol": 3,
            "role": "operator",
            "scopes": ["operator.admin"],
        }
        if token:
            params["auth"] = {"token": token}
        await self._ws.send(
            json.dumps(
                {
                    "type": "req",
                    "id": req_id,
                    "method": "connect",
                    "params": params,
                }
            )
        )

        # Wait for hello-ok
        raw = await self._ws.recv()
        try:
            hello = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"Malformed hello frame from gateway: {exc.msg} "
                f"(line {exc.lineno} col {exc.colno})"
            ) from exc
        if not isinstance(hello, dict):
            raise SystemExit(f"Handshake failed: {hello!r}")
        if hello.get("type") != "hello-ok":
            raise SystemExit(f"Handshake failed: {hello}")
        policy_value = hello.get("policy")
        policy = cast(dict[str, Any], policy_value) if isinstance(policy_value, dict) else {}
        self._heartbeat_interval = _heartbeat_interval_from_policy(policy)

        # Start background listener and application-level keepalive.
        self._listener_task = asyncio.create_task(self._listen())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(self._ws))

    def set_auth_token(self, token: str | None) -> None:
        """Cache a bearer token used for HTTP-side requests (e.g. uploads)."""

        self._auth_token = token

    @property
    def is_local_gateway(self) -> bool:
        """True when the connected gateway URL is loopback/same-machine."""

        return gateway_base_is_local(self._http_base)

    async def upload_file(
        self,
        path: Any,
        mime: str,
        name: str,
    ) -> str:
        """POST a file to /api/v1/files/upload and return the file_uuid.

        The CLI keeps its WebSocket connection for RPC; the bridge upload
        is a sibling HTTP request to the same gateway origin (the
        ``/api/v1/files/upload`` endpoint). Multipart only — query-token
        auth is rejected by the
        endpoint, so we always send the Authorization header when a token
        is configured. When the upload fails (network, 4xx, 5xx) the
        error is raised so the caller can surface a clear message.
        """

        if self._http_base is None:
            raise ConnectionError("GatewayClient has no HTTP base URL — call connect() first")
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise SystemExit("httpx package is required: uv pip install httpx") from exc

        from pathlib import Path as _Path

        local = _Path(path)
        url = f"{self._http_base}/api/v1/files/upload"
        headers: dict[str, str] = {}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"

        with local.open("rb") as fh:
            files = {"file": (name, fh, mime)}
            data = {"mime": mime}
            async with httpx.AsyncClient(timeout=60.0) as http:
                response = await http.post(url, headers=headers, files=files, data=data)

        if response.status_code != 200:
            raise ConnectionError(
                f"upload {url} failed: HTTP {response.status_code} {response.text[:200]}"
            )
        body = response.json()
        if not isinstance(body, dict) or "file_uuid" not in body:
            raise ConnectionError(f"upload returned malformed body: {body!r}")
        return str(body["file_uuid"])

    async def _listen(self) -> None:
        """Read frames and route to pending futures or the event queue."""
        try:
            async for raw in self._ws:
                # Malformed protocol frames cannot be matched safely to
                # pending requests. Fail the connection explicitly so no
                # caller remains blocked on a future that cannot resolve.
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    self._mark_connection_failed(
                        ConnectionError(
                            "Gateway sent a malformed frame; closing connection"
                        )
                    )
                    return
                if not isinstance(frame, dict):
                    self._mark_connection_failed(
                        ConnectionError(
                            "Gateway sent a non-object frame; closing connection"
                        )
                    )
                    return
                frame_type = frame.get("type")
                if frame_type == "res":
                    frame_id = frame.get("id")
                    if not isinstance(frame_id, str):
                        # A response frame whose id is missing or not a
                        # string can never be matched to its pending
                        # request. Treat it as a protocol error and fail
                        # in-flight RPCs so callers get a clean
                        # connection error instead of hanging forever on
                        # a future that nothing will ever resolve.
                        self._mark_connection_failed(
                            ConnectionError(
                                "Gateway sent a response frame with a "
                                "missing or invalid id; closing connection"
                            )
                        )
                        return
                    fut = self._pending.pop(frame_id, None)
                    if fut and not fut.done():
                        fut.set_result(frame)
                elif frame_type == "event":
                    delivered = self._publish_event(frame)
                    # Preserve the legacy raw-event API for unclaimed events and
                    # functional probes. Active consumers receive independent
                    # copies, so one turn can never steal another's frames.
                    if not delivered:
                        await self._recv_queue.put(frame)
                elif frame_type == "pong":
                    continue
            if not self._closing:
                self._mark_connection_failed(ConnectionError("WebSocket connection closed"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Fail all pending requests so callers don't hang forever
            self._mark_connection_failed(exc)

    async def _heartbeat_loop(self, ws: Any | None = None) -> None:
        """Send application-level text pings so server receive_text() stays active."""
        heartbeat_ws = self._ws if ws is None else ws
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                if self._closing or heartbeat_ws is not self._ws:
                    return
                await self._send_ping(heartbeat_ws)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._mark_connection_failed(exc)

    async def _send_ping(self, ws: Any | None = None) -> None:
        target = self._ws if ws is None else ws
        if target is None:
            raise ConnectionError("WebSocket is not connected")
        await target.send('{"type":"ping"}')

    async def _call(self, method: str, params: dict | None = None) -> Any:
        """Send a JSON-RPC request and await its response."""
        if self._connection_error is not None:
            raise self._connection_error
        if self._ws is None:
            raise ConnectionError(
                "Gateway connection lost; restart chat or reconnect before sending another command."
            )
        req_id = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        self._pending[req_id] = fut
        try:
            await self._ws.send(
                json.dumps({"type": "req", "id": req_id, "method": method, "params": params})
            )
        except asyncio.CancelledError:
            self._pending.pop(req_id, None)
            raise
        except Exception as exc:
            self._pending.pop(req_id, None)
            err = self._mark_connection_failed(exc)
            raise err from exc
        res = await fut
        if not res.get("ok"):
            err = res.get("error", {})
            raw_details = err.get("data")
            if not isinstance(raw_details, dict):
                raw_details = err.get("details")
            raise GatewayRPCError(
                method,
                code=err.get("code"),
                message=err.get("message") or "RPC failed",
                data=raw_details if isinstance(raw_details, dict) else None,
            )
        payload = res.get("payload")
        return {} if payload is None else payload

    async def call(self, method: str, params: dict | None = None) -> Any:
        """Public thin wrapper for CLI commands that intentionally use RPC names."""

        return await self._call(method, params)

    async def create_session(
        self,
        agent_id: str = "main",
        model: str | None = None,
        display_name: str | None = None,
    ) -> str:
        """Create a new session, return session key."""
        params: dict[str, Any] = {"agentId": agent_id, "kind": "cli"}
        if model is not None:
            params["model"] = model
        if display_name:
            params["displayName"] = display_name
        result = await self._call("sessions.create", params)
        return cast(str, result["key"])

    async def list_sessions(self, limit: int = 50) -> dict[str, Any]:
        return cast(dict[str, Any], await self._call("sessions.list", {"limit": limit}))

    async def preview_sessions(
        self,
        keys: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if keys is not None:
            params["keys"] = keys
        return cast(dict[str, Any], await self._call("sessions.preview", params))

    async def resolve_session(self, key: str) -> dict[str, Any]:
        return cast(dict[str, Any], await self._call("sessions.resolve", {"key": key}))

    async def bootstrap_session(
        self,
        key: str,
        *,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Return a session startup snapshot, including an rc4-daemon fallback.

        ``sessions.bootstrap`` is additive.  A user can upgrade the CLI while
        an older Gateway process is still serving the same profile, so a
        method-missing response must not break either the plain rescue renderer
        or the TUI before the user has a chance to restart that daemon.  The
        legacy composition deliberately stays read-only and uses only RPCs
        already present in Preview 4.
        """

        try:
            return cast(
                dict[str, Any],
                await self._call("sessions.bootstrap", {"key": key, "limit": limit}),
            )
        except GatewayRPCError as exc:
            if str(exc.code or "").upper() != "METHOD_NOT_FOUND":
                raise

        resolved = await self.resolve_session(key)
        session_key = str(
            resolved.get("session_key")
            or resolved.get("sessionKey")
            or resolved.get("key")
            or key
        )
        history = await self.session_history(session_key, limit=limit)
        session = dict(resolved)
        session["session_key"] = session_key
        return {
            "session": session,
            "history": history,
            "queue": {
                "mode": session.get("queue_mode") or "followup",
                "queued_count": 0,
                "running_count": 0,
            },
            "runtime": {},
            "epoch": 0,
            "stream_cursor": None,
            "compatibility": {"bootstrap": "legacy_gateway"},
        }

    async def reset_session(self, key: str) -> dict[str, Any]:
        return cast(dict[str, Any], await self._call("sessions.reset", {"key": key}))

    async def compact_session(self, key: str) -> dict[str, Any]:
        return cast(dict[str, Any], await self._call("sessions.contextCompact", {"key": key}))

    async def delete_sessions(self, keys: list[str]) -> dict[str, Any]:
        return cast(dict[str, Any], await self._call("sessions.delete", {"keys": keys}))

    async def session_history(
        self,
        session_key: str,
        limit: int = 1000,
        *,
        before: str | None = None,
        after: str | None = None,
        include_canonical: bool | None = None,
        include_summaries: bool | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"sessionKey": session_key, "limit": limit}
        if before is not None:
            params["before"] = before
        if after is not None:
            params["after"] = after
        if include_canonical is not None:
            params["includeCanonical"] = include_canonical
        if include_summaries is not None:
            params["includeSummaries"] = include_summaries
        return cast(
            dict[str, Any],
            await self._call("chat.history", params),
        )

    async def abort_session(self, key: str) -> dict[str, Any]:
        return cast(dict[str, Any], await self._call("sessions.abort", {"key": key}))

    async def steer_session(self, key: str, message: str) -> dict[str, Any]:
        from opensquilla.cli.tui.backend.input_identity import (
            current_tui_client_message_id,
        )

        client_message_id = current_tui_client_message_id() or uuid.uuid4().hex
        return cast(
            dict[str, Any],
            await self._call(
                "sessions.steer",
                {
                    "key": key,
                    "message": message,
                    "client_message_id": client_message_id,
                    "surface_id": self.surface_id,
                    "_source": {
                        "caller_kind": "cli",
                        "channel_kind": "cli",
                        "channel_id": "cli:chat",
                        "source_kind": "cli",
                        "source_name": "chat",
                        "client_message_id": client_message_id,
                        "surface_id": self.surface_id,
                    },
                },
            ),
        )

    async def patch_session(self, key: str, **fields: Any) -> dict[str, Any]:
        params: dict[str, Any] = {"key": key, **fields}
        return cast(dict[str, Any], await self._call("sessions.patch", params))

    async def list_models(
        self, provider: str | None = None, capabilities: list[str] | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if provider:
            params["provider"] = provider
        if capabilities:
            params["capabilities"] = capabilities
        result = await self._call("models.list", params)
        if isinstance(result, list):
            return cast(list[dict[str, Any]], result)
        return cast(list[dict[str, Any]], list(result.get("models", [])))

    async def get_model_routing(self) -> dict[str, Any]:
        result = await self._call("models.routing.get", {})
        return result if isinstance(result, dict) else {}

    async def set_model_routing(self, mode: str) -> dict[str, Any]:
        result = await self._call("models.routing.set", {"mode": mode})
        return result if isinstance(result, dict) else {}

    async def usage_status(self) -> dict[str, Any]:
        return cast(dict[str, Any], await self._call("usage.status", {}))

    async def usage_cost(self) -> dict[str, Any]:
        return cast(dict[str, Any], await self._call("usage.cost", {}))

    async def diagnostics_status(self) -> dict[str, Any]:
        return cast(dict[str, Any], await self._call("diagnostics.status", {}))

    async def diagnostics_set(self, *, enabled: bool, raw: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {"enabled": enabled}
        if enabled:
            params["raw"] = raw
        return cast(dict[str, Any], await self._call("diagnostics.set", params))

    async def get_config(self, path: str | None = None) -> Any:
        params = {"path": path} if path else None
        return await self._call("config.get", params)

    async def patch_config_safe(self, patches: dict[str, Any]) -> dict[str, Any]:
        result = await self._call("config.patch.safe", {"patches": patches})
        return result if isinstance(result, dict) else {}

    async def forget_approvals(self, target: str | None = None) -> dict[str, Any]:
        """Wipe cached intent approvals on the server.

        ``target`` selects a specific path/command; omit to clear all.
        Returns the scope reported by the server.
        """
        params: dict[str, Any] = {}
        if target:
            params["target"] = target
        return cast(dict[str, Any], await self._call("exec.approval.forget", params))

    async def approvals_snapshot(self) -> dict[str, Any]:
        """Return current approval mode + cache contents (diagnostic)."""
        return cast(dict[str, Any], await self._call("exec.approval.snapshot", {}))

    async def set_approval_mode(self, mode: str) -> dict[str, Any]:
        """Set the global approval queue mode (prompt / auto-approve / auto-deny)."""
        return cast(dict[str, Any], await self._call("exec.approvals.set", {"mode": mode}))

    async def resolve_approval(
        self,
        approval_id: str,
        approved: bool,
        *,
        choice: str | None = None,
    ) -> dict[str, Any]:
        """Approve or deny a pending approval by id."""
        params: dict[str, Any] = {
            "id": approval_id,
            "approved": approved,
        }
        if choice:
            params["choice"] = choice
        return cast(
            dict[str, Any],
            await self._call(
                "exec.approval.resolve",
                params,
            ),
        )

    async def subscribe_session_events(
        self,
        session_key: str,
        *,
        since_stream_seq: int | None = None,
        event_names: set[str] | frozenset[str] | None = None,
    ) -> GatewayEventSubscription:
        """Subscribe to one session with replay before returning to the caller."""

        subscription = self._new_event_subscription(
            session_key=session_key,
            event_names=event_names,
            active=False,
        )
        async with self._subscription_lock:
            if session_key in self._server_session_subscriptions:
                backlog = self._session_event_backlog.get(session_key, ())
                replay_frames: list[dict[str, Any]] = []
                if since_stream_seq is not None:
                    replay_frames = [
                        frame
                        for frame in backlog
                        if (_frame_stream_seq(frame) or 0) > since_stream_seq
                    ]
                backlog_seqs = [
                    seq for frame in backlog if (seq := _frame_stream_seq(frame)) is not None
                ]
                local_gap = bool(
                    since_stream_seq is not None
                    and backlog_seqs
                    and since_stream_seq < backlog_seqs[0] - 1
                )
                subscription.replay = {
                    "subscribed": True,
                    "key": session_key,
                    "replay_complete": not local_gap,
                    "current_stream_seq": (backlog_seqs[-1] if backlog_seqs else since_stream_seq),
                }
                if local_gap:
                    subscription.replay["replay_gap_reason"] = "client_backlog_window_missed"
                subscription._activate(replay_frames)
                return subscription
            params: dict[str, Any] = {"key": session_key}
            if since_stream_seq is not None:
                params["since_stream_seq"] = since_stream_seq
            try:
                replay = await self._call("sessions.messages.subscribe", params)
            except BaseException:
                self._event_subscriptions.pop(subscription.subscription_id, None)
                subscription._close_from_client()
                raise
            self._server_session_subscriptions.add(session_key)
            subscription.replay = replay if isinstance(replay, dict) else {}
            subscription._activate()
            return subscription

    def subscribe_global_events(
        self,
        event_names: set[str] | frozenset[str],
    ) -> GatewayEventSubscription:
        """Return an independent subscription for gateway-wide push events."""

        return self._new_event_subscription(event_names=event_names)

    def _new_event_subscription(
        self,
        *,
        session_key: str | None = None,
        event_names: set[str] | frozenset[str] | None = None,
        active: bool = True,
    ) -> GatewayEventSubscription:
        subscription_id = self._next_subscription_id
        self._next_subscription_id += 1
        subscription = GatewayEventSubscription(
            _client=self,
            subscription_id=subscription_id,
            session_key=session_key,
            event_names=frozenset(event_names or ()),
            _active=active,
        )
        self._event_subscriptions[subscription_id] = subscription
        return subscription

    def _publish_event(self, frame: dict[str, Any]) -> bool:
        payload = frame.get("payload")
        event = payload if isinstance(payload, dict) else {}
        session_key = event.get("session_key")
        stream_seq = event.get("stream_seq")
        if isinstance(session_key, str) and isinstance(stream_seq, int):
            backlog = self._session_event_backlog.setdefault(session_key, deque(maxlen=512))
            backlog.append(frame)
            while len(self._session_event_backlog) > 32:
                self._session_event_backlog.pop(next(iter(self._session_event_backlog)))
            # The cursor lives in each subscription's replay metadata so a
            # caller switching sessions can resume without sharing a consumer.
            for subscription in self._event_subscriptions.values():
                if subscription.session_key == session_key:
                    subscription.replay["current_stream_seq"] = stream_seq
        delivered = False
        for subscription in tuple(self._event_subscriptions.values()):
            if subscription.matches(frame):
                subscription._deliver(frame)
                delivered = True
        return delivered

    async def _remove_event_subscription(
        self,
        subscription: GatewayEventSubscription,
    ) -> None:
        self._event_subscriptions.pop(subscription.subscription_id, None)
        session_key = subscription.session_key
        if session_key is None:
            return
        if any(item.session_key == session_key for item in self._event_subscriptions.values()):
            return
        async with self._subscription_lock:
            if session_key not in self._server_session_subscriptions:
                return
            self._server_session_subscriptions.discard(session_key)
            if self._closing or self._connection_error is not None or self._ws is None:
                return
            try:
                await self._call("sessions.messages.unsubscribe", {"key": session_key})
            except (ConnectionError, GatewayRPCError):
                return

    def _preserve_foreign_event(
        self,
        owner: GatewayEventSubscription,
        frame: dict[str, Any],
    ) -> None:
        """Keep a pre-acceptance foreign frame reachable by another consumer."""

        if any(
            subscription is not owner and subscription.matches(frame)
            for subscription in self._event_subscriptions.values()
        ):
            return
        self._recv_queue.put_nowait(frame)

    async def send_message(
        self,
        session_key: str,
        message: str,
        attachments: list[dict] | None = None,
        elevated: str | None = None,
    ) -> AsyncIterator[dict]:
        """Send message and yield session events until done.

        ``elevated`` is a legacy surface kept for older clients. ``off``
        clears the override, ``on``/``bypass`` map to Managed Execution, and
        ``full`` maps to Full Host Access.
        """
        # Register the local queue before send. Replay/live frames are broadcast
        # to every matching subscriber; this iterator can no longer consume an
        # approval or another session's turn from one shared queue.
        subscription = await self.subscribe_session_events(session_key)
        # OpenTUI allocates this identity when the composer value is accepted,
        # before the optimistic prompt is drawn.  Preserve it across the RPC so
        # the Gateway can bind that exact card to its durable turn.  Plain/Web
        # callers have no TUI identity scope and retain the historical UUID
        # allocation here.
        from opensquilla.cli.tui.backend.input_identity import (
            current_tui_client_message_id,
        )

        client_message_id = current_tui_client_message_id() or uuid.uuid4().hex

        params: dict[str, Any] = {
            "key": session_key,
            "message": message,
            "attachments": attachments or [],
            "client_message_id": client_message_id,
            "surface_id": self.surface_id,
            "_source": {
                "caller_kind": "cli",
                "channel_kind": "cli",
                "channel_id": "cli:chat",
                "source_kind": "cli",
                "source_name": "chat",
                "client_message_id": client_message_id,
                "surface_id": self.surface_id,
            },
        }
        if elevated in ("on", "bypass", "full"):
            params["_source"]["elevated"] = elevated

        # Send the message (accepted immediately; agent runs async)
        try:
            accepted = await self._call("sessions.send", params)
        except BaseException:
            await subscription.close()
            raise
        accepted_payload = accepted if isinstance(accepted, dict) else {}
        subscription.bind_turn(
            turn_id=_optional_identity(accepted_payload, "turn_id", "task_id"),
            client_message_id=_optional_identity(accepted_payload, "client_message_id")
            or client_message_id,
        )
        accepted_turn_id = _optional_identity(accepted_payload, "turn_id", "task_id")
        accepted_client_message_id = (
            _optional_identity(accepted_payload, "client_message_id")
            or client_message_id
        )
        if accepted_turn_id is not None:
            from opensquilla.cli.tui.backend.input_identity import (
                notify_tui_turn_identity,
            )

            await notify_tui_turn_identity(
                accepted_turn_id,
                accepted_client_message_id,
            )

        active_task_groups: set[str] = set()
        legacy_event_source = self._listener_task is None and not self._recv_queue.empty()

        # Yield events until session completion, extending the stream while a
        # background subagent group is still waiting for parent synthesis.
        try:
            while True:
                legacy_frame = legacy_event_source
                if legacy_frame:
                    frame = await self._recv_queue.get()
                else:
                    frame = await subscription.get()
                if not legacy_frame and not subscription.matches(frame):
                    # Legacy test/probe frames can enter through _recv_queue;
                    # preserve the same identity guard as live multiplexing.
                    self._preserve_foreign_event(subscription, frame)
                    continue
                event_name: str = frame.get("event", "")
                payload: dict = frame.get("payload") or {}
                event, terminal = _advance_gateway_turn_event(
                    event_name,
                    payload,
                    active_task_groups,
                )
                yield event
                if terminal:
                    break
        finally:
            await subscription.close()

    async def close(self) -> None:
        """Close the WebSocket connection."""
        self._closing = True
        for task in (self._heartbeat_task, self._listener_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
        self._ws = None
        self._heartbeat_task = None
        self._listener_task = None
        self._server_session_subscriptions.clear()
        self._session_event_backlog.clear()
        for subscription in tuple(self._event_subscriptions.values()):
            subscription._close_from_client()
        self._event_subscriptions.clear()

    def _mark_connection_failed(self, exc: BaseException) -> ConnectionError:
        if isinstance(exc, ConnectionError) and str(exc).startswith("Gateway connection lost"):
            err = exc
        else:
            err = ConnectionError(
                "Gateway connection lost; restart chat or reconnect before sending "
                "another command. "
                f"Original error: {exc}"
            )
        if self._connection_error is None:
            self._connection_error = err
        else:
            err = self._connection_error
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()
        for subscription in tuple(self._event_subscriptions.values()):
            subscription._fail(err)
        current_task = asyncio.current_task()
        for task in (self._heartbeat_task, self._listener_task):
            if task is not None and task is not current_task and not task.done():
                task.cancel()
        return err


def _task_terminal_as_session_event(event_name: str, payload: dict) -> dict[str, Any] | None:
    """Map task-runtime terminal events to chat stream terminal events.

    Gateway chat normally terminates with ``session.event.done`` or
    ``session.event.error``. If the task runtime fails before the agent stream
    starts, older servers may only emit a task terminal event; without this
    fallback the CLI waits forever.
    """
    if event_name == "task.cancelled":
        return {"event": "session.event.done", "reason": "aborted"}

    if event_name not in {"task.failed", "task.timeout", "task.abandoned"}:
        return None

    reason = payload.get("terminal_reason")
    status = event_name.removeprefix("task.")
    message = build_terminal_reply(
        {
            "status": status,
            "terminal_reason": reason,
            **payload,
        }
    )
    return {
        "event": "session.event.error",
        "message": message,
        "code": status,
        **payload,
    }


def _advance_gateway_turn_event(
    event_name: str,
    payload: dict[str, Any],
    active_task_groups: set[str],
) -> tuple[dict[str, Any], bool]:
    """Normalize one frame, mutate active groups, and report turn completion."""

    if event_name == "session.event.error":
        payload = _normalize_session_error_payload(payload)
    if task_terminal := _task_terminal_as_session_event(event_name, payload):
        return task_terminal, not active_task_groups

    group_id = payload.get("group_id")
    active_group_event = event_name in {
        "session.event.task_group.waiting",
        "session.event.task_group.synthesizing",
    }
    terminal_group_event = event_name in {
        "session.event.task_group.done",
        "session.event.task_group.failed",
    }
    was_active_group = False
    if active_group_event and isinstance(group_id, str) and group_id:
        active_task_groups.add(group_id)
    elif terminal_group_event and isinstance(group_id, str) and group_id:
        was_active_group = group_id in active_task_groups
        active_task_groups.discard(group_id)

    event = {"event": event_name, **payload}
    terminal = bool(
        (terminal_group_event and was_active_group and not active_task_groups)
        or (
            event_name in {"session.event.done", "session.event.error"}
            and not active_task_groups
        )
    )
    return event, terminal


def _normalize_session_error_payload(payload: dict) -> dict[str, Any]:
    message = payload.get("message")
    error_message = payload.get("error_message")
    raw_message = error_message if isinstance(error_message, str) and error_message else message
    code = payload.get("code")
    code_text = str(code or "").lower()
    raw_text = raw_message if isinstance(raw_message, str) and raw_message else "Agent error"
    is_timeout = "timeout" in code_text or "stream idle" in raw_text.lower()
    terminal_payload = {
        "status": "timeout" if is_timeout else "failed",
        "terminal_reason": payload.get("terminal_reason") or ("timeout" if is_timeout else "error"),
        "error_class": code,
        "error_message": raw_text,
        **payload,
    }
    _, safe_error_message = sanitize_agent_error(
        terminal_payload,
        fallback_error_class=str(code) if code else None,
        fallback_error_message=raw_text,
    )
    terminal_message = build_terminal_reply(terminal_payload)
    return {
        **payload,
        "message": terminal_message,
        "terminal_message": terminal_message,
        "terminal_reason": terminal_payload["terminal_reason"],
        "error_message": safe_error_message,
    }


def _heartbeat_interval_from_policy(policy: dict[str, Any]) -> float:
    raw = policy.get("client_ws_keepalive_timeout_ms", 120_000)
    try:
        keepalive_ms = int(raw)
    except (TypeError, ValueError):
        keepalive_ms = 120_000
    keepalive_s = max(0, keepalive_ms) / 1000.0
    if keepalive_s <= 0.0:
        keepalive_s = 120.0
    minimum = 15.0 if keepalive_s > 15.0 else 0.05
    return max(minimum, keepalive_s * 0.4)


def _frame_stream_seq(frame: dict[str, Any]) -> int | None:
    payload = frame.get("payload")
    if not isinstance(payload, dict):
        return None
    value = payload.get("stream_seq")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_identity(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None
