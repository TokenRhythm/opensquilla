"""DiscordChannel: adapter for Discord Bot Gateway (WebSocket) and REST API."""

from __future__ import annotations

import asyncio
import json
import random
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import structlog
import websockets
import websockets.asyncio.client
from pydantic import BaseModel

from opensquilla.channels._attachment_io import (
    attachment_limit_for_mime,
    ensure_declared_size_within_limit,
    fetch_httpx_bytes_limited,
    preferred_attachment_mime,
)
from opensquilla.channels._util import (
    ChannelAccessPolicy,
    EventDedupeCache,
    RateLimiter,
    StreamThrottle,
    retry_request,
    split_text_for_channel,
)
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
    Attachment,
    AuthenticatedPrincipal,
    ChannelHealth,
    IncomingMessage,
    IngressProvenance,
    IngressVerification,
    OutgoingMessage,
)
from opensquilla.env import trust_env as _trust_env

log = structlog.get_logger(__name__)

_DISCORD_MENTION_RE = re.compile(r"<@!?(\d+)>")
_DISCORD_DM_CHANNEL_TYPES = {1}
# Discord rejects message content longer than 2000 characters.
_DISCORD_MAX_MESSAGE_CHARS = 2000
_DISCORD_GROUP_DM_CHANNEL_TYPES = {3}
_DISCORD_THREAD_CHANNEL_TYPES = {10, 11, 12}

# Gateway intents bitmask
GATEWAY_INTENTS = (
    (1 << 0)  # GUILDS
    | (1 << 9)  # GUILD_MESSAGES
    | (1 << 12)  # DIRECT_MESSAGES
    | (1 << 15)  # MESSAGE_CONTENT (privileged)
    | (1 << 10)  # GUILD_MESSAGE_REACTIONS
    | (1 << 13)  # DIRECT_MESSAGE_REACTIONS
)

# Channel-contract constants pinned by the adapter audit.
CAPABILITY_TIER = "YELLOW-experimental"

# Discord is a DM/group channel; the permission matrix denies admin-only tools.
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


class DiscordChannelConfig(BaseModel):
    """Pydantic config for Discord channel adapter."""

    token: str
    application_id: str = ""
    default_channel_id: str = ""
    api_base: str = "https://discord.com/api/v10"
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    intents: int = GATEWAY_INTENTS
    reconnect_max_retries: int = 5
    reconnect_base_delay_s: float = 1.0

    model_config = {}  # explicit params only; no env loading


@dataclass
class _GatewayState:
    session_id: str | None = None
    sequence: int | None = None
    resume_url: str | None = None
    heartbeat_interval_ms: int = 41250
    last_heartbeat_ack: bool = True


class _GatewayHandshakeRetryError(RuntimeError):
    """The provider requested a fresh handshake before readiness."""


@dataclass
class DiscordChannel:
    """Channel adapter for Discord via Gateway WebSocket and REST API.

    Uses the ``websockets`` library for the gateway connection
    and ``httpx.AsyncClient`` for REST calls.
    """

    config: DiscordChannelConfig
    bot_user_id: str | None = None
    supports_slash_commands: bool = True
    # Discord mirrors slack/feishu: DMs admit, groups require mention.
    policy: ChannelAccessPolicy = field(
        default_factory=lambda: ChannelAccessPolicy(
            dm_allowed=True,
            group_allowed=True,
            mention_required_in_group=True,
            allowlist=frozenset(),
        )
    )

    _queue: asyncio.Queue[IncomingMessage] = field(
        default_factory=asyncio.Queue, init=False, repr=False
    )
    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)
    _last_message_at: datetime | None = field(default=None, init=False, repr=False)
    _ws: Any = field(default=None, init=False, repr=False)
    _state: _GatewayState = field(default_factory=_GatewayState, init=False, repr=False)
    _heartbeat_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _dispatch_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _reconnect_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _reconnecting: bool = field(default=False, init=False, repr=False)
    _dedupe: EventDedupeCache = field(
        default_factory=lambda: EventDedupeCache(max_size=10_000),
        init=False,
        repr=False,
    )
    _rate_limiter: RateLimiter = field(default_factory=RateLimiter, init=False, repr=False)
    _sent_messages: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _resolved_interactions: set[str] = field(default_factory=set, init=False, repr=False)
    _channel_types: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _thread_parent_channels: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    @property
    def capability_profile(self) -> ChannelCapabilityProfile:
        return ChannelCapabilityProfile(
            channel_type="discord",
            max_message_len=2000,
            length_unit=ChannelLengthUnit.CODE_POINTS,
            splits_natively=True,
            group_chat=True,
            mentions=True,
            typing_indicator=True,
            native_file_upload=True,
            media=True,
            reactions=True,
            inbound_reactions=True,
            threads=True,
            group_dm=True,
            edit=True,
            delete=True,
            streamed_message_replacement=True,
            transports=("websocket",),
        )

    @property
    def platform_capability_manifest(self) -> ChannelPlatformManifest:
        return ChannelPlatformManifest.from_channel_profile(
            self.capability_profile,
            has_send_file=True,
            has_inbound_attachment_resolver=True,
        ).with_capabilities(
            ChannelPlatformCapability(
                category=ChannelPlatformCategories.FILES,
                status=ChannelPlatformCapabilityStatus.SUPPORTED,
                tools=("multipart/form-data message attachments",),
                mutates=True,
                notes=("Discord file delivery attaches files to create-message requests.",),
            ),
            ChannelPlatformCapability(
                category=ChannelPlatformCategories.ATTACHMENTS,
                status=ChannelPlatformCapabilityStatus.SUPPORTED,
                tools=("attachment.url",),
                notes=("Inbound Discord attachments are downloaded from message attachment URLs.",),
            ),
            ChannelPlatformCapability(
                category=ChannelPlatformCategories.THREADS,
                status=ChannelPlatformCapabilityStatus.SUPPORTED,
                notes=("Discord thread channels are detected from channel type metadata.",),
            ),
        )

    @property
    def capabilities(self) -> frozenset[str]:
        return self.capability_profile.capability_tags()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.config.api_base,
                timeout=30.0,
                trust_env=_trust_env(),
            )
        return self._client

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bot {self.config.token}"}

    def _require_credentials(self) -> None:
        if not self.config.token.strip():
            raise ValueError("discord: bot token is required")

    # ------------------------------------------------------------------
    # Gateway WebSocket helpers
    # ------------------------------------------------------------------

    async def _connect_ws(self, url: str) -> Any:
        ws = await websockets.asyncio.client.connect(url)
        return ws

    async def _ws_send(self, payload: dict[str, Any]) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps(payload))

    async def _ws_recv(self) -> dict[str, Any]:
        ws = self._ws
        if ws is None:
            # A concurrent reconnect (e.g. the heartbeat task detected a
            # missed ACK) tears the socket down before its replacement
            # exists.  Surface the closed-connection signal callers already
            # handle instead of dying on ``None.recv()`` with AttributeError.
            raise websockets.exceptions.ConnectionClosedError(None, None)
        raw = await ws.recv()
        return cast(dict[str, Any], json.loads(raw))

    async def _close_ws(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _fetch_gateway_url(self) -> str:
        client = self._get_client()
        resp = await client.get("/gateway/bot", headers=self._auth_headers())
        resp.raise_for_status()
        return cast(str, resp.json()["url"]) + "?v=10&encoding=json"

    async def _identify(self) -> None:
        await self._ws_send(
            {
                "op": 2,
                "d": {
                    "token": self.config.token,
                    "intents": self.config.intents,
                    "properties": {
                        "os": "linux",
                        "browser": "opensquilla",
                        "device": "opensquilla",
                    },
                },
            }
        )

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        # Discord requires the first heartbeat after Hello to be jittered by a
        # random fraction of the negotiated interval.  This also prevents a
        # fleet of freshly restarted adapters from synchronising heartbeats.
        interval_s = self._state.heartbeat_interval_ms / 1000.0
        await asyncio.sleep(random.random() * interval_s)
        while self._ws is not None:
            if not self._state.last_heartbeat_ack:
                log.warning("discord.heartbeat_timeout")
                await self._reconnect()
                return
            self._state.last_heartbeat_ack = False
            await self._ws_send({"op": 1, "d": self._state.sequence})
            await asyncio.sleep(interval_s)

    async def _cancel_heartbeat(self) -> None:
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _apply_hello(self, payload: dict[str, Any]) -> None:
        if payload.get("op") != 10:
            raise RuntimeError("discord gateway did not send Hello")
        data = payload.get("d")
        interval = data.get("heartbeat_interval") if isinstance(data, dict) else None
        if not isinstance(interval, int | float) or interval <= 0:
            raise RuntimeError("discord gateway Hello omitted heartbeat_interval")
        self._state.heartbeat_interval_ms = int(interval)

    async def _begin_gateway_session(self, url: str) -> None:
        self._ws = await self._connect_ws(url)
        self._apply_hello(await self._ws_recv())
        self._state.last_heartbeat_ack = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        if self._state.session_id and self._state.sequence is not None:
            await self._ws_send(
                {
                    "op": 6,
                    "d": {
                        "token": self.config.token,
                        "session_id": self._state.session_id,
                        "seq": self._state.sequence,
                    },
                }
            )
        else:
            await self._identify()

    async def _await_ready_dispatch(self) -> None:
        while True:
            raw = await self._ws_recv()
            op = raw.get("op")
            if op == 0:
                self._state.sequence = raw.get("s")
                event_type = raw.get("t")
                data = raw.get("d", {})
                if not isinstance(data, dict):
                    data = {}
                if event_type == "RESUMED" and not self._state.session_id:
                    raise RuntimeError("discord gateway resumed without a session")
                await self._handle_dispatch(event_type, data)
                if event_type in {"READY", "RESUMED"}:
                    return
            elif op == 1:
                await self._ws_send({"op": 1, "d": self._state.sequence})
            elif op == 7:
                raise _GatewayHandshakeRetryError("discord gateway requested reconnect")
            elif op == 9:
                if not raw.get("d", False):
                    self._state.session_id = None
                    self._state.sequence = None
                await asyncio.sleep(1 + random.random() * 4)
                raise _GatewayHandshakeRetryError("discord gateway invalidated the session")
            elif op == 11:
                self._state.last_heartbeat_ack = True

    # ------------------------------------------------------------------
    # Reconnection
    # ------------------------------------------------------------------

    async def _reconnect(self) -> None:
        """Re-establish the gateway connection.

        Idempotent under concurrent calls: a second invocation while a
        first is in flight waits for that attempt instead of starting its
        own. Without this guard a heartbeat timeout racing an op-7 / op-9
        in the dispatch loop could trigger two simultaneous IDENTIFY
        sequences and leave two heartbeat tasks running against the same
        socket. Waiting (rather than returning immediately) matters for the
        dispatch loop: the in-flight reconnect owns the socket and may hold
        ``self._ws = None`` for the whole retry window, so returning early
        would send the caller straight back into ``_ws_recv`` on a dead
        socket and spin until the new connection exists.
        """
        if self._reconnecting:
            log.info("discord.reconnect_waiting_for_in_flight")
            async with self._reconnect_lock:
                return
        async with self._reconnect_lock:
            self._reconnecting = True
            try:
                attempts = max(0, int(self.config.reconnect_max_retries)) + 1
                for attempt in range(attempts):
                    try:
                        await self._do_reconnect()
                        return
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if attempt + 1 >= attempts:
                            self._connected = False
                            log.error(
                                "discord.reconnect_exhausted",
                                attempts=attempts,
                                error=str(exc),
                            )
                            raise
                        delay = min(
                            self.config.reconnect_base_delay_s * (2**attempt),
                            30.0,
                        )
                        log.warning(
                            "discord.reconnect_retry",
                            attempt=attempt + 1,
                            delay=delay,
                            error=str(exc),
                        )
                        await asyncio.sleep(delay)
            finally:
                self._reconnecting = False

    async def _do_reconnect(self) -> None:
        log.info("discord.reconnecting", session_id=self._state.session_id)
        await self._cancel_heartbeat()
        await self._close_ws()

        url = self._state.resume_url or await self._fetch_gateway_url()
        await self._begin_gateway_session(url)

    # ------------------------------------------------------------------
    # Dispatch loop
    # ------------------------------------------------------------------

    async def _dispatch_loop(self) -> None:
        while self._connected:
            try:
                raw = await self._ws_recv()
            except (
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.ConnectionClosedError,
                websockets.exceptions.ConnectionClosedOK,
            ):
                if self._connected:
                    await self._reconnect()
                continue

            op = raw.get("op")
            if op == 0:  # Dispatch
                self._state.sequence = raw.get("s")
                event_type = raw.get("t")
                data = raw.get("d", {})
                await self._handle_dispatch(event_type, data)
            elif op == 1:  # Heartbeat request
                await self._ws_send({"op": 1, "d": self._state.sequence})
            elif op == 7:  # Reconnect
                await self._reconnect()
                continue
            elif op == 9:  # Invalid Session
                resumable = raw.get("d", False)
                if not resumable:
                    self._state.session_id = None
                    self._state.sequence = None
                await asyncio.sleep(1 + random.random() * 4)
                await self._reconnect()
                continue
            elif op == 11:  # Heartbeat ACK
                self._state.last_heartbeat_ack = True

    async def _handle_dispatch(self, event_type: str | None, data: dict[str, Any]) -> None:
        if event_type == "READY":
            self._state.session_id = data["session_id"]
            self._state.resume_url = data.get("resume_gateway_url")
            self.bot_user_id = data["user"]["id"]
            log.info(
                "discord.ready",
                user=data["user"]["username"],
                guilds=len(data.get("guilds", [])),
            )
        elif event_type == "MESSAGE_CREATE":
            if data.get("author", {}).get("id") == self.bot_user_id:
                return
            msg_id = str(data.get("id") or "")
            if msg_id and not self._dedupe.check_and_add(f"message:{msg_id}"):
                return
            msg = self.parse_event(self._annotate_channel_context(data))
            self.enqueue(msg)
        elif event_type == "MESSAGE_REACTION_ADD":
            reaction_key = self._reaction_dedupe_key(data)
            if reaction_key and not self._dedupe.check_and_add(reaction_key):
                return
            self._enqueue_reaction(self._annotate_channel_context(data))
        elif event_type == "INTERACTION_CREATE":
            # Gateway interactions still use the same interaction callback
            # contract as HTTP-delivered interactions.  Defer before doing any
            # channel dispatch work so the provider's three-second deadline is
            # met, then resolve the original response when the turn completes.
            deferred = await self._defer_interaction(data)
            interaction_id = str(data.get("id") or "")
            if interaction_id and not self._dedupe.check_and_add(f"interaction:{interaction_id}"):
                return
            enriched = self._annotate_channel_context(data)
            enriched["interaction_deferred"] = deferred
            self._handle_interaction(enriched)
        elif event_type in {"CHANNEL_CREATE", "CHANNEL_UPDATE", "THREAD_CREATE", "THREAD_UPDATE"}:
            self._cache_channel_context(data)
        elif event_type == "GUILD_CREATE":
            for channel in data.get("channels", []):
                if isinstance(channel, dict):
                    self._cache_channel_context(channel)
            for thread in data.get("threads", []):
                if isinstance(thread, dict):
                    self._cache_channel_context(thread)
        elif event_type == "THREAD_LIST_SYNC":
            for thread in data.get("threads", []):
                if isinstance(thread, dict):
                    self._cache_channel_context(thread)
        elif event_type == "RESUMED":
            log.info("discord.resumed")

    def _cache_channel_context(self, data: dict[str, Any]) -> None:
        channel_id = data.get("id")
        channel_type = self._channel_type(data.get("type"))
        if isinstance(channel_id, str) and channel_id and channel_type is not None:
            self._channel_types[channel_id] = channel_type
        parent_id = data.get("parent_id")
        if isinstance(channel_id, str) and channel_id and isinstance(parent_id, str) and parent_id:
            self._thread_parent_channels[channel_id] = parent_id

    def _annotate_channel_context(self, data: dict[str, Any]) -> dict[str, Any]:
        channel_id = data.get("channel_id")
        if not isinstance(channel_id, str) or not channel_id:
            return data
        enriched = dict(data)
        if "channel_type" not in enriched and channel_id in self._channel_types:
            enriched["channel_type"] = self._channel_types[channel_id]
        if (
            "thread_parent_channel_id" not in enriched
            and channel_id in self._thread_parent_channels
        ):
            enriched["thread_parent_channel_id"] = self._thread_parent_channels[channel_id]
        return enriched

    @staticmethod
    def _reaction_dedupe_key(data: dict[str, Any]) -> str:
        emoji = data.get("emoji", {})
        emoji_key = emoji.get("id") or emoji.get("name") or ""
        if not data.get("message_id") or not data.get("user_id") or not emoji_key:
            return ""
        return f"reaction:{data.get('message_id', '')}:{data.get('user_id', '')}:{emoji_key}"

    def _enqueue_reaction(self, data: dict[str, Any]) -> None:
        user_id = data.get("user_id", "unknown")
        channel_id = data.get("channel_id", "unknown")
        emoji = data.get("emoji", {})
        channel_type = self._channel_type(data.get("channel_type"))
        thread_id = self._native_thread_id(data, channel_type)
        conversation_kind = self._conversation_kind(data, channel_type, thread_id)
        parent_channel_id = data.get("thread_parent_channel_id")
        msg = IncomingMessage(
            sender_id=user_id,
            channel_id=channel_id,
            content="",
            metadata={
                "event_type": "MESSAGE_REACTION_ADD",
                "message_id": data.get("message_id"),
                "emoji_name": emoji.get("name", ""),
                "emoji_id": emoji.get("id"),
                "guild_id": data.get("guild_id"),
                "channel_type": channel_type,
                "is_group": conversation_kind in {"group", "group_dm", "thread", "topic"},
                "conversation_kind": conversation_kind,
                "native_message_id": data.get("message_id"),
                "native_chat_id": channel_id,
                "native_thread_id": thread_id,
                "native_parent_channel_id": parent_channel_id,
                "reply_target_id": data.get("message_id"),
            },
            provenance=IngressProvenance(
                provider="discord",
                account_id=self.config.application_id,
                transport="websocket",
                verification=IngressVerification.SDK_SESSION,
                event_id=str(data.get("message_id") or "") or None,
                principal=AuthenticatedPrincipal(subject_id=str(user_id)),
            ),
        )
        self.enqueue(msg)

    def _handle_interaction(self, data: dict[str, Any]) -> None:
        """Parse a slash command interaction into IncomingMessage."""
        interaction_data = data.get("data", {})
        user = data.get("member", {}).get("user", data.get("user", {}))
        channel_id = data.get("channel_id", "unknown")

        # Build content from command name and options
        command_name = interaction_data.get("name", "")
        options = interaction_data.get("options", [])
        option_parts = [opt.get("value", "") for opt in options if opt.get("value")]
        content = f"/{command_name} {' '.join(str(v) for v in option_parts)}".strip()
        channel_type = self._channel_type(data.get("channel_type"))
        thread_id = self._native_thread_id(data, channel_type)
        conversation_kind = self._conversation_kind(data, channel_type, thread_id)
        parent_channel_id = data.get("thread_parent_channel_id")

        msg = IncomingMessage(
            sender_id=str(user.get("id", "unknown")),
            channel_id=channel_id,
            content=content,
            metadata={
                "interaction_type": "slash_command",
                "command_name": command_name,
                "interaction_id": data.get("id"),
                "interaction_token": data.get("token"),
                "application_id": data.get("application_id") or self.config.application_id,
                "interaction_deferred": bool(data.get("interaction_deferred")),
                "guild_id": data.get("guild_id"),
                "channel_type": channel_type,
                "is_group": conversation_kind in {"group", "group_dm", "thread", "topic"},
                "conversation_kind": conversation_kind,
                "native_message_id": data.get("id"),
                "native_chat_id": channel_id,
                "native_thread_id": thread_id,
                "native_parent_channel_id": parent_channel_id,
                "reply_target_id": data.get("id"),
            },
            provenance=IngressProvenance(
                provider="discord",
                account_id=str(data.get("application_id") or self.config.application_id),
                transport="websocket",
                verification=IngressVerification.SDK_SESSION,
                event_id=str(data.get("id") or "") or None,
                principal=AuthenticatedPrincipal(subject_id=str(user.get("id", "unknown"))),
            ),
        )
        self.enqueue(msg)

    async def _defer_interaction(self, data: dict[str, Any]) -> bool:
        """Acknowledge an interaction with a deferred channel response."""
        interaction_id = str(data.get("id") or "")
        token = str(data.get("token") or "")
        if not interaction_id or not token:
            log.warning("discord.interaction_missing_callback_identity")
            return False
        try:
            resp = await self._get_client().post(
                f"/interactions/{interaction_id}/{token}/callback",
                json={"type": 5},
            )
            resp.raise_for_status()
        except Exception as exc:
            log.warning(
                "discord.interaction_defer_failed",
                interaction_id=interaction_id,
                error=str(exc),
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect to the Discord gateway and begin dispatch/heartbeat loops."""
        self._require_credentials()
        attempts = max(0, int(self.config.reconnect_max_retries)) + 1
        for attempt in range(attempts):
            try:
                url = (
                    self.config.gateway_url
                    if attempt == 0
                    else self._state.resume_url or await self._fetch_gateway_url()
                )
                await self._begin_gateway_session(url)
                await self._await_ready_dispatch()
            except asyncio.CancelledError:
                await self._cancel_heartbeat()
                await self._close_ws()
                raise
            except Exception:
                await self._cancel_heartbeat()
                await self._close_ws()
                if attempt + 1 >= attempts:
                    self._connected = False
                    raise
                delay = min(self.config.reconnect_base_delay_s * (2**attempt), 30.0)
                await asyncio.sleep(delay)
                continue

            self._connected = True
            self._dispatch_task = asyncio.create_task(self._dispatch_loop())
            log.info("discord.started", bot_user_id=self.bot_user_id)
            return

    async def probe_connection(self) -> dict[str, Any]:
        """Validate the bot token and Gateway availability without connecting."""
        self._require_credentials()
        gateway_url = await self._fetch_gateway_url()
        return {"authenticated": True, "gateway_url": gateway_url}

    async def stop(self) -> None:
        """Disconnect from gateway and clean up."""
        self._connected = False
        await self._cancel_heartbeat()
        dispatch_task = self._dispatch_task
        self._dispatch_task = None
        if dispatch_task is not None and dispatch_task is not asyncio.current_task():
            dispatch_task.cancel()
            await asyncio.gather(dispatch_task, return_exceptions=True)
        await self._close_ws()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        log.info("discord.stopped")

    def is_connected(self) -> bool:
        return self._connected

    async def health_check(self) -> ChannelHealth:
        workers_alive = (
            self._heartbeat_task is not None
            and not self._heartbeat_task.done()
            and self._dispatch_task is not None
            and not self._dispatch_task.done()
        )
        return ChannelHealth(
            connected=self._connected and workers_alive,
            bot_user_id=self.bot_user_id,
            last_message_at=self._last_message_at,
            extra={
                "session_id": self._state.session_id,
                "sequence": self._state.sequence,
            },
        )

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    def enqueue(self, message: IncomingMessage) -> None:
        from opensquilla.channels.delivery_store import durable_enqueue

        durable_enqueue(self, message, self._queue)
        self._last_message_at = datetime.now(UTC)

    async def receive(self) -> IncomingMessage:
        msg = await self._queue.get()
        log.debug("discord.receive", content=msg.content[:80])
        return msg

    async def resolve_inbound_attachment(self, attachment: Attachment) -> Attachment:
        """Fetch Discord attachment bytes; shared ingest owns validation."""

        if attachment.data is not None or not attachment.url:
            return attachment
        limit = attachment_limit_for_mime(attachment.mime_type)
        ensure_declared_size_within_limit(attachment.size, name=attachment.name, limit=limit)
        payload, content_type = await fetch_httpx_bytes_limited(
            self._get_client(),
            attachment.url,
            name=attachment.name,
            limit=limit,
        )
        return Attachment(
            name=attachment.name,
            mime_type=preferred_attachment_mime(content_type, attachment.mime_type),
            data=payload,
            size=len(payload),
            metadata={**attachment.metadata, "source_url": attachment.url},
        )

    def parse_event(self, data: dict[str, Any]) -> IncomingMessage:
        author = data.get("author", {})
        content = data.get("content", "")
        channel_id = data.get("channel_id", "unknown")
        message_id = data.get("id")
        channel_type = self._channel_type(data.get("channel_type"))
        thread_id = self._native_thread_id(data, channel_type)
        referenced_message_id = data.get("message_reference", {}).get("message_id")
        parent_channel_id = data.get("thread_parent_channel_id")
        if not isinstance(parent_channel_id, str):
            parent_channel_id = data.get("message_reference", {}).get("channel_id")
        conversation_kind = self._conversation_kind(data, channel_type, thread_id)
        is_group = conversation_kind in {"group", "group_dm", "thread", "topic"}

        attachments: list[Attachment] = []
        for att in data.get("attachments", []):
            attachments.append(
                Attachment(
                    name=att.get("filename", "unknown"),
                    mime_type=att.get("content_type"),
                    url=att.get("url"),
                    size=att.get("size"),
                )
            )

        metadata: dict[str, Any] = {
            "message_id": message_id,
            "channel_id": channel_id,
            "guild_id": data.get("guild_id"),
            "channel_type": channel_type,
            "is_group": is_group,
            "conversation_kind": conversation_kind,
            "thread_id": thread_id,
            "referenced_message_id": referenced_message_id,
            "native_message_id": message_id,
            "native_chat_id": channel_id,
            "native_thread_id": thread_id,
            "native_parent_id": referenced_message_id,
            "native_parent_channel_id": parent_channel_id,
            "native_root_id": referenced_message_id,
            "reply_target_id": message_id,
        }

        sender_id = str(author.get("id", "unknown"))
        return IncomingMessage(
            sender_id=sender_id,
            channel_id=channel_id,
            content=content,
            attachments=attachments,
            metadata=metadata,
            provenance=IngressProvenance(
                provider="discord",
                account_id=self.config.application_id,
                transport="websocket",
                verification=IngressVerification.SDK_SESSION,
                event_id=str(message_id or "") or None,
                principal=AuthenticatedPrincipal(subject_id=sender_id),
            ),
        )

    @staticmethod
    def _channel_type(raw: Any) -> int | None:
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and raw.isdecimal():
            return int(raw)
        return None

    @staticmethod
    def _native_thread_id(data: dict[str, Any], channel_type: int | None) -> str | None:
        explicit = data.get("thread_id")
        if isinstance(explicit, str) and explicit:
            return explicit
        thread = data.get("thread")
        if isinstance(thread, dict):
            thread_id = thread.get("id")
            if isinstance(thread_id, str) and thread_id:
                return thread_id
        if channel_type in _DISCORD_THREAD_CHANNEL_TYPES:
            channel_id = data.get("channel_id")
            if isinstance(channel_id, str) and channel_id:
                return channel_id
        return None

    @staticmethod
    def _conversation_kind(
        data: dict[str, Any],
        channel_type: int | None,
        thread_id: str | None,
    ) -> str:
        if thread_id or channel_type in _DISCORD_THREAD_CHANNEL_TYPES:
            return "thread"
        if channel_type in _DISCORD_GROUP_DM_CHANNEL_TYPES:
            return "group_dm"
        if channel_type in _DISCORD_DM_CHANNEL_TYPES:
            return "dm"
        if data.get("guild_id") is not None:
            return "group"
        return "dm"

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def send(self, message: OutgoingMessage) -> ChannelSendResult:
        channel_id = str(message.reply_to or self.config.default_channel_id or "").strip()

        interaction_token = str(message.metadata.get("interaction_token") or "")
        application_id = str(
            message.metadata.get("application_id") or self.config.application_id or ""
        )
        if interaction_token and application_id:
            return await self._send_interaction_response(
                message,
                application_id=application_id,
                interaction_token=interaction_token,
                target_id=channel_id,
            )

        if not channel_id:
            raise ValueError("discord.send: channel target is required")
        client = self._get_client()

        # Discord rejects message content over 2000 chars; split long replies
        # so the whole answer is delivered across sequential messages instead
        # of the API rejecting (and dropping) it.
        chunks = split_text_for_channel(message.content, _DISCORD_MAX_MESSAGE_CHARS)
        data: dict[str, Any] = {}
        for idx, chunk in enumerate(chunks):
            await self._rate_limiter.acquire()
            payload: dict[str, Any] = {"content": chunk}

            if idx == 0 and message.metadata.get("embeds"):
                payload["embeds"] = message.metadata["embeds"]

            if idx == 0 and message.metadata.get("reply_to_message_id"):
                payload["message_reference"] = {
                    "message_id": message.metadata["reply_to_message_id"],
                }

            resp = await retry_request(
                client.post,
                f"/channels/{channel_id}/messages",
                json=payload,
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            self._sent_messages[data["id"]] = channel_id
        log.debug("discord.send", channel_id=channel_id, message_id=data.get("id"))
        return ChannelSendResult.sent(
            capability=ChannelCapabilities.GROUP_CHAT,
            target_id=channel_id,
            provider_message_id=str(data.get("id", "")),
        )

    async def _send_interaction_response(
        self,
        message: OutgoingMessage,
        *,
        application_id: str,
        interaction_token: str,
        target_id: str,
    ) -> ChannelSendResult:
        """Resolve a deferred interaction, then use followups for extra parts."""
        client = self._get_client()
        chunks = split_text_for_channel(message.content, _DISCORD_MAX_MESSAGE_CHARS)
        data: dict[str, Any] = {}
        original_pending = interaction_token not in self._resolved_interactions
        for chunk in chunks:
            if original_pending:
                resp = await client.patch(
                    f"/webhooks/{application_id}/{interaction_token}/messages/@original",
                    json={"content": chunk},
                )
                original_pending = False
                self._resolved_interactions.add(interaction_token)
            else:
                # Follow-up creation isn't blindly retried: after a transport
                # timeout Discord may already have accepted it.
                resp = await client.post(
                    f"/webhooks/{application_id}/{interaction_token}",
                    json={"content": chunk},
                )
            resp.raise_for_status()
            if resp.content:
                data = resp.json()
        return ChannelSendResult.sent(
            capability=ChannelCapabilities.GROUP_CHAT,
            target_id=target_id,
            provider_message_id=str(data.get("id", "")),
        )

    async def send_file(
        self,
        channel_id: str,
        file_path: str,
        content: str = "",
    ) -> ChannelSendResult:
        channel_id = str(channel_id or "").strip()
        if not channel_id:
            raise ValueError("discord.send_file: channel target is required")
        await self._rate_limiter.acquire()
        client = self._get_client()
        with open(file_path, "rb") as f:
            resp = await retry_request(
                client.post,
                f"/channels/{channel_id}/messages",
                data={"content": content} if content else {},
                files={"file": (Path(file_path).name, f)},
                headers=self._auth_headers(),
            )
        resp.raise_for_status()
        data = resp.json()
        message_id = str(data.get("id", ""))
        if message_id:
            self._sent_messages[message_id] = channel_id
        return ChannelSendResult.sent(
            capability=ChannelCapabilities.NATIVE_FILE_UPLOAD,
            target_id=channel_id,
            provider_message_id=message_id,
        )

    async def edit(
        self,
        message_id: str,
        content: str,
        *,
        channel_id: str | None = None,
    ) -> ChannelSendResult:
        target = (
            channel_id
            or self._sent_messages.get(message_id)
            or self.config.default_channel_id
            or ""
        ).strip()
        if not target:
            raise ValueError("discord.edit: channel target is required")
        await self._rate_limiter.acquire()
        client = self._get_client()
        resp = await retry_request(
            client.patch,
            f"/channels/{target}/messages/{message_id}",
            json={"content": content},
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        log.debug("discord.edit", message_id=message_id)
        return ChannelSendResult.sent(
            capability=ChannelCapabilities.EDIT,
            target_id=target,
            provider_message_id=message_id,
        )

    async def delete(
        self,
        message_id: str,
        *,
        channel_id: str | None = None,
    ) -> ChannelSendResult:
        target = (
            channel_id
            or self._sent_messages.get(message_id)
            or self.config.default_channel_id
            or ""
        ).strip()
        if not target:
            raise ValueError("discord.delete: channel target is required")
        await self._rate_limiter.acquire()
        client = self._get_client()
        resp = await retry_request(
            client.delete,
            f"/channels/{target}/messages/{message_id}",
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        self._sent_messages.pop(message_id, None)
        log.debug("discord.delete", message_id=message_id)
        return ChannelSendResult.sent(
            capability=ChannelCapabilities.DELETE,
            target_id=target,
            provider_message_id=message_id,
        )

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    async def register_slash_commands(self, commands: list[dict[str, Any]]) -> None:
        """Register global slash commands for the bot application."""
        await self._rate_limiter.acquire()
        client = self._get_client()
        resp = await retry_request(
            client.put,
            f"/applications/{self.config.application_id}/commands",
            json=commands,
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        log.info("discord.commands_registered", count=len(commands))

    # ------------------------------------------------------------------
    # Mentions
    # ------------------------------------------------------------------

    @staticmethod
    def extract_mentions(text: str) -> list[str]:
        return _DISCORD_MENTION_RE.findall(text)

    @staticmethod
    def format_mention(user_id: str) -> str:
        return f"<@{user_id}>"

    def is_mentioned(self, text: str) -> bool:
        if self.bot_user_id is None:
            return False
        return self.bot_user_id in self.extract_mentions(text)

    def is_group_mentioned(self, msg: IncomingMessage) -> bool:
        """Uniform mention check for group gating. Delegates to is_mentioned."""
        return self.is_mentioned(msg.content)

    # ------------------------------------------------------------------
    # Typing indicator
    # ------------------------------------------------------------------

    async def send_typing(self, channel_id: str | None = None) -> ChannelSendResult:
        """Send typing indicator via Discord REST API (lasts ~10s)."""
        target = channel_id or self.config.default_channel_id
        if not target:
            return ChannelSendResult.unsupported(
                capability=ChannelCapabilities.TYPING_INDICATOR,
                reason="no channel target",
            )
        client = self._get_client()
        await client.post(
            f"/channels/{target}/typing",
            headers=self._auth_headers(),
        )
        return ChannelSendResult.sent(
            capability=ChannelCapabilities.TYPING_INDICATOR,
            target_id=target,
        )

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def send_streaming(
        self,
        chunks: AsyncIterator[str],
        *,
        channel_id: str | None = None,
        interaction_token: str | None = None,
        application_id: str | None = None,
        update_interval_ms: int = 500,
    ) -> str | None:
        """Stream a message: post first chunk, PATCH edits for subsequent.

        Returns the message ID or None if iterator was empty.

        Uses ``StreamThrottle`` so two PATCH calls cannot race and a
        single transient failure does not lose accumulated text.
        """
        target = channel_id or self.config.default_channel_id
        if not target:
            raise RuntimeError("Discord stream has no target channel")
        client = self._get_client()
        throttle = StreamThrottle(interval_s=update_interval_ms / 1000.0)
        message_id: str | None = None

        async def _post(text: str) -> None:
            nonlocal message_id
            await self._rate_limiter.acquire()
            if interaction_token and application_id:
                resp = await client.patch(
                    f"/webhooks/{application_id}/{interaction_token}/messages/@original",
                    json={"content": text},
                )
                self._resolved_interactions.add(interaction_token)
            else:
                resp = await retry_request(
                    client.post,
                    f"/channels/{target}/messages",
                    json={"content": text},
                    headers=self._auth_headers(),
                )
            resp.raise_for_status()
            message_id = resp.json().get("id") if resp.content else None
            if message_id:
                self._sent_messages[str(message_id)] = str(target)

        async def _edit(text: str) -> None:
            await self._rate_limiter.acquire()
            if interaction_token and application_id:
                resp = await client.patch(
                    f"/webhooks/{application_id}/{interaction_token}/messages/@original",
                    json={"content": text},
                )
            else:
                resp = await retry_request(
                    client.patch,
                    f"/channels/{target}/messages/{message_id}",
                    json={"content": text},
                    headers=self._auth_headers(),
                )
            # retry_request returns 4xx as-is; surface them like every other
            # REST helper so a rejected edit (e.g. 400 code 50035 for >2000
            # chars) triggers the stream relay's batch fallback instead of
            # silently truncating the reply.
            resp.raise_for_status()

        async for chunk in chunks:
            throttle.add(chunk)
            await throttle.maybe_flush(post=_post, edit=_edit)

        await throttle.force_flush(post=_post, edit=_edit)
        return message_id

    # ------------------------------------------------------------------
    # Reply routing (ChannelTransport hooks)
    # ------------------------------------------------------------------

    def streaming_reply_kwargs(self, inbound: IncomingMessage) -> dict[str, Any]:
        """Stream the reply into the channel/thread that triggered the turn.

        Without this, ``send_streaming`` falls back to
        ``config.default_channel_id`` and every streamed reply lands in one
        static channel regardless of who asked. Discord thread IDs are channel
        IDs, so ``inbound.channel_id`` is the correct target for threads too.
        """
        kwargs: dict[str, Any] = {"channel_id": inbound.channel_id}
        if inbound.metadata.get("interaction_deferred"):
            kwargs["interaction_token"] = inbound.metadata.get("interaction_token")
            kwargs["application_id"] = (
                inbound.metadata.get("application_id") or self.config.application_id
            )
        return kwargs

    def build_reply_message(self, content: str, inbound: IncomingMessage) -> OutgoingMessage:
        """Target the inbound channel so batch replies need no static channel id."""
        metadata: dict[str, Any] = {}
        message_id = inbound.metadata.get("message_id") or inbound.metadata.get("native_message_id")
        if isinstance(message_id, str) and message_id:
            metadata["reply_to_message_id"] = message_id
        if inbound.metadata.get("interaction_deferred"):
            metadata["interaction_token"] = inbound.metadata.get("interaction_token")
            metadata["application_id"] = (
                inbound.metadata.get("application_id") or self.config.application_id
            )
            metadata["interaction_deferred"] = True
        return OutgoingMessage(content=content, reply_to=inbound.channel_id, metadata=metadata)

    # ------------------------------------------------------------------
    # Session key
    # ------------------------------------------------------------------

    def session_key(self, user_id: str, channel_id: str) -> str:
        return f"discord:{user_id}:{channel_id}"

    def session_key_from_event(self, data: dict[str, Any]) -> str:
        user_id = data.get("author", {}).get("id", "unknown")
        channel_id = data.get("channel_id", "unknown")
        return self.session_key(user_id, channel_id)
