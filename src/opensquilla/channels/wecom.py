"""WeCom corp-app channel adapter.

Vendored AES-256-CBC + PKCS7 + sha1 msg-signature crypto in
:mod:`opensquilla.channels._wecom_crypto`, native ``httpx.AsyncClient``
outbound against ``https://qyapi.weixin.qq.com``, no ``wechatpy`` dependency.

WeCom corp app has no message-edit primitive: ``send_streaming`` accumulates
the LLM stream and emits exactly one outbound POST at completion. No calls
to ``cgi-bin/message/recall`` or ``cgi-bin/message/update_template_card``
happen mid-stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import mimetypes
import time
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

import httpx
import structlog
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from opensquilla.channels._util import ChannelAccessPolicy, EventDedupeCache
from opensquilla.channels._wecom_crypto import WeComCrypto
from opensquilla.channels.contract import (
    ChannelCapabilities,
    ChannelCapabilityProfile,
    ChannelLengthUnit,
    ChannelPlatformCapability,
    ChannelPlatformCapabilityStatus,
    ChannelPlatformCategories,
    ChannelPlatformManifest,
    ChannelSendResult,
)
from opensquilla.channels.types import (
    AuthenticatedPrincipal,
    ChannelHealth,
    IncomingMessage,
    IngressProvenance,
    IngressVerification,
    OutgoingMessage,
    UnsupportedChannelOperation,
)
from opensquilla.env import trust_env as _trust_env

log = structlog.get_logger(__name__)

_TOKEN_REFRESH_INTERVAL_S = 7000.0  # WeCom access_token TTL is 7200 s
_DEFAULT_TIMEOUT_S = 10.0
_DEDUPE_SIZE = 4096
_DEFAULT_WEBSOCKET_URL = "wss://openws.work.weixin.qq.com"
_WEBSOCKET_HANDSHAKE_TIMEOUT_S = 10.0
_WEBSOCKET_REQUEST_TIMEOUT_S = 10.0
_WEBSOCKET_APP_PING_INTERVAL_S = 30.0
_WEBSOCKET_REPLY_REQ_ID_TTL_S = 300.0
_WEBSOCKET_RECONNECT_INITIAL_S = 1.0
_WEBSOCKET_RECONNECT_MAX_S = 60.0

_APP_CMD_SUBSCRIBE = "aibot_subscribe"
_APP_CMD_CALLBACK = "aibot_msg_callback"
_APP_CMD_LEGACY_CALLBACK = "aibot_callback"
_APP_CMD_EVENT_CALLBACK = "aibot_event_callback"
_APP_CMD_SEND = "aibot_send_msg"
_APP_CMD_RESPONSE = "aibot_respond_msg"
_APP_CMD_PING = "ping"
_APP_CMD_PONG = "pong"
_MESSAGE_CALLBACK_COMMANDS = frozenset({_APP_CMD_CALLBACK, _APP_CMD_LEGACY_CALLBACK})

# Channel-contract constants pinned by the adapter audit.
CAPABILITY_TIER = "YELLOW-experimental"

DM_SAFETY_TIERS: tuple[str, ...] = ("safe", "confirm")

RETRYABLE_ERROR_CLASSES: tuple[str, ...] = (
    "transport_transient",
    "rate_limited",
    "channel_degraded",
)
FATAL_ERROR_CLASSES: tuple[str, ...] = (
    "auth_invalid",
    "payload_rejected",
    "target_missing",
    "contract_violation",
)


class WeComAuthError(Exception):
    """Raised when ``gettoken`` fails."""


class WeComApiError(Exception):
    """Raised when a WeCom API call returns a non-zero ``errcode``."""

    def __init__(self, msg: str, *, code: int | None = None) -> None:
        self.code = code
        super().__init__(msg)


class WeComChannelConfig(BaseModel):
    """Adapter-level config for WeCom.

    ``websocket`` is the WeCom AI Bot long-connection protocol. ``webhook``
    is the older corp-app callback + ``message/send`` path.
    """

    name: str = "wecom"
    connection_mode: Literal["webhook", "websocket"] = "webhook"
    bot_id: str = ""
    bot_secret: str = ""
    websocket_url: str = _DEFAULT_WEBSOCKET_URL
    corp_id: str = ""
    corp_secret: str = ""
    agent_id_int: int = 0
    token: str = ""
    encoding_aes_key: str = ""
    webhook_path: str = "/wecom/events"
    api_base: str = "https://qyapi.weixin.qq.com"

    model_config = {}


@dataclass
class _TokenState:
    token: str
    expires_at: float  # time.monotonic() based


@dataclass
class WeComChannel:
    """WeCom corp-app channel adapter.

    Webhook callbacks are AES-decrypted via the vendored
    :class:`~opensquilla.channels._wecom_crypto.WeComCrypto`. Outbound
    messages POST to ``cgi-bin/message/send`` with a cached ``access_token``
    refreshed by a background task.
    """

    config: WeComChannelConfig
    policy: ChannelAccessPolicy = field(
        default_factory=lambda: ChannelAccessPolicy(
            dm_allowed=True,
            group_allowed=True,
            mention_required_in_group=True,
            allowlist=frozenset(),
        )
    )

    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)
    _token_state: _TokenState | None = field(default=None, init=False, repr=False)
    _token_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _refresh_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _ws: Any | None = field(default=None, init=False, repr=False)
    _ws_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _ws_heartbeat_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _pending_ws_responses: dict[str, asyncio.Future[dict[str, Any]]] = field(
        default_factory=dict, init=False, repr=False
    )
    _reply_req_ids: dict[str, tuple[str, float]] = field(
        default_factory=dict, init=False, repr=False
    )
    _last_chat_req_ids: dict[str, tuple[str, float]] = field(
        default_factory=dict, init=False, repr=False
    )
    _queue: asyncio.Queue[IncomingMessage] = field(
        default_factory=asyncio.Queue, init=False, repr=False
    )
    _dedupe: EventDedupeCache = field(init=False, repr=False)
    _last_message_at: datetime | None = field(default=None, init=False, repr=False)
    _last_incoming_envelope: IncomingMessage | None = field(default=None, init=False, repr=False)
    _crypto: WeComCrypto | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._dedupe = EventDedupeCache(max_size=_DEDUPE_SIZE)
        if self.config.encoding_aes_key and self.config.token and self.config.corp_id:
            self._crypto = WeComCrypto(
                token=self.config.token,
                encoding_aes_key=self.config.encoding_aes_key,
                receiver_id=self.config.corp_id,
            )

    @property
    def capability_profile(self) -> ChannelCapabilityProfile:
        if self.config.connection_mode == "websocket":
            return ChannelCapabilityProfile(
                channel_type="wecom",
                max_message_len=2048,
                length_unit=ChannelLengthUnit.UTF8_BYTES,
                splits_natively=False,
                group_chat=True,
                mentions=True,
                reply=True,
                transports=("websocket",),
                notes=(
                    "WeCom AI Bot long-connection mode uses bot_id/bot_secret "
                    "and aibot_* websocket frames.",
                ),
            )
        return ChannelCapabilityProfile(
            channel_type="wecom",
            max_message_len=2048,
            length_unit=ChannelLengthUnit.UTF8_BYTES,
            splits_natively=False,
            group_chat=True,
            mentions=True,
            native_file_upload=True,
            media=True,
            reply=True,
            transports=("webhook",),
        )

    @property
    def platform_capability_manifest(self) -> ChannelPlatformManifest:
        if self.config.connection_mode == "websocket":
            return ChannelPlatformManifest.from_channel_profile(
                self.capability_profile,
            ).with_capabilities(
                ChannelPlatformCapability(
                    category=ChannelPlatformCategories.FILES,
                    status=ChannelPlatformCapabilityStatus.UNSUPPORTED,
                    notes=("WeCom AI Bot websocket media upload is not implemented yet.",),
                ),
                ChannelPlatformCapability(
                    category=ChannelPlatformCategories.ATTACHMENTS,
                    status=ChannelPlatformCapabilityStatus.UNSUPPORTED,
                    notes=(
                        "Inbound WeCom attachment resolution is not implemented in this adapter.",
                    ),
                ),
            )

        return ChannelPlatformManifest.from_channel_profile(
            self.capability_profile,
            has_send_file=True,
        ).with_capabilities(
            ChannelPlatformCapability(
                category=ChannelPlatformCategories.FILES,
                status=ChannelPlatformCapabilityStatus.SUPPORTED,
                tools=("media/upload", "message/send:file"),
                mutates=True,
                notes=("WeCom file delivery uploads media then sends a file message.",),
            ),
            ChannelPlatformCapability(
                category=ChannelPlatformCategories.ATTACHMENTS,
                status=ChannelPlatformCapabilityStatus.UNSUPPORTED,
                notes=("Inbound WeCom attachment resolution is not implemented in this adapter.",),
            ),
        )

    @property
    def capabilities(self) -> frozenset[str]:
        return self.capability_profile.capability_tags()

    @property
    def transport_name(self) -> str:
        return self.config.connection_mode

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.config.api_base,
                timeout=_DEFAULT_TIMEOUT_S,
                trust_env=_trust_env(),
            )
        return self._client

    def _get_crypto(self) -> WeComCrypto:
        if self._crypto is None:
            raise WeComAuthError("WeCom adapter is missing token/encoding_aes_key/corp_id")
        return self._crypto

    # ------------------------------------------------------------------
    # Token cache + refresh
    # ------------------------------------------------------------------

    def _require_webhook_credentials(self) -> None:
        if not self.config.corp_id.strip() or not self.config.corp_secret.strip():
            raise ValueError("wecom webhook mode requires corp_id and corp_secret")

    async def _refresh_token(self) -> str:
        """Hit ``cgi-bin/gettoken`` and cache the access_token."""
        client = self._get_client()
        params = {
            "corpid": self.config.corp_id,
            "corpsecret": self.config.corp_secret,
        }
        resp = await client.get("/cgi-bin/gettoken", params=params)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            raise WeComAuthError(
                f"gettoken failed: errcode={data.get('errcode')} errmsg={data.get('errmsg')}"
            )
        token = cast(str, data["access_token"])
        ttl = float(data.get("expires_in", 7200))
        self._token_state = _TokenState(
            token=token,
            expires_at=time.monotonic() + ttl,
        )
        log.info("wecom.token_refreshed", expires_in=ttl)
        return token

    async def _get_token(self) -> str:
        async with self._token_lock:
            if self._token_state is not None and time.monotonic() < self._token_state.expires_at:
                return self._token_state.token
            self._require_webhook_credentials()
            return await self._refresh_token()

    async def _refresh_loop(self) -> None:
        """Background task that proactively refreshes the access_token.

        Runs once per ``_TOKEN_REFRESH_INTERVAL_S`` (~7000 s, < 7200 s TTL).
        """
        try:
            while True:
                await asyncio.sleep(_TOKEN_REFRESH_INTERVAL_S)
                async with self._token_lock:
                    await self._refresh_token()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._connected = False
            log.error("wecom.token_refresh_failed", error=str(exc))
            raise

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self.config.connection_mode == "websocket":
            await self._start_websocket()
            return

        await self._get_token()
        self._connected = True
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(), name=f"wecom-token-refresh:{self.config.name}"
        )
        log.info("wecom.started", name=self.config.name)

    async def probe_connection(self) -> dict[str, Any]:
        """Validate corp-app credentials without disturbing AI Bot WebSocket ownership."""
        if self.config.connection_mode == "websocket":
            return {
                "authenticated": False,
                "supported": False,
                "reason": (
                    "WeCom AI Bot credentials can only be validated by subscribing; "
                    "a probe could disconnect the active single-owner connection."
                ),
            }
        await self._get_token()
        return {"authenticated": True, "supported": True}

    async def stop(self) -> None:
        await self._stop_websocket()
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
            self._refresh_task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._connected = False
        self._token_state = None
        log.info("wecom.stopped", name=self.config.name)

    async def health_check(self) -> ChannelHealth:
        if self.config.connection_mode == "websocket":
            transport_alive = self._ws_task is not None and not self._ws_task.done()
        else:
            transport_alive = self._refresh_task is not None and not self._refresh_task.done()
        return ChannelHealth(
            connected=self._connected and transport_alive,
            last_message_at=self._last_message_at,
            extra={"transport": self.config.connection_mode},
        )

    # ------------------------------------------------------------------
    # WeCom AI Bot websocket transport
    # ------------------------------------------------------------------

    def _require_websocket_credentials(self) -> None:
        if not self.config.bot_id.strip() or not self.config.bot_secret.strip():
            raise ValueError("wecom websocket mode requires bot_id and bot_secret")
        if not self.config.websocket_url.strip():
            raise ValueError("wecom websocket mode requires websocket_url")

    @staticmethod
    def _new_req_id(prefix: str) -> str:
        return f"{prefix}-{uuid4().hex}"

    @staticmethod
    def _payload_req_id(payload: dict[str, Any]) -> str:
        headers = payload.get("headers")
        if isinstance(headers, dict):
            return str(headers.get("req_id") or "")
        return ""

    @staticmethod
    def _parse_ws_json(raw: Any) -> dict[str, Any] | None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            payload = json.loads(str(raw))
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    async def _connect_websocket(self) -> Any:
        import websockets

        return await websockets.connect(
            self.config.websocket_url,
            # WeCom uses application-level JSON ping/pong. Protocol-level
            # Ping frames can trigger 1002 "incorrect masking" responses.
            ping_interval=None,
        )

    async def _ws_send_json(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("wecom websocket is not connected")
        await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def _ws_recv_json(self) -> dict[str, Any]:
        if self._ws is None:
            raise RuntimeError("wecom websocket is not connected")
        while True:
            payload = self._parse_ws_json(await self._ws.recv())
            if payload is not None:
                return payload

    @classmethod
    def _response_error(cls, payload: dict[str, Any]) -> tuple[int | None, str]:
        body_raw = payload.get("body")
        body: dict[str, Any] = body_raw if isinstance(body_raw, dict) else {}
        errcode_raw = payload.get("errcode", body.get("errcode", 0))
        try:
            errcode = int(errcode_raw) if errcode_raw is not None else None
        except (TypeError, ValueError):
            errcode = None
        errmsg = str(payload.get("errmsg") or body.get("errmsg") or "")
        return errcode, errmsg

    async def _open_and_authenticate_websocket(self) -> None:
        self._ws = await self._connect_websocket()
        req_id = self._new_req_id("subscribe")
        await self._ws_send_json(
            {
                "cmd": _APP_CMD_SUBSCRIBE,
                "headers": {"req_id": req_id},
                "body": {
                    "bot_id": self.config.bot_id,
                    "secret": self.config.bot_secret,
                },
            }
        )
        auth_payload = await asyncio.wait_for(
            self._wait_for_ws_response(req_id), timeout=_WEBSOCKET_HANDSHAKE_TIMEOUT_S
        )
        errcode, errmsg = self._response_error(auth_payload)
        if errcode not in (0, None):
            raise WeComAuthError(
                f"aibot_subscribe failed: errcode={errcode} errmsg={errmsg or 'unknown'}"
            )
        self._connected = True

    async def _start_websocket(self) -> None:
        self._require_websocket_credentials()
        try:
            await self._open_and_authenticate_websocket()
        except Exception:
            await self._close_websocket()
            raise
        self._ws_task = asyncio.create_task(
            self._websocket_receive_loop(), name=f"wecom-websocket:{self.config.name}"
        )
        self._ws_heartbeat_task = asyncio.create_task(
            self._websocket_heartbeat_loop(), name=f"wecom-websocket-heartbeat:{self.config.name}"
        )
        log.info("wecom.websocket_started", name=self.config.name)

    async def _stop_websocket(self) -> None:
        if self._ws_heartbeat_task is not None:
            self._ws_heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ws_heartbeat_task
            self._ws_heartbeat_task = None
        if self._ws_task is not None:
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ws_task
            self._ws_task = None
        self._fail_pending_ws_responses(asyncio.CancelledError())
        await self._close_websocket()
        self._reply_req_ids.clear()
        self._last_chat_req_ids.clear()
        if self.config.connection_mode == "websocket":
            self._connected = False

    async def _close_websocket(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is None:
            return
        close = getattr(ws, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                await close()

    def _fail_pending_ws_responses(self, exc: BaseException) -> None:
        for future in list(self._pending_ws_responses.values()):
            if not future.done():
                if isinstance(exc, asyncio.CancelledError):
                    future.cancel()
                else:
                    future.set_exception(exc)
        self._pending_ws_responses.clear()

    async def _wait_for_ws_response(self, req_id: str) -> dict[str, Any]:
        while True:
            payload = await self._ws_recv_json()
            if self._payload_req_id(payload) == req_id:
                return payload
            await self._handle_websocket_payload(payload, pre_auth=True)

    async def _websocket_receive_loop(self) -> None:
        backoff = _WEBSOCKET_RECONNECT_INITIAL_S
        while True:
            try:
                while True:
                    payload = await self._ws_recv_json()
                    await self._handle_websocket_payload(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connected = False
                self._fail_pending_ws_responses(exc)
                log.warning("wecom.websocket_error", error=str(exc))
                await self._close_websocket()
            await asyncio.sleep(backoff)
            try:
                await self._open_and_authenticate_websocket()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connected = False
                log.warning("wecom.websocket_reconnect_failed", error=str(exc), backoff_s=backoff)
                await self._close_websocket()
                backoff = min(backoff * 2, _WEBSOCKET_RECONNECT_MAX_S)
                continue
            log.info("wecom.websocket_reconnected", name=self.config.name)
            backoff = _WEBSOCKET_RECONNECT_INITIAL_S

    async def _websocket_heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(_WEBSOCKET_APP_PING_INTERVAL_S)
                if not self._connected or self._ws is None:
                    continue
                try:
                    await self._send_ws_request(_APP_CMD_PING, {})
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._connected = False
                    log.warning("wecom.websocket_heartbeat_failed", error=str(exc))
                    await self._close_websocket()
        except asyncio.CancelledError:
            raise

    async def _handle_websocket_payload(
        self, payload: dict[str, Any], *, pre_auth: bool = False
    ) -> None:
        cmd = str(payload.get("cmd") or "")
        req_id = self._payload_req_id(payload)
        if (
            not pre_auth
            and req_id in self._pending_ws_responses
            and cmd not in _MESSAGE_CALLBACK_COMMANDS
            and cmd != _APP_CMD_EVENT_CALLBACK
        ):
            future = self._pending_ws_responses.get(req_id)
            if future is not None and not future.done():
                future.set_result(payload)
            return
        if cmd == _APP_CMD_PING:
            await self._ws_send_json(
                {
                    "cmd": _APP_CMD_PONG,
                    "headers": {"req_id": req_id or self._new_req_id("pong")},
                    "body": {},
                }
            )
            return
        if cmd == _APP_CMD_EVENT_CALLBACK:
            log.info("wecom.websocket_event_ignored", req_id=req_id)
            return
        if cmd in _MESSAGE_CALLBACK_COMMANDS:
            msg = self._parse_inbound_websocket_json(payload)
            if msg is None:
                return
            msg_id = str(msg.metadata.get("message_id", ""))
            if msg_id and not self._dedupe.check_and_add(msg_id):
                log.info("wecom.dedup_drop", message_id=msg_id)
                return
            self._remember_reply_req_id(msg_id, str(msg.metadata.get("wecom_req_id", "")))
            self._remember_chat_req_id(
                str(msg.metadata.get("chat_id", msg.channel_id)),
                str(msg.metadata.get("wecom_req_id", "")),
            )
            self.enqueue(msg)

    async def _send_ws_request(
        self,
        cmd: str,
        body: dict[str, Any],
        *,
        req_id: str | None = None,
    ) -> dict[str, Any]:
        if self._ws is None or not self._connected:
            raise RuntimeError("wecom websocket is not connected")
        request_id = req_id or self._new_req_id(cmd)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_ws_responses[request_id] = future
        try:
            await self._ws_send_json({"cmd": cmd, "headers": {"req_id": request_id}, "body": body})
            response = await asyncio.wait_for(future, timeout=_WEBSOCKET_REQUEST_TIMEOUT_S)
        finally:
            self._pending_ws_responses.pop(request_id, None)
        errcode, errmsg = self._response_error(response)
        if errcode not in (0, None):
            raise WeComApiError(errmsg or "websocket request failed", code=errcode)
        return response

    def _reply_req_id_expires_at(self) -> float:
        return time.monotonic() + _WEBSOCKET_REPLY_REQ_ID_TTL_S

    def _remember_reply_req_id(self, message_id: str, req_id: str) -> None:
        if not message_id or not req_id:
            return
        self._reply_req_ids[message_id] = (req_id, self._reply_req_id_expires_at())
        while len(self._reply_req_ids) > _DEDUPE_SIZE:
            self._reply_req_ids.pop(next(iter(self._reply_req_ids)))

    def _remember_chat_req_id(self, chat_id: str, req_id: str) -> None:
        if not chat_id or not req_id:
            return
        self._last_chat_req_ids[chat_id] = (req_id, self._reply_req_id_expires_at())
        while len(self._last_chat_req_ids) > _DEDUPE_SIZE:
            self._last_chat_req_ids.pop(next(iter(self._last_chat_req_ids)))

    def _get_valid_reply_req_id(self, message_id: str) -> str:
        return self._get_valid_req_id(self._reply_req_ids, message_id)

    def _get_valid_chat_req_id(self, chat_id: str) -> str:
        return self._get_valid_req_id(self._last_chat_req_ids, chat_id)

    @staticmethod
    def _get_valid_req_id(store: dict[str, tuple[str, float]], key: str) -> str:
        if not key:
            return ""
        entry = store.get(key)
        if entry is None:
            return ""
        req_id, expires_at = entry
        if time.monotonic() >= expires_at:
            store.pop(key, None)
            return ""
        return req_id

    def _forget_req_id(self, req_id: str) -> None:
        if not req_id:
            return
        for store in (self._reply_req_ids, self._last_chat_req_ids):
            for key, (stored_req_id, _expires_at) in list(store.items()):
                if stored_req_id == req_id:
                    store.pop(key, None)

    @staticmethod
    def _extract_websocket_text(body: dict[str, Any], msg_type: str) -> str:
        for key in ("text", "markdown", "voice"):
            value = body.get(key)
            if isinstance(value, dict):
                content = value.get("content")
                if content:
                    return str(content)
            elif isinstance(value, str):
                return value
        content = body.get("content")
        if content:
            return str(content)
        return f"[{msg_type}]" if msg_type else ""

    def _parse_inbound_websocket_json(self, payload: dict[str, Any]) -> IncomingMessage | None:
        body = payload.get("body")
        if not isinstance(body, dict):
            return None
        req_id = self._payload_req_id(payload)
        msg_id = str(body.get("msgid") or req_id or uuid4().hex)
        sender_raw = body.get("from")
        sender: dict[str, Any] = sender_raw if isinstance(sender_raw, dict) else {}
        from_user = str(sender.get("userid") or body.get("from_user") or "unknown")
        chat_id = str(body.get("chatid") or from_user or "unknown")
        chat_type = str(body.get("chattype") or body.get("chat_type") or "single").lower()
        is_group = chat_type == "group"
        msg_type = str(body.get("msgtype") or body.get("msg_type") or "text")
        content = self._extract_websocket_text(body, msg_type)
        metadata: dict[str, Any] = {
            "message_id": msg_id,
            "msg_type": msg_type,
            "chat_type": chat_type,
            "is_group": is_group,
            "from_user": from_user,
            "chat_id": chat_id,
            "wecom_protocol": "aibot",
            "wecom_req_id": req_id,
            "wecom_req_id_expires_at": self._reply_req_id_expires_at(),
            "bot_mentioned": True,
        }
        return IncomingMessage(
            sender_id=from_user,
            channel_id=chat_id,
            content=content,
            metadata=metadata,
            provenance=IngressProvenance(
                provider="wecom",
                account_id=self.config.bot_id,
                transport="websocket",
                verification=IngressVerification.SDK_SESSION,
                event_id=msg_id,
                principal=AuthenticatedPrincipal(subject_id=from_user),
            ),
        )

    # ------------------------------------------------------------------
    # Inbound queue
    # ------------------------------------------------------------------

    def enqueue(self, message: IncomingMessage) -> None:
        from opensquilla.channels.delivery_store import durable_enqueue

        durable_enqueue(self, message, self._queue)

    async def receive(self) -> IncomingMessage:
        msg = await self._queue.get()
        self._last_message_at = datetime.now(UTC)
        # Cache the most recent envelope so ``send_streaming(chunks)``
        # invocations from the dispatcher (which carry no target kwarg)
        # can route the reply back to the originator.
        self._last_incoming_envelope = msg
        return msg

    # ------------------------------------------------------------------
    # Webhook route
    # ------------------------------------------------------------------

    def create_webhook_route(self, path: str | None = None) -> Route:
        route_path = path or self.config.webhook_path
        return Route(route_path, endpoint=self._handle_webhook, methods=["GET", "POST"])

    async def _handle_webhook(self, request: Request) -> Response:
        try:
            crypto = self._get_crypto()
        except WeComAuthError:
            return Response(status_code=503)

        params = request.query_params
        msg_signature = params.get("msg_signature", "")
        timestamp = params.get("timestamp", "")
        nonce = params.get("nonce", "")

        if request.method == "GET":
            echostr = params.get("echostr", "")
            if not crypto.verify_signature(
                self.config.token, timestamp, nonce, echostr, msg_signature
            ):
                log.warning("wecom.signature_invalid", phase="url_verify")
                return Response(status_code=401)
            try:
                plaintext = crypto.decrypt_message(echostr)
            except ValueError as exc:
                log.warning("wecom.signature_invalid", reason=str(exc))
                return Response(status_code=401)
            return PlainTextResponse(plaintext)

        # POST: encrypted event
        body_bytes = await request.body()
        try:
            outer = ET.fromstring(body_bytes.decode("utf-8"))
        except (ET.ParseError, UnicodeDecodeError):
            return Response(status_code=400)
        encrypt_node = outer.find("Encrypt")
        if encrypt_node is None or not encrypt_node.text:
            return Response(status_code=400)
        encrypt_b64 = encrypt_node.text

        if not crypto.verify_signature(
            self.config.token, timestamp, nonce, encrypt_b64, msg_signature
        ):
            log.warning("wecom.signature_invalid", phase="event")
            return Response(status_code=401)

        try:
            inner_xml = crypto.decrypt_message(encrypt_b64)
        except ValueError as exc:
            log.warning("wecom.signature_invalid", reason=str(exc))
            return Response(status_code=401)

        try:
            inner = ET.fromstring(inner_xml)
        except ET.ParseError:
            return Response(status_code=400)

        msg = self._parse_inbound_xml(inner)
        if msg is None:
            return Response(status_code=200)

        msg_id = str(msg.metadata.get("message_id", ""))
        if msg_id and not self._dedupe.check_and_add(msg_id):
            log.info("wecom.dedup_drop", message_id=msg_id)
            return Response(status_code=200)

        log.info(
            "wecom.inbound_received",
            message_id=msg_id,
            is_group=msg.metadata.get("is_group"),
        )
        self.enqueue(msg)
        return Response(status_code=200)

    @staticmethod
    def _xml_text(node: ET.Element, tag: str, default: str = "") -> str:
        child = node.find(tag)
        if child is None or child.text is None:
            return default
        return child.text

    def _parse_inbound_xml(self, root: ET.Element) -> IncomingMessage | None:
        """Map a decrypted WeCom callback XML into an :class:`IncomingMessage`.

        Sets ``metadata['is_group']`` from ``<ChatType>``: ``group`` → True,
        ``single`` (or absent) → False.
        """
        msg_type = self._xml_text(root, "MsgType", "text")
        msg_id = self._xml_text(root, "MsgId")
        from_user = self._xml_text(root, "FromUserName")
        to_user = self._xml_text(root, "ToUserName")
        chat_type = self._xml_text(root, "ChatType", "single")
        is_group = chat_type == "group"
        # Group messages may carry a chat ID separately.
        chat_id = self._xml_text(root, "ChatId") or to_user
        channel_id = chat_id if is_group else from_user

        if msg_type == "text":
            content = self._xml_text(root, "Content")
        elif msg_type == "image":
            content = "[image]"
        elif msg_type == "voice":
            content = "[voice]"
        elif msg_type == "event":
            event = self._xml_text(root, "Event", "")
            content = f"[event:{event}]" if event else "[event]"
        else:
            content = f"[{msg_type}]"

        metadata: dict[str, Any] = {
            "message_id": msg_id,
            "msg_type": msg_type,
            "chat_type": chat_type,
            "is_group": is_group,
            "from_user": from_user,
            "to_user": to_user,
        }
        if is_group:
            # Current WeCom group support targets the AI Bot callback shape:
            # group callbacks are delivered only when the bot is addressed.
            metadata["chat_id"] = chat_id
            metadata["wecom_protocol"] = "aibot"
            metadata["bot_mentioned"] = True

        sender_id = from_user or "unknown"
        return IncomingMessage(
            sender_id=sender_id,
            channel_id=channel_id or "unknown",
            content=content,
            metadata=metadata,
            provenance=IngressProvenance(
                provider="wecom",
                account_id=self.config.corp_id,
                transport="webhook",
                verification=IngressVerification.WEBHOOK_SIGNATURE,
                event_id=msg_id or None,
                principal=AuthenticatedPrincipal(subject_id=sender_id),
            ),
        )

    def is_group_mentioned(self, msg: IncomingMessage) -> bool:
        if not bool(msg.metadata.get("is_group")):
            return True
        return msg.metadata.get("wecom_protocol") == "aibot" and bool(
            msg.metadata.get("bot_mentioned")
        )

    def _websocket_reply_metadata(self, inbound: IncomingMessage) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "chat_id": inbound.metadata.get("chat_id") or inbound.channel_id,
        }
        req_id = str(inbound.metadata.get("wecom_req_id") or "")
        if req_id:
            metadata["wecom_req_id"] = req_id
            expires_at = inbound.metadata.get("wecom_req_id_expires_at")
            if isinstance(expires_at, int | float):
                metadata["wecom_req_id_expires_at"] = float(expires_at)
        return metadata

    def build_reply_message(self, content: str, inbound: IncomingMessage) -> OutgoingMessage:
        if self.config.connection_mode != "websocket":
            metadata: dict[str, Any] = {}
            for inherit_key in ("toparty", "totag"):
                if inherit_key in inbound.metadata:
                    metadata[inherit_key] = inbound.metadata[inherit_key]
            return OutgoingMessage(
                content=content,
                reply_to=inbound.sender_id,
                metadata=metadata,
            )
        return OutgoingMessage(
            content=content,
            reply_to=inbound.channel_id,
            metadata=self._websocket_reply_metadata(inbound),
        )

    def streaming_reply_kwargs(self, inbound: IncomingMessage) -> dict[str, Any]:
        if self.config.connection_mode != "websocket":
            # Webhook (corp-app) mode: pin the reply to the inbound sender so a
            # concurrent inbound message cannot redirect it via the shared
            # _last_incoming_envelope slot. Inherit toparty/totag from inbound.
            out_meta: dict[str, Any] = {}
            for inherit_key in ("toparty", "totag"):
                if inherit_key in inbound.metadata:
                    out_meta[inherit_key] = inbound.metadata[inherit_key]
            return {"reply_to": inbound.sender_id, "metadata": out_meta}
        return {
            "reply_to": inbound.channel_id,
            "metadata": self._websocket_reply_metadata(inbound),
        }

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    def _build_send_payload(self, message: OutgoingMessage) -> dict[str, Any]:
        """Translate ``OutgoingMessage`` into a WeCom corp-app message body.

        ``reply_to`` is interpreted as the user target. Party and tag targets
        may be supplied explicitly via metadata. An omitted recipient is a
        caller error; it must never turn into an implicit broadcast.
        """
        user_target = str(message.reply_to or message.metadata.get("touser") or "").strip()
        party_target = str(message.metadata.get("toparty") or "").strip()
        tag_target = str(message.metadata.get("totag") or "").strip()
        if not any((user_target, party_target, tag_target)):
            raise WeComApiError("an explicit touser, toparty, or totag target is required")

        payload: dict[str, Any] = {
            "msgtype": "text",
            "agentid": self.config.agent_id_int,
            "text": {"content": message.content},
            "safe": 0,
        }
        if user_target:
            payload["touser"] = user_target
        if party_target:
            payload["toparty"] = party_target
        if tag_target:
            payload["totag"] = tag_target
        return payload

    async def send(self, message: OutgoingMessage) -> None:
        if self.config.connection_mode == "websocket":
            await self._send_websocket_message(message)
            return

        token = await self._get_token()
        client = self._get_client()
        payload = self._build_send_payload(message)
        resp = await client.post(
            "/cgi-bin/message/send",
            params={"access_token": token},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            raise WeComApiError(data.get("errmsg", "send failed"), code=data.get("errcode"))
        log.info("wecom.outbound_sent", touser=payload.get("touser"))

    async def _send_websocket_message(self, message: OutgoingMessage) -> None:
        metadata = dict(message.metadata or {})
        reply_req_id = self._metadata_reply_req_id(metadata)
        if not reply_req_id and message.reply_to:
            reply_req_id = self._get_valid_reply_req_id(str(message.reply_to))
        explicit_target = bool(
            message.reply_to
            or metadata.get("chatid")
            or metadata.get("chat_id")
            or metadata.get("touser")
        )
        target = str(
            message.reply_to
            or metadata.get("chatid")
            or metadata.get("chat_id")
            or metadata.get("touser")
            or ""
        )
        if not reply_req_id and not explicit_target and self._last_incoming_envelope is not None:
            last = self._last_incoming_envelope
            last_target = str(last.metadata.get("chat_id") or last.channel_id or "")
            target = last_target
            reply_req_id = self._get_valid_chat_req_id(last_target)

        body = {
            "msgtype": "markdown",
            "markdown": {"content": message.content},
        }
        if reply_req_id:
            await self._send_ws_request(_APP_CMD_RESPONSE, body, req_id=reply_req_id)
            self._forget_req_id(reply_req_id)
            log.info("wecom.websocket_reply_sent", req_id=reply_req_id)
            return
        if not target:
            raise WeComApiError("chatid is required for proactive websocket sends")
        await self._send_ws_request(_APP_CMD_SEND, {"chatid": target, **body})
        log.info("wecom.websocket_outbound_sent", chatid=target)

    @staticmethod
    def _metadata_reply_req_id(metadata: dict[str, Any]) -> str:
        req_id = str(metadata.get("wecom_req_id") or "")
        if not req_id:
            return ""
        expires_at = metadata.get("wecom_req_id_expires_at")
        if isinstance(expires_at, int | float) and time.monotonic() >= float(expires_at):
            return ""
        return req_id

    async def send_file(
        self,
        target_id: str,
        file_path: str,
        content: str = "",
    ) -> ChannelSendResult:
        if self.config.connection_mode == "websocket":
            raise UnsupportedChannelOperation(
                channel="wecom",
                operation="send_file",
                reason="WeCom AI Bot websocket media upload is not implemented yet",
            )

        token = await self._get_token()
        client = self._get_client()
        path = Path(file_path)
        media_type = self._wecom_media_type(path)
        with path.open("rb") as f:
            upload_resp = await client.post(
                "/cgi-bin/media/upload",
                params={"access_token": token, "type": media_type},
                files={"media": (path.name, f)},
            )
        upload_resp.raise_for_status()
        upload_data = upload_resp.json()
        if upload_data.get("errcode", 0) != 0:
            raise WeComApiError(
                upload_data.get("errmsg", "media upload failed"),
                code=upload_data.get("errcode"),
            )
        media_id = str(upload_data.get("media_id", ""))
        if not media_id:
            raise WeComApiError("media upload returned no media_id")
        payload = {
            "touser": str(target_id),
            "msgtype": media_type,
            "agentid": self.config.agent_id_int,
            media_type: {"media_id": media_id},
            "safe": 0,
        }
        if content:
            await self.send(OutgoingMessage(content=content, reply_to=target_id))
        send_resp = await client.post(
            "/cgi-bin/message/send",
            params={"access_token": token},
            json=payload,
        )
        send_resp.raise_for_status()
        send_data = send_resp.json()
        if send_data.get("errcode", 0) != 0:
            raise WeComApiError(
                send_data.get("errmsg", "send failed"),
                code=send_data.get("errcode"),
            )
        return ChannelSendResult.sent(
            capability=ChannelCapabilities.NATIVE_FILE_UPLOAD,
            target_id=str(target_id),
            provider_message_id=str(send_data.get("msgid", "")),
            provider_file_id=media_id,
        )

    @staticmethod
    def _wecom_media_type(path: Path) -> str:
        mime_type = mimetypes.guess_type(path.name)[0] or ""
        if mime_type.startswith("image/"):
            return "image"
        if mime_type.startswith("video/"):
            return "video"
        if mime_type.startswith("audio/"):
            return "voice"
        return "file"

    async def edit(self, message_id: str, content: str) -> None:
        raise UnsupportedChannelOperation(
            channel="wecom",
            operation="edit",
            reason="WeCom corp app has no generic message-edit primitive",
        )

    async def delete(self, message_id: str) -> None:
        # `recall` exists but is intentionally NOT used here: streaming/edit
        # must not call recall mid-stream.
        raise UnsupportedChannelOperation(
            channel="wecom",
            operation="delete",
            reason="corp-app recall is not exposed as a generic delete primitive.",
        )

    # ------------------------------------------------------------------
    # Streaming — final-flush only
    # ------------------------------------------------------------------

    async def send_streaming(
        self,
        chunks: AsyncIterator[str],
        *,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Accumulate the LLM stream and emit exactly one outbound POST.

        WeCom corp app has no edit/recall-safe streaming path; we
        never call ``cgi-bin/message/recall`` or
        ``cgi-bin/message/update_template_card`` mid-stream. If the
        stream raises before completing, we issue zero outbound HTTP.
        """
        accumulated = ""
        async for chunk in chunks:
            accumulated += chunk
        if not accumulated:
            return

        # Implicit reply-target fallback: when the dispatcher invokes
        # ``send_streaming(chunks)`` with no kwargs, fall back to the
        # last received envelope's sender so the reply is targeted at
        # the originator instead of broadcasting to ``@all``.
        last = self._last_incoming_envelope
        out_meta: dict[str, Any] = dict(metadata or {})
        target = reply_to or out_meta.get("touser")
        if not target and last is not None:
            if self.config.connection_mode == "websocket":
                target = last.metadata.get("chat_id") or last.channel_id
                out_meta.update(self._websocket_reply_metadata(last))
            else:
                target = last.sender_id
                for inherit_key in ("toparty", "totag"):
                    if inherit_key not in out_meta and inherit_key in last.metadata:
                        out_meta[inherit_key] = last.metadata[inherit_key]
        await self.send(
            OutgoingMessage(
                content=accumulated,
                reply_to=target or "",
                metadata=out_meta,
            )
        )
