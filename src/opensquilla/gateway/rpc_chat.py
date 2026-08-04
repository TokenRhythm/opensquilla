"""RPC handlers for the chat domain — wired to sessions engine bridge."""

from __future__ import annotations

import asyncio
import base64
import codecs
import json
import time
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import quote
from uuid import uuid4

import structlog

from opensquilla.chat.conversation import ChatSendRequest, sessions_send_params
from opensquilla.chat.history import transcript_entries_to_chat_messages
from opensquilla.chat.source import chat_source_metadata
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.context_overflow import apply_context_overflow_policy
from opensquilla.gateway.history_detail import (
    HistoryDetailCapacityError,
    HistoryDetailEntryTooLargeError,
    HistoryDetailSpool,
    HistoryDetailSpoolError,
    HistoryDetailStorageError,
    HistoryDetailWriter,
)
from opensquilla.gateway.rpc import (
    BudgetedRpcResult,
    RpcContext,
    RpcHandlerError,
    RpcUnavailableError,
    get_dispatcher,
)
from opensquilla.gateway.session_services import get_session_lock, get_session_storage
from opensquilla.observability.network_policy import (
    provider_request_correlation_disabled,
)
from opensquilla.provider.types import ProviderRequestCorrelation
from opensquilla.session.compaction import build_compaction_config_from_provider
from opensquilla.session.compaction_lifecycle import new_compaction_id
from opensquilla.session.keys import build_webchat_key, canonicalize_session_key, parse_agent_id
from opensquilla.session.storage import (
    CanonicalTranscriptCursorInvalidatedError,
    StorageBusyError,
    bounded_interactive_storage_reads,
)

_d = get_dispatcher()
log = structlog.get_logger(__name__)

_WEBCHAT_SESSION_KEY = build_webchat_key()
_CHAT_HISTORY_DEFAULT_LIMIT = 50
_CHAT_HISTORY_MAX_LIMIT = 200
_CHAT_HISTORY_LOCK_BUDGET_SECONDS = 2.0
_CHAT_HISTORY_RETRY_AFTER_MS = 100
_CHAT_HISTORY_V2_DEFAULT_RESPONSE_BYTES = 768 * 1024
_CHAT_HISTORY_V2_MIN_RESPONSE_BYTES = 64 * 1024
_CHAT_HISTORY_V2_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_CHAT_HISTORY_V2_ENVELOPE_RESERVE_BYTES = 8 * 1024
_CHAT_HISTORY_ENTRY_DEFAULT_CHUNK_BYTES = 128 * 1024
_CHAT_HISTORY_ENTRY_MAX_CHUNK_BYTES = 256 * 1024
_CHAT_HISTORY_ENTRY_PREVIEW_BYTES = 4 * 1024
_CHAT_HISTORY_DETAIL_EXACT_ROW_BYTES = 8 * 1024 * 1024
_CHAT_HISTORY_DETAIL_STREAM_CONTENT_THRESHOLD_BYTES = 1024 * 1024
_CHAT_HISTORY_DETAIL_STORAGE_CHUNK_BYTES = 256 * 1024
_CHAT_HISTORY_DETAIL_METADATA_FIELD_MAX_BYTES = 64 * 1024
_CHAT_HISTORY_DETAIL_ROLE_MAX_BYTES = 1024

_HISTORY_ENTRY_SPOOL = HistoryDetailSpool()


def _canonical_webchat_session_key(value: object = None) -> str:
    """Map legacy WebChat defaults onto the canonical WebChat session."""
    raw = str(value or "").strip()
    if not raw or raw in {"default", "webchat:default", "unknown"}:
        return _WEBCHAT_SESSION_KEY
    if raw.startswith("sess-"):
        return f"agent:main:webchat:{raw[len('sess-') :]}"
    return canonicalize_session_key(raw)


def _requested_initial_collaboration_mode(params: dict[str, Any]) -> str | None:
    mode = params.get("collaborationMode")
    snake_mode = params.get("collaboration_mode")
    if mode is not None and snake_mode is not None and mode != snake_mode:
        raise ValueError("collaborationMode and collaboration_mode must match")
    if mode is None:
        mode = snake_mode
    if mode is None:
        return None
    if not isinstance(mode, str) or mode not in {"default", "plan"}:
        raise ValueError("collaborationMode must be default or plan")
    if params.get("intent") != "new_chat":
        raise ValueError("collaborationMode requires explicit new_chat intent")
    return mode


def _require_chat_session_manager(ctx: RpcContext):
    if ctx.session_manager is None:
        raise RpcUnavailableError("Chat session manager not available")
    return ctx.session_manager


def _normalize_chat_history_limit(value: object) -> int:
    try:
        if isinstance(value, int):
            limit = value
        elif isinstance(value, str):
            limit = int(value)
        else:
            limit = _CHAT_HISTORY_DEFAULT_LIMIT
    except (TypeError, ValueError):
        limit = _CHAT_HISTORY_DEFAULT_LIMIT
    return max(1, min(limit, _CHAT_HISTORY_MAX_LIMIT))


def _is_webchat_session_key(key: str) -> bool:
    parts = str(key or "").split(":")
    return (
        len(parts) == 4
        and parts[0] == "agent"
        and bool(parts[1])
        and parts[2] == "webchat"
        and bool(parts[3])
    )


def _empty_chat_history_payload(limit: int) -> dict[str, Any]:
    return {
        "messages": [],
        "has_more": False,
        "oldest_cursor": None,
        "newest_cursor": None,
        "history_scope": "complete",
        "loaded_count": 0,
        "page_size": limit,
        "canonical_available": False,
        # A missing WebChat key has an empty but complete transcript. Keep
        # canonical_available's compatibility meaning while distinguishing this
        # normal state from a temporary reader failure or lost legacy archive.
        "canonical_complete": True,
        "compaction_summaries": [],
        "turn_outcomes": [],
    }


def _chat_history_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _compact_json_bytes(value: object) -> bytes:
    """Encode one RPC payload exactly as compact UTF-8 JSON for byte budgeting."""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _bounded_int_param(
    value: object,
    *,
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        normalized = int(value) if isinstance(value, int | str) else None
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if normalized is None:
        raise ValueError(f"{name} must be an integer")
    if normalized < minimum or normalized > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return normalized


def _chat_history_v2_response_budget(params: dict[str, Any]) -> int:
    value = params.get("maxResponseBytes")
    if value is None:
        value = params.get("max_response_bytes")
    return _bounded_int_param(
        value,
        name="maxResponseBytes",
        default=_CHAT_HISTORY_V2_DEFAULT_RESPONSE_BYTES,
        minimum=_CHAT_HISTORY_V2_MIN_RESPONSE_BYTES,
        maximum=_CHAT_HISTORY_V2_MAX_RESPONSE_BYTES,
    )


def _chat_history_entry_chunk_bytes(params: dict[str, Any]) -> int:
    value = params.get("chunkBytes")
    if value is None:
        value = params.get("chunk_bytes")
    return _bounded_int_param(
        value,
        name="chunkBytes",
        default=_CHAT_HISTORY_ENTRY_DEFAULT_CHUNK_BYTES,
        minimum=1,
        maximum=_CHAT_HISTORY_ENTRY_MAX_CHUNK_BYTES,
    )


def _chat_history_entry_offset(params: dict[str, Any], *, total: int) -> int:
    offset = _bounded_int_param(
        params.get("offset"),
        name="offset",
        default=0,
        minimum=0,
        maximum=total,
    )
    return offset


def _chat_history_v2_summary_metadata(summary: object) -> dict[str, Any]:
    raw = summary if isinstance(summary, dict) else {}
    text = raw.get("summary_text")
    metadata = {
        key: raw.get(key)
        for key in (
            "id",
            "compaction_id",
            "compaction_index",
            "trigger_reason",
            "summary_format",
            "coverage_status",
            "removed_count",
            "kept_count",
            "covered_through_id",
            "created_at",
        )
    }
    existing_bytes = raw.get("summary_bytes")
    metadata["summary_bytes"] = (
        len(text.encode("utf-8"))
        if isinstance(text, str)
        else (
            existing_bytes
            if isinstance(existing_bytes, int) and not isinstance(existing_bytes, bool)
            else None
        )
    )
    return metadata


def _chat_history_v2_message_cursor(message: object) -> str | None:
    if not isinstance(message, dict):
        return None
    created_at = message.get("timestamp")
    stable_id = message.get("transcript_id")
    if stable_id in {None, ""}:
        stable_id = message.get("message_id") or message.get("id")
    if created_at in {None, ""} or stable_id in {None, ""}:
        return None
    return f"{created_at}|{stable_id}"


def _utf8_prefix(value: object, *, max_bytes: int) -> str:
    text = value if isinstance(value, str) else str(value or "")
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _chat_history_v2_preview(
    message: dict[str, Any],
    *,
    session_key: str,
    max_preview_bytes: int,
) -> dict[str, Any]:
    cursor = _chat_history_v2_message_cursor(message)
    if cursor is None:
        raise RpcHandlerError(
            "HISTORY_ENTRY_TOO_LARGE",
            "The history entry is too large and has no stable cursor.",
        )
    preview: dict[str, Any] = {
        key: message.get(key)
        for key in (
            "id",
            "message_id",
            "transcript_id",
            "role",
            "timestamp",
            "provenance_kind",
            "provenance_source_session_key",
            "provenance_source_tool",
        )
        if key in message
    }
    turn_context = message.get("turn_context")
    if isinstance(turn_context, dict) and isinstance(turn_context.get("turn_id"), str):
        preview["turn_context"] = {"turn_id": turn_context["turn_id"]}
    preview.update(
        {
            "preview": _utf8_prefix(message.get("text"), max_bytes=max_preview_bytes),
            "original_bytes": len(_compact_json_bytes(message)),
            "detail_ref": {
                "method": "chat.history.entry.v1",
                "sessionKey": session_key,
                "cursor": cursor,
            },
            "truncated_by_bytes": True,
        }
    )
    return preview


def _chat_history_v2_turn_ids(messages: list[dict[str, Any]]) -> set[str]:
    return {
        turn_id
        for message in messages
        if isinstance((turn_context := message.get("turn_context")), dict)
        and isinstance((turn_id := turn_context.get("turn_id")), str)
        and turn_id
    }


def _chat_history_v2_set_wire_bytes(payload: dict[str, Any]) -> int:
    """Set the self-describing byte count to the compact payload's fixed point."""

    for _ in range(8):
        measured = len(_compact_json_bytes(payload))
        if payload.get("wire_bytes") == measured:
            return measured
        payload["wire_bytes"] = measured
    measured = len(_compact_json_bytes(payload))
    payload["wire_bytes"] = measured
    return len(_compact_json_bytes(payload))


def _chat_history_v2_candidate(
    base: dict[str, Any],
    *,
    messages: list[dict[str, Any]],
    original_message_count: int,
    original_has_more: bool,
    byte_budget: int,
    truncated_by_bytes: bool,
) -> tuple[dict[str, Any], int]:
    candidate = dict(base)
    candidate["messages"] = messages
    candidate["loaded_count"] = len(messages)
    dropped_records = len(messages) < original_message_count
    candidate["has_more"] = bool(original_has_more or dropped_records)
    candidate["oldest_cursor"] = (
        _chat_history_v2_message_cursor(messages[0])
        if messages
        else base.get("oldest_cursor")
    )
    candidate["newest_cursor"] = (
        _chat_history_v2_message_cursor(messages[-1])
        if messages
        else base.get("newest_cursor")
    )
    turn_ids = _chat_history_v2_turn_ids(messages)
    candidate["turn_outcomes"] = [
        outcome
        for outcome in base.get("turn_outcomes") or []
        if isinstance(outcome, dict) and outcome.get("turn_id") in turn_ids
    ]
    if candidate.get("compaction_summaries") or candidate.get("compaction_summary_count"):
        candidate["history_scope"] = "compacted"
    elif candidate["has_more"] or truncated_by_bytes:
        candidate["history_scope"] = "latest_window"
    candidate["byte_budget"] = byte_budget
    candidate["truncated_by_bytes"] = truncated_by_bytes
    candidate["wire_bytes"] = 0
    return candidate, _chat_history_v2_set_wire_bytes(candidate)


def _chat_history_v2_smallest_fitting_suffix(
    build_candidate: Any,
    *,
    count: int,
    byte_budget: int,
) -> dict[str, Any] | None:
    low = 0
    high = count - 1
    best: dict[str, Any] | None = None
    while low <= high:
        start = (low + high) // 2
        candidate, size = build_candidate(start)
        if size <= byte_budget:
            best = candidate
            high = start - 1
        else:
            low = start + 1
    return best


def _chat_history_v2_largest_fitting_prefix(
    build_candidate: Any,
    *,
    count: int,
    byte_budget: int,
) -> dict[str, Any] | None:
    low = 1
    high = count
    best: dict[str, Any] | None = None
    while low <= high:
        end = (low + high) // 2
        candidate, size = build_candidate(end)
        if size <= byte_budget:
            best = candidate
            low = end + 1
        else:
            high = end - 1
    return best


def _fit_chat_history_v2_payload(
    history: dict[str, Any],
    *,
    session_key: str,
    byte_budget: int,
    retain_earliest: bool = False,
) -> dict[str, Any]:
    """Return a cursor-contiguous whole-record window that fits the budget.

    Default and ``before`` reads move backward, so they retain the newest
    suffix. ``after`` reads move forward and must retain the earliest prefix;
    keeping their suffix would make the skipped prefix unreachable.
    """

    base = dict(history)
    base["compaction_summaries"] = [
        _chat_history_v2_summary_metadata(summary)
        for summary in history.get("compaction_summaries") or []
    ]
    original_messages = [
        dict(message)
        for message in history.get("messages") or []
        if isinstance(message, dict)
    ]
    original_count = len(original_messages)
    original_has_more = bool(history.get("has_more"))
    source_truncated = bool(history.get("truncated_by_bytes"))

    full, full_size = _chat_history_v2_candidate(
        base,
        messages=original_messages,
        original_message_count=original_count,
        original_has_more=original_has_more,
        byte_budget=byte_budget,
        truncated_by_bytes=source_truncated,
    )
    if full_size <= byte_budget:
        return full

    # Typed turn outcomes are useful terminal metadata, but messages and their
    # stable cursors are the pageable history contract.  If the optional
    # outcomes make the response overflow, drop them before reducing the
    # message window.  This also covers the tight summary-metadata boundary:
    # summary fitting reserves room for one message preview, not for an
    # arbitrarily large (though storage-bounded) outcome attached afterwards.
    if full.get("turn_outcomes"):
        base["turn_outcomes"] = []
        source_truncated = True
        full, full_size = _chat_history_v2_candidate(
            base,
            messages=original_messages,
            original_message_count=original_count,
            original_has_more=original_has_more,
            byte_budget=byte_budget,
            truncated_by_bytes=True,
        )
        if full_size <= byte_budget:
            return full

    if not original_messages:
        raise RpcHandlerError(
            "HISTORY_RESPONSE_TOO_LARGE",
            "History metadata exceeds maxResponseBytes.",
            details={"byte_budget": byte_budget},
        )

    def _full_suffix(start: int) -> tuple[dict[str, Any], int]:
        return _chat_history_v2_candidate(
            base,
            messages=original_messages[start:],
            original_message_count=original_count,
            original_has_more=original_has_more,
            byte_budget=byte_budget,
            truncated_by_bytes=start > 0,
        )

    def _full_prefix(end: int) -> tuple[dict[str, Any], int]:
        return _chat_history_v2_candidate(
            base,
            messages=original_messages[:end],
            original_message_count=original_count,
            original_has_more=original_has_more,
            byte_budget=byte_budget,
            truncated_by_bytes=end < original_count,
        )

    boundary_only, boundary_only_size = (
        _full_prefix(1) if retain_earliest else _full_suffix(original_count - 1)
    )
    if boundary_only_size <= byte_budget:
        fitted = (
            _chat_history_v2_largest_fitting_prefix(
                _full_prefix,
                count=original_count,
                byte_budget=byte_budget,
            )
            if retain_earliest
            else _chat_history_v2_smallest_fitting_suffix(
                _full_suffix,
                count=original_count,
                byte_budget=byte_budget,
            )
        )
        assert fitted is not None
        return fitted

    boundary_message = original_messages[0] if retain_earliest else original_messages[-1]

    def _preview_only(max_preview_bytes: int) -> tuple[dict[str, Any], int]:
        preview = _chat_history_v2_preview(
            boundary_message,
            session_key=session_key,
            max_preview_bytes=max_preview_bytes,
        )
        return _chat_history_v2_candidate(
            base,
            messages=[preview],
            original_message_count=original_count,
            original_has_more=original_has_more,
            byte_budget=byte_budget,
            truncated_by_bytes=True,
        )

    preview_cap = _CHAT_HISTORY_ENTRY_PREVIEW_BYTES
    preview_candidate, preview_size = _preview_only(preview_cap)
    if preview_size > byte_budget:
        low = 0
        high = preview_cap
        fitting_cap: int | None = None
        fitting_candidate: dict[str, Any] | None = None
        while low <= high:
            candidate_cap = (low + high) // 2
            candidate, size = _preview_only(candidate_cap)
            if size <= byte_budget:
                fitting_cap = candidate_cap
                fitting_candidate = candidate
                low = candidate_cap + 1
            else:
                high = candidate_cap - 1
        if fitting_cap is None or fitting_candidate is None:
            raise RpcHandlerError(
                "HISTORY_RESPONSE_TOO_LARGE",
                "History metadata exceeds maxResponseBytes.",
                details={"byte_budget": byte_budget},
            )
        preview_cap = fitting_cap
        preview_candidate = fitting_candidate

    preview = _chat_history_v2_preview(
        boundary_message,
        session_key=session_key,
        max_preview_bytes=preview_cap,
    )

    def _suffix_with_preview(start: int) -> tuple[dict[str, Any], int]:
        return _chat_history_v2_candidate(
            base,
            messages=[*original_messages[start:-1], preview],
            original_message_count=original_count,
            original_has_more=original_has_more,
            byte_budget=byte_budget,
            truncated_by_bytes=True,
        )

    def _prefix_with_preview(end: int) -> tuple[dict[str, Any], int]:
        return _chat_history_v2_candidate(
            base,
            messages=[preview, *original_messages[1:end]],
            original_message_count=original_count,
            original_has_more=original_has_more,
            byte_budget=byte_budget,
            truncated_by_bytes=True,
        )

    fitted = (
        _chat_history_v2_largest_fitting_prefix(
            _prefix_with_preview,
            count=original_count,
            byte_budget=byte_budget,
        )
        if retain_earliest
        else _chat_history_v2_smallest_fitting_suffix(
            _suffix_with_preview,
            count=original_count,
            byte_budget=byte_budget,
        )
    )
    return fitted or preview_candidate


def _canonical_cursor_page_parts(page: object) -> tuple[list[object], bool, bool]:
    if isinstance(page, dict):
        items = page.get("items")
        has_more = page.get("has_more", False)
        canonical_complete = page.get("canonical_complete", True)
    elif isinstance(page, tuple):
        items = page[0] if page else None
        has_more = page[1] if len(page) > 1 else False
        canonical_complete = page[2] if len(page) > 2 else True
    else:
        items = getattr(page, "items", None)
        has_more = getattr(page, "has_more", False)
        canonical_complete = getattr(page, "canonical_complete", True)
    if items is None:
        raise TypeError("canonical transcript cursor page is missing items")
    return list(items), bool(has_more), bool(canonical_complete)


def _chat_history_v2_probe_cursor(item: object) -> tuple[int, int] | None:
    raw = getattr(item, "cursor", None)
    if (
        not isinstance(raw, tuple)
        or len(raw) != 2
        or isinstance(raw[0], bool)
        or isinstance(raw[1], bool)
    ):
        return None
    try:
        return int(raw[0]), int(raw[1])
    except (TypeError, ValueError):
        return None


def _chat_history_v2_probe_preview(
    item: object,
    *,
    session_key: str,
    max_preview_bytes: int,
) -> dict[str, Any]:
    """Build a bounded preview from SQLite-limited cursor metadata.

    ``original_bytes`` is explicitly null here. Its exact value describes the
    projected JSON detail rather than the stored row and is reported by the
    first detail chunk. ``stored_bytes`` remains a bounded storage diagnostic;
    clients must validate the chunk's authoritative ``total`` value.
    """

    cursor_key = _chat_history_v2_probe_cursor(item)
    if cursor_key is None:
        raise RpcHandlerError(
            "HISTORY_ENTRY_TOO_LARGE",
            "The history entry is too large and has no stable cursor.",
        )
    created_at, transcript_id = cursor_key
    message_id = getattr(item, "message_id", None)
    preview: dict[str, Any] = {
        "role": str(getattr(item, "role", None) or "unknown"),
        "timestamp": created_at,
        "transcript_id": transcript_id,
        # Cursor metadata is intentionally not a display projection. Raw
        # storage prefixes can contain preflight control text, artifact
        # markers, or legacy ContentBlock encodings that the normal history
        # projector hides. Keep an unmaterialized preview empty and let the
        # bounded detail method perform the authoritative projection.
        "preview": "",
        "original_bytes": None,
        "detail_ref": {
            "method": "chat.history.entry.v1",
            "sessionKey": session_key,
            "cursor": f"{created_at}|{transcript_id}",
        },
        "truncated_by_bytes": True,
    }
    if isinstance(message_id, str) and message_id:
        preview["id"] = message_id
        preview["message_id"] = message_id
    for field in (
        "provenance_kind",
        "provenance_source_session_key",
        "provenance_source_tool",
    ):
        value = getattr(item, field, None)
        if isinstance(value, str) and value:
            preview[field] = value
    turn_id = getattr(item, "turn_id", None)
    if isinstance(turn_id, str) and turn_id:
        preview["turn_context"] = {"turn_id": turn_id}
    del max_preview_bytes
    stored_bytes = getattr(item, "stored_bytes", None)
    if isinstance(stored_bytes, int) and not isinstance(stored_bytes, bool):
        preview["stored_bytes"] = max(0, stored_bytes)
    return preview


def _chat_history_v2_with_message(
    base: dict[str, Any],
    *,
    selected: list[dict[str, Any]],
    message: dict[str, Any],
    append: bool,
    original_message_count: int,
    original_has_more: bool,
    byte_budget: int,
    truncated_by_bytes: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    messages = [*selected, message] if append else [message, *selected]
    candidate, size = _chat_history_v2_candidate(
        base,
        messages=messages,
        original_message_count=original_message_count,
        original_has_more=original_has_more,
        byte_budget=byte_budget,
        truncated_by_bytes=truncated_by_bytes,
    )
    return messages, candidate, size


async def _load_chat_history_v2_summary_metadata(
    mgr: object,
    session_key: str,
    *,
    include_summaries: bool,
) -> tuple[list[dict[str, Any]], bool, int]:
    if not include_summaries:
        return [], False, 0
    getter = getattr(mgr, "get_summary_metadata", None)
    if callable(getter):
        try:
            with bounded_interactive_storage_reads():
                raw = await getter(session_key)
            if isinstance(raw, tuple) and len(raw) == 3:
                items, has_more, total_count = raw
            else:
                items = raw
                has_more = False
                total_count = len(items or [])
            metadata = [dict(item) for item in items or [] if isinstance(item, dict)]
            return metadata, bool(has_more), max(len(metadata), int(total_count))
        except StorageBusyError:
            return [], False, 0
        except Exception:  # noqa: BLE001 - optional metadata must not hide history.
            log.warning(
                "chat.history.v2.summary_metadata_failed",
                session_key=session_key,
                exc_info=True,
            )
            return [], False, 0
    # A v2 response must stay bounded even for custom manager implementations.
    # Falling back to ``get_summaries`` here would load every summary body.
    return [], False, 0


async def _read_chat_history_v2_cursor_page(
    ctx: RpcContext,
    *,
    session_key: str,
    reader: Callable[[], Awaitable[object]],
) -> object:
    """Capture only the cursor page under the session mutation lock.

    Exact rows, summary metadata, task outcomes, JSON projection, and response
    fitting can all involve multiple storage calls. Keeping that work outside
    the turn/compaction lock prevents a large history read from blocking a live
    turn while the stable cursor identities still let later reads detect a
    reset or incompatible rewrite.
    """

    history_lock = get_session_lock(ctx.turn_runner, session_key)
    if history_lock is None:
        return await reader()

    started = time.monotonic()
    acquired = False
    try:
        try:
            async with asyncio.timeout(_CHAT_HISTORY_LOCK_BUDGET_SECONDS):
                await history_lock.acquire()
        except TimeoutError as exc:
            raise StorageBusyError(
                "chat.history.v2",
                waited_ms=max(0, int((time.monotonic() - started) * 1000)),
                retry_after_ms=_CHAT_HISTORY_RETRY_AFTER_MS,
                stage="lock_acquire",
                resource="session_mutation_lock",
            ) from exc
        acquired = True
        return await reader()
    finally:
        if acquired:
            history_lock.release()


async def _load_chat_history_v2_payload_unlocked(
    raw_params: dict[str, Any],
    ctx: RpcContext,
    *,
    payload_budget: int,
) -> dict[str, Any]:
    """Incrementally project a cursor page while keeping page memory bounded.

    The storage cursor page contains only bounded metadata. Exact transcript
    rows are fetched one at a time, and rows whose stored footprint already
    exceeds the response budget become previews without entering Python in
    full. Legacy RPCs continue to use their original count-bounded page reader.
    """

    session_key = _canonical_webchat_session_key(raw_params.get("sessionKey"))
    limit = _normalize_chat_history_limit(raw_params.get("limit"))
    before = raw_params.get("before")
    after = raw_params.get("after")
    before_key = _chat_history_cursor_key(before)
    after_key = _chat_history_cursor_key(after)
    if before is not None and before_key is None:
        raise ValueError("before must be a stable created_at|transcript_id cursor")
    if after is not None and after_key is None:
        raise ValueError("after must be a stable created_at|transcript_id cursor")
    retain_earliest = after_key is not None and before_key is None
    include_canonical = _chat_history_bool(
        raw_params.get("includeCanonical"),
        default=True,
    )
    include_summaries = _chat_history_bool(
        raw_params.get("includeSummaries"),
        default=True,
    )
    mgr = _require_chat_session_manager(ctx)
    cursor_getter = getattr(mgr, "get_canonical_transcript_cursor_page", None)
    exact_getter = getattr(mgr, "get_canonical_transcript_entry_by_cursor", None)

    # Do not invoke the legacy full-row reader from inside a method that
    # promises a byte-bounded response. Old Gateways omit this method from
    # capabilities; new clients already perform their protocol-level fallback.
    if not include_canonical or not callable(cursor_getter) or not callable(exact_getter):
        raise RpcUnavailableError(
            "Byte-bounded canonical history is unavailable for this session manager"
        )

    async def _read_cursor_page() -> object:
        return await cursor_getter(
            session_key,
            limit=limit,
            before=before_key,
            after=after_key,
        )

    try:
        with bounded_interactive_storage_reads():
            cursor_page = await _read_chat_history_v2_cursor_page(
                ctx,
                session_key=session_key,
                reader=_read_cursor_page,
            )
    except CanonicalTranscriptCursorInvalidatedError as exc:
        raise RpcHandlerError(
            "HISTORY_CURSOR_INVALIDATED",
            "The requested history cursor no longer exists.",
            details={"before": before, "after": after},
            retryable=True,
        ) from exc
    except KeyError:
        if _is_webchat_session_key(session_key):
            empty = _empty_chat_history_payload(limit)
            empty.update(
                {
                    "byte_budget": payload_budget,
                    "wire_bytes": 0,
                    "truncated_by_bytes": False,
                }
            )
            _chat_history_v2_set_wire_bytes(empty)
            return empty
        raise

    items, source_has_more, canonical_complete = _canonical_cursor_page_parts(cursor_page)
    summaries, summaries_has_more, summary_count = await _load_chat_history_v2_summary_metadata(
        mgr,
        session_key,
        include_summaries=include_summaries,
    )
    base: dict[str, Any] = {
        "messages": [],
        "has_more": source_has_more,
        "oldest_cursor": None,
        "newest_cursor": None,
        "history_scope": (
            "compacted" if summaries else ("latest_window" if source_has_more else "complete")
        ),
        "loaded_count": 0,
        "page_size": limit,
        "canonical_available": True,
        "canonical_complete": canonical_complete,
        "compaction_summaries": summaries,
        "compaction_summaries_has_more": summaries_has_more,
        "compaction_summary_count": summary_count,
        "turn_outcomes": [],
    }
    if not canonical_complete:
        base["canonical_incomplete_reason"] = "canonical_archive_coverage_unverified"

    def _empty_with_summary_count(
        retained_count: int,
    ) -> tuple[dict[str, Any], dict[str, Any], int]:
        retained = summaries[-retained_count:] if retained_count else []
        candidate_base = {
            **base,
            "compaction_summaries": retained,
            "compaction_summaries_has_more": bool(
                summaries_has_more or retained_count < len(summaries)
            ),
        }
        candidate, size = _chat_history_v2_candidate(
            candidate_base,
            messages=[],
            original_message_count=len(items),
            original_has_more=source_has_more,
            byte_budget=payload_budget,
            truncated_by_bytes=retained_count < len(summaries),
        )
        return candidate_base, candidate, size

    base, empty_candidate, empty_size = _empty_with_summary_count(len(summaries))
    summaries_truncated_by_bytes = False
    if empty_size > payload_budget and summaries:
        # Summary bodies are never loaded, but even bounded boundary metadata
        # can outgrow the caller's smallest response budget after many
        # compactions. Keep the largest newest suffix that fits and expose the
        # omitted count through the existing summary pagination metadata.
        low = 0
        high = len(summaries) - 1
        best = _empty_with_summary_count(0)
        while low <= high:
            retained_count = (low + high) // 2
            summary_candidate = _empty_with_summary_count(retained_count)
            if summary_candidate[2] <= payload_budget:
                best = summary_candidate
                low = retained_count + 1
            else:
                high = retained_count - 1
        base, empty_candidate, empty_size = best
        summaries_truncated_by_bytes = True
    if empty_size > payload_budget:
        raise RpcHandlerError(
            "HISTORY_RESPONSE_TOO_LARGE",
            "History metadata exceeds maxResponseBytes.",
            details={"byte_budget": payload_budget},
        )
    if not items:
        return empty_candidate

    ordered_items = items if retain_earliest else list(reversed(items))

    def _boundary_with_summary_count(
        retained_count: int,
    ) -> tuple[dict[str, Any], dict[str, Any], int, int]:
        candidate_base, candidate_empty, candidate_empty_size = (
            _empty_with_summary_count(retained_count)
        )
        boundary_preview = _chat_history_v2_probe_preview(
            ordered_items[0],
            session_key=session_key,
            max_preview_bytes=0,
        )
        _, _, boundary_size = _chat_history_v2_with_message(
            candidate_base,
            selected=[],
            message=boundary_preview,
            append=retain_earliest,
            original_message_count=len(items),
            original_has_more=source_has_more,
            byte_budget=payload_budget,
            truncated_by_bytes=True,
        )
        return candidate_base, candidate_empty, candidate_empty_size, boundary_size

    # Messages are the pageable data; compaction summaries are optional boundary
    # metadata. Reserve enough room for at least one safe detail reference before
    # deciding how many summary records to retain. Otherwise a valid non-empty
    # history could report has_more without any cursor that lets a client advance.
    retained_summary_count = len(base.get("compaction_summaries") or [])
    boundary = _boundary_with_summary_count(retained_summary_count)
    if boundary[3] > payload_budget and retained_summary_count:
        low = 0
        high = retained_summary_count - 1
        best_boundary = _boundary_with_summary_count(0)
        while low <= high:
            candidate_count = (low + high) // 2
            candidate_boundary = _boundary_with_summary_count(candidate_count)
            if candidate_boundary[3] <= payload_budget:
                best_boundary = candidate_boundary
                low = candidate_count + 1
            else:
                high = candidate_count - 1
        boundary = best_boundary
        summaries_truncated_by_bytes = True
    if boundary[3] > payload_budget:
        raise RpcHandlerError(
            "HISTORY_RESPONSE_TOO_LARGE",
            "History entry metadata exceeds maxResponseBytes.",
            details={"byte_budget": payload_budget},
        )
    base, empty_candidate, empty_size, _boundary_size = boundary

    selected: list[dict[str, Any]] = []
    selected_candidate = empty_candidate
    processed = 0
    skipped = 0
    processed_cursors: list[tuple[int, int]] = []
    truncated_by_bytes = summaries_truncated_by_bytes
    # A row at or below the payload budget is safe to materialize alone. JSON
    # escaping can expand it, but only by a fixed factor; larger rows stay in
    # SQLite and use the bounded cursor preview.
    exact_row_budget = max(1, payload_budget)

    for item in ordered_items:
        cursor_key = _chat_history_v2_probe_cursor(item)
        stored_bytes = getattr(item, "stored_bytes", None)
        should_fetch_exact = (
            isinstance(stored_bytes, int)
            and not isinstance(stored_bytes, bool)
            and 0 <= stored_bytes <= exact_row_budget
        )
        projected_message: dict[str, Any] | None = None
        exact_attempted = False
        if should_fetch_exact:
            if cursor_key is not None:
                exact_attempted = True
                with bounded_interactive_storage_reads():
                    entry = await exact_getter(session_key, cursor=cursor_key)
                if entry is None:
                    raise RpcHandlerError(
                        "HISTORY_CURSOR_INVALIDATED",
                        "The history entry changed while its page was being prepared.",
                        details={
                            "cursor": f"{cursor_key[0]}|{cursor_key[1]}",
                        },
                        retryable=True,
                    )
                projected = transcript_entries_to_chat_messages(
                    [entry],
                    limit=None,
                )
                if projected:
                    projected_message = _annotate_transcript_attachment_downloads(
                        [dict(projected[0])],
                        session_key=session_key,
                    )[0]

        if exact_attempted and projected_message is None:
            # The legacy display projector intentionally hides marker-only
            # ContentBlock records. Consume that raw cursor without inventing a
            # detail_ref that the same projector could never resolve.
            processed += 1
            skipped += 1
            if cursor_key is not None:
                processed_cursors.append(cursor_key)
            continue

        if projected_message is None:
            message_for_candidate = _chat_history_v2_probe_preview(
                item,
                session_key=session_key,
                max_preview_bytes=_CHAT_HISTORY_ENTRY_PREVIEW_BYTES,
            )
            is_preview = True
        else:
            message_for_candidate = projected_message
            is_preview = False

        candidate_messages, message_candidate, candidate_size = _chat_history_v2_with_message(
            base,
            selected=selected,
            message=message_for_candidate,
            append=retain_earliest,
            original_message_count=len(items),
            original_has_more=source_has_more,
            byte_budget=payload_budget,
            truncated_by_bytes=truncated_by_bytes or is_preview,
        )
        if candidate_size <= payload_budget:
            selected = candidate_messages
            selected_candidate = message_candidate
            truncated_by_bytes = truncated_by_bytes or is_preview
            processed += 1
            if cursor_key is not None:
                processed_cursors.append(cursor_key)
            if is_preview:
                # A preview represents the page boundary. Continuing past it
                # would make ``has_more`` false even though full records were
                # omitted by the byte budget and would no longer be pageable.
                break
            continue

        # If at least one whole row already fits, stop at that cursor boundary.
        # A preview is reserved for the first row itself being larger than the
        # response budget; otherwise clients page normally for older/newer rows.
        if selected:
            break

        # The first fully projected row did not fit. Replace that boundary row
        # with a bounded detail reference, then shrink its text preview if
        # metadata is close to the minimum response budget.
        low = 0
        high = _CHAT_HISTORY_ENTRY_PREVIEW_BYTES
        fitting: tuple[list[dict[str, Any]], dict[str, Any]] | None = None
        while low <= high:
            preview_bytes = (low + high) // 2
            preview = (
                _chat_history_v2_preview(
                    projected_message,
                    session_key=session_key,
                    max_preview_bytes=preview_bytes,
                )
                if projected_message is not None
                else _chat_history_v2_probe_preview(
                    item,
                    session_key=session_key,
                    max_preview_bytes=preview_bytes,
                )
            )
            preview_messages, preview_candidate, preview_size = _chat_history_v2_with_message(
                base,
                selected=selected,
                message=preview,
                append=retain_earliest,
                original_message_count=len(items),
                original_has_more=source_has_more,
                byte_budget=payload_budget,
                truncated_by_bytes=True,
            )
            if preview_size <= payload_budget:
                fitting = preview_messages, preview_candidate
                low = preview_bytes + 1
            else:
                high = preview_bytes - 1
        if fitting is None:
            break
        selected, selected_candidate = fitting
        truncated_by_bytes = True
        processed += 1
        if cursor_key is not None:
            processed_cursors.append(cursor_key)
        break

    if processed < len(items):
        truncated_by_bytes = True
    selected_candidate["has_more"] = bool(source_has_more or processed < len(items))
    selected_candidate["truncated_by_bytes"] = truncated_by_bytes
    if skipped:
        selected_candidate["skipped_count"] = skipped
    if not selected and processed_cursors:
        first_cursor = processed_cursors[0]
        last_cursor = processed_cursors[-1]
        oldest = first_cursor if retain_earliest else last_cursor
        newest = last_cursor if retain_earliest else first_cursor
        selected_candidate["oldest_cursor"] = f"{oldest[0]}|{oldest[1]}"
        selected_candidate["newest_cursor"] = f"{newest[0]}|{newest[1]}"
    if selected_candidate["has_more"] and not summaries:
        selected_candidate["history_scope"] = "latest_window"

    selected_candidate["turn_outcomes"] = await _chat_history_v2_turn_outcomes_for_ids(
        ctx,
        _chat_history_v2_turn_ids(selected),
    )
    return _fit_chat_history_v2_payload(
        selected_candidate,
        session_key=session_key,
        byte_budget=payload_budget,
        retain_earliest=retain_earliest,
    )


async def _load_chat_history_v2_payload(
    raw_params: dict[str, Any],
    ctx: RpcContext,
    *,
    payload_budget: int,
) -> dict[str, Any]:
    """Read one stable cursor page, then project it outside the mutation lock."""

    return await _load_chat_history_v2_payload_unlocked(
        raw_params,
        ctx,
        payload_budget=payload_budget,
    )


async def _chat_history_turn_outcomes(
    ctx: RpcContext,
    session_key: str,
    entries: list[object],
) -> list[dict[str, Any]]:
    """Return typed outcomes only for explicit turn ids present in this page."""

    turn_ids = {
        str(turn_id)
        for entry in entries
        if isinstance((turn_context := getattr(entry, "turn_context", None)), dict)
        and isinstance((turn_id := turn_context.get("turn_id")), str)
        and turn_id
    }
    return await _chat_history_turn_outcomes_for_ids(ctx, session_key, turn_ids)


async def _chat_history_turn_outcomes_for_ids(
    ctx: RpcContext,
    session_key: str,
    turn_ids: set[str],
) -> list[dict[str, Any]]:
    """Return typed outcomes for an already-bounded set of turn ids."""

    if not turn_ids:
        return []
    storage = get_session_storage(getattr(ctx, "session_manager", None))
    exact_tasks = getattr(storage, "get_agent_tasks_by_ids", None)
    get_task = getattr(storage, "get_agent_task", None)
    list_tasks = getattr(storage, "list_agent_tasks", None)
    try:
        if callable(exact_tasks):
            rows = await exact_tasks(sorted(turn_ids))
        elif callable(get_task):
            rows = [
                row
                for turn_id in sorted(turn_ids)
                if (row := await get_task(turn_id)) is not None
            ]
        elif callable(list_tasks):
            rows = await list_tasks(session_key=session_key)
        else:
            return []
    except Exception:  # noqa: BLE001 - history remains readable without outcomes.
        log.warning(
            "chat.history.turn_outcomes_failed",
            session_key=session_key,
            exc_info=True,
        )
        return []

    outcomes: list[dict[str, Any]] = []
    for row in rows:
        task_id = getattr(row, "task_id", None)
        details = getattr(row, "details", None)
        details = details if isinstance(details, dict) else {}
        turn_id = details.get("turn_id") or task_id
        status = getattr(row, "status", None)
        status = str(getattr(status, "value", status) or "")
        outcome = details.get("turn_outcome")
        if not isinstance(outcome, dict):
            # Upgrade compatibility: older task rows predate typed outcomes.
            # Derive only from that row's own explicit terminal status; never
            # inspect neighboring transcript roles or repeated user messages.
            legacy_kind = {
                "succeeded": "completed",
                "failed": "failed",
                "cancelled": "interrupted",
                "timeout": "interrupted",
                "abandoned": "interrupted",
            }.get(status)
            if legacy_kind is None:
                continue
            outcome = {
                "kind": legacy_kind,
                "reason": status,
            }
        if (
            not isinstance(turn_id, str)
            or turn_id not in turn_ids
        ):
            continue
        outcomes.append(
            {
                "turn_id": turn_id,
                "task_id": task_id,
                "status": status,
                "started_at": getattr(row, "started_at", None),
                "finished_at": getattr(row, "finished_at", None),
                "outcome": dict(outcome),
            }
        )
    outcomes.sort(
        key=lambda item: (
            int(item.get("started_at") or 0),
            str(item.get("task_id") or ""),
        )
    )
    return outcomes


async def _chat_history_v2_turn_outcomes_for_ids(
    ctx: RpcContext,
    turn_ids: set[str],
) -> list[dict[str, Any]]:
    """Return outcomes without selecting unbounded task diagnostic columns."""

    if not turn_ids:
        return []
    storage = get_session_storage(getattr(ctx, "session_manager", None))
    getter = getattr(storage, "get_agent_task_outcome_metadata_by_ids", None)
    if not callable(getter):
        return []
    try:
        rows = await getter(sorted(turn_ids))
    except Exception:  # noqa: BLE001 - optional outcomes must not hide history.
        log.warning("chat.history.v2.turn_outcomes_failed", exc_info=True)
        return []

    outcomes: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        task_id = row.get("task_id")
        details = row.get("details")
        details = details if isinstance(details, dict) else {}
        turn_id = details.get("turn_id") or task_id
        if not isinstance(turn_id, str) or turn_id not in turn_ids:
            continue
        status = str(row.get("status") or "")
        outcome = details.get("turn_outcome")
        if not isinstance(outcome, dict):
            legacy_kind = {
                "succeeded": "completed",
                "failed": "failed",
                "cancelled": "interrupted",
                "timeout": "interrupted",
                "abandoned": "interrupted",
            }.get(status)
            if legacy_kind is None:
                continue
            outcome = {"kind": legacy_kind, "reason": status}
        outcomes.append(
            {
                "turn_id": turn_id,
                "task_id": task_id,
                "status": status,
                "started_at": row.get("started_at"),
                "finished_at": row.get("finished_at"),
                "outcome": dict(outcome),
            }
        )
    outcomes.sort(
        key=lambda item: (
            int(item.get("started_at") or 0),
            str(item.get("task_id") or ""),
        )
    )
    return outcomes


def _chat_history_cursor(entry: object | None) -> str | None:
    if entry is None:
        return None
    created_at = getattr(entry, "created_at", "")
    stable_id = getattr(entry, "id", None) or getattr(entry, "message_id", "")
    if created_at in {None, ""} or stable_id in {None, ""}:
        return None
    return f"{created_at}|{stable_id}"


def _chat_history_cursor_index(entries: list[object], cursor: object) -> int | None:
    raw = str(cursor or "").strip()
    if not raw:
        return None
    for idx, entry in enumerate(entries):
        if _chat_history_cursor(entry) == raw:
            return idx
    return None


def _chat_history_cursor_key(cursor: object) -> tuple[int, int] | None:
    raw = str(cursor or "").strip()
    if not raw or "|" not in raw:
        return None
    created_at, stable_id = raw.split("|", 1)
    try:
        created_at_value = int(created_at)
        stable_id_value = int(stable_id)
    except ValueError:
        return None
    sqlite_max = (1 << 63) - 1
    if not 0 <= created_at_value <= sqlite_max:
        return None
    if not 1 <= stable_id_value <= sqlite_max:
        return None
    return created_at_value, stable_id_value


def _chat_history_page(
    entries: list[object],
    *,
    limit: int,
    before: object = None,
    after: object = None,
) -> tuple[list[object], bool]:
    if not entries:
        return [], False
    before_idx = _chat_history_cursor_index(entries, before)
    if before_idx is not None:
        end = before_idx
        start = max(0, end - limit)
        return entries[start:end], start > 0

    after_idx = _chat_history_cursor_index(entries, after)
    if after_idx is not None:
        start = min(len(entries), after_idx + 1)
        end = min(len(entries), start + limit)
        return entries[start:end], end < len(entries)

    if len(entries) <= limit:
        return entries, False
    return entries[-limit:], True


def _session_summary_to_chat_payload(summary: object) -> dict[str, Any]:
    return {
        "id": getattr(summary, "id", None),
        "compaction_id": getattr(summary, "compaction_id", None),
        "compaction_index": getattr(summary, "compaction_index", None),
        "trigger_reason": getattr(summary, "trigger_reason", None),
        "summary_text": getattr(summary, "summary_text", "") or "",
        "summary_format": getattr(summary, "summary_format", "") or "",
        "coverage_status": getattr(summary, "coverage_status", "") or "",
        "removed_count": getattr(summary, "removed_count", None),
        "kept_count": getattr(summary, "kept_count", None),
        "covered_through_id": getattr(summary, "covered_through_id", None),
        "created_at": getattr(summary, "created_at", None),
    }


def _annotate_transcript_attachment_downloads(
    messages: list[dict[str, Any]],
    *,
    session_key: str,
) -> list[dict[str, Any]]:
    session_qs = quote(session_key, safe="")
    for msg in messages:
        attachments = msg.get("attachments")
        if not isinstance(attachments, list):
            continue
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            sha = attachment.get("sha256_ref")
            if not isinstance(sha, str) or not sha:
                continue
            if attachment.get("download_url"):
                continue
            name = str(attachment.get("name") or "attachment")
            mime = str(attachment.get("mime") or attachment.get("type") or "")
            attachment["download_url"] = (
                f"/api/v1/attachments/{quote(sha, safe='')}?sessionKey={session_qs}"
                f"&name={quote(name, safe='')}&mime={quote(mime, safe='')}"
            )
    return messages


def _canonical_page_parts(page: object) -> tuple[list[object], bool, bool]:
    if isinstance(page, dict):
        entries = page.get("entries")
        has_more = page.get("has_more", False)
        canonical_complete = page.get("canonical_complete", True)
    elif isinstance(page, tuple):
        entries = page[0] if page else None
        has_more = page[1] if len(page) > 1 else False
        canonical_complete = page[2] if len(page) > 2 else True
    else:
        entries = getattr(page, "entries", None)
        has_more = getattr(page, "has_more", False)
        canonical_complete = getattr(page, "canonical_complete", True)
    if entries is None:
        raise TypeError("canonical transcript page is missing entries")
    return list(entries), bool(has_more), bool(canonical_complete)


async def _load_chat_history_page(
    mgr: object,
    session_key: str,
    *,
    limit: int,
    before: object = None,
    after: object = None,
    include_canonical: bool,
) -> tuple[list[object], bool, bool, bool]:
    if include_canonical:
        page_getter = getattr(mgr, "get_canonical_transcript_page", None)
        if callable(page_getter):
            try:
                page = await page_getter(
                    session_key,
                    limit=limit,
                    before=_chat_history_cursor_key(before),
                    after=_chat_history_cursor_key(after),
                )
                entries, has_more, canonical_complete = _canonical_page_parts(page)
                return entries, has_more, True, canonical_complete
            except StorageBusyError:
                raise
            except Exception:  # noqa: BLE001 - fall back to active transcript
                pass
        else:
            getter = getattr(mgr, "get_canonical_transcript", None)
            if callable(getter):
                try:
                    transcript = list(await getter(session_key))
                    entries, has_more = _chat_history_page(
                        transcript,
                        limit=limit,
                        before=before,
                        after=after,
                    )
                    return entries, has_more, True, True
                except StorageBusyError:
                    raise
                except Exception:  # noqa: BLE001 - fall back to active transcript
                    pass
    transcript_getter = getattr(mgr, "get_transcript", None)
    if not callable(transcript_getter):
        return [], False, False, False
    transcript = await transcript_getter(session_key)
    entries, has_more = _chat_history_page(
        list(transcript or []),
        limit=limit,
        before=before,
        after=after,
    )
    return entries, has_more, False, False


async def _chat_history_summaries(
    mgr: object,
    session_key: str,
    *,
    include_summaries: bool,
) -> list[dict[str, Any]]:
    """Return requested summaries without letting lock contention hide history."""

    if not include_summaries:
        return []
    getter = getattr(mgr, "get_summaries", None)
    if not callable(getter):
        return []
    try:
        with bounded_interactive_storage_reads():
            summaries = await getter(session_key)
    except StorageBusyError:
        # The message page is already available. Let callers retry the optional
        # summary metadata instead of converting a useful history response into
        # STORAGE_BUSY.
        return []
    except Exception:  # noqa: BLE001 - summaries remain optional display metadata
        return []
    return [_session_summary_to_chat_payload(summary) for summary in summaries or []]


def _effective_compaction_model(session: object | None) -> str | None:
    if session is None:
        return None
    return getattr(session, "model_override", None) or getattr(session, "model", None)


def _resolve_compaction_provider(ctx: RpcContext, session: object | None) -> object | None:
    selector = getattr(ctx, "provider_selector", None)
    if selector is None:
        return None

    resolved_selector = selector
    clone = getattr(selector, "clone", None)
    if callable(clone):
        try:
            resolved_selector = clone()
        except Exception:  # noqa: BLE001
            resolved_selector = selector

    model = _effective_compaction_model(session)
    if model and resolved_selector is not selector:
        override = getattr(resolved_selector, "override_model", None)
        if callable(override):
            try:
                override(model)
            except Exception:  # noqa: BLE001
                pass

    resolver = getattr(resolved_selector, "resolve", None)
    if not callable(resolver):
        return None
    try:
        return cast(object | None, resolver())
    except Exception:  # noqa: BLE001
        return None


async def _build_context_overflow_compaction_config(ctx: RpcContext, session_key: str):
    session = None
    storage = getattr(getattr(ctx, "session_manager", None), "_storage", None)
    if storage is not None:
        try:
            session = await storage.get_session(session_key)
        except Exception:  # noqa: BLE001
            session = None
    return build_compaction_config_from_provider(
        _resolve_compaction_provider(ctx, session),
        model_override=_effective_compaction_model(session),
        compaction_config=getattr(getattr(ctx, "config", None), "compaction", None),
    )


async def _enforce_context_overflow(
    ctx: RpcContext,
    session_key: str,
    message: str,
) -> dict | None:
    """Apply the configured context-overflow policy before a turn runs.

    Returns a stable error envelope when the policy is REFUSE and the
    payload exceeds the budget; returns ``None`` for every other path
    (policy consults pass, HARD_TRUNCATE dropped some history in place,
    AUTO_SUMMARIZE kicked off a compaction). The caller short-circuits
    on a non-None return.
    """

    config = ctx.config if isinstance(ctx.config, GatewayConfig) else GatewayConfig()

    transcript: list = []
    if ctx.session_manager is not None:
        try:
            transcript = list(await ctx.session_manager.get_transcript(session_key))
        except Exception:  # noqa: BLE001 — missing transcript just means "no history"
            transcript = []

    # Per-session context-budget overrides are independent from runtime/request
    # timeout resolution, which happens in TurnRunner.
    # A session-scoped context_budget_tokens override is supported via
    # ctx.session_manager.get_config(session_key) if present.
    budget_override = None
    policy_override = None
    if ctx.session_manager is not None and hasattr(ctx.session_manager, "get_session_config"):
        try:
            session_cfg = await ctx.session_manager.get_session_config(session_key)
            if session_cfg is not None:
                budget_override = getattr(session_cfg, "context_budget_tokens", None)
                policy_override = getattr(session_cfg, "context_overflow_policy", None)
        except Exception:  # noqa: BLE001
            pass

    from opensquilla.engine.usage_accounting import bind_usage_accounting_scope
    from opensquilla.gateway.usage_ledger_runtime import build_session_usage_scope

    usage_scope = await build_session_usage_scope(
        getattr(ctx, "usage_event_sink", None),
        ctx.session_manager,
        session_key,
        run_kind="session_compaction",
    )
    root_operation_id = new_compaction_id()
    provider_request_correlation = None
    if not provider_request_correlation_disabled(config=config):
        try:
            session = await ctx.session_manager.get_session(session_key)
        except Exception:  # noqa: BLE001 - observability is best-effort
            session = None
        durable_session_id = getattr(session, "session_id", None)
        if isinstance(durable_session_id, str) and durable_session_id:
            provider_request_correlation = ProviderRequestCorrelation(
                session_id=durable_session_id,
                turn_id=root_operation_id,
                execution_id=uuid4().hex,
                call_kind="auxiliary.compaction",
            )
    with bind_usage_accounting_scope(usage_scope):
        outcome = await apply_context_overflow_policy(
            config=config,
            message=message,
            transcript=transcript,
            session_key=session_key,
            session_manager=ctx.session_manager,
            compaction_config=await _build_context_overflow_compaction_config(
                ctx, session_key
            ),
            flush_service=getattr(ctx, "flush_service", None),
            compaction_marker=getattr(ctx, "turn_runner", None),
            policy_override=policy_override,
            budget_override=budget_override,
            provider_request_correlation=provider_request_correlation,
            root_operation_id=root_operation_id,
        )

    if outcome.refusal is not None:
        log.warning(
            "chat_send.context_overflow_refused",
            session_key=session_key,
            estimated_tokens=outcome.estimated_tokens,
            budget_tokens=outcome.budget_tokens,
        )
        return outcome.refusal

    if outcome.compacted_this_turn:
        marker = getattr(ctx, "turn_runner", None)
        mark = getattr(marker, "mark_compacted_this_turn", None)
        if callable(mark):
            mark(session_key)

    return None


@_d.method("chat.send", scope="operator.write")
async def _handle_chat_send(params: dict | None, ctx: RpcContext) -> dict:
    if not isinstance(params, dict) or "message" not in params:
        raise ValueError("params.message is required")

    message = params["message"]
    session_key = _canonical_webchat_session_key(params.get("sessionKey"))
    agent_id = parse_agent_id(session_key)
    initial_collaboration_mode = _requested_initial_collaboration_mode(params)

    # Fresh-WebUI / smoke path: when no session manager is wired (webui
    # simulator, dispatcher-only boot), instant-accept without kicking off a
    # turn. This matches the roundtrip the WebUI observes on first paint
    # before the sessions engine is attached.
    if ctx.session_manager is None:
        if initial_collaboration_mode is not None:
            raise RpcUnavailableError(
                "Initial collaboration mode requires atomic turn acceptance"
            )
        return {"ok": True, "sessionKey": session_key, "instant_accept": True}

    mgr = _require_chat_session_manager(ctx)
    intent = params.get("intent")
    intent_was_provided = intent is not None
    requested_intent = intent
    if intent is None and (
        isinstance(params.get("workspaceId"), str)
        or isinstance(params.get("workspace_id"), str)
    ):
        # A project draft is always a first-turn request. Keeping this intent
        # stable on retries lets sessions.send consult the durable ingress
        # receipt before an already-created session can change the strategy.
        intent = "new_chat"

    # WebChat must accept the turn even when existing history is oversized.
    # Context shaping happens inside TurnRunner so it can produce a request-scoped
    # sendable view instead of making the RPC layer a terminal overflow gate.

    try:
        if intent != "new_chat":
            # Detect a draft without creating it yet. sessions.send folds the
            # session row into the same durable acceptance transaction as the
            # first message/task/receipt.
            storage = getattr(mgr, "storage", None) or getattr(mgr, "_storage", None)
            get_session = getattr(storage, "get_session", None)
            if callable(get_session):
                try:
                    if await get_session(session_key) is None:
                        intent = "new_chat"
                except Exception as exc:
                    raise RpcUnavailableError(
                        f"Failed to inspect chat session: {exc}"
                    ) from exc
            else:
                # Compatibility for minimal test/simulator managers that do
                # not expose storage: retain the historical initializer.
                try:
                    await mgr.get_or_create(
                        session_key=session_key,
                        agent_id=agent_id,
                        display_name="WebChat",
                    )
                except Exception as exc:
                    raise RpcUnavailableError(
                        f"Failed to initialize chat session: {exc}"
                    ) from exc

        from opensquilla.gateway.rpc_sessions import _handle_sessions_send

        incoming_source = params.get("_source")
        if not isinstance(incoming_source, dict):
            incoming_source = {}

        elevated_hint = incoming_source.get("elevated")
        run_mode_hint = incoming_source.get("runMode") or incoming_source.get("run_mode")
        attachments = params.get("attachments")
        extra: dict = {}
        for source_key, target_key in (
            ("noMemoryCapture", "noMemoryCapture"),
            ("no_memory_capture", "no_memory_capture"),
            ("inputProvenance", "inputProvenance"),
            ("input_provenance", "input_provenance"),
            ("inputProvenanceKind", "inputProvenanceKind"),
            ("input_provenance_kind", "input_provenance_kind"),
            ("provenance_kind", "provenance_kind"),
            ("runKind", "runKind"),
            ("run_kind", "run_kind"),
            ("queueMode", "queueMode"),
            ("queue_mode", "queue_mode"),
            ("forkBeforeMessageId", "forkBeforeMessageId"),
            ("fork_before_message_id", "fork_before_message_id"),
            ("clientRequestId", "clientRequestId"),
            ("client_request_id", "client_request_id"),
            ("clientMessageId", "clientMessageId"),
            ("client_message_id", "client_message_id"),
            ("surfaceId", "surfaceId"),
            ("surface_id", "surface_id"),
            ("workspaceId", "workspaceId"),
            ("workspace_id", "workspace_id"),
        ):
            if source_key in params:
                extra[target_key] = params[source_key]
        send_params = sessions_send_params(
            ChatSendRequest(
                session_key=session_key,
                message=message,
                attachments=attachments if isinstance(attachments, list) else [],
                display_text=params.get("displayText") if "displayText" in params else None,
                intent=cast(str, intent) if intent is not None else None,
                extra=extra,
            ),
            chat_source_metadata(
                caller_kind="web",
                channel_kind="webchat",
                channel_id=f"webchat:{session_key}",
                sender_id=ctx.principal.role,
                source_kind="webui",
                source_name="WebChat",
                elevated=elevated_hint if isinstance(elevated_hint, str) else None,
                run_mode=run_mode_hint if isinstance(run_mode_hint, str) else None,
            ),
        )
        # Keep the public handler params free of fingerprint-control fields.
        # The logical request fingerprint uses the caller's original intent,
        # while the actual send may use the internal ``continue`` ->
        # ``new_chat`` strategy to create a first session atomically.
        fingerprint_params = dict(send_params)
        if intent_was_provided:
            fingerprint_params["intent"] = requested_intent
        else:
            fingerprint_params.pop("intent", None)
        if initial_collaboration_mode is not None:
            # Both public spellings represent the same logical request. Keep
            # one canonical field in the durable idempotency fingerprint.
            fingerprint_params["initialCollaborationMode"] = (
                initial_collaboration_mode
            )
        result = await _handle_sessions_send(
            send_params,
            ctx,
            fingerprint_params=fingerprint_params,
            initial_collaboration_mode=initial_collaboration_mode,
        )
        result_session_key = result.get("sessionKey") or result.get("key") or session_key
        return {"ok": True, "sessionKey": result_session_key, **result}
    except Exception:
        marker = getattr(ctx, "turn_runner", None)
        clear = getattr(marker, "clear_compacted_this_turn", None)
        if callable(clear):
            clear(session_key)
        raise


@_d.method("chat.abort", scope="operator.write")
async def _handle_chat_abort(params: dict | None, ctx: RpcContext) -> dict:
    raw_params = params or {}
    session_key = _canonical_webchat_session_key(raw_params.get("sessionKey"))
    # Fresh-WebUI / smoke path: abort always returns an ok envelope keyed by
    # sessionKey, regardless of whether a live task exists to cancel.
    if ctx.session_manager is None:
        return {"ok": True, "sessionKey": session_key, "aborted": False}
    _require_chat_session_manager(ctx)
    from opensquilla.gateway.rpc_sessions import _handle_sessions_abort

    abort_params = {
        "key": session_key,
        "source": raw_params.get("source") or "webui_abort",
    }
    task_id = raw_params.get("taskId") or raw_params.get("task_id")
    source = str(abort_params["source"])
    if source != "webui_stop" and isinstance(task_id, str) and task_id.strip():
        abort_params["task_id"] = task_id.strip()
    result = await _handle_sessions_abort(
        abort_params,
        ctx,
    )
    return {"sessionKey": session_key, **result}


@_d.method("chat.history", scope="operator.read")
async def _handle_chat_history(params: dict | None, ctx: RpcContext) -> dict:
    raw_params = params or {}
    session_key = _canonical_webchat_session_key(raw_params.get("sessionKey"))
    limit = _normalize_chat_history_limit(raw_params.get("limit"))
    before = raw_params.get("before")
    after = raw_params.get("after")
    include_canonical = _chat_history_bool(
        raw_params.get("includeCanonical"),
        default=True,
    )
    include_summaries = _chat_history_bool(
        raw_params.get("includeSummaries"),
        default=True,
    )

    mgr = _require_chat_session_manager(ctx)

    async def _load_page() -> tuple[list[object], bool, bool, bool]:
        return await _load_chat_history_page(
            mgr,
            session_key,
            limit=limit,
            before=before,
            after=after,
            include_canonical=include_canonical,
        )

    try:
        with bounded_interactive_storage_reads():
            history_lock = get_session_lock(ctx.turn_runner, session_key)
            if history_lock is None:
                page_entries, has_more, canonical_available, canonical_complete = (
                    await _load_page()
                )
            else:
                # Canonical reads and compaction rewrites share one aiosqlite
                # connection.  SQLite statements are snapshots, but a statement on
                # that same connection can still observe the connection's own
                # uncommitted archive/delete/reinsert work.  Use the short session
                # mutation lock so the page and its coverage metadata are read only
                # before or after a rewrite, never from its intermediate state.
                started = time.monotonic()
                acquired = False
                try:
                    try:
                        async with asyncio.timeout(_CHAT_HISTORY_LOCK_BUDGET_SECONDS):
                            await history_lock.acquire()
                    except TimeoutError as exc:
                        raise StorageBusyError(
                            "chat.history",
                            waited_ms=max(0, int((time.monotonic() - started) * 1000)),
                            retry_after_ms=_CHAT_HISTORY_RETRY_AFTER_MS,
                            stage="lock_acquire",
                            resource="session_mutation_lock",
                        ) from exc
                    acquired = True
                    page_entries, has_more, canonical_available, canonical_complete = (
                        await _load_page()
                    )
                finally:
                    if acquired:
                        history_lock.release()
    except KeyError:
        if _is_webchat_session_key(session_key):
            return _empty_chat_history_payload(limit)
        raise
    summaries = await _chat_history_summaries(
        mgr,
        session_key,
        include_summaries=include_summaries,
    )
    if summaries:
        history_scope = "compacted"
    elif has_more:
        history_scope = "latest_window"
    else:
        history_scope = "complete"

    messages = transcript_entries_to_chat_messages(page_entries, limit=None)
    turn_outcomes = await _chat_history_turn_outcomes(
        ctx,
        session_key,
        page_entries,
    )
    return {
        "messages": _annotate_transcript_attachment_downloads(
            messages,
            session_key=session_key,
        ),
        "has_more": has_more,
        "oldest_cursor": _chat_history_cursor(page_entries[0]) if page_entries else None,
        "newest_cursor": _chat_history_cursor(page_entries[-1]) if page_entries else None,
        "history_scope": history_scope,
        "loaded_count": len(page_entries),
        "page_size": limit,
        "canonical_available": canonical_available,
        "canonical_complete": canonical_complete,
        "compaction_summaries": summaries,
        "turn_outcomes": turn_outcomes,
    }


@_d.method("chat.history.v2", scope="operator.read")
async def _handle_chat_history_v2(
    params: dict | None,
    ctx: RpcContext,
) -> BudgetedRpcResult:
    """Return a byte-budgeted history page without changing the legacy RPC."""

    raw_params = dict(params or {})
    byte_budget = _chat_history_v2_response_budget(raw_params)
    payload_budget = byte_budget - _CHAT_HISTORY_V2_ENVELOPE_RESERVE_BYTES
    payload = await _load_chat_history_v2_payload(
        raw_params,
        ctx,
        payload_budget=payload_budget,
    )
    # The nested fitter budgets the payload. The public field describes the
    # caller-declared limit for the complete ResFrame, which the dispatcher
    # verifies after the actual request id is known.
    payload["byte_budget"] = byte_budget
    return BudgetedRpcResult(payload, byte_budget)


HistoryDetailFieldReader = Callable[[str, int, int], Awaitable[bytes]]


async def _write_history_detail_text(
    writer: HistoryDetailWriter,
    value: str,
) -> None:
    for start in range(0, len(value), 64 * 1024):
        await writer.write(value[start : start + 64 * 1024].encode("utf-8"))


async def _write_history_detail_json_string(
    writer: HistoryDetailWriter,
    value: str,
) -> None:
    await writer.write(b'"')
    for start in range(0, len(value), 64 * 1024):
        encoded = json.dumps(
            value[start : start + 64 * 1024],
            ensure_ascii=False,
        )[1:-1].encode("utf-8")
        await writer.write(encoded)
    await writer.write(b'"')


async def _write_history_detail_json(
    writer: HistoryDetailWriter,
    value: object,
) -> None:
    """Compact-encode JSON without allocating a second giant byte string."""

    if isinstance(value, str):
        await _write_history_detail_json_string(writer, value)
        return
    if isinstance(value, dict):
        await writer.write(b"{")
        for index, (key, item) in enumerate(value.items()):
            if index:
                await writer.write(b",")
            await _write_history_detail_json_string(writer, str(key))
            await writer.write(b":")
            await _write_history_detail_json(writer, item)
        await writer.write(b"}")
        return
    if isinstance(value, list | tuple):
        await writer.write(b"[")
        for index, item in enumerate(value):
            if index:
                await writer.write(b",")
            await _write_history_detail_json(writer, item)
        await writer.write(b"]")
        return
    await writer.write(_compact_json_bytes(value))


async def _read_history_detail_field(
    reader: HistoryDetailFieldReader,
    *,
    field: str,
    total: int,
) -> bytes:
    """Read one already-size-bounded storage field in transport-sized chunks."""

    if total <= 0:
        return b""
    chunks = bytearray()
    offset = 0
    while offset < total:
        chunk = await reader(
            field,
            offset,
            min(_CHAT_HISTORY_DETAIL_STORAGE_CHUNK_BYTES, total - offset),
        )
        if not chunk:
            raise KeyError("History entry changed while its detail was being read")
        chunks.extend(chunk)
        offset += len(chunk)
    return bytes(chunks)


async def _copy_history_detail_field(
    writer: HistoryDetailWriter,
    reader: HistoryDetailFieldReader,
    *,
    field: str,
    total: int,
) -> None:
    offset = 0
    while offset < total:
        chunk = await reader(
            field,
            offset,
            min(_CHAT_HISTORY_DETAIL_STORAGE_CHUNK_BYTES, total - offset),
        )
        if not chunk:
            raise KeyError("History entry changed while its detail was being read")
        await writer.write(chunk)
        offset += len(chunk)


async def _write_history_detail_storage_json_string(
    writer: HistoryDetailWriter,
    reader: HistoryDetailFieldReader,
    *,
    field: str,
    total: int,
) -> None:
    """JSON-escape one SQLite TEXT field without materializing it in Python."""

    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    await writer.write(b'"')
    offset = 0
    while offset < total:
        chunk = await reader(
            field,
            offset,
            min(_CHAT_HISTORY_DETAIL_STORAGE_CHUNK_BYTES, total - offset),
        )
        if not chunk:
            raise KeyError("History entry changed while its detail was being read")
        offset += len(chunk)
        decoded = decoder.decode(chunk, final=offset == total)
        if decoded:
            escaped = json.dumps(decoded, ensure_ascii=False)[1:-1].encode("utf-8")
            await writer.write(escaped)
    if total == 0:
        decoder.decode(b"", final=True)
    await writer.write(b'"')


async def _build_exact_history_detail(
    writer: HistoryDetailWriter,
    *,
    storage: object,
    session_id: str,
    session_key: str,
    cursor: str,
    cursor_key: tuple[int, int],
    field: str,
) -> None:
    getter = getattr(storage, "get_canonical_transcript_entry_by_cursor", None)
    if not callable(getter):
        raise RpcUnavailableError("Canonical history entry lookup is unavailable")
    with bounded_interactive_storage_reads():
        entry = await getter(session_id, cursor=cursor_key)
    if entry is None:
        raise KeyError(f"History entry not found: {cursor}")
    messages = transcript_entries_to_chat_messages([entry], limit=None)
    if not messages:
        raise KeyError(f"History entry has no visible projection: {cursor}")
    projected = _annotate_transcript_attachment_downloads(
        [dict(messages[0])],
        session_key=session_key,
    )[0]
    if _chat_history_v2_message_cursor(projected) != cursor:
        raise KeyError(f"History entry cursor changed: {cursor}")
    if field == "message":
        await _write_history_detail_json(writer, projected)
    else:
        await _write_history_detail_text(writer, str(projected.get("text") or ""))


async def _build_streamed_plain_history_message(
    writer: HistoryDetailWriter,
    *,
    reader: HistoryDetailFieldReader,
    metadata: object,
    session_key: str,
    cursor: str,
    cursor_key: tuple[int, int],
) -> None:
    """Project the common giant-plain-text row without loading its content."""

    field_bytes = getattr(metadata, "field_bytes", {})

    async def _text(field: str) -> str | None:
        total = int(field_bytes.get(field, 0) or 0)
        if not total:
            return None
        raw = await _read_history_detail_field(reader, field=field, total=total)
        return raw.decode("utf-8")

    async def _json(field: str) -> object:
        raw = await _text(field)
        return json.loads(raw) if raw else None

    message_id = await _text("message_id")
    role = await _text("role") or "unknown"
    reasoning_content = await _text("reasoning_content")
    turn_context = await _json("turn_context")
    turn_usage = await _json("turn_usage")
    tool_calls = await _json("tool_calls")
    entry = SimpleNamespace(
        id=cursor_key[1],
        message_id=message_id,
        role=role,
        content="",
        created_at=cursor_key[0],
        provenance_kind=await _text("provenance_kind"),
        provenance_source_session_key=await _text("provenance_source_session_key"),
        provenance_source_tool=await _text("provenance_source_tool"),
        reasoning_content=reasoning_content,
        turn_context=turn_context,
        turn_usage=turn_usage,
        tool_calls=tool_calls,
    )
    messages = transcript_entries_to_chat_messages([entry], limit=None)
    if not messages:
        raise KeyError(f"History entry has no visible projection: {cursor}")
    projected = _annotate_transcript_attachment_downloads(
        [dict(messages[0])],
        session_key=session_key,
    )[0]
    if _chat_history_v2_message_cursor(projected) != cursor:
        raise KeyError(f"History entry cursor changed: {cursor}")

    await writer.write(b"{")
    for index, (key, value) in enumerate(projected.items()):
        if index:
            await writer.write(b",")
        await _write_history_detail_json_string(writer, key)
        await writer.write(b":")
        if key == "text":
            await _write_history_detail_storage_json_string(
                writer,
                reader,
                field="content",
                total=int(field_bytes.get("content", 0) or 0),
            )
        else:
            await _write_history_detail_json(writer, value)
    await writer.write(b"}")


@_d.method("chat.history.entry.v1", scope="operator.read")
async def _handle_chat_history_entry_v1(params: dict | None, ctx: RpcContext) -> dict:
    """Return one canonical projected history entry as bounded base64 chunks."""

    raw_params = params or {}
    session_key = _canonical_webchat_session_key(raw_params.get("sessionKey"))
    raw_cursor = raw_params.get("cursor")
    cursor = str(raw_cursor or "").strip()
    cursor_key = _chat_history_cursor_key(cursor)
    if cursor_key is None:
        raise ValueError("cursor must be a stable created_at|transcript_id value")
    chunk_bytes = _chat_history_entry_chunk_bytes(raw_params)
    offset = _bounded_int_param(
        raw_params.get("offset"),
        name="offset",
        default=0,
        minimum=0,
        maximum=(1 << 63) - 1,
    )
    field = raw_params.get("field", "message")
    if field not in {"message", "text"}:
        raise ValueError("field must be message or text")

    mgr = _require_chat_session_manager(ctx)
    storage = get_session_storage(mgr)
    session_getter = getattr(storage, "get_session", None)
    if not callable(session_getter):
        raise RpcUnavailableError("Canonical history session lookup is unavailable")
    with bounded_interactive_storage_reads():
        session = await session_getter(session_key)
    if session is None:
        raise KeyError(f"Session not found: {session_key}")
    session_id = str(getattr(session, "session_id", "") or "")
    metadata_getter = getattr(storage, "get_canonical_transcript_detail_metadata", None)
    field_reader = getattr(storage, "read_canonical_transcript_detail_field_chunk", None)
    if not callable(metadata_getter) or not callable(field_reader):
        raise RpcUnavailableError("Canonical history detail streaming is unavailable")
    with bounded_interactive_storage_reads():
        metadata = await metadata_getter(session_id, cursor=cursor_key)
    if metadata is None:
        raise KeyError(f"History entry not found: {cursor}")

    field_bytes = getattr(metadata, "field_bytes", {})
    if not isinstance(field_bytes, dict):
        raise RpcUnavailableError("Canonical history detail metadata is unavailable")

    async def _read_field(name: str, start: int, size: int) -> bytes:
        with bounded_interactive_storage_reads():
            return cast(
                bytes,
                await field_reader(
                    session_id,
                    cursor=cursor_key,
                    field=name,
                    offset=start,
                    max_bytes=size,
                ),
            )

    async def _build(writer: HistoryDetailWriter) -> None:
        content_bytes = int(field_bytes.get("content", 0) or 0)
        stored_bytes = sum(max(0, int(size or 0)) for size in field_bytes.values())
        non_content_too_large = any(
            int(size or 0) > _CHAT_HISTORY_DETAIL_METADATA_FIELD_MAX_BYTES
            for name, size in field_bytes.items()
            if name != "content"
        )
        prefix = str(getattr(metadata, "content_prefix", None) or "")
        prefix_bytes = len(prefix.encode("utf-8"))
        classification_uncertain = (
            content_bytes > prefix_bytes and not prefix.strip()
        )
        role_bytes = int(field_bytes.get("role", 0) or 0)
        if role_bytes > _CHAT_HISTORY_DETAIL_ROLE_MAX_BYTES:
            raise RpcHandlerError(
                "HISTORY_DETAIL_PROJECTION_TOO_LARGE",
                "History detail role metadata exceeds the bounded projection limit.",
                details={"cursor": cursor},
            )
        role = (
            await _read_history_detail_field(
                _read_field,
                field="role",
                total=role_bytes,
            )
        ).decode("utf-8")
        requires_normalized_projection = (
            role == "user"
            or prefix.startswith("{")
            or prefix.lstrip().startswith("[ContentBlock")
            or classification_uncertain
            or non_content_too_large
        )
        if (
            content_bytes <= _CHAT_HISTORY_DETAIL_STREAM_CONTENT_THRESHOLD_BYTES
            or requires_normalized_projection
        ):
            if stored_bytes > _CHAT_HISTORY_DETAIL_EXACT_ROW_BYTES:
                raise RpcHandlerError(
                    "HISTORY_DETAIL_PROJECTION_TOO_LARGE",
                    "This legacy history entry cannot be normalized without an "
                    "unbounded projection.",
                    details={"cursor": cursor},
                )
            await _build_exact_history_detail(
                writer,
                storage=storage,
                session_id=session_id,
                session_key=session_key,
                cursor=cursor,
                cursor_key=cursor_key,
                field=str(field),
            )
            return
        if field == "text":
            await _copy_history_detail_field(
                writer,
                _read_field,
                field="content",
                total=content_bytes,
            )
            return
        await _build_streamed_plain_history_message(
            writer,
            reader=_read_field,
            metadata=metadata,
            session_key=session_key,
            cursor=cursor,
            cursor_key=cursor_key,
        )

    cache_key = (
        session_id,
        int(getattr(session, "epoch", 0) or 0),
        cursor,
        field,
    )
    try:
        chunk = await _HISTORY_ENTRY_SPOOL.read_chunk(
            cache_key,
            offset=offset,
            max_bytes=chunk_bytes,
            builder=_build,
        )
    except HistoryDetailEntryTooLargeError as exc:
        raise RpcHandlerError(
            "HISTORY_DETAIL_TOO_LARGE",
            "The projected history detail exceeds the server's bounded spool limit.",
            details={"cursor": cursor},
        ) from exc
    except HistoryDetailCapacityError as exc:
        raise RpcHandlerError(
            "HISTORY_DETAIL_BUSY",
            "Too many history details are being prepared; retry shortly.",
            retryable=True,
        ) from exc
    except HistoryDetailStorageError as exc:
        raise RpcHandlerError(
            "HISTORY_DETAIL_STORAGE_UNAVAILABLE",
            "Unable to prepare the requested history detail.",
            retryable=True,
        ) from exc
    except HistoryDetailSpoolError as exc:
        raise RpcHandlerError(
            "HISTORY_DETAIL_UNAVAILABLE",
            "History detail preparation was interrupted; retry shortly.",
            retryable=True,
        ) from exc
    except KeyError as exc:
        raise RpcHandlerError(
            "HISTORY_CURSOR_INVALIDATED",
            "The history entry changed while its detail was being prepared.",
            details={"cursor": cursor},
            retryable=True,
        ) from exc

    content_type = (
        "application/json" if field == "message" else "text/plain; charset=utf-8"
    )
    return {
        "session_key": session_key,
        "cursor": cursor,
        "encoding": "base64",
        "field": field,
        "content_type": content_type,
        "chunk_base64": base64.b64encode(chunk.data).decode("ascii"),
        "offset": chunk.offset,
        "next": chunk.next_offset,
        "total": chunk.total,
        "sha256": chunk.sha256,
    }


def _clarify_fields_to_text(fields: dict[str, object]) -> str:
    """Serialise a clarify-form submission into a ``key: value\\n`` reply.

    The synthetic message is fed back through ``chat.send`` so it
    traverses the regular meta-resolution pipeline:
      peek_awaiting → parse_clarify_reply (key:value mode) →
      try_claim_resume → DAG continues.

    Bools are rendered as ``true``/``false``; everything else uses
    Python's natural string representation. Empty / None values are
    skipped — they signal "optional field omitted".
    """
    lines: list[str] = []
    for key, value in fields.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
        lines.append(f"{key}: {rendered}")
    return "\n".join(lines)


@_d.method("chat.clarify_submit", scope="operator.write")
async def _handle_chat_clarify_submit(params: dict | None, ctx: RpcContext) -> dict:
    """Accept a structured clarify-form submission from a Web UI surface.

    Params:
      ``sessionKey``  (str)  — same WebChat session that triggered the pause
      ``fields``      (dict) — ``{field_name: value}`` collected by the form
      ``run_id``      (str, optional) — awaiting run id for trace/log only;
                                          the awaiting branch in meta_resolution
                                          uses ``session_key`` for the CAS

    A request carrying ``request_id`` resolves the exact deferred tool call and
    continues its existing turn. Legacy Meta clarifications have no request id;
    those remain a cross-turn protocol and are fed through ``chat.send``.
    """
    if not isinstance(params, dict):
        raise ValueError("params required: sessionKey, fields")
    fields = params.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("params.fields must be a non-empty mapping")

    session_key = _canonical_webchat_session_key(params.get("sessionKey"))
    raw_request_id = params.get("request_id", params.get("requestId"))
    if raw_request_id is not None:
        request_id = str(raw_request_id).strip()
        if not request_id:
            raise ValueError("params.request_id must be a non-empty string")
        task_runtime = getattr(ctx, "task_runtime", None)
        resolve_user_input = getattr(task_runtime, "resolve_user_input", None)
        if not callable(resolve_user_input):
            raise RpcUnavailableError(
                "Deferred user-input resolution is not available"
            )
        result = await resolve_user_input(
            session_key=session_key,
            request_id=request_id,
            fields=fields,
        )
        log.info(
            "chat.clarify_submit.deferred",
            session_key=session_key,
            request_id=request_id,
            field_count=len(fields),
            replayed=bool(result.get("replayed")),
        )
        return {"sessionKey": session_key, **result}

    text = _clarify_fields_to_text(fields)

    run_id = params.get("run_id")
    log.info(
        "chat.clarify_submit.params",
        session_key=session_key,
        field_count=len(fields),
        run_id=run_id if isinstance(run_id, str) and run_id else None,
    )

    send_params: dict = {
        "message": text,
        "sessionKey": session_key,
        # meta_resolution's awaiting branch keys off session_key, not
        # intent — so we deliberately stay on the default "continue"
        # intent (SessionIntent enum rejects unknown values). The
        # provenance tag is the observability hook for distinguishing
        # form submits from typed replies downstream.
        "inputProvenance": {"kind": "clarify_form", "source": "webui"},
    }
    if isinstance(run_id, str) and run_id:
        send_params["_source"] = {
            "caller_kind": "web",
            "channel_kind": "webchat",
            "channel_id": f"webchat:{session_key}",
            "source_kind": "webui",
            "source_name": "WebChat",
            "clarify_run_id": run_id,
        }
    result = await _handle_chat_send(send_params, ctx)
    return cast(dict, result)


@_d.method("chat.inject", scope="operator.admin")
async def _handle_chat_inject(params: dict | None, ctx: RpcContext) -> dict:
    if not isinstance(params, dict):
        raise ValueError("params required: sessionKey, role, content")
    for field in ("sessionKey", "role", "content"):
        if field not in params:
            raise ValueError(f"params.{field} is required")

    role = params["role"]
    if role not in ("user", "assistant", "system"):
        raise ValueError(f"Invalid role: {role}")

    session_key = _canonical_webchat_session_key(params["sessionKey"])

    if ctx.session_manager is None:
        raise KeyError("No session manager available")

    storage = getattr(ctx.session_manager, "_storage", None)
    if storage is not None:
        existing = await storage.get_session(session_key)
        if existing is None:
            raise KeyError(f"Session not found: {session_key}")

    await ctx.session_manager.append_message(session_key, role=role, content=params["content"])
    return {"ok": True, "sessionKey": session_key}
