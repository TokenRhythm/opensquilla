"""MS Teams channel adapter.

Inbound is webhook-driven via Bot Framework's
``BotFrameworkAdapter``. The webhook handler MUST deserialize the
Starlette request body into a ``botbuilder.schema.Activity`` first
(``Activity().deserialize(await request.json())``) and then call
``adapter.process_activity(activity, auth_header, logic)`` — the SDK's
``parse_request`` helper rejects raw Starlette ``Request`` objects.

Outbound proactive sends use ``BotFrameworkAdapter.continue_conversation``
keyed off persisted ``ConversationReference`` records cached in gateway state
with a ``schema_version`` and auto-rebuild on mismatch.

Streaming edits use ``TurnContext.update_activity``; if the channel
reports the operation as unsupported, the adapter falls back to
final-flush for the remainder of the stream — same flicker mitigation
shape as ``WeComChannel.send_streaming``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from opensquilla.channels._util import ChannelAccessPolicy, EventDedupeCache
from opensquilla.channels.contract import (
    ChannelCapabilityProfile,
    ChannelLengthUnit,
    ChannelPlatformCapability,
    ChannelPlatformCapabilityStatus,
    ChannelPlatformCategories,
    ChannelPlatformManifest,
)
from opensquilla.channels.types import (
    ChannelHealth,
    IncomingMessage,
    OutgoingMessage,
)

log = structlog.get_logger(__name__)

# Channel-contract constants pinned by the adapter audit.
CAPABILITY_TIER = "GREEN-shipping"

# Teams is a DM/group channel — the permission matrix denies admin-only.
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

_CONVERSATION_CACHE_SCHEMA_VERSION = 1


def _default_workspace_dir() -> Path:
    """Return the default per-user opensquilla workspace directory."""
    return Path.home() / ".opensquilla"


class MSTeamsChannelConfig(BaseModel):
    """Adapter-level config for MS Teams.

    Defaults keep ``MSTeamsChannel(config=MSTeamsChannelConfig(name=...))``
    valid for minimal contract construction; callers wiring real bots must
    populate ``app_id`` / ``app_password``.
    """

    name: str = "msteams"
    app_id: str = ""
    app_password: str = ""
    webhook_path: str = "/msteams/messages"
    workspace_dir: str | None = None
    edit_interval_s: float = 2.0


@dataclass
class MSTeamsChannel:
    """MS Teams adapter implementing :class:`~opensquilla.channels.types.ManagedChannel`.

    Inbound is webhook-driven (Bot Framework REST callback). Outbound
    proactive sends use ``continue_conversation`` against a cached
    ``ConversationReference`` keyed on the conversation id.
    """

    config: MSTeamsChannelConfig
    policy: ChannelAccessPolicy = field(
        default_factory=lambda: ChannelAccessPolicy(
            dm_allowed=True,
            group_allowed=True,
            mention_required_in_group=True,
            allowlist=frozenset(),
        )
    )

    _adapter: Any = field(default=None, init=False, repr=False)  # BotFrameworkAdapter
    _queue: asyncio.Queue[IncomingMessage] = field(
        default_factory=asyncio.Queue, init=False, repr=False
    )
    _references: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _bot_id: str | None = field(default=None, init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)
    _last_message_at: datetime | None = field(default=None, init=False, repr=False)
    _streams_unsupported: bool = field(default=False, init=False, repr=False)
    _dedupe: EventDedupeCache = field(
        default_factory=lambda: EventDedupeCache(max_size=10_000),
        init=False,
        repr=False,
    )

    @property
    def capability_profile(self) -> ChannelCapabilityProfile:
        return ChannelCapabilityProfile(
            channel_type="msteams",
            max_message_len=20000,
            length_unit=ChannelLengthUnit.CODE_POINTS,
            splits_natively=False,
            group_chat=True,
            mentions=True,
            reply=True,
            edit=True,
            delete=True,
            transports=("webhook",),
            notes=(
                "Bot Framework attachments/cards exist, but this adapter currently "
                "exposes text activity sends only.",
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
                tools=("FileConsentCard", "Microsoft Graph file attachments"),
                notes=(
                    "Teams supports file flows, but this adapter does not yet implement "
                    "FileConsentCard or Graph-backed file delivery.",
                ),
            ),
            ChannelPlatformCapability(
                category=ChannelPlatformCategories.ATTACHMENTS,
                status=ChannelPlatformCapabilityStatus.UNSUPPORTED,
                tools=("Bot Framework attachments",),
                notes=("Inbound Teams attachment resolution is not implemented in this adapter.",),
            ),
        )

    @property
    def capabilities(self) -> frozenset[str]:
        return self.capability_profile.capability_tags()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Build the BotFrameworkAdapter and load the persisted ref cache."""
        if self._adapter is None:
            # Lazy import keeps Bot Framework out of gateway startup unless configured.
            from botbuilder.core import (  # type: ignore[import-untyped]  # noqa: PLC0415
                BotFrameworkAdapter,
                BotFrameworkAdapterSettings,
            )

            settings = BotFrameworkAdapterSettings(
                app_id=self.config.app_id,
                app_password=self.config.app_password,
            )
            self._adapter = BotFrameworkAdapter(settings)

        self._load_conversation_cache()
        self._connected = True
        log.info(
            "msteams.started",
            app_id=self.config.app_id or None,
            cached_refs=len(self._references),
        )

    async def stop(self) -> None:
        """Persist the conversation cache and mark disconnected."""
        try:
            self._save_conversation_cache()
        finally:
            self._connected = False
            log.info("msteams.stopped", cached_refs=len(self._references))

    async def health_check(self) -> ChannelHealth:
        return ChannelHealth(
            connected=self._connected,
            bot_user_id=self._bot_id,
            last_message_at=self._last_message_at,
        )

    # ------------------------------------------------------------------
    # Conversation reference cache
    # ------------------------------------------------------------------

    def _cache_path(self) -> Path:
        workspace = Path(self.config.workspace_dir or _default_workspace_dir())
        return workspace / "state" / "msteams" / "conversations.json"

    def _load_conversation_cache(self) -> None:
        from botbuilder.schema import (  # type: ignore[import-untyped]  # noqa: PLC0415
            ConversationReference,
        )

        path = self._cache_path()
        if not path.is_file():
            self._references = {}
            return

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("msteams.cache_load_failed", error=str(exc))
            self._references = {}
            return

        version = data.get("schema_version") if isinstance(data, dict) else None
        if version != _CONVERSATION_CACHE_SCHEMA_VERSION:
            log.warning(
                "msteams.cache_schema_mismatch",
                expected=_CONVERSATION_CACHE_SCHEMA_VERSION,
                seen=version,
            )
            self._references = {}
            return

        loaded: dict[str, Any] = {}
        for key, ref_dict in data.get("conversations", {}).items():
            try:
                loaded[key] = ConversationReference().deserialize(ref_dict)
            except Exception as exc:  # noqa: BLE001 — surface but skip bad entries
                log.warning("msteams.cache_entry_invalid", key=key, error=str(exc))
        self._references = loaded

    def _save_conversation_cache(self) -> None:
        path = self._cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        serialized = {
            key: ref.serialize() if hasattr(ref, "serialize") else ref
            for key, ref in self._references.items()
        }
        payload = {
            "schema_version": _CONVERSATION_CACHE_SCHEMA_VERSION,
            "conversations": serialized,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    # ------------------------------------------------------------------
    # Webhook
    # ------------------------------------------------------------------

    def create_webhook_route(self, path: str | None = None) -> Route:
        """Return a Starlette ``Route`` for the Bot Framework callback."""
        route_path = path or self.config.webhook_path
        return Route(route_path, endpoint=self._handle_webhook, methods=["POST"])

    async def _handle_webhook(self, request: Request) -> Response:
        from botbuilder.schema import Activity  # type: ignore[import-untyped]  # noqa: PLC0415

        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return Response(status_code=400)

        try:
            activity = Activity().deserialize(body)
        except Exception as exc:  # noqa: BLE001 — protect the dispatch loop
            log.warning("msteams.activity_deserialize_failed", error=str(exc))
            return Response(status_code=400)

        auth_header = request.headers.get("Authorization", "")

        if self._adapter is None:
            return Response(status_code=503)

        try:
            await self._adapter.process_activity(activity, auth_header, self._on_turn)
        except Exception as exc:  # noqa: BLE001
            log.warning("msteams.process_activity_failed", error=str(exc))
            return Response(status_code=500)

        return Response(status_code=200)

    async def _on_turn(self, turn_context: Any) -> None:
        """Per-turn handler invoked by ``BotFrameworkAdapter.process_activity``."""
        from botbuilder.core import TurnContext  # noqa: PLC0415

        activity = turn_context.activity
        if activity is None or activity.type != "message":
            return

        ref = TurnContext.get_conversation_reference(activity)
        cache_key = self._reference_cache_key(activity)
        if cache_key:
            self._references[cache_key] = ref
        if activity.recipient is not None and getattr(activity.recipient, "id", None):
            self._bot_id = activity.recipient.id

        msg = self._activity_to_incoming(activity)
        self.enqueue(msg)
        log.info(
            "msteams.inbound_received",
            conversation_id=msg.metadata.get("conversation_id"),
            is_group=msg.metadata.get("is_group"),
        )

    @staticmethod
    def _reference_cache_key(activity: Any) -> str:
        conversation = getattr(activity, "conversation", None)
        if conversation is None:
            return ""
        return getattr(conversation, "id", "") or ""

    @staticmethod
    def _entity_value(entity: Any, key: str) -> Any:
        if isinstance(entity, dict):
            return entity.get(key)
        value = getattr(entity, key, None)
        if value is not None:
            return value
        additional = getattr(entity, "additional_properties", None)
        if isinstance(additional, dict):
            return additional.get(key)
        return None

    @classmethod
    def _mentioned_ids(cls, activity: Any) -> set[str]:
        entities = getattr(activity, "entities", None) or []
        mentioned_ids: set[str] = set()
        if not isinstance(entities, list):
            return mentioned_ids
        for entity in entities:
            if cls._entity_value(entity, "type") != "mention":
                continue
            mentioned = cls._entity_value(entity, "mentioned")
            if isinstance(mentioned, dict):
                mentioned_id = mentioned.get("id")
            else:
                mentioned_id = getattr(mentioned, "id", None)
            if mentioned_id:
                mentioned_ids.add(str(mentioned_id))
        return mentioned_ids

    @staticmethod
    def _activity_to_incoming(activity: Any) -> IncomingMessage:
        from_property = getattr(activity, "from_property", None)
        sender_id = getattr(from_property, "id", "") or "unknown"
        conversation = getattr(activity, "conversation", None)
        channel_id = getattr(conversation, "id", "") or "unknown"
        conv_type = getattr(conversation, "conversation_type", "") or ""
        recipient = getattr(activity, "recipient", None)

        # Teams puts tenant under ``channel_data.tenant.id``; older Bot
        # Framework schemas expose ``ConversationAccount.tenant_id``.
        tenant_id = getattr(conversation, "tenant_id", "") or ""
        if not tenant_id:
            channel_data = getattr(activity, "channel_data", None) or {}
            if isinstance(channel_data, dict):
                tenant = channel_data.get("tenant") or {}
                if isinstance(tenant, dict):
                    tenant_id = tenant.get("id", "") or ""

        bot_id = getattr(recipient, "id", "") or ""
        mentioned_ids = MSTeamsChannel._mentioned_ids(activity)
        metadata: dict[str, Any] = {
            "is_group": conv_type in {"groupChat", "channel"},
            "conversation_type": conv_type,
            "service_url": getattr(activity, "service_url", "") or "",
            "conversation_id": channel_id,
            "tenant_id": tenant_id,
            "bot_id": bot_id,
            "activity_id": getattr(activity, "id", "") or "",
            "mentioned_ids": sorted(mentioned_ids),
            "bot_mentioned": bool(bot_id and bot_id in mentioned_ids),
        }
        content = getattr(activity, "text", "") or ""
        return IncomingMessage(
            sender_id=sender_id,
            channel_id=channel_id,
            content=content,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Inbound queue
    # ------------------------------------------------------------------

    def enqueue(self, message: IncomingMessage) -> None:
        activity_id = str(message.metadata.get("activity_id") or "")
        if activity_id and not self._dedupe.check_and_add(activity_id):
            return
        self._queue.put_nowait(message)

    async def receive(self) -> IncomingMessage:
        msg = await self._queue.get()
        self._last_message_at = datetime.now(UTC)
        return msg

    def is_group_mentioned(self, msg: IncomingMessage) -> bool:
        if not bool(msg.metadata.get("is_group")):
            return True
        return bool(msg.metadata.get("bot_mentioned"))

    # ------------------------------------------------------------------
    # Outbound (proactive via continue_conversation)
    # ------------------------------------------------------------------

    def _resolve_reference(self, message: OutgoingMessage) -> Any | None:
        key = (message.reply_to or "").strip()
        if key and key in self._references:
            return self._references[key]
        # Fall back to most-recent reference for callers that just want
        # to reply to whoever last spoke.
        if not key and self._references:
            return next(reversed(self._references.values()))
        return None

    def build_reply_message(
        self,
        content: str,
        inbound: IncomingMessage,
    ) -> OutgoingMessage:
        """Pin a batch reply to the triggering Teams conversation."""
        return OutgoingMessage(content=content, reply_to=inbound.channel_id)

    def streaming_reply_kwargs(self, inbound: IncomingMessage) -> dict[str, Any]:
        """Pin streaming updates and terminal replacement to one conversation."""
        return {"reply_to": inbound.channel_id}

    def _operation_reference(self, reply_to: str | None) -> Any | None:
        key = (reply_to or "").strip()
        if key:
            return self._references.get(key)
        # Preserve the legacy direct edit/delete fallback for callers that do
        # not have route context. Dispatcher-owned replacements always pass it.
        return next(iter(self._references.values()), None)

    async def send(self, message: OutgoingMessage) -> None:
        if self._adapter is None:
            raise RuntimeError("MSTeamsChannel.send requires start() first")
        ref = self._resolve_reference(message)
        if ref is None:
            raise RuntimeError("MSTeamsChannel.send has no conversation reference for reply_to")

        async def _callback(turn_context: Any) -> None:
            await turn_context.send_activity(message.content)

        await self._adapter.continue_conversation(
            ref,
            _callback,
            bot_id=self._bot_id,
        )
        log.info(
            "msteams.outbound_sent",
            conversation_id=getattr(getattr(ref, "conversation", None), "id", ""),
        )

    async def edit(
        self,
        message_id: str,
        content: str,
        *,
        reply_to: str | None = None,
    ) -> None:
        if self._adapter is None:
            raise RuntimeError("MSTeamsChannel.edit requires start() first")
        ref = self._operation_reference(reply_to)
        if ref is None:
            raise RuntimeError(
                "MSTeamsChannel.edit has no matching conversation reference cached"
            )

        from botbuilder.schema import Activity  # noqa: PLC0415

        async def _callback(turn_context: Any) -> None:
            updated = Activity(type="message", id=message_id, text=content)
            await turn_context.update_activity(updated)

        await self._adapter.continue_conversation(ref, _callback, bot_id=self._bot_id)

    async def delete(
        self,
        message_id: str,
        *,
        reply_to: str | None = None,
    ) -> None:
        if self._adapter is None:
            raise RuntimeError("MSTeamsChannel.delete requires start() first")
        ref = self._operation_reference(reply_to)
        if ref is None:
            raise RuntimeError(
                "MSTeamsChannel.delete has no matching conversation reference cached"
            )

        async def _callback(turn_context: Any) -> None:
            await turn_context.delete_activity(message_id)

        await self._adapter.continue_conversation(ref, _callback, bot_id=self._bot_id)

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def send_streaming(
        self,
        chunks: AsyncIterator[str],
        *,
        reply_to: str | None = None,
    ) -> str | None:
        """Stream a message via send_activity + update_activity edits.

        Falls back to final-flush behavior if ``update_activity`` reports
        the channel as unsupported (matches WeCom flicker mitigation).
        Edits throttled at ``edit_interval_s`` (default 2.0 s).
        """
        if self._adapter is None:
            raise RuntimeError("MSTeamsChannel.send_streaming requires start() first")

        from botbuilder.schema import Activity  # noqa: PLC0415

        ref_key = (reply_to or "").strip()
        ref = (
            self._references.get(ref_key)
            if ref_key
            else next(reversed(self._references.values()), None)
        )
        if ref is None:
            raise RuntimeError("MSTeamsChannel.send_streaming has no conversation reference cached")

        accumulated = ""
        message_id: str | None = None
        unsupported = False
        last_edit = 0.0
        interval = self.config.edit_interval_s

        async for chunk in chunks:
            if not chunk:
                continue
            accumulated += chunk

            if message_id is None:
                holder: dict[str, str | None] = {"id": None}

                async def _send(turn_context: Any, _holder: dict[str, str | None] = holder) -> None:
                    response = await turn_context.send_activity(accumulated)
                    if response is not None and getattr(response, "id", None):
                        _holder["id"] = response.id

                await self._adapter.continue_conversation(ref, _send, bot_id=self._bot_id)
                message_id = holder["id"]
                last_edit = time.monotonic()
                continue

            if unsupported:
                # Flicker mitigation — accumulate and emit one final flush.
                continue

            now = time.monotonic()
            if now - last_edit < interval:
                continue

            current_message_id = message_id

            async def _edit(
                turn_context: Any, _id: str = current_message_id, _text: str = accumulated
            ) -> None:
                updated = Activity(type="message", id=_id, text=_text)
                await turn_context.update_activity(updated)

            try:
                await self._adapter.continue_conversation(ref, _edit, bot_id=self._bot_id)
                last_edit = now
            except Exception as exc:  # noqa: BLE001 — channel may not support edits
                if _is_update_unsupported(exc):
                    unsupported = True
                    self._streams_unsupported = True
                    log.info(
                        "msteams.update_unsupported",
                        message_id=message_id,
                        error=str(exc),
                    )
                else:
                    raise

        # Final flush — emit one last full-text update if we have a message
        # and either the stream produced more content after the first send
        # or edits were unsupported and we never updated mid-stream.
        if message_id is not None and accumulated:
            final_callback: Any
            if unsupported:
                # Channel doesn't support ``update_activity``. The
                # first chunk already shipped as a partial message; we
                # send the **complete** accumulated text as a fresh
                # message so the user gets the full reply (the partial
                # first chunk stays in place but is no longer the only
                # thing visible). Same flicker-mitigation shape as
                # WeCom's final-flush-only path.
                async def _final_send(turn_context: Any, _text: str = accumulated) -> None:
                    await turn_context.send_activity(_text)

                final_callback = _final_send
            else:
                final_message_id = message_id

                async def _final_update(
                    turn_context: Any, _id: str = final_message_id, _text: str = accumulated
                ) -> None:
                    updated = Activity(type="message", id=_id, text=_text)
                    await turn_context.update_activity(updated)

                final_callback = _final_update

            try:
                await self._adapter.continue_conversation(ref, final_callback, bot_id=self._bot_id)
            except Exception as exc:  # noqa: BLE001
                if not _is_update_unsupported(exc):
                    raise
                # The final-flush ``update_activity`` was the first edit
                # attempt of this stream — fast streams skip mid-stream
                # edits because of ``edit_interval_s`` throttling, so
                # we don't learn the channel is edit-incapable until
                # this point. Retry with a fresh ``send_activity`` so
                # the user receives the full accumulated text instead
                # of only the first chunk that shipped at stream start.
                self._streams_unsupported = True

                async def _retry_send(turn_context: Any, _text: str = accumulated) -> None:
                    await turn_context.send_activity(_text)

                try:
                    await self._adapter.continue_conversation(ref, _retry_send, bot_id=self._bot_id)
                except Exception as retry_exc:  # noqa: BLE001
                    log.warning(
                        "msteams.unsupported_retry_failed",
                        message_id=message_id,
                        error=str(retry_exc),
                    )

        return message_id


def _is_update_unsupported(exc: BaseException) -> bool:
    """Best-effort detection of "channel does not support update_activity"."""
    text = str(exc).lower()
    if "not supported" in text or "notsupported" in text or "not implemented" in text:
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status in {405, 501}:
        return True
    return False
