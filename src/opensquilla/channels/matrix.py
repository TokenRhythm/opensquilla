"""MatrixChannel: adapter for the Matrix protocol via ``matrix-nio``.

Lifecycle: ``start()`` builds an :class:`nio.AsyncClient`, performs login or
restores a previously persisted session, registers an event callback, and
spawns a background task that runs ``client.sync_forever``. ``stop()``
cancels that task and closes the underlying ``aiohttp`` session.

Outbound messages are rendered as Matrix ``m.text`` events with an
``org.matrix.custom.html`` ``formatted_body`` produced by the existing
``markdown`` package. Streaming edits use ``m.replace`` relations and are
throttled at ``edit_interval_s`` (default 2.0 s) — a conservative local
policy, not an SDK floor.

Olm (E2EE) is **lazy-imported** only when ``encryption != "off"``; the
library is installed via the ``[matrix-e2e]`` extra and may not be
available on all platforms (notably Windows without a libolm wheel).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import inspect
import json
import mimetypes
import os
import re
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import structlog
from pydantic import BaseModel

from opensquilla.channels._attachment_io import (
    attachment_limit_for_mime,
    ensure_declared_size_within_limit,
    preferred_attachment_mime,
    read_aiohttp_response_bytes_limited,
)
from opensquilla.channels._util import EventDedupeCache
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

log = structlog.get_logger(__name__)

# Channel-contract constants pinned by the adapter audit.
CAPABILITY_TIER = "YELLOW-experimental"

# Matrix is a DM/group channel — the permission matrix denies admin-only.
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

SESSION_SCHEMA_VERSION = 2


class MatrixChannelConfig(BaseModel):
    """Pydantic config for the Matrix channel adapter.

    Mirrors :class:`opensquilla.gateway.config.MatrixChannelEntry` so the
    gateway-level entry can be lifted directly into adapter config. All
    fields except ``name`` carry safe defaults so config parsing can still
    construct a minimal-arity adapter for contract tests.
    """

    name: str = "matrix"
    homeserver_url: str = ""
    user_id: str = ""
    password: str = ""
    access_token: str = ""
    device_id: str = ""
    encryption: Literal["off", "required", "best_effort"] = "off"
    edit_interval_s: float = 2.0
    workspace_dir: str = ""
    event_dedupe_size: int = 4096

    model_config = {}  # explicit params only; no env loading


@dataclass
class MatrixChannel:
    """Channel adapter for Matrix using :mod:`matrix-nio`.

    Direct rooms are treated as direct sessions; larger rooms are group
    sessions that require explicit Matrix mention metadata.
    """

    config: MatrixChannelConfig

    _client: Any = field(default=None, init=False, repr=False)
    _sync_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _queue: asyncio.Queue[IncomingMessage] = field(
        default_factory=asyncio.Queue, init=False, repr=False
    )
    _connected: bool = field(default=False, init=False, repr=False)
    _last_message_at: datetime | None = field(default=None, init=False, repr=False)
    _last_incoming_envelope: IncomingMessage | None = field(default=None, init=False, repr=False)
    _bot_user_id: str | None = field(default=None, init=False, repr=False)
    _stream_event_id: str | None = field(default=None, init=False, repr=False)
    _last_edit_at: float = field(default=0.0, init=False, repr=False)
    _dedupe: EventDedupeCache = field(init=False, repr=False)
    _direct_room_ids: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        self._dedupe = EventDedupeCache(max_size=self.config.event_dedupe_size)

    @property
    def capability_profile(self) -> ChannelCapabilityProfile:
        return ChannelCapabilityProfile(
            channel_type="matrix",
            max_message_len=20000,
            length_unit=ChannelLengthUnit.UTF8_BYTES,
            splits_natively=False,
            group_chat=True,
            mentions=True,
            native_file_upload=True,
            media=True,
            reply=True,
            edit=True,
            delete=True,
            streamed_message_replacement=True,
            transports=("http_sync",),
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
                tools=("media.upload", "room_send"),
                mutates=True,
                notes=("Matrix file delivery uploads media and sends an m.room.message event.",),
            ),
            ChannelPlatformCapability(
                category=ChannelPlatformCategories.ATTACHMENTS,
                status=ChannelPlatformCapabilityStatus.SUPPORTED,
                tools=("media.download",),
                notes=("Inbound Matrix mxc media is downloaded through the content repository.",),
            ),
        )

    @property
    def capabilities(self) -> frozenset[str]:
        return self.capability_profile.capability_tags()

    # ------------------------------------------------------------------
    # Workspace paths / session persistence
    # ------------------------------------------------------------------

    def _state_dir(self) -> Path:
        base = Path(self.config.workspace_dir) if self.config.workspace_dir else Path.cwd()
        account_key = hashlib.sha256(
            (f"{self.config.homeserver_url}\0{self.config.user_id}\0{self.config.name}").encode()
        ).hexdigest()[:16]
        path = base / "state" / "matrix" / account_key
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            path.chmod(0o700)
        return path

    def _session_path(self) -> Path:
        return self._state_dir() / "session.json"

    def _store_path(self) -> str:
        return str(self._state_dir() / "store")

    def _load_session(self) -> dict[str, Any] | None:
        path = self._session_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("matrix.session_load_failed", error=str(exc))
            return None
        if not isinstance(data, dict):
            log.warning("matrix.session_payload_invalid")
            return None
        if data.get("schema_version") != SESSION_SCHEMA_VERSION:
            log.warning(
                "matrix.session_schema_mismatch",
                expected=SESSION_SCHEMA_VERSION,
                found=data.get("schema_version"),
            )
            return None
        if data.get("homeserver_url") != self.config.homeserver_url:
            log.warning("matrix.session_homeserver_mismatch")
            return None
        if self.config.user_id and data.get("user_id") != self.config.user_id:
            log.warning("matrix.session_user_mismatch")
            return None
        log.info("matrix.session_loaded", user_id=data.get("user_id"))
        return cast(dict[str, Any], data)

    def _save_session(self, *, user_id: str, device_id: str, access_token: str) -> None:
        path = self._session_path()
        payload = {
            "schema_version": SESSION_SCHEMA_VERSION,
            "homeserver_url": self.config.homeserver_url,
            "user_id": user_id,
            "device_id": device_id,
            "access_token": access_token,
        }
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            path.chmod(0o600)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink()

    # ------------------------------------------------------------------
    # Olm gating
    # ------------------------------------------------------------------

    def _build_client_config(self) -> Any:
        """Build an ``AsyncClientConfig`` honouring the Olm gate.

        ``encryption == "off"`` returns a plain config with
        ``encryption_enabled=False`` and never imports ``olm``. Any other
        value lazy-imports ``olm`` so missing extras surface as a clear
        :class:`RuntimeError` rather than a cryptic import failure deep
        inside ``nio``.
        """
        from nio import AsyncClientConfig  # type: ignore[import-untyped]

        if self.config.encryption == "off":
            return AsyncClientConfig(encryption_enabled=False, store_sync_tokens=True)

        try:
            import olm  # type: ignore[import-not-found]  # noqa: F401
        except ImportError as exc:
            log.warning("matrix.olm_unavailable", encryption=self.config.encryption)
            raise RuntimeError(
                "Matrix E2EE requires the [matrix-e2e] extra; on Windows "
                "this requires the libolm wheel which may not be available."
            ) from exc

        return AsyncClientConfig(encryption_enabled=True, store_sync_tokens=True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Build the AsyncClient, log in, register callbacks, spawn sync task."""
        from nio import AsyncClient, RoomMessageText

        client_config = self._build_client_config()
        client = AsyncClient(
            self.config.homeserver_url,
            self.config.user_id,
            device_id=self.config.device_id or None,
            store_path=self._store_path(),
            config=client_config,
        )

        session = self._load_session()
        if session is not None and session.get("access_token"):
            client.restore_login(
                user_id=session["user_id"],
                device_id=session["device_id"],
                access_token=session["access_token"],
            )
            self._bot_user_id = session["user_id"]
            log.info("matrix.logged_in", method="restore", user_id=self._bot_user_id)
        elif self.config.password:
            response = await client.login(
                password=self.config.password,
                device_name=self.config.device_id or "opensquilla",
            )
            access_token = getattr(response, "access_token", "")
            user_id = getattr(response, "user_id", self.config.user_id)
            device_id = getattr(response, "device_id", self.config.device_id)
            if not access_token:
                raise RuntimeError(f"Matrix login failed: {response!r}")
            self._save_session(
                user_id=user_id,
                device_id=device_id,
                access_token=access_token,
            )
            self._bot_user_id = user_id
            log.info("matrix.logged_in", method="password", user_id=user_id)
        elif self.config.access_token and self.config.device_id:
            client.restore_login(
                user_id=self.config.user_id,
                device_id=self.config.device_id,
                access_token=self.config.access_token,
            )
            self._save_session(
                user_id=self.config.user_id,
                device_id=self.config.device_id,
                access_token=self.config.access_token,
            )
            self._bot_user_id = self.config.user_id
            log.info("matrix.logged_in", method="access_token", user_id=self.config.user_id)
        else:
            raise RuntimeError(
                "Matrix adapter requires either a password or access_token + device_id"
            )

        client.add_event_callback(self._on_room_message_text, RoomMessageText)
        # Surface inbound media events too (image / audio / video / file).
        # Lazy-imported here so a missing ``RoomMessageMedia`` in older
        # nio releases does not break login.
        from nio import RoomMessageMedia  # noqa: PLC0415

        client.add_event_callback(self._on_room_message_media, RoomMessageMedia)

        list_direct_rooms = getattr(client, "list_direct_rooms", None)
        if callable(list_direct_rooms):
            try:
                direct_response = await list_direct_rooms()
                direct_mapping = getattr(direct_response, "rooms", {})
                if isinstance(direct_mapping, dict):
                    self._direct_room_ids = {
                        str(room_id)
                        for room_ids in direct_mapping.values()
                        if isinstance(room_ids, list)
                        for room_id in room_ids
                        if room_id
                    }
            except Exception as exc:
                log.warning("matrix.direct_rooms_load_failed", error=str(exc))

        self._client = client
        self._sync_task = asyncio.create_task(self._sync_forever())
        self._connected = True
        log.info("matrix.started", user_id=self._bot_user_id)

    async def _sync_forever(self) -> None:
        """Background task wrapping ``client.sync_forever``."""
        try:
            await self._client.sync_forever(timeout=30000, full_state=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("matrix.sync_failed", error=str(exc))
        finally:
            self._connected = False

    async def stop(self) -> None:
        """Cancel the sync task and close the underlying client."""
        if self._sync_task is not None:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("matrix.stop_task_error", error=str(exc))
            self._sync_task = None
        if self._client is not None:
            try:
                await self._client.close()
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("matrix.stop_close_error", error=str(exc))
            self._client = None
        self._connected = False
        log.info("matrix.stopped")

    def is_connected(self) -> bool:
        return self._connected

    async def health_check(self) -> ChannelHealth:
        return ChannelHealth(
            connected=(
                self._connected and self._sync_task is not None and not self._sync_task.done()
            ),
            bot_user_id=self._bot_user_id,
            last_message_at=self._last_message_at,
        )

    def _is_direct_room(self, room: Any) -> bool:
        """Use the Matrix ``m.direct`` account-data mapping as authority."""
        return str(getattr(room, "room_id", "")) in self._direct_room_ids

    @staticmethod
    def _event_content(event: Any) -> dict[str, Any]:
        source = getattr(event, "source", None)
        if isinstance(source, dict):
            content = source.get("content")
            if isinstance(content, dict):
                return content
        return {}

    def _event_mentions_bot(self, event: Any) -> bool:
        bot_user_id = self._bot_user_id or self.config.user_id
        if not bot_user_id:
            return False
        content = self._event_content(event)
        mentions = content.get("m.mentions")
        if isinstance(mentions, dict):
            user_ids = mentions.get("user_ids")
            if isinstance(user_ids, list) and bot_user_id in user_ids:
                return True
        body = getattr(event, "body", "")
        return isinstance(body, str) and bot_user_id in body

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def _on_room_message_text(self, room: Any, event: Any) -> None:
        """Callback registered with ``client.add_event_callback``.

        Dedupes on ``event_id`` and pushes a normalized
        :class:`IncomingMessage` onto the receive queue.
        """
        event_id = getattr(event, "event_id", None)
        if event_id and not self._dedupe.check_and_add(event_id):
            return
        sender = getattr(event, "sender", "unknown")
        if sender == self._bot_user_id:
            return
        room_id = getattr(room, "room_id", "")
        body = getattr(event, "body", "")
        server_ts = getattr(event, "server_timestamp", None)
        mention_user_ids = self._extract_mention_user_ids(getattr(event, "source", None))
        is_group = not self._is_direct_room(room)
        msg = IncomingMessage(
            sender_id=str(sender),
            channel_id=room_id,
            content=body,
            metadata={
                "is_group": is_group,
                "bot_mentioned": self._event_mentions_bot(event),
                "room_id": room_id,
                "event_id": event_id,
                "sender": sender,
                "server_timestamp": server_ts,
                "mention_user_ids": mention_user_ids,
            },
            provenance=IngressProvenance(
                provider="matrix",
                account_id=self.config.name,
                transport="http_sync",
                verification=IngressVerification.SDK_SESSION,
                event_id=str(event_id or "") or None,
                principal=AuthenticatedPrincipal(subject_id=str(sender)),
            ),
        )
        from opensquilla.channels.delivery_store import durable_enqueue

        durable_enqueue(self, msg, self._queue)
        log.debug(
            "matrix.inbound_received",
            room_id=room_id,
            event_id=event_id,
            sender=sender,
        )

    async def _on_room_message_media(self, room: Any, event: Any) -> None:
        """Surface inbound media as a text placeholder.

        Image / audio / video / file events arrive as nio
        ``RoomMessageMedia`` subclasses. The adapter turns each into an
        :class:`IncomingMessage` whose ``content`` describes the media
        kind + ``body`` (often the filename) and whose ``metadata``
        carries the ``mxc://`` URL for downstream tooling. Bot's own
        events are filtered the same way as text messages.
        """
        event_id = getattr(event, "event_id", None)
        if event_id and not self._dedupe.check_and_add(event_id):
            return
        sender = getattr(event, "sender", "unknown")
        if sender == self._bot_user_id:
            return
        room_id = getattr(room, "room_id", "")
        body = getattr(event, "body", "") or ""
        url = getattr(event, "url", "") or ""
        content = self._event_content(event)
        msgtype = content.get("msgtype", "media")
        kind = msgtype.removeprefix("m.") if isinstance(msgtype, str) else "media"
        raw_info = content.get("info")
        info = raw_info if isinstance(raw_info, dict) else {}
        mime_type = info.get("mimetype") if isinstance(info.get("mimetype"), str) else None
        size = info.get("size") if isinstance(info.get("size"), int) else None
        attachments = (
            [
                Attachment(
                    name=body or f"matrix-{kind}",
                    mime_type=mime_type,
                    url=url,
                    size=size,
                    metadata={"matrix_mxc_url": url, "matrix_media_kind": kind},
                )
            ]
            if url
            else []
        )
        mention_user_ids = self._extract_mention_user_ids(getattr(event, "source", None))
        is_group = not self._is_direct_room(room)
        msg = IncomingMessage(
            sender_id=str(sender),
            channel_id=room_id,
            content=f"[{kind}] {body}".strip(),
            attachments=attachments,
            metadata={
                "is_group": is_group,
                "bot_mentioned": self._event_mentions_bot(event),
                "room_id": room_id,
                "event_id": event_id,
                "sender": sender,
                "media_url": url,
                "media_kind": kind,
                "mention_user_ids": mention_user_ids,
            },
            provenance=IngressProvenance(
                provider="matrix",
                account_id=self.config.name,
                transport="http_sync",
                verification=IngressVerification.SDK_SESSION,
                event_id=str(event_id or "") or None,
                principal=AuthenticatedPrincipal(subject_id=str(sender)),
            ),
        )
        from opensquilla.channels.delivery_store import durable_enqueue

        durable_enqueue(self, msg, self._queue)
        log.debug(
            "matrix.inbound_media_received",
            room_id=room_id,
            event_id=event_id,
            kind=kind,
        )

    async def receive(self) -> IncomingMessage:
        msg = await self._queue.get()
        self._last_message_at = datetime.now(UTC)
        # Cache for ``send_streaming(chunks)`` no-kwarg dispatcher invocation.
        self._last_incoming_envelope = msg
        return msg

    async def resolve_inbound_attachment(self, attachment: Attachment) -> Attachment:
        """Download Matrix media bytes; shared ingest owns validation."""

        if attachment.data is not None:
            return attachment
        mxc_url = attachment.metadata.get("matrix_mxc_url") or attachment.url
        if not isinstance(mxc_url, str) or not mxc_url:
            return attachment
        limit = attachment_limit_for_mime(attachment.mime_type)
        ensure_declared_size_within_limit(attachment.size, name=attachment.name, limit=limit)
        client = self._client
        resolved = await self._stream_matrix_attachment(
            client,
            mxc_url,
            attachment.name,
            limit=limit,
        )
        if resolved is not None:
            payload, content_type = resolved
            return Attachment(
                name=attachment.name,
                mime_type=preferred_attachment_mime(content_type, attachment.mime_type),
                data=payload,
                size=len(payload),
                metadata={**attachment.metadata, "matrix_mxc_url": mxc_url},
            )

        raise RuntimeError("Matrix client does not expose bounded media streaming for attachments")

    async def _stream_matrix_attachment(
        self,
        client: Any,
        mxc_url: str,
        name: str,
        limit: int,
    ) -> tuple[bytes, str | None] | None:
        mxc_to_http = getattr(client, "mxc_to_http", None)
        client_session = getattr(client, "client_session", None)
        if not callable(mxc_to_http) or client_session is None:
            return None
        http_url = mxc_to_http(mxc_url)
        if inspect.isawaitable(http_url):
            http_url = await http_url
        if not isinstance(http_url, str) or not http_url:
            return None
        request = client_session.get(
            http_url,
            ssl=getattr(client, "ssl", None),
            timeout=30,
        )
        async with request as response:
            return await read_aiohttp_response_bytes_limited(
                response,
                name=name,
                limit=limit,
            )

    @staticmethod
    def _extract_mention_user_ids(source: Any) -> list[str]:
        if not isinstance(source, dict):
            return []
        content = source.get("content")
        if not isinstance(content, dict):
            return []
        mentions = content.get("m.mentions")
        if not isinstance(mentions, dict):
            return []
        user_ids = mentions.get("user_ids")
        if not isinstance(user_ids, list):
            return []
        return [user_id for user_id in user_ids if isinstance(user_id, str)]

    def is_group_mentioned(self, msg: IncomingMessage) -> bool:
        if not msg.metadata.get("is_group"):
            return True
        if "bot_mentioned" in msg.metadata:
            return bool(msg.metadata.get("bot_mentioned"))
        if not self._bot_user_id:
            return False
        user_ids = msg.metadata.get("mention_user_ids")
        if not isinstance(user_ids, list):
            return False
        return self._bot_user_id in user_ids

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    @staticmethod
    def _render_html(content: str) -> str:
        """Render markdown body to HTML without making module import depend on markdown."""
        try:
            import markdown  # type: ignore[import-untyped, import-not-found]  # noqa: PLC0415
        except ImportError:
            escaped = html.escape(content)
            escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
            return f"<p>{escaped}</p>"
        return cast(str, markdown.markdown(content))

    @staticmethod
    def _build_text_content(content: str, formatted: str) -> dict[str, Any]:
        return {
            "msgtype": "m.text",
            "body": content,
            "format": "org.matrix.custom.html",
            "formatted_body": formatted,
        }

    async def send(self, message: OutgoingMessage) -> None:
        if self._client is None:
            raise RuntimeError("Matrix adapter not started")
        room_id = message.reply_to or message.metadata.get("room_id", "")
        if not room_id:
            raise RuntimeError("Matrix outbound requires a room_id (reply_to)")
        formatted = self._render_html(message.content)
        content = self._build_text_content(message.content, formatted)
        reply_event_id = message.metadata.get("reply_event_id")
        if isinstance(reply_event_id, str) and reply_event_id:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_event_id}}
        response = await self._client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
        )
        if not getattr(response, "event_id", None):
            raise RuntimeError(f"Matrix send failed: {response!r}")
        log.debug("matrix.outbound_sent", room_id=room_id)

    def build_reply_message(
        self,
        content: str,
        inbound: IncomingMessage,
    ) -> OutgoingMessage:
        """Pin a batch reply to the room (and event) that triggered the turn."""
        metadata: dict[str, Any] = {"room_id": inbound.channel_id}
        event_id = inbound.metadata.get("event_id")
        if isinstance(event_id, str) and event_id:
            metadata["reply_event_id"] = event_id
        return OutgoingMessage(
            content=content,
            reply_to=inbound.channel_id,
            metadata=metadata,
        )

    def streaming_reply_kwargs(self, inbound: IncomingMessage) -> dict[str, Any]:
        """Pin a streaming reply and its terminal replacement to one room."""
        return {"room_id": inbound.channel_id}


    async def send_file(
        self,
        room_id: str,
        file_path: str,
        content: str = "",
    ) -> ChannelSendResult:
        if self._client is None:
            raise RuntimeError("Matrix adapter not started")
        path = Path(file_path)
        upload = getattr(self._client, "upload", None)
        if not callable(upload):
            raise RuntimeError("Matrix client does not expose media upload")
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as f:
            upload_result = upload(f, content_type=mime_type, filename=path.name)
            if inspect.isawaitable(upload_result):
                upload_result = await upload_result
        content_uri = self._matrix_upload_content_uri(upload_result)
        if not content_uri:
            raise RuntimeError("Matrix media upload did not return a content URI")
        file_content = {
            "msgtype": "m.file",
            "body": path.name,
            "filename": path.name,
            "url": content_uri,
            "info": {"mimetype": mime_type, "size": path.stat().st_size},
        }
        if content:
            file_content["body"] = f"{content}\n{path.name}"
        response = await self._client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=file_content,
        )
        return ChannelSendResult.sent(
            capability=ChannelCapabilities.NATIVE_FILE_UPLOAD,
            target_id=room_id,
            provider_message_id=str(getattr(response, "event_id", "")),
            provider_file_id=content_uri,
        )

    @staticmethod
    def _matrix_upload_content_uri(upload_result: Any) -> str:
        if isinstance(upload_result, dict):
            return str(upload_result.get("content_uri", ""))
        return str(getattr(upload_result, "content_uri", ""))

    @staticmethod
    def _message_route(
        message_id: str,
        room_id: str | None,
    ) -> tuple[str, str]:
        encoded_room, separator, encoded_event = message_id.partition("|")
        if room_id:
            if separator and encoded_room and encoded_room != room_id:
                raise RuntimeError("Matrix edit/delete route does not match message handle")
            event_id = encoded_event if separator else message_id
            if event_id:
                return room_id, event_id
        if separator and encoded_room and encoded_event:
            return encoded_room, encoded_event
        raise RuntimeError(
            "Matrix edit/delete requires room_id or '<room_id>|<event_id>'"
        )

    async def edit(
        self,
        message_id: str,
        content: str,
        *,
        room_id: str | None = None,
    ) -> None:
        """Edit a previously sent event by ``event_id`` via ``m.replace``.

        ``message_id`` is encoded as ``<room_id>|<event_id>`` so the
        adapter can resolve both halves without storing per-event state on the
        channel object. Streaming dispatch may instead pass the pinned
        ``room_id`` route alongside the historical raw event-id return value.
        """
        if self._client is None:
            raise RuntimeError("Matrix adapter not started")
        target_room, event_id = self._message_route(message_id, room_id)
        formatted = self._render_html(content)
        new_content = self._build_text_content(content, formatted)
        payload = {
            "msgtype": "m.text",
            "body": f"* {content}",
            "m.new_content": new_content,
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": event_id,
            },
        }
        await self._client.room_send(
            room_id=target_room,
            message_type="m.room.message",
            content=payload,
        )

    async def delete(
        self,
        message_id: str,
        *,
        room_id: str | None = None,
    ) -> None:
        """Delete (redact) a previously sent event."""
        if self._client is None:
            raise RuntimeError("Matrix adapter not started")
        target_room, event_id = self._message_route(message_id, room_id)
        await self._client.room_redact(room_id=target_room, event_id=event_id)

    # ------------------------------------------------------------------
    # Streaming (edit-throttled)
    # ------------------------------------------------------------------

    async def send_streaming(
        self,
        chunks: AsyncIterator[str],
        *,
        room_id: str | None = None,
    ) -> str | None:
        """Stream a message through ``room_send``, coalescing edits.

        First chunk is sent as a fresh ``m.text`` event; subsequent chunks
        rewrite that event via ``m.replace`` no faster than
        ``edit_interval_s`` (default 2.0 s) using ``time.monotonic``.

        ``room_id`` defaults to the room of the most recently received
        ``IncomingMessage`` so the dispatcher's no-kwarg
        ``send_streaming(chunks)`` invocation routes the reply back to
        the correct room.

        Returns the originating ``event_id`` for the parent event, or
        ``None`` if the iterator was empty.
        """
        if self._client is None:
            raise RuntimeError("Matrix adapter not started")
        if not room_id:
            last = self._last_incoming_envelope
            if last is not None:
                room_id = last.metadata.get("room_id") or last.channel_id
        if not room_id:
            raise RuntimeError(
                "Matrix send_streaming requires a room_id (no last-incoming context)"
            )
        accumulated = ""
        event_id: str | None = None
        last_edit = 0.0
        interval = self.config.edit_interval_s

        async for chunk in chunks:
            accumulated += chunk
            if event_id is None:
                formatted = self._render_html(accumulated)
                response = await self._client.room_send(
                    room_id=room_id,
                    message_type="m.room.message",
                    content=self._build_text_content(accumulated, formatted),
                )
                event_id = getattr(response, "event_id", None)
                last_edit = time.monotonic()
                continue
            now = time.monotonic()
            if now - last_edit < interval:
                continue
            formatted = self._render_html(accumulated)
            new_content = self._build_text_content(accumulated, formatted)
            await self._client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content={
                    "msgtype": "m.text",
                    "body": f"* {accumulated}",
                    "m.new_content": new_content,
                    "m.relates_to": {
                        "rel_type": "m.replace",
                        "event_id": event_id,
                    },
                },
            )
            last_edit = now

        # Final flush — send the trailing edit if there was any time-skipped
        # content beyond the last issued edit.
        if event_id is not None and accumulated:
            formatted = self._render_html(accumulated)
            new_content = self._build_text_content(accumulated, formatted)
            await self._client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content={
                    "msgtype": "m.text",
                    "body": f"* {accumulated}",
                    "m.new_content": new_content,
                    "m.relates_to": {
                        "rel_type": "m.replace",
                        "event_id": event_id,
                    },
                },
            )

        return event_id

    # ------------------------------------------------------------------
    # Diagnostics helpers used by contract tests
    # ------------------------------------------------------------------

    def session_key(self, sender_id: str, room_id: str) -> str:
        return f"matrix:{sender_id}:{room_id}"


__all__ = [
    "CAPABILITY_TIER",
    "DM_SAFETY_TIERS",
    "FATAL_ERROR_CLASSES",
    "RETRYABLE_ERROR_CLASSES",
    "SESSION_SCHEMA_VERSION",
    "MatrixChannel",
    "MatrixChannelConfig",
]


# Re-export the platform check used by tests so they can decide whether the
# Olm extra is available without rewriting the discovery logic.
def olm_available() -> bool:
    """Return True when the [matrix-e2e] extra (libolm) is installed."""
    if "olm" in sys.modules:
        return True
    try:
        import olm  # noqa: F401
    except ImportError:
        return False
    return True
