"""SlackChannel: adapter for Slack Web API with threading, mentions, lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from opensquilla.channels._reactions import NULL_STATUS_REACTOR, SlackStatusReactor
from opensquilla.channels._util import (
    ChannelAccessPolicy,
    EventDedupeCache,
    StreamThrottle,
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
    AuthenticatedPrincipal,
    ChannelHealth,
    IncomingMessage,
    IngressProvenance,
    IngressVerification,
    OutgoingMessage,
)
from opensquilla.env import trust_env as _trust_env

log = structlog.get_logger(__name__)

SLACK_API_BASE = "https://slack.com/api"
# Slack rejects chat.postMessage text longer than ~40000 characters.
_SLACK_MAX_MESSAGE_CHARS = 40000

_MENTION_RE = re.compile(r"<@(U[A-Z0-9]+)(?:\|[^>]*)?>")

# Channel-contract constants pinned by the adapter audit.
CAPABILITY_TIER = "YELLOW-experimental"

# Slack is a DM/group channel; the permission matrix denies admin-only tools.
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


class SlackAuthError(Exception):
    """Raised when Slack token validation fails."""


@dataclass
class SlackChannel:
    """Channel adapter for Slack Web API.

    Inbound messages are delivered via ``enqueue`` (populated by a Slack
    Events API webhook handler or socket-mode listener).

    Outbound messages use ``chat.postMessage`` via httpx.
    """

    token: str
    slack_channel_id: str
    channel_id: str = "slack"
    sender_id: str = "slack-user"
    bot_user_id: str | None = None
    reply_in_thread: bool = False
    signing_secret: str | None = None
    status_reactions_enabled: bool = False
    # Transport selection. ``socket`` uses Slack Socket Mode (an outbound
    # websocket long-connection) and needs no public Request URL; it requires
    # ``app_token`` (an ``xapp-`` App-Level Token with ``connections:write``).
    connection_mode: str = "webhook"
    app_token: str = ""
    # ``policy`` declares the admit/deny semantics consumed by
    # ``opensquilla.channels._util.evaluate_policy``. Slack admits DMs, admits
    # group messages only when the bot is mentioned, and applies no
    # sender-allowlist filter at the dispatch layer today.
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
    _last_thread_ts: str | None = field(default=None, init=False, repr=False)
    _last_message_at: datetime | None = field(default=None, init=False, repr=False)
    _dedupe: EventDedupeCache = field(
        default_factory=lambda: EventDedupeCache(max_size=10_000),
        init=False,
        repr=False,
    )
    _socket_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _socket_stop: asyncio.Event | None = field(default=None, init=False, repr=False)
    supports_slash_commands: bool = True

    @property
    def transport_name(self) -> str:
        """``websocket`` in Socket Mode so the gateway skips webhook routing."""
        return "websocket" if self.connection_mode == "socket" else "webhook"

    @property
    def capability_profile(self) -> ChannelCapabilityProfile:
        return ChannelCapabilityProfile(
            channel_type="slack",
            max_message_len=40000,
            length_unit=ChannelLengthUnit.CODE_POINTS,
            splits_natively=True,
            group_chat=True,
            mentions=True,
            native_file_upload=True,
            media=True,
            reactions=self.status_reactions_enabled,
            outbound_status_reactions=self.status_reactions_enabled,
            threads=True,
            thread_reply=True,
            edit=True,
            delete=True,
            streamed_message_replacement=True,
            transports=(self.transport_name,),
        )

    @property
    def platform_capability_manifest(self) -> ChannelPlatformManifest:
        return ChannelPlatformManifest.from_channel_profile(
            self.capability_profile,
            has_send_file=True,
        ).with_capabilities(
            ChannelPlatformCapability(
                category=ChannelPlatformCategories.FILES,
                status=ChannelPlatformCapabilityStatus.SUPPORTED,
                tools=("files.getUploadURLExternal", "files.completeUploadExternal"),
                required_scopes=("files:write",),
                mutates=True,
                notes=(
                    "Slack file delivery uses the external upload flow and requires files:write.",
                ),
            ),
            ChannelPlatformCapability(
                category=ChannelPlatformCategories.THREADS,
                status=ChannelPlatformCapabilityStatus.SUPPORTED,
                tools=("thread_ts",),
                notes=("Slack uploads and replies can target an existing thread_ts.",),
            ),
            ChannelPlatformCapability(
                category=ChannelPlatformCategories.ATTACHMENTS,
                status=ChannelPlatformCapabilityStatus.UNSUPPORTED,
                notes=("Inbound Slack file download is not implemented in this adapter.",),
            ),
        )

    @property
    def capabilities(self) -> frozenset[str]:
        return self.capability_profile.capability_tags()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=SLACK_API_BASE,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=30.0,
                trust_env=_trust_env(),
            )
        return self._client

    @property
    def status_reactor(self) -> Any:
        if not self.status_reactions_enabled:
            return NULL_STATUS_REACTOR
        if (reactor := getattr(self, "_status_reactor", None)) is None:
            reactor = self._status_reactor = SlackStatusReactor(self, log)
        return reactor

    async def close(self) -> None:
        """Close the underlying httpx client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    def enqueue(self, message: IncomingMessage) -> None:
        """Push an inbound Slack message into the receive queue."""
        from opensquilla.channels.delivery_store import durable_enqueue

        durable_enqueue(self, message, self._queue)

    async def receive(self) -> IncomingMessage:
        """Block until an inbound message is available."""
        msg = await self._queue.get()
        self._last_message_at = datetime.now(UTC)
        log.debug("slack.receive", channel=self.slack_channel_id, content=msg.content[:80])
        return msg

    def parse_event(
        self,
        event: dict[str, Any],
        *,
        verification: IngressVerification = IngressVerification.LEGACY_UNVERIFIED,
    ) -> IncomingMessage:
        """Convert a Slack event payload dict into IncomingMessage."""
        ts = event.get("ts")
        thread_ts = event.get("thread_ts")

        # channel_type: "channel"/"group" = group, "im"/"mpim" = DM
        # Fallback: infer from channel ID prefix (C/G = group, D = direct)
        channel_type = event.get("channel_type")
        if channel_type is None:
            ch = event.get("channel", "")
            if ch.startswith(("C", "G")):
                channel_type = "channel"
            elif ch.startswith("D"):
                channel_type = "im"

        metadata: dict[str, Any] = {
            "ts": ts,
            "thread_ts": thread_ts,
            "team": event.get("team"),
            "channel_type": channel_type,
            "is_group": channel_type in {"channel", "group"},
        }

        # Detect thread root: thread_ts == ts
        if thread_ts is not None and ts is not None:
            metadata["is_thread_root"] = thread_ts == ts

        # Track last thread_ts for reply_in_thread auto-threading
        if thread_ts is not None:
            self._last_thread_ts = thread_ts

        sender_id = str(event.get("user", self.sender_id))
        return IncomingMessage(
            sender_id=sender_id,
            channel_id=event.get("channel", self.slack_channel_id),
            content=event.get("text", ""),
            metadata=metadata,
            provenance=IngressProvenance(
                provider="slack",
                account_id=str(event.get("team") or ""),
                transport=self.transport_name,
                verification=verification,
                event_id=str(event.get("client_msg_id") or ts or "") or None,
                principal=(
                    AuthenticatedPrincipal(subject_id=sender_id)
                    if verification != IngressVerification.LEGACY_UNVERIFIED
                    else None
                ),
            ),
        )

    def build_reply_message(self, content: str, inbound: IncomingMessage) -> OutgoingMessage:
        """Target the inbound conversation so batch replies need no static channel id."""
        metadata: dict[str, Any] = {"channel": inbound.channel_id}
        if thread_ts := self._reply_thread_ts(inbound):
            metadata["thread_ts"] = thread_ts
        return OutgoingMessage(content=content, reply_to=inbound.channel_id, metadata=metadata)

    def streaming_reply_kwargs(self, inbound: IncomingMessage) -> dict[str, Any]:
        """Stream the reply into the inbound conversation."""
        kwargs = {"channel": inbound.channel_id}
        if thread_ts := self._reply_thread_ts(inbound):
            kwargs["thread_ts"] = thread_ts
        return kwargs

    def _reply_thread_ts(self, inbound: IncomingMessage) -> str | None:
        """Resolve the Slack thread target for a reply to ``inbound``."""
        if not self.reply_in_thread:
            return None
        metadata = inbound.metadata or {}
        raw = metadata.get("thread_ts") or metadata.get("ts")
        return str(raw) if raw else None

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def send(self, message: OutgoingMessage) -> None:
        """Post a message to the originating Slack conversation.

        The destination is resolved from ``reply_to``: the gateway routes the
        source conversation there, so the bot answers wherever it was
        addressed without a statically configured ``slack_channel_id``. Slack
        conversation ids are prefixed ``C``/``G``/``D``; a bare message
        timestamp is treated as a thread anchor instead. An explicit
        ``metadata['channel']`` / ``metadata['thread_ts']`` still wins.
        """
        meta = message.metadata or {}
        channel = self.slack_channel_id
        thread_ts: str | None = None
        rt = (message.reply_to or "").strip()
        if rt[:1] in ("C", "G", "D"):
            channel = rt
        elif rt:
            thread_ts = rt
        if meta.get("channel"):
            channel = str(meta["channel"])
        if "thread_ts" in meta:
            thread_ts = meta["thread_ts"]
        elif thread_ts is None and self.reply_in_thread and self._last_thread_ts:
            thread_ts = self._last_thread_ts
        if not channel:
            log.error("slack.send_failed", channel="", error="no_target_channel")
            raise RuntimeError("Slack send has no target channel")

        payload: dict[str, Any] = {"channel": channel, "text": message.content}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        for key, value in meta.items():
            if key in ("channel", "thread_ts") or value is None:
                continue
            payload[key] = value

        client = self._get_client()
        # Slack rejects text longer than 40000 chars; split so the full answer
        # is delivered across sequential messages instead of being dropped.
        chunks = split_text_for_channel(message.content, _SLACK_MAX_MESSAGE_CHARS)
        for chunk in chunks:
            payload["text"] = chunk
            resp = await client.post("/chat.postMessage", json=payload)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                log.error("slack.send_failed", channel=channel, error=data.get("error"))
                raise RuntimeError(f"Slack API error: {data.get('error')}")
        log.debug("slack.send", channel=channel, ts=data.get("ts"))

    async def send_file(
        self,
        channel_id: str,
        file_path: str,
        content: str = "",
    ) -> ChannelSendResult:
        """Upload a local file to Slack using the external upload flow."""
        path = Path(file_path)
        client = self._get_client()
        start_resp = await client.post(
            "/files.getUploadURLExternal",
            json={"filename": path.name, "length": path.stat().st_size},
        )
        start_resp.raise_for_status()
        start_data = start_resp.json()
        if not start_data.get("ok"):
            raise RuntimeError(f"Slack file upload init error: {start_data.get('error')}")
        upload_url = str(start_data.get("upload_url", ""))
        file_id = str(start_data.get("file_id", ""))
        if not upload_url or not file_id:
            raise RuntimeError("Slack file upload init response missing upload_url/file_id")

        with path.open("rb") as f:
            upload_resp = await client.post(upload_url, files={"file": (path.name, f)})
        upload_resp.raise_for_status()

        complete_payload: dict[str, Any] = {
            "files": [{"id": file_id, "title": path.name}],
            "channel_id": channel_id,
        }
        if content:
            complete_payload["initial_comment"] = content
        complete_resp = await client.post(
            "/files.completeUploadExternal",
            json=complete_payload,
        )
        complete_resp.raise_for_status()
        complete_data = complete_resp.json()
        if not complete_data.get("ok"):
            raise RuntimeError(f"Slack file upload complete error: {complete_data.get('error')}")
        return ChannelSendResult.sent(
            capability=ChannelCapabilities.NATIVE_FILE_UPLOAD,
            target_id=channel_id,
            provider_file_id=file_id,
        )

    async def edit(
        self,
        message_id: str,
        content: str,
        *,
        channel: str | None = None,
    ) -> None:
        """Update an existing Slack message via chat.update.

        ``channel`` pins dynamic replies to the conversation that created the
        message. Direct callers that only know a message id retain the legacy
        configured-channel fallback.
        """
        target = channel or self.slack_channel_id
        if not target:
            raise RuntimeError("Slack edit has no target channel")
        payload: dict[str, Any] = {
            "channel": target,
            "ts": message_id,
            "text": content,
        }
        client = self._get_client()
        resp = await client.post("/chat.update", json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            log.error("slack.edit_failed", error=data.get("error"), message_id=message_id)
            raise RuntimeError(f"Slack API error: {data.get('error')}")
        log.debug("slack.edit", message_id=message_id)

    async def delete(
        self,
        message_id: str,
        *,
        channel: str | None = None,
    ) -> None:
        """Delete a Slack message via chat.delete."""
        target = channel or self.slack_channel_id
        if not target:
            raise RuntimeError("Slack delete has no target channel")
        payload: dict[str, Any] = {
            "channel": target,
            "ts": message_id,
        }
        client = self._get_client()
        resp = await client.post("/chat.delete", json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            log.error("slack.delete_failed", error=data.get("error"), message_id=message_id)
            raise RuntimeError(f"Slack API error: {data.get('error')}")
        log.debug("slack.delete", message_id=message_id)

    # ------------------------------------------------------------------
    # Streaming (T008)
    # ------------------------------------------------------------------

    async def send_streaming(
        self,
        chunks: AsyncIterator[str],
        *,
        channel: str | None = None,
        thread_ts: str | None = None,
        update_interval_ms: int = 500,
    ) -> str | None:
        """Send a streaming message: post first chunk, update with subsequent chunks.

        Returns the message ``ts`` or ``None`` if the iterator was empty.
        ``channel`` targets the originating conversation (defaulting to
        ``slack_channel_id``) so a streamed reply lands where the bot was
        addressed without a statically configured channel.

        Uses ``StreamThrottle`` so a fast producer cannot fire two
        concurrent ``chat.update`` calls and a single network failure
        does not lose accumulated text.
        """
        client = self._get_client()
        target = channel or self.slack_channel_id
        if not target:
            log.error("slack.stream_failed", channel="", error="no_target_channel")
            raise RuntimeError("Slack stream has no target channel")
        throttle = StreamThrottle(interval_s=update_interval_ms / 1000.0)
        message_ts: str | None = None

        async def _post(text: str) -> None:
            nonlocal message_ts
            payload: dict[str, Any] = {
                "channel": target,
                "text": text,
            }
            if thread_ts:
                payload["thread_ts"] = thread_ts
            resp = await client.post("/chat.postMessage", json=payload)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"Slack API error: {data.get('error')}")
            message_ts = data["ts"]
            log.debug("slack.stream_start", ts=message_ts)

        async def _edit(text: str) -> None:
            resp = await client.post(
                "/chat.update",
                json={
                    "channel": target,
                    "ts": message_ts,
                    "text": text,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            # Slack signals errors (e.g. msg_too_long) as HTTP 200 + ok:false;
            # check the envelope like _post so a rejected update surfaces and
            # the stream relay can fall back instead of silently truncating.
            if not data.get("ok"):
                raise RuntimeError(f"Slack API error: {data.get('error')}")

        async for chunk in chunks:
            throttle.add(chunk)
            await throttle.maybe_flush(post=_post, edit=_edit)

        await throttle.force_flush(post=_post, edit=_edit)
        if message_ts is not None:
            log.debug("slack.stream_end", ts=message_ts, length=len(throttle.text))

        return message_ts

    # ------------------------------------------------------------------
    # Mentions (T004)
    # ------------------------------------------------------------------

    @staticmethod
    def extract_mentions(text: str) -> list[str]:
        """Extract user IDs from Slack mention markup like ``<@U123>`` or ``<@U123|name>``."""
        return _MENTION_RE.findall(text)

    @staticmethod
    def format_mention(user_id: str) -> str:
        """Format a user ID as a Slack mention string."""
        return f"<@{user_id}>"

    def is_mentioned(self, text: str) -> bool:
        """Check whether this bot is mentioned in the given text."""
        if self.bot_user_id is None:
            return False
        return self.bot_user_id in self.extract_mentions(text)

    def is_group_mentioned(self, msg: IncomingMessage) -> bool:
        """Uniform mention check for group gating. Delegates to is_mentioned."""
        return self.is_mentioned(msg.content)

    # Note: Slack Web API has no typing endpoint for webhook-based bots.
    # send_typing() is intentionally not implemented here.

    # ------------------------------------------------------------------
    # Lifecycle (T006)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Validate the bot token, store ``bot_user_id``, and - in Socket Mode -
        open the Slack Socket Mode long-connection."""
        if self.connection_mode == "webhook" and not (self.signing_secret or "").strip():
            raise SlackAuthError(
                "connection_mode='webhook' requires signing_secret for request verification"
            )
        client = self._get_client()
        resp = await client.post("/auth.test")
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise SlackAuthError(data.get("error", "unknown auth error"))
        self.bot_user_id = data["user_id"]
        if self.connection_mode == "socket":
            if not self.app_token:
                raise SlackAuthError(
                    "connection_mode='socket' requires app_token (an xapp- App-Level Token)"
                )
            initial_socket_url = await self._open_socket_connection()
            self._socket_stop = asyncio.Event()
            self._socket_task = asyncio.create_task(
                self._run_socket_loop(initial_socket_url), name="slack-socket-mode"
            )
        self._connected = True
        log.info("slack.started", bot_user_id=self.bot_user_id, mode=self.connection_mode)

    async def probe_connection(self) -> dict[str, Any]:
        """Validate bot credentials without opening or mutating a transport."""
        response = await self._get_client().post("/auth.test")
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise SlackAuthError(data.get("error", "unknown auth error"))
        return {
            "authenticated": True,
            "bot_user_id": str(data.get("user_id") or ""),
            "team_id": str(data.get("team_id") or ""),
        }

    async def stop(self) -> None:
        """Gracefully shut down the channel adapter."""
        if self._socket_stop is not None:
            self._socket_stop.set()
        if self._socket_task is not None:
            self._socket_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._socket_task
            self._socket_task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._connected = False
        log.info("slack.stopped")

    # ------------------------------------------------------------------
    # Socket Mode transport (no public Request URL required)
    # ------------------------------------------------------------------

    async def _open_socket_connection(self) -> str:
        """Open a Socket Mode session and return the issued ``wss://`` url."""
        client = self._get_client()
        resp = await client.post(
            "/apps.connections.open",
            headers={"Authorization": f"Bearer {self.app_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise SlackAuthError(f"apps.connections.open failed: {data.get('error')}")
        return str(data["url"])

    async def _run_socket_loop(self, initial_socket_url: str | None = None) -> None:
        """Maintain the Socket Mode websocket, reconnecting with backoff."""
        import websockets

        backoff = 1.0
        stop = self._socket_stop
        next_socket_url = initial_socket_url
        while stop is None or not stop.is_set():
            try:
                ws_url = next_socket_url or await self._open_socket_connection()
                next_socket_url = None
                async with websockets.connect(
                    ws_url, ping_interval=30, ping_timeout=20, max_size=None
                ) as ws:
                    self._connected = True
                    backoff = 1.0
                    log.info("slack.socket_mode.connected")
                    async for raw in ws:
                        if stop is not None and stop.is_set():
                            break
                        await self._handle_socket_frame(ws, raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                next_socket_url = None
                self._connected = False
                log.warning("slack.socket_mode.reconnect", error=str(exc), backoff=backoff)
                if stop is None:
                    break
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                if stop.is_set():
                    break
                backoff = min(backoff * 2, 30.0)

    async def _handle_socket_frame(self, ws: Any, raw: str | bytes) -> None:
        """Ack and dispatch a single Socket Mode frame."""
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            return
        # Slack requires an ack (echo the envelope id) within 3 seconds.
        envelope_id = msg.get("envelope_id")
        if envelope_id:
            with contextlib.suppress(Exception):
                await ws.send(json.dumps({"envelope_id": envelope_id}))
        mtype = msg.get("type")
        if mtype == "disconnect":
            # Slack rotates connections periodically; close so the loop reconnects.
            with contextlib.suppress(Exception):
                await ws.close()
            return
        if mtype != "events_api":
            return
        payload = msg.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "event_callback":
            self._ingest_event_callback(
                payload,
                verification=IngressVerification.SDK_SESSION,
            )

    def is_connected(self) -> bool:
        """Return whether the adapter has been started and is connected."""
        return self._connected

    # ------------------------------------------------------------------
    # Gateway Webhook (T012)
    # ------------------------------------------------------------------

    def create_webhook_route(self, path: str = "/slack/events") -> Route:
        """Return a Starlette Route for handling Slack Events API webhooks."""
        return Route(path, endpoint=self._handle_webhook, methods=["POST"])

    async def _handle_webhook(self, request: Request) -> Response:
        """Handle an incoming Slack Events API request."""
        body = await request.body()

        # Never accept an unsigned webhook, even if an adapter is constructed
        # outside the normal gateway/onboarding validation path.
        if not (self.signing_secret or "").strip():
            log.error("slack.webhook_missing_signing_secret")
            return Response(status_code=503)

        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        signature = request.headers.get("X-Slack-Signature", "")

        # Reject requests older than 5 minutes (replay protection)
        try:
            if abs(time.time() - float(timestamp)) > 300:
                return Response(status_code=403)
        except (ValueError, TypeError):
            return Response(status_code=403)

        if not self._verify_signature(body, timestamp, signature):
            return Response(status_code=401)

        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return Response(status_code=400)

        event_type = data.get("type")

        if event_type == "url_verification":
            challenge = data.get("challenge", "")
            return JSONResponse({"challenge": challenge})

        if event_type == "event_callback":
            self._ingest_event_callback(
                data,
                verification=IngressVerification.WEBHOOK_SIGNATURE,
            )

        return Response(status_code=200)

    def _ingest_event_callback(
        self,
        data: dict[str, Any],
        *,
        verification: IngressVerification = IngressVerification.LEGACY_UNVERIFIED,
    ) -> None:
        """Shared inbound path for an Events API ``event_callback`` payload,
        used by both the webhook handler and the Socket Mode loop."""
        event = data.get("event", {})
        if not isinstance(event, dict) or event.get("type") not in {"message", "app_mention"}:
            return
        # Only plain user messages are input; drop edits/deletes/joins and the
        # bot's own streaming-edit echoes (``message_changed`` etc.).
        if event.get("subtype") not in (None, "file_share", "me_message", "thread_broadcast"):
            return
        if self._is_own_message(event):
            return
        event_instance_key = (
            f"{event.get('channel')}:{event.get('ts')}"
            if event.get("channel") and event.get("ts")
            else ""
        )
        dedupe_key = str(
            event.get("client_msg_id") or event_instance_key or data.get("event_id") or ""
        ).strip(":")
        if dedupe_key and not self._dedupe.check_and_add(dedupe_key):
            return
        self.enqueue(self.parse_event(event, verification=verification))

    def _is_own_message(self, event: dict[str, Any]) -> bool:
        """Drop the bot's own messages so replies never loop back as input."""
        if event.get("subtype") == "bot_message" or event.get("bot_id"):
            return True
        return bool(self.bot_user_id and event.get("user") == self.bot_user_id)

    def _verify_signature(self, body: bytes, timestamp: str, signature: str) -> bool:
        """Verify Slack request signature using HMAC-SHA256."""
        if self.signing_secret is None:
            return False
        sig_basestring = f"v0:{timestamp}:{body.decode()}"
        expected = (
            "v0="
            + hmac.HMAC(
                self.signing_secret.encode(),
                sig_basestring.encode(),
                hashlib.sha256,
            ).hexdigest()
        )
        return hmac.compare_digest(expected, signature)

    # ------------------------------------------------------------------
    # Health Check (T014)
    # ------------------------------------------------------------------

    async def health_check(self) -> ChannelHealth:
        """Return current health status of the Slack adapter."""
        socket_alive = self.connection_mode != "socket" or (
            self._socket_task is not None and not self._socket_task.done()
        )
        return ChannelHealth(
            connected=self._connected and socket_alive,
            bot_user_id=self.bot_user_id,
            last_message_at=self._last_message_at,
        )
