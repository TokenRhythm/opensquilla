"""Gateway-backed implementation for the inbound OpenSquilla MCP bridge."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from opensquilla.gateway_client import GatewayRPCError, normalize_gateway_url

_DETAIL_METHOD = "chat.history.entry.v1"
_DETAIL_CHUNK_BYTES = 128 * 1024


class GatewayClientLike(Protocol):
    async def connect(self, url: str) -> None: ...

    async def close(self) -> None: ...

    async def list_sessions(self, limit: int = 50) -> dict[str, Any]: ...

    async def resolve_session(self, key: str) -> dict[str, Any]: ...

    async def session_history(
        self,
        session_key: str,
        limit: int = 1000,
        *,
        before: str | None = None,
        after: str | None = None,
        include_canonical: bool | None = None,
        include_summaries: bool | None = None,
    ) -> dict[str, Any]: ...

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any: ...

    async def recv_event(self, timeout: float | None = None) -> dict[str, Any]: ...


def _default_gateway_client() -> GatewayClientLike:
    from opensquilla.gateway_client import GatewayRPCClient

    return GatewayRPCClient()


class OpenSquillaMCPBridge:
    """Small product bridge from MCP tools/resources to existing gateway RPCs."""

    def __init__(
        self,
        *,
        gateway_url: str | None = None,
        gateway_client_factory: Callable[[], GatewayClientLike] = _default_gateway_client,
    ) -> None:
        raw_url = (
            gateway_url
            or os.environ.get("OPENSQUILLA_GATEWAY_URL")
            or "ws://localhost:18791/ws"
        )
        self.gateway_url = normalize_gateway_url(raw_url)
        self._gateway_client_factory = gateway_client_factory
        self._client: GatewayClientLike | None = None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def _ensure_client(self) -> GatewayClientLike:
        if self._client is None:
            client = self._gateway_client_factory()
            await client.connect(self.gateway_url)
            self._client = client
        return self._client

    async def conversations_list(self, limit: int = 50) -> dict[str, Any]:
        client = await self._ensure_client()
        return await client.list_sessions(limit=limit)

    async def session_resolve(self, key: str) -> dict[str, Any]:
        client = await self._ensure_client()
        return await client.resolve_session(key)

    async def messages_read(
        self,
        key: str,
        limit: int = 1000,
        *,
        before: str | None = None,
        after: str | None = None,
    ) -> dict[str, Any]:
        client = await self._ensure_client()
        kwargs: dict[str, Any] = {"limit": limit}
        if before is not None:
            kwargs["before"] = before
        if after is not None:
            kwargs["after"] = after
        return await client.session_history(key, **kwargs)

    async def message_detail_read(
        self,
        key: str,
        cursor: str,
        *,
        offset: int = 0,
        chunk_bytes: int = _DETAIL_CHUNK_BYTES,
        field: str = "message",
    ) -> dict[str, Any]:
        """Read one bounded chunk from an oversized canonical history entry."""

        if field not in {"message", "text"}:
            raise ValueError("field must be message or text")
        client = await self._ensure_client()
        payload = await client.call(
            _DETAIL_METHOD,
            {
                "sessionKey": key,
                "cursor": cursor,
                "offset": offset,
                "chunkBytes": chunk_bytes,
                "field": field,
            },
        )
        if not isinstance(payload, dict):
            raise _history_error(
                _DETAIL_METHOD,
                "INVALID_HISTORY_DETAIL_CHUNK",
                "gateway returned a non-object history detail chunk",
            )
        return payload

    async def messages_send(
        self,
        key: str,
        message: str,
        *,
        intent: str = "continue",
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        client = self._gateway_client_factory()
        await client.connect(self.gateway_url)
        try:
            subscription = await self._subscribe_messages(client, key, since_stream_seq=None)
            result = await client.call(
                "sessions.send",
                {
                    "key": key,
                    "message": message,
                    "attachments": attachments or [],
                    "intent": intent,
                    "_source": {
                        "caller_kind": "cli",
                        "channel_kind": "cli",
                        "channel_id": "mcp:bridge",
                        "source_kind": "mcp",
                        "source_name": "mcp_server",
                    },
                },
            )
            response = result if isinstance(result, dict) else {"result": result}
            response["current_stream_seq"] = subscription.get("current_stream_seq")
            response["replay_complete"] = subscription.get("replay_complete")
            response["replay_gap_reason"] = subscription.get("replay_gap_reason")
            return response
        finally:
            await client.close()

    async def events_wait(
        self,
        key: str,
        *,
        since_stream_seq: int | None = None,
        timeout_ms: int = 30_000,
        max_events: int = 100,
        terminal_only: bool = False,
    ) -> dict[str, Any]:
        client = self._gateway_client_factory()
        await client.connect(self.gateway_url)
        try:
            subscription = await self._subscribe_messages(
                client, key, since_stream_seq=since_stream_seq
            )

            events: list[dict[str, Any]] = []
            current_stream_seq = int(
                subscription.get("current_stream_seq") or since_stream_seq or 0
            )
            deadline = time.monotonic() + max(0, timeout_ms) / 1000
            max_events = max(1, max_events)

            while len(events) < max_events:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    frame = await client.recv_event(timeout=remaining)
                except TimeoutError:
                    break
                normalized = _normalize_event_frame(frame)
                payload = normalized.get("payload")
                if not isinstance(payload, dict) or payload.get("session_key") != key:
                    continue
                event_name = str(normalized.get("event") or "")
                stream_seq = payload.get("stream_seq")
                if isinstance(stream_seq, int):
                    current_stream_seq = max(current_stream_seq, stream_seq)
                is_terminal = event_name in _TERMINAL_EVENTS
                if not terminal_only or is_terminal:
                    events.append({"event": event_name, "payload": payload})
                if is_terminal:
                    break

            return {
                "key": key,
                "events": events,
                "current_stream_seq": current_stream_seq,
                "replay_complete": subscription.get("replay_complete"),
                "replay_gap_reason": subscription.get("replay_gap_reason"),
                "timed_out": not events or (events[-1]["event"] not in _TERMINAL_EVENTS),
            }
        finally:
            await client.close()

    async def transcript_jsonl(self, key: str, limit: int = 1000) -> str:
        """Export at most ``limit`` newest messages in chronological JSONL order.

        History pages start at the newest window. Page output is spooled to
        temporary files and replayed in reverse page order so the bridge keeps
        only the final MCP string plus the current message in memory. Callers
        that need more than ``limit`` can page with ``messages_read`` and
        ``message_detail_read`` without changing this public limit contract.
        """

        client = await self._ensure_client()
        total_limit = max(1, int(limit))
        page_size = min(total_limit, 200)
        with tempfile.TemporaryDirectory(prefix="opensquilla-mcp-transcript-") as raw_root:
            root = Path(raw_root)
            before: str | None = None
            seen_cursors: set[str] = set()
            seen_messages: set[tuple[str, str]] = set()
            page_paths: list[Path] = []
            exported_messages = 0

            while exported_messages < total_limit:
                request_limit = min(page_size, total_limit - exported_messages)
                history = await client.session_history(
                    key,
                    limit=request_limit,
                    before=before,
                    include_canonical=True,
                    include_summaries=False,
                )
                messages, has_more = _history_page(history)
                if len(messages) > request_limit:
                    messages = messages[-request_limit:]
                if before is not None:
                    requested_key = _history_cursor_key(before)
                    newest_key = _history_cursor_key(history.get("newest_cursor"))
                    if (
                        requested_key is None
                        or newest_key is None
                        or newest_key >= requested_key
                    ):
                        raise _history_error(
                            "chat.history",
                            "HISTORY_CURSOR_INVALIDATED",
                            (
                                "gateway history no longer precedes the requested cursor; "
                                "transcript export was cancelled"
                            ),
                        )

                page_path = root / f"page-{len(page_paths):08d}.jsonl"
                with page_path.open("w", encoding="utf-8", newline="\n") as page_stream:
                    for raw_message in messages:
                        identity = _history_message_identity(raw_message)
                        if identity is not None:
                            if identity in seen_messages:
                                continue
                            seen_messages.add(identity)
                        message = await self._resolve_history_detail(key, raw_message)
                        row = _message_to_event(message)
                        events = row if isinstance(row, list) else [row]
                        for event in events:
                            json.dump(
                                event,
                                page_stream,
                                ensure_ascii=False,
                                default=str,
                            )
                            page_stream.write("\n")
                        exported_messages += 1
                        if exported_messages >= total_limit:
                            break
                page_paths.append(page_path)

                if exported_messages >= total_limit or not has_more:
                    break
                raw_cursor = history.get("oldest_cursor")
                next_before = str(raw_cursor).strip() if raw_cursor is not None else ""
                if (
                    not next_before
                    or next_before == before
                    or next_before in seen_cursors
                ):
                    raise _history_error(
                        "chat.history",
                        "HISTORY_PAGINATION_STALLED",
                        "gateway history cursor did not advance; transcript export was cancelled",
                    )
                seen_cursors.add(next_before)
                before = next_before

            output = io.StringIO()
            first_line = True
            for page_path in reversed(page_paths):
                with page_path.open(encoding="utf-8") as page_stream:
                    for line in page_stream:
                        if not first_line:
                            output.write("\n")
                        output.write(line.removesuffix("\n"))
                        first_line = False
            return output.getvalue()

    async def _resolve_history_detail(
        self,
        key: str,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        raw_reference = message.get("detail_ref")
        if raw_reference is None:
            if message.get("truncated_by_bytes") is True:
                raise _history_error(
                    "chat.history",
                    "HISTORY_DETAIL_REFERENCE_MISSING",
                    "gateway returned a truncated history preview without a detail reference",
                )
            return message
        if not isinstance(raw_reference, dict):
            raise _history_error(
                "chat.history",
                "INVALID_HISTORY_DETAIL_REF",
                "gateway returned a non-object history detail reference",
            )
        method = str(raw_reference.get("method") or "")
        reference_key = str(raw_reference.get("sessionKey") or "")
        cursor = str(raw_reference.get("cursor") or "")
        if method != _DETAIL_METHOD or reference_key != key or not cursor:
            raise _history_error(
                "chat.history",
                "INVALID_HISTORY_DETAIL_REF",
                "gateway returned an invalid history detail reference",
            )

        expected_original_bytes = message.get("original_bytes")
        if (
            isinstance(expected_original_bytes, bool)
            or not isinstance(expected_original_bytes, int)
            or expected_original_bytes < 0
        ):
            expected_original_bytes = None

        offset = 0
        expected_total: int | None = None
        expected_digest: str | None = None
        digest = hashlib.sha256()
        with tempfile.TemporaryFile(mode="w+b") as detail_stream:
            while True:
                chunk = await self.message_detail_read(
                    key,
                    cursor,
                    offset=offset,
                    chunk_bytes=_DETAIL_CHUNK_BYTES,
                    field="message",
                )
                raw_offset = _history_chunk_int(chunk, "offset")
                total = _history_chunk_int(chunk, "total")
                if raw_offset != offset:
                    raise _history_error(
                        _DETAIL_METHOD,
                        "HISTORY_DETAIL_OFFSET_MISMATCH",
                        "gateway history detail did not start at the requested offset",
                    )
                raw_digest = chunk.get("sha256")
                if not isinstance(raw_digest, str) or not _valid_sha256(raw_digest):
                    raise _history_error(
                        _DETAIL_METHOD,
                        "INVALID_HISTORY_DETAIL_CHUNK",
                        "gateway history detail omitted a valid SHA-256 digest",
                    )
                if expected_total is None:
                    if expected_original_bytes is not None and total != expected_original_bytes:
                        raise _history_error(
                            _DETAIL_METHOD,
                            "HISTORY_DETAIL_LENGTH_MISMATCH",
                            (
                                "gateway history detail did not match its declared "
                                "original byte length"
                            ),
                        )
                    expected_total = total
                    expected_digest = raw_digest.lower()
                elif total != expected_total or raw_digest.lower() != expected_digest:
                    raise _history_error(
                        _DETAIL_METHOD,
                        "HISTORY_DETAIL_IDENTITY_CHANGED",
                        "gateway history detail changed while it was being exported",
                    )
                if chunk.get("encoding") != "base64" or chunk.get("field") != "message":
                    raise _history_error(
                        _DETAIL_METHOD,
                        "INVALID_HISTORY_DETAIL_CHUNK",
                        "gateway history detail changed encoding or field",
                    )
                encoded = chunk.get("chunk_base64")
                if not isinstance(encoded, str):
                    raise _history_error(
                        _DETAIL_METHOD,
                        "INVALID_HISTORY_DETAIL_CHUNK",
                        "gateway history detail omitted base64 content",
                    )
                try:
                    decoded = base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise _history_error(
                        _DETAIL_METHOD,
                        "INVALID_HISTORY_DETAIL_CHUNK",
                        "gateway history detail contained invalid base64 content",
                    ) from exc
                end = offset + len(decoded)
                if expected_total is None or end > expected_total:
                    raise _history_error(
                        _DETAIL_METHOD,
                        "INVALID_HISTORY_DETAIL_CHUNK",
                        "gateway history detail exceeded its declared byte length",
                    )
                detail_stream.write(decoded)
                digest.update(decoded)

                next_offset = chunk.get("next")
                if next_offset is None:
                    if end != expected_total:
                        raise _history_error(
                            _DETAIL_METHOD,
                            "HISTORY_DETAIL_INCOMPLETE",
                            "gateway history detail ended before its declared byte length",
                        )
                    break
                if (
                    isinstance(next_offset, bool)
                    or not isinstance(next_offset, int)
                    or next_offset != end
                    or next_offset <= offset
                    or next_offset >= expected_total
                ):
                    raise _history_error(
                        _DETAIL_METHOD,
                        "HISTORY_DETAIL_PAGINATION_STALLED",
                        "gateway history detail offset did not advance",
                    )
                offset = next_offset

            if expected_digest is None or digest.hexdigest() != expected_digest:
                raise _history_error(
                    _DETAIL_METHOD,
                    "HISTORY_DETAIL_CHECKSUM_MISMATCH",
                    "gateway history detail failed its SHA-256 integrity check",
                )
            detail_stream.seek(0)
            try:
                with io.TextIOWrapper(detail_stream, encoding="utf-8") as text_stream:
                    resolved = json.load(text_stream)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _history_error(
                    _DETAIL_METHOD,
                    "INVALID_HISTORY_DETAIL_MESSAGE",
                    "gateway history detail did not contain a UTF-8 JSON message object",
                ) from exc
        if not isinstance(resolved, dict):
            raise _history_error(
                _DETAIL_METHOD,
                "INVALID_HISTORY_DETAIL_MESSAGE",
                "gateway history detail did not contain a JSON message object",
            )
        preview_identity = _history_message_identity(message)
        resolved_identity = _history_message_identity(resolved)
        if (
            preview_identity is not None
            and resolved_identity is not None
            and preview_identity != resolved_identity
        ):
            raise _history_error(
                _DETAIL_METHOD,
                "HISTORY_DETAIL_IDENTITY_CHANGED",
                "gateway history detail identity did not match its preview",
            )
        return resolved

    async def _subscribe_messages(
        self,
        client: GatewayClientLike,
        key: str,
        *,
        since_stream_seq: int | None,
    ) -> dict[str, Any]:
        result = await client.call(
            "sessions.messages.subscribe",
            {
                "key": key,
                "since_stream_seq": since_stream_seq,
                "fast_ack": True,
            },
        )
        return result if isinstance(result, dict) else {}


def _history_error(method: str, code: str, message: str) -> GatewayRPCError:
    return GatewayRPCError(method, code=code, message=message)


def _history_page(history: object) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(history, dict):
        raise _history_error(
            "chat.history",
            "INVALID_HISTORY_PAGE",
            "gateway returned a non-object history page",
        )
    if history.get("canonical_available") is False:
        raise _history_error(
            "chat.history",
            "CANONICAL_HISTORY_UNAVAILABLE",
            "complete canonical history is temporarily unavailable; export was cancelled",
        )
    if history.get("canonical_complete") is False:
        raise _history_error(
            "chat.history",
            "CANONICAL_HISTORY_INCOMPLETE",
            "older original messages were not preserved; export was cancelled",
        )
    raw_messages = history.get("messages")
    if not isinstance(raw_messages, list):
        raise _history_error(
            "chat.history",
            "INVALID_HISTORY_PAGE",
            "gateway history page did not contain a messages list",
        )
    return [message for message in raw_messages if isinstance(message, dict)], bool(
        history.get("has_more")
    )


def _history_cursor_key(value: object) -> tuple[int, int] | None:
    raw = str(value or "").strip()
    if not raw or "|" not in raw:
        return None
    created_at, transcript_id = raw.split("|", 1)
    try:
        return int(created_at), int(transcript_id)
    except ValueError:
        return None


def _history_message_identity(message: dict[str, Any]) -> tuple[str, str] | None:
    transcript_id = message.get("transcript_id")
    if transcript_id not in (None, ""):
        return "transcript", str(transcript_id)
    message_id = message.get("message_id") or message.get("id")
    if message_id not in (None, ""):
        return "message", str(message_id)
    return None


def _history_chunk_int(chunk: dict[str, Any], name: str) -> int:
    value = chunk.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _history_error(
            _DETAIL_METHOD,
            "INVALID_HISTORY_DETAIL_CHUNK",
            f"gateway history detail had an invalid {name}",
        )
    return value


def _valid_sha256(value: str) -> bool:
    if len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


_TERMINAL_EVENTS = {
    "session.event.done",
    "session.event.error",
    "task.cancelled",
    "task.failed",
    "task.timeout",
    "task.abandoned",
}


def _normalize_event_frame(frame: dict[str, Any]) -> dict[str, Any]:
    if "event" in frame and "payload" in frame:
        return frame
    return {"event": frame.get("event"), "payload": frame.get("payload") or frame}


def _message_to_event(message: dict[str, Any]) -> dict[str, Any] | list[dict[str, Any]]:
    role = str(message.get("role") or "unknown")
    timestamp = message.get("timestamp")
    tool_calls = message.get("tool_calls") or []
    if role == "assistant" and isinstance(tool_calls, list) and tool_calls:
        return _assistant_tool_events(tool_calls, timestamp=timestamp)
    return _message_event(
        role,
        [{"type": "text", "text": str(message.get("text") or "")}] if message.get("text") else [],
        timestamp=timestamp,
    )


def _assistant_tool_events(
    tool_calls: list[Any],
    *,
    timestamp: Any,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    assistant_blocks: list[dict[str, Any]] = []
    for segment in tool_calls:
        if not isinstance(segment, dict):
            continue
        segment_type = segment.get("type")
        if segment_type == "text":
            text = segment.get("text")
            if text:
                assistant_blocks.append({"type": "text", "text": str(text)})
        elif segment_type == "tool_use":
            assistant_blocks.append(
                {
                    "type": "toolCall",
                    "name": str(segment.get("name") or ""),
                    "id": str(segment.get("tool_use_id") or ""),
                    "arguments": segment.get("input") or {},
                }
            )
        elif segment_type == "tool_result":
            if assistant_blocks:
                output.append(_message_event("assistant", assistant_blocks, timestamp=timestamp))
                assistant_blocks = []
            output.append(
                _message_event(
                    "toolResult",
                    [{"type": "text", "text": str(segment.get("result") or "")}],
                    timestamp=timestamp,
                    tool_call_id=str(segment.get("tool_use_id") or ""),
                    tool_name=str(segment.get("name") or ""),
                    is_error=bool(segment.get("is_error", False)),
                    execution_status=(
                        segment.get("execution_status")
                        if isinstance(segment.get("execution_status"), dict)
                        else None
                    ),
                )
            )
    if assistant_blocks:
        output.append(_message_event("assistant", assistant_blocks, timestamp=timestamp))
    return output


def _message_event(
    role: str,
    content: list[dict[str, Any]],
    *,
    timestamp: Any = None,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
    is_error: bool | None = None,
    execution_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {"type": "message", "message": {"role": role, "content": content}}
    if timestamp is not None:
        event["timestamp"] = timestamp
    if tool_call_id is not None:
        event["message"]["toolCallId"] = tool_call_id
    if tool_name is not None:
        event["message"]["toolName"] = tool_name
    if is_error is not None:
        event["message"]["isError"] = is_error
    if execution_status is not None:
        event["message"]["executionStatus"] = execution_status
    return event
