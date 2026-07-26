"""DingTalk (钉钉) channel adapter.

Uses the ``dingtalk-stream`` SDK Stream Mode (WebSocket) for inbound, and
the SDK's own card-instance + chat-reply primitives (which delegate to the
DingTalk OpenAPI) for outbound. There is no HTTP webhook; the SDK keeps a
persistent WS to DingTalk.

Streaming edits use :class:`dingtalk_stream.MarkdownCardInstance` —
``async_create_and_send_card`` for the first emission, then
``async_put_card_data`` for subsequent updates throttled to ~2 s.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import platform
import socket
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import requests  # type: ignore[import-untyped]
import structlog
from pydantic import BaseModel, Field

from opensquilla.channels._util import EventDedupeCache
from opensquilla.channels.contract import (
    ChannelCapabilityProfile,
    ChannelLengthUnit,
    ChannelPlatformCapability,
    ChannelPlatformCapabilityStatus,
    ChannelPlatformCategories,
    ChannelPlatformManifest,
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

if TYPE_CHECKING:  # pragma: no cover — typing-only
    from dingtalk_stream import ChatbotMessage as _ChatbotMessage  # type: ignore[import-untyped]

log = structlog.get_logger(__name__)


# Channel-contract constants pinned by the adapter audit.
CAPABILITY_TIER = "YELLOW-experimental"

# DingTalk is a DM/group channel — the permission matrix denies admin-only.
DM_SAFETY_TIERS: tuple[str, ...] = ("safe", "confirm")

# Bounded number of recent inbound messages kept so a reply can be routed back
# to the exact message that triggered the turn, immune to concurrent inbound
# frames overwriting a single shared slot.
_REPLY_CONTEXT_MAX = 256

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

_DINGTALK_AUTH_INVALID_MESSAGE = "凭证无效：检查 DingTalk AppKey/AppSecret"
_STREAM_STOP_TIMEOUT_S = 5.0
_STREAM_CANCEL_GRACE_S = 0.25


class DingTalkAuthError(RuntimeError):
    """Raised when DingTalk rejects the configured Stream Mode credentials."""

    def __init__(self, *, provider_code: str = "authFailed") -> None:
        self.diagnostic = {
            "error_class": "auth_invalid",
            "provider_code": provider_code or "authFailed",
            "message": _DINGTALK_AUTH_INVALID_MESSAGE,
            "retryable": False,
        }
        super().__init__("DingTalk credentials were rejected")


class DingTalkChannelConfig(BaseModel):
    """Pydantic config for the DingTalk channel adapter.

    ``client_id`` / ``client_secret`` are the AppKey / AppSecret pair from
    the DingTalk Open Platform robot configuration. They are optional here
    so the existing ``ChannelManager.from_config`` branch (which currently
    forwards only ``name``) keeps working; ``start()`` enforces presence.
    """

    name: str = "dingtalk"
    client_id: str = ""
    client_secret: str = ""
    event_dedupe_size: int = 4096
    streaming_update_interval_s: float = 2.0
    reconnect_initial_delay_s: float = Field(default=1.0, ge=0.0)
    reconnect_max_delay_s: float = Field(default=30.0, ge=0.0)

    model_config = {}  # explicit params only; no env loading


@dataclass
class DingTalkChannel:
    """Channel adapter for DingTalk via Stream Mode (WebSocket).

    Inbound flow: ``DingTalkStreamClient`` runs the WS loop on a background
    asyncio task, dispatches each ``ChatbotMessage`` to a callback handler,
    which parses it and pushes an :class:`IncomingMessage` onto an internal
    queue. ``receive()`` awaits that queue.

    Outbound flow: ``send`` posts plain text via ``ChatbotHandler.reply_text``
    (wrapped in :func:`asyncio.to_thread` because the SDK helper is sync);
    ``send_streaming`` uses :class:`MarkdownCardInstance` to push a card and
    update it as new chunks arrive.

    ``edit`` / ``delete`` raise :class:`UnsupportedChannelOperation` because
    DingTalk does not expose a public "edit message" or "delete message" API
    for robots; edits on the streaming path go through the card-update helper
    instead.
    """

    config: DingTalkChannelConfig

    _queue: asyncio.Queue[IncomingMessage] = field(
        default_factory=asyncio.Queue, init=False, repr=False
    )
    _client: Any = field(default=None, init=False, repr=False)
    _handler: Any = field(default=None, init=False, repr=False)
    _run_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _stop_event: asyncio.Event | None = field(default=None, init=False, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _last_incoming: Any = field(default=None, init=False, repr=False)
    _msg_by_id: OrderedDict[str, Any] = field(
        default_factory=OrderedDict, init=False, repr=False
    )
    _msg_by_conversation: OrderedDict[str, Any] = field(
        default_factory=OrderedDict, init=False, repr=False
    )
    _last_card_instance: Any = field(default=None, init=False, repr=False)
    _last_message_at: datetime | None = field(default=None, init=False, repr=False)
    _msg_count: int = field(default=0, init=False, repr=False)
    _dedupe: EventDedupeCache = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._dedupe = EventDedupeCache(max_size=self.config.event_dedupe_size)

    @property
    def capability_profile(self) -> ChannelCapabilityProfile:
        return ChannelCapabilityProfile(
            channel_type="dingtalk",
            max_message_len=16000,
            length_unit=ChannelLengthUnit.UTF8_BYTES,
            splits_natively=False,
            group_chat=True,
            mentions=True,
            reply=True,
            cards=True,
            transports=("websocket",),
            notes=(
                "DingTalk stream mode supports card updates for streaming, but robot "
                "text replies have no generic edit/delete primitive.",
            ),
        )

    @property
    def platform_capability_manifest(self) -> ChannelPlatformManifest:
        return ChannelPlatformManifest.from_channel_profile(
            self.capability_profile,
        ).with_capabilities(
            ChannelPlatformCapability(
                category=ChannelPlatformCategories.FILES,
                status=ChannelPlatformCapabilityStatus.UNSUPPORTED,
                notes=("DingTalk file upload/download is not implemented in this adapter.",),
            ),
            ChannelPlatformCapability(
                category=ChannelPlatformCategories.CARDS,
                status=ChannelPlatformCapabilityStatus.SUPPORTED,
                tools=("MarkdownCardInstance",),
                mutates=True,
                notes=("DingTalk streaming uses MarkdownCardInstance create/update helpers.",),
            ),
        )

    @property
    def capabilities(self) -> frozenset[str]:
        return self.capability_profile.capability_tags()

    # ------------------------------------------------------------------
    # Inbound — parsing & dispatch
    # ------------------------------------------------------------------

    @staticmethod
    def _bot_mentioned(msg: _ChatbotMessage) -> bool:
        for attr in ("is_in_at_list", "isInAtList"):
            value = getattr(msg, attr, None)
            if value is not None:
                return bool(value)
        raw_data = getattr(msg, "raw_data", None) or getattr(msg, "source", None)
        if isinstance(raw_data, dict):
            return bool(raw_data.get("isInAtList") or raw_data.get("is_in_at_list"))
        return False

    def parse_message(self, msg: _ChatbotMessage) -> IncomingMessage | None:
        """Convert an SDK ``ChatbotMessage`` into our envelope.

        Returns ``None`` if the message is a duplicate. Non-text bodies
        emit the SDK's ``message_type`` placeholder so the runtime never
        sees an empty content string.
        """
        msg_id = getattr(msg, "message_id", None) or ""
        if msg_id and not self._dedupe.check_and_add(msg_id):
            log.debug("dingtalk.duplicate_dropped", msg_id=msg_id)
            return None

        message_type = getattr(msg, "message_type", "") or ""
        text_obj = getattr(msg, "text", None)
        if message_type == "text" and text_obj is not None:
            content = (getattr(text_obj, "content", "") or "").strip()
        else:
            content = f"[{message_type}]" if message_type else ""

        sender_id = (
            getattr(msg, "sender_staff_id", None) or getattr(msg, "sender_id", None) or "unknown"
        )
        conversation_id = getattr(msg, "conversation_id", None) or "unknown"
        conversation_type = getattr(msg, "conversation_type", "") or ""
        # DingTalk encodes conversation type as a string: "1" = single, "2" = group.
        is_group = conversation_type == "2"

        metadata: dict[str, Any] = {
            "msg_id": msg_id,
            "sender_staff_id": getattr(msg, "sender_staff_id", None),
            "sender_nick": getattr(msg, "sender_nick", None),
            "conversation_type": conversation_type,
            "conversation_id": conversation_id,
            "message_type": message_type,
            "is_group": is_group,
            "bot_mentioned": self._bot_mentioned(msg),
        }

        # Bind the reply target to THIS message rather than resolving it from
        # the channel-global _last_incoming at send time. That slot is
        # overwritten by every inbound frame (on the SDK worker thread), so a
        # concurrent conversation would otherwise steal the reply target and
        # leak one user's answer into another user's chat. Keep a bounded
        # msg_id -> ChatbotMessage map and reference it by id in metadata.
        if msg_id:
            self._msg_by_id[msg_id] = msg
            while len(self._msg_by_id) > _REPLY_CONTEXT_MAX:
                self._msg_by_id.popitem(last=False)
        conv_key = str(conversation_id)
        if conv_key and conv_key != "unknown":
            self._msg_by_conversation[conv_key] = msg
            self._msg_by_conversation.move_to_end(conv_key)
            while len(self._msg_by_conversation) > _REPLY_CONTEXT_MAX:
                self._msg_by_conversation.popitem(last=False)

        return IncomingMessage(
            sender_id=str(sender_id),
            channel_id=str(conversation_id),
            content=content,
            metadata=metadata,
            provenance=IngressProvenance(
                provider="dingtalk",
                account_id=self.config.name,
                transport="websocket",
                verification=IngressVerification.SDK_SESSION,
                event_id=str(msg_id or "") or None,
                principal=AuthenticatedPrincipal(subject_id=str(sender_id)),
            ),
        )

    def enqueue(self, message: IncomingMessage) -> None:
        from opensquilla.channels.delivery_store import durable_enqueue

        durable_enqueue(self, message, self._queue)
        self._last_message_at = datetime.now(UTC)
        self._msg_count += 1

    async def receive(self) -> IncomingMessage:
        msg = await self._queue.get()
        log.debug("dingtalk.inbound_received", content=msg.content[:80])
        return msg

    def is_group_mentioned(self, msg: IncomingMessage) -> bool:
        if not bool(msg.metadata.get("is_group")):
            return True
        return bool(msg.metadata.get("bot_mentioned"))

    # ------------------------------------------------------------------
    # Startup diagnostics
    # ------------------------------------------------------------------

    @staticmethod
    def _sdk_version() -> str:
        try:
            from dingtalk_stream.version import VERSION_STRING  # type: ignore[import-untyped]

            return str(VERSION_STRING)
        except Exception:
            return "unknown"

    @staticmethod
    def _host_ip() -> str:
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
        except OSError:
            return ""
        finally:
            if sock is not None:
                sock.close()

    @staticmethod
    def _response_json(response: Any) -> dict[str, Any]:
        try:
            payload = response.json()
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _safe_error_text(self, exc: BaseException) -> str:
        text = str(exc)
        if self.config.client_secret:
            text = text.replace(self.config.client_secret, "[redacted]")
        return text

    @staticmethod
    def _provider_code(payload: dict[str, Any], *, status_code: int | None) -> str:
        code = str(payload.get("code") or payload.get("errcode") or "")
        if code:
            return code
        return "authFailed" if status_code == 401 else ""

    @classmethod
    def _is_auth_failure(
        cls,
        *,
        status_code: int | None,
        payload: dict[str, Any],
        response_text: str,
    ) -> bool:
        code = cls._provider_code(payload, status_code=status_code).lower()
        message = str(payload.get("message") or payload.get("errmsg") or "")
        haystack = f"{response_text}\n{message}".lower()
        return (
            status_code == 401
            or code == "authfailed"
            or "authfailed" in haystack
            or "鉴权失败" in response_text
            or "鉴权失败" in message
        )

    async def _preflight_open_connection(
        self,
        *,
        endpoint: str,
        callback_topic: str,
        strict_transport: bool = False,
    ) -> bool:
        """Validate Stream credentials through DingTalk's connection-open API.

        Startup keeps transient network failures best-effort because the stream
        supervisor retries the connection lifecycle. Explicit live certification uses
        ``strict_transport=True`` so a timeout can never be reported as a
        successful credential check.
        """
        version = self._sdk_version()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": (
                f"DingTalkStream/1.0 SDK/{version} Python/{platform.python_version()} "
                "(+https://github.com/open-dingtalk/dingtalk-stream-sdk-python)"
            ),
        }
        body = json.dumps(
            {
                "clientId": self.config.client_id,
                "clientSecret": self.config.client_secret,
                "subscriptions": [{"type": "CALLBACK", "topic": callback_topic}],
                "ua": f"dingtalk-sdk-python/v{version}-union",
                "localIp": self._host_ip(),
            }
        ).encode("utf-8")

        def _post() -> Any:
            return requests.post(endpoint, headers=headers, data=body, timeout=10.0)

        try:
            response = await asyncio.to_thread(_post)
        except requests.RequestException as exc:
            log.warning(
                "dingtalk.preflight_transient_failed",
                name=self.config.name,
                error_type=type(exc).__name__,
                error=self._safe_error_text(exc),
            )
            if strict_transport:
                raise RuntimeError(
                    "DingTalk credential probe could not reach the Stream gateway"
                ) from None
            return False

        status_code_raw = getattr(response, "status_code", None)
        try:
            status_code = int(status_code_raw) if status_code_raw is not None else None
        except (TypeError, ValueError):
            status_code = None
        payload = self._response_json(response)
        response_text = str(getattr(response, "text", "") or "")
        if self._is_auth_failure(
            status_code=status_code,
            payload=payload,
            response_text=response_text,
        ):
            provider_code = self._provider_code(payload, status_code=status_code)
            raise DingTalkAuthError(provider_code=provider_code)

        raise_for_status = getattr(response, "raise_for_status", None)
        if callable(raise_for_status):
            try:
                raise_for_status()
            except requests.RequestException as exc:
                log.warning(
                    "dingtalk.preflight_transient_failed",
                    name=self.config.name,
                    status_code=status_code,
                    error_type=type(exc).__name__,
                    error=self._safe_error_text(exc),
                )
                if strict_transport:
                    raise RuntimeError(
                        "DingTalk credential probe received an unsuccessful response"
                    ) from None
                return False
        if strict_transport and not (
            isinstance(payload.get("endpoint"), str)
            and payload.get("endpoint")
            and isinstance(payload.get("ticket"), str)
            and payload.get("ticket")
        ):
            raise RuntimeError(
                "DingTalk credential probe response omitted connection metadata"
            )
        return True

    async def probe_connection(self) -> dict[str, Any]:
        """Validate Stream credentials without starting the WebSocket loop."""
        if not self.config.client_id or not self.config.client_secret:
            raise ValueError(
                "dingtalk.probe_connection: client_id and client_secret are required"
            )
        from dingtalk_stream import (  # type: ignore[import-untyped]
            ChatbotMessage,
            DingTalkStreamClient,
        )

        endpoint = getattr(
            DingTalkStreamClient,
            "OPEN_CONNECTION_API",
            "https://api.dingtalk.com/v1.0/gateway/connections/open",
        )
        await self._preflight_open_connection(
            endpoint=str(endpoint),
            callback_topic=str(ChatbotMessage.TOPIC),
            strict_transport=True,
        )
        return {
            "authenticated": True,
            "supported": True,
            "transport": "stream",
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Validate creds, build the SDK client, and launch the stream supervisor."""
        if not self.config.client_id or not self.config.client_secret:
            raise ValueError("dingtalk.start: client_id and client_secret are required")

        # Lazy import — keeps the adapter importable without the [dingtalk] extra
        # for unit tests that mock the SDK out of the picture.
        from dingtalk_stream import (  # type: ignore[import-untyped]
            ChatbotMessage,
            Credential,
            DingTalkStreamClient,
        )

        endpoint = getattr(
            DingTalkStreamClient,
            "OPEN_CONNECTION_API",
            "https://api.dingtalk.com/v1.0/gateway/connections/open",
        )
        await self._preflight_open_connection(
            endpoint=str(endpoint),
            callback_topic=str(ChatbotMessage.TOPIC),
        )

        credential = Credential(self.config.client_id, self.config.client_secret)
        self._client = DingTalkStreamClient(credential)
        self._handler = _DingTalkCallbackHandler(channel=self)
        self._client.register_callback_handler(ChatbotMessage.TOPIC, self._handler)

        # Capture the running loop so the worker-thread ``_Handler.process``
        # can hand parsed messages back via ``call_soon_threadsafe`` (the
        # SDK's ``AsyncChatbotHandler.raw_process`` runs ``process`` on a
        # ThreadPoolExecutor — see SDK source ``chatbot.py:829-836``).
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._run_task = asyncio.create_task(
            self._run_stream_supervisor(),
            name="dingtalk:stream-supervisor",
        )
        log.info(
            "dingtalk.started",
            name=self.config.name,
            client_id=self.config.client_id,
        )

    async def stop(self) -> None:
        """Signal the stream supervisor and await bounded shutdown."""
        task = self._run_task
        self._run_task = None
        stop_event = self._stop_event
        self._stop_event = None
        if stop_event is not None:
            stop_event.set()
        if task is not None and not task.done():
            done, _ = await asyncio.wait((task,), timeout=_STREAM_STOP_TIMEOUT_S)
            if not done:
                task.cancel()
                done, _ = await asyncio.wait(
                    (task,),
                    timeout=_STREAM_CANCEL_GRACE_S,
                )
                log.warning(
                    "dingtalk.stop_timeout",
                    name=self.config.name,
                    note="stream supervisor did not stop within 5 s",
                )
            if done:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            else:
                task.add_done_callback(self._consume_task_result)
        self._client = None
        self._handler = None
        self._loop = None
        log.info("dingtalk.stopped", name=self.config.name)

    async def _run_stream_supervisor(self) -> None:
        """Restart a completed stream loop with interruptible capped backoff."""
        stop_event = self._stop_event
        client = self._client
        if stop_event is None or client is None:
            return

        maximum = self.config.reconnect_max_delay_s
        delay = min(self.config.reconnect_initial_delay_s, maximum)
        while not stop_event.is_set():
            stream_task = asyncio.create_task(
                client.start(),
                name="dingtalk:stream-attempt",
            )
            stop_task = asyncio.create_task(
                stop_event.wait(),
                name="dingtalk:stream-stop",
            )
            try:
                done, _ = await asyncio.wait(
                    (stream_task, stop_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_task in done:
                    await self._cancel_stream_task(stream_task)
                    return
                await stream_task
                log.warning(
                    "dingtalk.stream_ended",
                    name=self.config.name,
                    reconnect_delay_s=delay,
                )
            except asyncio.CancelledError:
                await self._cancel_stream_task(stream_task)
                raise
            except Exception as exc:
                log.warning(
                    "dingtalk.stream_failed",
                    name=self.config.name,
                    error_type=type(exc).__name__,
                    error=self._safe_error_text(exc),
                    reconnect_delay_s=delay,
                )
            finally:
                if not stop_task.done():
                    stop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await stop_task

            if await self._wait_for_reconnect(delay):
                return
            delay = min(delay * 2 if delay else 0.0, maximum)

    async def _wait_for_reconnect(self, delay: float) -> bool:
        """Return true when shutdown interrupts the reconnect delay."""
        stop_event = self._stop_event
        if stop_event is None or stop_event.is_set():
            return True
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except TimeoutError:
            return False
        return True

    @staticmethod
    async def _cancel_stream_task(task: asyncio.Task[Any]) -> None:
        """Cancel one SDK stream attempt without allowing an unbounded drain."""
        if task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            return
        task.cancel()
        await asyncio.sleep(0)
        if not task.done():
            task.cancel()
        done, _ = await asyncio.wait((task,), timeout=_STREAM_CANCEL_GRACE_S)
        if done:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        else:
            task.add_done_callback(DingTalkChannel._consume_task_result)

    @staticmethod
    def _consume_task_result(task: asyncio.Task[Any]) -> None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()

    async def health_check(self) -> ChannelHealth:
        running = self._run_task is not None and not self._run_task.done()
        return ChannelHealth(
            connected=running,
            last_message_at=self._last_message_at,
            extra={
                "transport": "stream",
                "msg_count": self._msg_count,
            },
        )

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def send(self, message: OutgoingMessage) -> None:
        """Send a plain-text reply through the SDK's chatbot helper.

        ``ChatbotHandler.reply_text`` is sync (uses ``requests``); we run it
        in a worker thread so the event loop stays free.
        """
        if self._handler is None:
            raise RuntimeError("dingtalk.send: adapter is not started")
        target = self._resolve_reply_target(message)
        if target is None:
            metadata = message.metadata or {}
            if (
                metadata.get("dingtalk_reply_msg_id")
                or metadata.get("msg_id")
                or message.reply_to
                or metadata.get("conversation_id")
            ):
                raise RuntimeError("dingtalk.send: reply context is missing or expired")
            raise RuntimeError(
                "dingtalk.send: no inbound context yet — robot replies "
                "require the original ChatbotMessage to resolve sessionWebhook"
            )
        await asyncio.to_thread(self._handler.reply_text, message.content, target)
        log.debug("dingtalk.outbound_sent", length=len(message.content))

    def _resolve_reply_target(self, message: OutgoingMessage) -> Any:
        """Resolve the ChatbotMessage a reply must be delivered to.

        Prefers the per-message reply context threaded through the envelope
        (immune to concurrent inbound frames): first by ``msg_id``, then by the
        target conversation id. Falls back to ``_last_incoming`` only for
        direct/tool-initiated sends that carry no inbound context.
        """
        metadata = message.metadata or {}
        msg_id = metadata.get("dingtalk_reply_msg_id") or metadata.get("msg_id")
        if msg_id:
            return self._valid_reply_target(self._msg_by_id.get(msg_id))
        conv = message.reply_to or metadata.get("conversation_id")
        if conv:
            return self._valid_reply_target(self._msg_by_conversation.get(conv))
        return self._valid_reply_target(self._last_incoming)

    @staticmethod
    def _valid_reply_target(target: Any) -> Any | None:
        """Reject an SDK reply context after its sessionWebhook expires."""
        if target is None:
            return None
        expiry = None
        for attr in (
            "session_webhook_expired_time",
            "sessionWebhookExpiredTime",
        ):
            value = getattr(target, attr, None)
            if value is not None:
                expiry = value
                break
        if expiry is None:
            raw = getattr(target, "raw_data", None) or getattr(target, "source", None)
            if isinstance(raw, dict):
                expiry = raw.get("sessionWebhookExpiredTime") or raw.get(
                    "session_webhook_expired_time"
                )
        if expiry is None:
            return target
        try:
            expires_at = float(expiry)
        except (TypeError, ValueError):
            return target
        if expires_at > 10_000_000_000:
            expires_at /= 1000.0
        return target if time.time() < expires_at else None

    def build_reply_message(self, content: str, inbound: IncomingMessage) -> OutgoingMessage:
        """Bind the reply to the triggering message's DingTalk session.

        Carries the inbound ``msg_id`` so :meth:`send` can resolve the exact
        ``ChatbotMessage`` (and therefore the correct ``sessionWebhook``)
        instead of the channel-global ``_last_incoming``.
        """
        metadata: dict[str, Any] = {}
        msg_id = inbound.metadata.get("msg_id")
        if msg_id:
            metadata["dingtalk_reply_msg_id"] = msg_id
        return OutgoingMessage(
            content=content, reply_to=inbound.channel_id, metadata=metadata
        )

    def streaming_reply_kwargs(self, inbound: IncomingMessage) -> dict[str, Any]:
        """Pin the streamed card to the triggering message."""
        msg_id = inbound.metadata.get("msg_id")
        return {"reply_msg_id": msg_id} if msg_id else {}

    async def edit(self, message_id: str, content: str) -> None:
        """Raise: DingTalk has no public edit-message API for robot text.

        Streaming edits are handled by ``send_streaming`` via the
        interactive-card update path. This method exists to satisfy the
        :class:`~opensquilla.channels.types.ManagedChannel` Protocol.
        """
        raise UnsupportedChannelOperation(
            channel="dingtalk",
            operation="edit",
            reason="robot text replies are not editable; streaming cards update separately",
        )

    async def delete(self, message_id: str) -> None:
        """Raise: DingTalk has no public delete-message API for robots."""
        raise UnsupportedChannelOperation(
            channel="dingtalk",
            operation="delete",
            reason="robot text replies are not deletable via the public API",
        )

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def send_streaming(
        self,
        chunks: AsyncIterator[str],
        *,
        update_interval_s: float | None = None,
        reply_msg_id: str | None = None,
    ) -> str | None:
        """Stream a markdown card: send first chunk, edit on a throttle.

        Uses :class:`dingtalk_stream.MarkdownCardInstance` directly so we
        can control the card-instance ID and update cadence. The very
        first chunk creates and sends the card; subsequent chunks edit
        the same card via ``async_put_card_data`` no more often than
        ``update_interval_s`` (default ~2 s, matching plan Section G).

        ``reply_msg_id`` binds the card to the triggering inbound message so a
        concurrent conversation cannot redirect the card via the shared
        ``_last_incoming`` slot; it falls back to ``_last_incoming`` only when
        no reply context is available (direct/tool-initiated sends).

        Returns the card-instance ID, or ``None`` if the iterator was empty.
        """
        if self._client is None:
            raise RuntimeError("dingtalk.send_streaming: adapter is not started")
        if reply_msg_id:
            last = self._msg_by_id.get(reply_msg_id)
            if last is None:
                raise RuntimeError(
                    "dingtalk.send_streaming: reply context is missing or expired"
                )
        else:
            last = self._last_incoming
        if last is None:
            raise RuntimeError(
                "dingtalk.send_streaming: no inbound context yet — card "
                "replies need the original ChatbotMessage"
            )

        interval = (
            update_interval_s
            if update_interval_s is not None
            else self.config.streaming_update_interval_s
        )

        accumulated = ""
        instance: Any | None = None
        instance_id: str | None = None
        card_instance_cls: Any | None = None
        last_edit_t: float = 0.0
        edit_count: int = 0

        async for chunk in chunks:
            accumulated += chunk
            now = time.monotonic()
            if instance is None:
                if card_instance_cls is None:
                    from dingtalk_stream import (  # type: ignore[import-untyped]
                        MarkdownCardInstance,
                    )

                    card_instance_cls = MarkdownCardInstance
                instance = card_instance_cls(self._client, last)
                instance_id = await instance.async_create_and_send_card(
                    instance.card_template_id,
                    {"markdown": accumulated},
                )
                self._last_card_instance = instance
                last_edit_t = now
                edit_count += 1
            else:
                if now - last_edit_t >= interval:
                    await instance.async_put_card_data(
                        instance_id,
                        {"markdown": accumulated},
                    )
                    last_edit_t = now
                    edit_count += 1

        # Final flush so the card always reflects the complete text.
        if instance is not None and instance_id is not None and accumulated:
            await instance.async_put_card_data(
                instance_id,
                {"markdown": accumulated},
            )
            edit_count += 1

        log.debug(
            "dingtalk.streaming_done",
            edits=edit_count,
            chars=len(accumulated),
        )
        return instance_id


# ---------------------------------------------------------------------------
# Internal SDK callback handler
# ---------------------------------------------------------------------------


def _build_callback_handler_class() -> type:
    """Build the SDK callback handler class lazily.

    The base ``AsyncChatbotHandler`` lives behind the ``[dingtalk]`` extra,
    so we resolve it on first use rather than at module import time.
    """
    from dingtalk_stream import (  # type: ignore[import-untyped]
        AckMessage,
        AsyncChatbotHandler,
        ChatbotMessage,
    )

    class _Handler(AsyncChatbotHandler):
        def __init__(self, channel: DingTalkChannel) -> None:
            super().__init__()
            self._channel = channel

        def process(self, callback_message: Any) -> Any:  # type: ignore[override]
            """SDK contract: ``AsyncChatbotHandler.raw_process`` submits this
            method to a ``ThreadPoolExecutor`` and never awaits it.
            The method MUST therefore be sync.
            We hop back to the channel's event loop via
            ``call_soon_threadsafe`` to deliver the parsed message into
            the asyncio queue safely from the worker thread.
            """
            try:
                msg = ChatbotMessage.from_dict(callback_message.data)
                self._channel._last_incoming = msg
                parsed = self._channel.parse_message(msg)
                if parsed is not None:
                    loop = self._channel._loop
                    if loop is not None:
                        loop.call_soon_threadsafe(self._channel.enqueue, parsed)
                    else:
                        # Same-thread fallback (covers tests that invoke the
                        # handler directly without going through ``start_forever``).
                        self._channel.enqueue(parsed)
            except Exception as exc:  # pragma: no cover — defensive
                log.error("dingtalk.dispatch_error", error=str(exc))
            return AckMessage.STATUS_OK, "OK"

    return _Handler


def _DingTalkCallbackHandler(*, channel: DingTalkChannel) -> Any:  # noqa: N802
    """Factory wrapper: return an instance of the lazy handler class."""
    cls = _build_callback_handler_class()
    return cls(channel=channel)
