"""Reusable WebSocket RPC client for OpenSquilla gateway integrations."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, cast
from urllib.parse import urlparse, urlunparse


class GatewayRPCError(Exception):
    """RPC failure returned by the OpenSquilla gateway."""

    def __init__(
        self,
        method: str,
        *,
        code: str | None = None,
        message: str = "RPC failed",
        data: dict[str, Any] | None = None,
    ) -> None:
        self.method = method
        self.code = code
        self.message = message
        self.data = data
        super().__init__(self.__str__())

    def __str__(self) -> str:
        code = f"{self.code}: " if self.code else ""
        return f"{self.method} failed: {code}{self.message}"


def normalize_gateway_url(url: str) -> str:
    """Normalize user-supplied gateway URLs to websocket endpoints."""

    stripped = url.strip()
    if "://" in stripped:
        parsed = urlparse(stripped)
        scheme = parsed.scheme
        netloc = parsed.netloc
        path = parsed.path
    else:
        parsed = urlparse(f"//{stripped}")
        scheme = "ws"
        netloc = parsed.netloc
        path = parsed.path
    params = parsed.params
    query = parsed.query
    fragment = parsed.fragment

    if scheme not in {"http", "https", "ws", "wss"}:
        raise ValueError(f"Unsupported gateway URL scheme: {scheme!r}")

    websocket_scheme = "wss" if scheme in {"https", "wss"} else "ws"
    websocket_path = "/ws" if path in ("", "/") else path
    return urlunparse((websocket_scheme, netloc, websocket_path, params, query, fragment))


class GatewayRPCClient:
    """Small async gateway client for non-CLI adapters."""

    def __init__(
        self,
        *,
        scopes: list[str] | None = None,
        request_timeout_s: float | None = 30.0,
    ) -> None:
        self.scopes = scopes or ["operator.read", "operator.write"]
        self.request_timeout_s = request_timeout_s
        self._ws: Any = None
        self._recv_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._listener_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._heartbeat_interval = 48.0
        self._connection_error: ConnectionError | None = None
        self._closing = False

    async def connect(self, url: str = "ws://localhost:18791/ws") -> None:
        if self._ws is not None:
            await self.close()
        self._closing = False
        self._connection_error = None
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - dependency is part of base install.
            raise RuntimeError("websockets package is required") from exc

        self._ws = await websockets.connect(normalize_gateway_url(url))

        try:
            raw = await self._ws.recv()
            challenge = json.loads(raw)
            if challenge.get("type") != "event" or challenge.get("event") != "connect.challenge":
                raise RuntimeError(f"Unexpected gateway handshake frame: {challenge}")

            req_id = str(uuid.uuid4())
            await self._ws.send(
                json.dumps(
                    {
                        "type": "req",
                        "id": req_id,
                        "method": "connect",
                        "params": {
                            "minProtocol": 1,
                            "maxProtocol": 3,
                            "role": "operator",
                            "scopes": self.scopes,
                        },
                    }
                )
            )

            raw = await self._ws.recv()
            hello = json.loads(raw)
            if hello.get("type") != "hello-ok":
                raise RuntimeError(f"Gateway handshake failed: {hello}")
        except Exception:
            await self._close_failed_connect()
            raise

        policy = hello.get("policy") if isinstance(hello.get("policy"), dict) else {}
        self._heartbeat_interval = _heartbeat_interval_from_policy(policy)
        self._listener_task = asyncio.create_task(self._listen())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(self._ws))

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self._connection_error is not None:
            raise self._connection_error
        if self._ws is None:
            raise ConnectionError("Gateway connection is not open")

        req_id = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[req_id] = fut
        try:
            await self._ws.send(
                json.dumps({"type": "req", "id": req_id, "method": method, "params": params})
            )
        except Exception as exc:
            self._pending.pop(req_id, None)
            err = self._mark_connection_failed(exc)
            raise err from exc

        try:
            if self.request_timeout_s is None:
                res = await fut
            else:
                res = await asyncio.wait_for(fut, timeout=self.request_timeout_s)
        except TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise TimeoutError(f"{method} timed out after {self.request_timeout_s:g}s") from exc
        if not res.get("ok"):
            err = res.get("error", {})
            raise GatewayRPCError(
                method,
                code=err.get("code"),
                message=err.get("message") or "RPC failed",
                data=err.get("data") if isinstance(err.get("data"), dict) else None,
            )
        payload = res.get("payload")
        return {} if payload is None else payload

    async def list_sessions(self, limit: int = 50) -> dict[str, Any]:
        return cast(dict[str, Any], await self.call("sessions.list", {"limit": limit}))

    async def resolve_session(self, key: str) -> dict[str, Any]:
        return cast(dict[str, Any], await self.call("sessions.resolve", {"key": key}))

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
            await self.call("chat.history", params),
        )

    async def recv_event(self, timeout: float | None = None) -> dict[str, Any]:
        if timeout is None:
            return await self._recv_queue.get()
        return await asyncio.wait_for(self._recv_queue.get(), timeout=timeout)

    async def close(self) -> None:
        self._closing = True
        for task in (self._heartbeat_task, self._listener_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._ws is not None:
            await self._ws.close()
        self._ws = None
        self._heartbeat_task = None
        self._listener_task = None

    async def _close_failed_connect(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is not None:
            await ws.close()

    async def _listen(self) -> None:
        try:
            async for raw in self._ws:
                frame = json.loads(raw)
                frame_type = frame.get("type")
                if frame_type == "res":
                    fut = self._pending.pop(frame["id"], None)
                    if fut is not None and not fut.done():
                        fut.set_result(frame)
                elif frame_type == "event":
                    await self._recv_queue.put(frame)
                elif frame_type == "pong":
                    continue
            if not self._closing:
                self._mark_connection_failed(ConnectionError("WebSocket connection closed"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._mark_connection_failed(exc)

    async def _heartbeat_loop(self, ws: Any) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                if self._closing or ws is not self._ws:
                    return
                await ws.send('{"type":"ping"}')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._mark_connection_failed(exc)

    def _mark_connection_failed(self, exc: BaseException) -> ConnectionError:
        if isinstance(exc, ConnectionError):
            err = exc
        else:
            err = ConnectionError(f"Gateway connection lost: {exc}")
        if self._connection_error is None:
            self._connection_error = err
        else:
            err = self._connection_error
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()
        current_task = asyncio.current_task()
        for task in (self._heartbeat_task, self._listener_task):
            if task is not None and task is not current_task and not task.done():
                task.cancel()
        return err


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
