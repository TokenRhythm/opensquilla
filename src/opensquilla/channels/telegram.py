"""Telegram channel adapter backed by the public Bot API over HTTP."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
import structlog
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from opensquilla.channels._attachment_io import (
    attachment_limit_for_mime,
    ensure_declared_size_within_limit,
    fetch_httpx_bytes_limited,
    preferred_attachment_mime,
)
from opensquilla.channels._util import (
    ChannelAccessPolicy,
    EventDedupeCache,
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

_DEFAULT_TIMEOUT_S = 30.0
_DEDUPE_SIZE = 4096
_ALLOWED_UPDATES = ("message", "channel_post")
# Telegram Bot API hard limit on sendMessage text length.
_TELEGRAM_MAX_MESSAGE_CHARS = 4096


class TelegramApiError(RuntimeError):
    """Raised when the Telegram Bot API returns ``ok: false``."""

    def __init__(
        self,
        message: str,
        *,
        error_code: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        self.error_code = error_code
        self.retry_after = retry_after
        super().__init__(message)


class TelegramChannelConfig(BaseModel):
    """Adapter-level config for Telegram Bot API."""

    name: str = "telegram"
    token: str = ""
    default_chat_id: str = ""
    api_base: str = "https://api.telegram.org"
    transport_name: Literal["polling", "webhook"] = "polling"
    webhook_path: str = "/telegram/events"
    webhook_url: str = ""
    webhook_secret_token: str = ""
    drop_pending_updates: bool = False
    poll_timeout_s: int = 30
    poll_limit: int = 100
    poll_idle_sleep_s: float = 0.1
    event_dedupe_size: int = _DEDUPE_SIZE
    allowed_updates: tuple[str, ...] = _ALLOWED_UPDATES

    model_config = {}


def _coerce_telegram_int(value: Any) -> int | str:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return str(value)


@dataclass
class TelegramChannel:
    """Managed adapter for Telegram Bot API polling or webhooks."""

    config: TelegramChannelConfig

    supports_slash_commands: bool = True
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
    _owns_client: bool = field(default=False, init=False, repr=False)
    _poll_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _update_offset: int | None = field(default=None, init=False, repr=False)
    _dedupe: EventDedupeCache = field(init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)
    _last_message_at: datetime | None = field(default=None, init=False, repr=False)
    # Private chats already told (this process) that edits are ignored, so a
    # habitual editor gets exactly one explanation instead of one per edit.
    _edit_notice_chats: set[str] = field(default_factory=set, init=False, repr=False)
    bot_user_id: str | None = None
    bot_username: str | None = None

    def __post_init__(self) -> None:
        self._dedupe = EventDedupeCache(max_size=self.config.event_dedupe_size)

    @property
    def transport_name(self) -> str:
        return self.config.transport_name

    @property
    def capability_profile(self) -> ChannelCapabilityProfile:
        return ChannelCapabilityProfile(
            channel_type="telegram",
            max_message_len=4096,
            length_unit=ChannelLengthUnit.UTF16_UNITS,
            splits_natively=True,
            group_chat=True,
            mentions=True,
            native_file_upload=True,
            media=True,
            reactions=True,
            typing_indicator=True,
            reply=True,
            thread_reply=True,
            edit=True,
            delete=True,
            transports=(self.config.transport_name,),
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
                tools=("sendDocument", "getFile"),
                mutates=True,
                notes=("Telegram sends generated files with sendDocument.",),
            ),
            ChannelPlatformCapability(
                category=ChannelPlatformCategories.ATTACHMENTS,
                status=ChannelPlatformCapabilityStatus.SUPPORTED,
                tools=("getFile",),
                notes=("Inbound Telegram files are resolved through getFile.",),
            ),
            ChannelPlatformCapability(
                category=ChannelPlatformCategories.THREADS,
                status=ChannelPlatformCapabilityStatus.SUPPORTED,
                notes=("Forum topic thread IDs are preserved when Telegram provides them.",),
            ),
        )

    @property
    def capabilities(self) -> frozenset[str]:
        return self.capability_profile.capability_tags()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.config.api_base,
                timeout=_DEFAULT_TIMEOUT_S,
                trust_env=_trust_env(),
            )
            self._owns_client = True
        return self._client

    async def _api(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        if not self.config.token:
            raise ValueError("telegram API call requires token")
        client = self._get_client()
        response = await client.post(f"/bot{self.config.token}/{method}", json=payload or {})
        try:
            data = response.json()
        except ValueError:
            response.raise_for_status()
            raise TelegramApiError(f"Telegram {method} returned invalid JSON") from None
        if data.get("ok") is not True:
            parameters = data.get("parameters")
            retry_after = (
                parameters.get("retry_after")
                if isinstance(parameters, dict) and isinstance(parameters.get("retry_after"), int)
                else None
            )
            raise TelegramApiError(
                data.get("description", f"Telegram {method} failed"),
                error_code=(
                    data.get("error_code")
                    if isinstance(data.get("error_code"), int)
                    else response.status_code
                ),
                retry_after=retry_after,
            )
        response.raise_for_status()
        return data.get("result")

    async def start(self) -> None:
        if not self.config.token:
            raise ValueError("telegram.start: token is required")
        if self.config.transport_name == "webhook":
            if not self.config.webhook_url:
                raise ValueError("telegram.start: webhook_url is required for webhook mode")
            if not self.config.webhook_secret_token:
                raise ValueError(
                    "telegram.start: webhook_secret_token is required for webhook mode"
                )

        me = await self._api("getMe")
        if isinstance(me, dict):
            self.bot_user_id = str(me.get("id", "")) or None
            username = me.get("username")
            self.bot_username = str(username) if username else None

        if self.config.transport_name == "webhook":
            if self.config.webhook_url:
                payload: dict[str, Any] = {
                    "url": self.config.webhook_url,
                    "drop_pending_updates": self.config.drop_pending_updates,
                    "allowed_updates": list(self.config.allowed_updates),
                }
                payload["secret_token"] = self.config.webhook_secret_token
                await self._api("setWebhook", payload)
        else:
            await self._api(
                "deleteWebhook",
                {"drop_pending_updates": self.config.drop_pending_updates},
            )
            self._poll_task = asyncio.create_task(self._poll_loop(), name="telegram:poll")

        self._connected = True
        log.info(
            "telegram.started",
            name=self.config.name,
            transport=self.config.transport_name,
            bot_user_id=self.bot_user_id,
        )

    async def probe_connection(self) -> dict[str, Any]:
        """Validate the token with getMe without changing webhook state."""
        me = await self._api("getMe")
        if not isinstance(me, dict):
            raise TelegramApiError("Telegram getMe returned invalid result")
        return {
            "authenticated": True,
            "bot_user_id": str(me.get("id") or ""),
            "bot_username": str(me.get("username") or ""),
        }

    async def stop(self) -> None:
        task = self._poll_task
        self._poll_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None
        self._owns_client = False
        self._connected = False
        log.info("telegram.stopped", name=self.config.name)

    async def health_check(self) -> ChannelHealth:
        transport_alive = self.config.transport_name != "polling" or (
            self._poll_task is not None and not self._poll_task.done()
        )
        return ChannelHealth(
            connected=self._connected and transport_alive,
            bot_user_id=self.bot_user_id,
            last_message_at=self._last_message_at,
            extra={"transport": self.config.transport_name},
        )

    async def _poll_loop(self) -> None:
        while True:
            try:
                updates = await self._api(
                    "getUpdates",
                    self._get_updates_payload(),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - polling must survive transient faults.
                log.warning(
                    "telegram.poll_error",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                await asyncio.sleep(self.config.poll_idle_sleep_s)
                continue
            if not isinstance(updates, list):
                updates = []
            for update in updates:
                if not isinstance(update, dict):
                    continue
                update_id = update.get("update_id")
                try:
                    msg = self.parse_incoming(
                        update,
                        verification=IngressVerification.SDK_SESSION,
                    )
                except ValueError:
                    log.debug("telegram.unsupported_update_ignored", update_id=update_id)
                    await self._maybe_notify_edit_ignored(update)
                    if isinstance(update_id, int):
                        self._update_offset = update_id + 1
                    continue
                self.enqueue(msg)
                # Confirm an update only after its normalized message has been
                # accepted by the local queue. The durable ingress journal can
                # replace this checkpoint without changing adapter semantics.
                if isinstance(update_id, int):
                    self._update_offset = update_id + 1
            if not updates:
                await asyncio.sleep(self.config.poll_idle_sleep_s)

    def _get_updates_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "timeout": self.config.poll_timeout_s,
            "limit": self.config.poll_limit,
            "allowed_updates": list(self.config.allowed_updates),
        }
        if self._update_offset is not None:
            payload["offset"] = self._update_offset
        return payload

    def enqueue(self, message: IncomingMessage) -> bool:
        msg_id = str(message.metadata.get("message_id", ""))
        update_id = message.metadata.get("update_id")
        dedupe_key = f"{update_id}:{msg_id}" if update_id is not None else msg_id
        if dedupe_key and not self._dedupe.check_and_add(dedupe_key):
            log.debug("telegram.duplicate_dropped", key=dedupe_key)
            return False
        from opensquilla.channels.delivery_store import durable_enqueue

        if not durable_enqueue(self, message, self._queue):
            return False
        self._last_message_at = datetime.now(UTC)
        return True

    async def receive(self) -> IncomingMessage:
        msg = await self._queue.get()
        self._last_message_at = datetime.now(UTC)
        return msg

    def create_webhook_route(self, path: str | None = None) -> Route:
        if not self.config.webhook_secret_token:
            raise ValueError("telegram webhook route requires webhook_secret_token")
        route_path = path or self.config.webhook_path
        return Route(route_path, endpoint=self._handle_webhook, methods=["POST"])

    async def _handle_webhook(self, request: Request) -> Response:
        secret = self.config.webhook_secret_token
        if not secret:
            return Response(status_code=503)
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
            return Response(status_code=401)
        try:
            update = await request.json()
        except Exception:
            return Response(status_code=400)
        if not isinstance(update, dict):
            return Response(status_code=400)
        try:
            msg = self.parse_incoming(
                update,
                verification=IngressVerification.WEBHOOK_TOKEN,
            )
        except ValueError:
            log.debug("telegram.unsupported_update_ignored", update_id=update.get("update_id"))
            await self._maybe_notify_edit_ignored(update)
            return Response(status_code=200)
        self.enqueue(msg)
        return Response(status_code=200)

    async def _maybe_notify_edit_ignored(self, update: dict[str, Any]) -> None:
        """One-time, per-chat, best-effort notice that edits no longer run turns.

        Editing a previously sent message used to re-trigger a turn; edited
        updates are now dropped deliberately (an edit is not a new request).
        Without feedback, a sender who edits a typo to re-ask cannot tell the
        drop from the bot being down. Only private chats are notified — in
        groups an edit never triggered a turn unless the bot was mentioned,
        and unsolicited notices there would be noise. The once-per-chat memory
        is process-local, so a restart may repeat the notice. Never raises:
        this runs on the receive path.
        """
        edited = update.get("edited_message")
        if not isinstance(edited, dict):
            return
        chat = edited.get("chat")
        if not isinstance(chat, dict) or chat.get("type") != "private":
            return
        chat_id = chat.get("id")
        if chat_id is None:
            return
        key = str(chat_id)
        if key in self._edit_notice_chats:
            return
        self._edit_notice_chats.add(key)
        try:
            await self._api(
                "sendMessage",
                {
                    "chat_id": key,
                    "text": (
                        "Edited messages are ignored: editing an earlier message no "
                        "longer starts a new turn. Send the corrected text as a new "
                        "message instead."
                    ),
                },
            )
        except Exception as exc:  # noqa: BLE001 - notice is best-effort
            log.debug(
                "telegram.edit_notice_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )

    @staticmethod
    def _telegram_file_attachment(
        media: dict[str, Any],
        *,
        media_kind: str,
        default_name: str,
        default_mime: str | None = None,
    ) -> Attachment | None:
        file_id = media.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            return None
        name = media.get("file_name")
        if not isinstance(name, str) or not name.strip():
            unique = media.get("file_unique_id")
            suffix = str(unique) if unique else file_id
            name = f"{default_name}-{suffix}"
        mime = media.get("mime_type") if isinstance(media.get("mime_type"), str) else default_mime
        size = media.get("file_size") if isinstance(media.get("file_size"), int) else None
        return Attachment(
            name=name,
            mime_type=mime,
            size=size,
            metadata={"telegram_file_id": file_id, "telegram_media_kind": media_kind},
        )

    def _telegram_media_attachments(self, msg: dict[str, Any]) -> list[Attachment]:
        attachments: list[Attachment] = []

        document = msg.get("document")
        if isinstance(document, dict):
            att = self._telegram_file_attachment(
                document,
                media_kind="document",
                default_name="telegram-document",
            )
            if att is not None:
                attachments.append(att)

        photo = msg.get("photo")
        if isinstance(photo, list) and photo:
            candidates = [p for p in photo if isinstance(p, dict)]
            if candidates:
                best = max(
                    candidates,
                    key=lambda p: (
                        int(p.get("file_size") or 0),
                        int(p.get("width") or 0) * int(p.get("height") or 0),
                    ),
                )
                att = self._telegram_file_attachment(
                    best,
                    media_kind="photo",
                    default_name="telegram-photo",
                    default_mime="image/jpeg",
                )
                if att is not None:
                    attachments.append(att)

        for key, default_name in (
            ("video", "telegram-video"),
            ("audio", "telegram-audio"),
            ("voice", "telegram-voice"),
            ("sticker", "telegram-sticker"),
        ):
            media = msg.get(key)
            if isinstance(media, dict):
                default_mime = "image/webp" if key == "sticker" else None
                att = self._telegram_file_attachment(
                    media,
                    media_kind=key,
                    default_name=default_name,
                    default_mime=default_mime,
                )
                if att is not None:
                    attachments.append(att)

        return attachments

    async def resolve_inbound_attachment(self, attachment: Attachment) -> Attachment:
        """Resolve Telegram file references into bytes; shared ingest validates."""

        if attachment.data is not None:
            return attachment
        file_id = attachment.metadata.get("telegram_file_id")
        if not isinstance(file_id, str) or not file_id:
            return attachment
        limit = attachment_limit_for_mime(attachment.mime_type)
        ensure_declared_size_within_limit(attachment.size, name=attachment.name, limit=limit)
        file_info = await self._api("getFile", {"file_id": file_id})
        if not isinstance(file_info, dict):
            raise TelegramApiError("Telegram getFile returned invalid result")
        ensure_declared_size_within_limit(
            file_info.get("file_size"),
            name=attachment.name,
            limit=limit,
        )
        file_path = file_info.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            raise TelegramApiError("Telegram getFile returned no file_path")
        payload, content_type = await fetch_httpx_bytes_limited(
            self._get_client(),
            f"/file/bot{self.config.token}/{file_path}",
            name=attachment.name,
            limit=limit,
        )
        name = attachment.name
        if not name or name.startswith("telegram-"):
            path_name = file_path.rsplit("/", 1)[-1]
            if path_name:
                name = path_name
        return Attachment(
            name=name,
            mime_type=preferred_attachment_mime(content_type, attachment.mime_type),
            data=payload,
            size=len(payload),
            metadata={**attachment.metadata, "telegram_file_path": file_path},
        )

    def parse_incoming(
        self,
        update: dict[str, Any],
        *,
        verification: IngressVerification = IngressVerification.LEGACY_UNVERIFIED,
    ) -> IncomingMessage:
        if "edited_message" in update or "edited_channel_post" in update:
            raise ValueError("Telegram edited updates are not new agent turns")
        msg = update.get("message") or update.get("channel_post")
        if not isinstance(msg, dict):
            raise ValueError("Telegram update did not contain a supported message payload")
        chat = msg.get("chat", {}) or {}
        sender = msg.get("from", {}) or {}
        chat_type = chat.get("type", "")
        is_group = chat_type in {"group", "supergroup", "channel"}
        message_id = msg.get("message_id", "")

        metadata: dict[str, Any] = {
            "is_group": is_group,
            "chat_type": chat_type,
            "chat_id": str(chat.get("id", self.config.default_chat_id)),
            "message_id": str(message_id),
        }
        if (update_id := update.get("update_id")) is not None:
            metadata["update_id"] = update_id
        if (thread_id := msg.get("message_thread_id")) is not None:
            metadata["thread_id"] = str(thread_id)
        for key in ("entities", "caption_entities"):
            if key in msg:
                metadata[key] = msg[key]

        content = msg.get("text") or msg.get("caption") or ""
        attachments = self._telegram_media_attachments(msg)
        if not content:
            for media_key in ("document", "photo", "video", "audio", "voice", "sticker"):
                if media_key in msg:
                    content = f"[{media_key}]"
                    break

        sender_id = str(sender.get("id", ""))
        return IncomingMessage(
            sender_id=sender_id,
            channel_id=str(chat.get("id", self.config.default_chat_id)),
            content=str(content),
            attachments=attachments,
            metadata=metadata,
            provenance=IngressProvenance(
                provider="telegram",
                account_id=self.config.name,
                transport=self.config.transport_name,
                verification=verification,
                event_id=(
                    str(update.get("update_id"))
                    if update.get("update_id") is not None
                    else str(message_id or "") or None
                ),
                principal=(
                    AuthenticatedPrincipal(subject_id=sender_id)
                    if verification != IngressVerification.LEGACY_UNVERIFIED
                    else None
                ),
            ),
        )

    def is_group_mentioned(self, msg: IncomingMessage) -> bool:
        if not msg.metadata.get("is_group"):
            return True
        username = self.bot_username
        if not username:
            return False
        mention = f"@{username}".lower()
        text = msg.content or ""
        entities = msg.metadata.get("entities") or []
        if isinstance(entities, list):
            for entity in entities:
                if not isinstance(entity, dict):
                    continue
                entity_type = entity.get("type")
                if entity_type == "mention":
                    offset = int(entity.get("offset", 0))
                    length = int(entity.get("length", 0))
                    if text[offset : offset + length].lower() == mention:
                        return True
                if entity_type == "text_mention":
                    user = entity.get("user") or {}
                    if str(user.get("id", "")) == str(self.bot_user_id or ""):
                        return True
        return mention in text.lower()

    def build_reply_message(self, content: str, inbound: IncomingMessage) -> OutgoingMessage:
        metadata: dict[str, Any] = {"chat_id": inbound.channel_id}
        if (message_id := inbound.metadata.get("message_id")) is not None:
            metadata["reply_to_message_id"] = message_id
        if (thread_id := inbound.metadata.get("thread_id")) is not None:
            metadata["thread_id"] = thread_id
        return OutgoingMessage(content=content, reply_to=inbound.channel_id, metadata=metadata)

    async def send(self, message: OutgoingMessage) -> dict[str, Any]:
        # Telegram hard-rejects sendMessage when text exceeds 4096 chars, which
        # would otherwise drop the entire reply. Split long content into
        # sequential messages so the full answer is delivered.
        # Telegram counts the 4096 cap in UTF-16 code units, not code points:
        # an astral char (emoji, some CJK) is two units, so a code-point split
        # can still overflow and be rejected. Measure in the unit Telegram uses.
        chunks = split_text_for_channel(
            message.content,
            _TELEGRAM_MAX_MESSAGE_CHARS,
            unit=ChannelLengthUnit.UTF16_UNITS,
        )
        result: Any = None
        for chunk in chunks:
            payload = self._build_send_payload(message)
            payload["text"] = chunk
            result = await self._api("sendMessage", payload)
        return result if isinstance(result, dict) else {"result": result}

    async def send_typing(self, channel_id: str | None = None) -> ChannelSendResult:
        target = str(channel_id or self.config.default_chat_id or "")
        if not target:
            return ChannelSendResult.unsupported(
                capability=ChannelCapabilities.TYPING_INDICATOR,
                reason="no chat target",
            )
        await self._api("sendChatAction", {"chat_id": target, "action": "typing"})
        return ChannelSendResult.sent(
            capability=ChannelCapabilities.TYPING_INDICATOR,
            target_id=target,
        )

    async def set_reaction(
        self,
        chat_id: str,
        message_id: str | int,
        emoji: str,
    ) -> ChannelSendResult:
        await self._api(
            "setMessageReaction",
            {
                "chat_id": _coerce_telegram_int(chat_id),
                "message_id": _coerce_telegram_int(message_id),
                "reaction": [{"type": "emoji", "emoji": emoji}],
            },
        )
        return ChannelSendResult.sent(
            capability=ChannelCapabilities.REACTIONS,
            target_id=str(chat_id),
            provider_message_id=str(message_id),
        )

    async def send_file(
        self,
        chat_id: str,
        file_path: str,
        content: str = "",
    ) -> ChannelSendResult:
        if not self.config.token:
            raise ValueError("telegram.send_file requires token")
        path = Path(file_path)
        payload = {"chat_id": str(chat_id)}
        if content:
            payload["caption"] = content
        client = self._get_client()
        with path.open("rb") as f:
            response = await client.post(
                f"/bot{self.config.token}/sendDocument",
                data=payload,
                files={"document": (path.name, f)},
            )
        response.raise_for_status()
        data = response.json()
        if data.get("ok") is not True:
            raise TelegramApiError(data.get("description", "Telegram sendDocument failed"))
        raw_result = data.get("result")
        result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
        raw_document = result.get("document")
        document: dict[str, Any] = raw_document if isinstance(raw_document, dict) else {}
        return ChannelSendResult.sent(
            capability=ChannelCapabilities.NATIVE_FILE_UPLOAD,
            target_id=str(chat_id),
            provider_message_id=str(result.get("message_id", "")),
            provider_file_id=str(document.get("file_id", "")),
        )

    def _build_send_payload(self, message: OutgoingMessage) -> dict[str, Any]:
        chat_id = (
            message.metadata.get("chat_id")
            or message.metadata.get("channel_id")
            or message.reply_to
            or self.config.default_chat_id
        )
        if not chat_id:
            raise ValueError("telegram.send requires chat_id via metadata, reply_to, or config")
        payload: dict[str, Any] = {"chat_id": str(chat_id), "text": message.content}
        thread_id = message.metadata.get("thread_id") or message.metadata.get("message_thread_id")
        if thread_id:
            payload["message_thread_id"] = _coerce_telegram_int(thread_id)
        if (reply_message_id := message.metadata.get("reply_to_message_id")) is not None:
            payload["reply_parameters"] = {
                "message_id": _coerce_telegram_int(reply_message_id),
            }
        if parse_mode := message.metadata.get("parse_mode"):
            payload["parse_mode"] = str(parse_mode)
        return payload

    async def edit(self, message_id: str, content: str) -> None:
        chat_id, raw_message_id = self._split_message_ref(message_id)
        await self._api(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": _coerce_telegram_int(raw_message_id),
                "text": content,
            },
        )

    async def delete(self, message_id: str) -> None:
        chat_id, raw_message_id = self._split_message_ref(message_id)
        await self._api(
            "deleteMessage",
            {
                "chat_id": chat_id,
                "message_id": _coerce_telegram_int(raw_message_id),
            },
        )

    def _split_message_ref(self, message_id: str) -> tuple[str, str]:
        chat_id, sep, raw_message_id = message_id.partition("|")
        if sep:
            return chat_id, raw_message_id
        if not self.config.default_chat_id:
            raise ValueError("telegram edit/delete requires '<chat_id>|<message_id>'")
        return self.config.default_chat_id, message_id


__all__ = [
    "CAPABILITY_TIER",
    "DM_SAFETY_TIERS",
    "FATAL_ERROR_CLASSES",
    "RETRYABLE_ERROR_CLASSES",
    "TelegramApiError",
    "TelegramChannel",
    "TelegramChannelConfig",
]
