"""Bounded model-facing recent history, separate from legacy transcript reads."""

from __future__ import annotations

import json
from typing import Any

from opensquilla.session.keys import canonicalize_session_key, is_sidechat_key
from opensquilla.session.sidechat import sidechat_metadata
from opensquilla.tools.types import ToolContext, ToolError

RECENT_HISTORY_MAX_BYTES = 32 * 1024


def _identity(node: Any) -> tuple[str, int]:
    session_id, epoch = getattr(node, "session_id", None), getattr(node, "epoch", None)
    if not isinstance(session_id, str) or not session_id or type(epoch) is not int:
        raise ToolError("Recent history requires a durable session identity")
    return session_id, epoch


def _require_admitted_owner(ctx: ToolContext, node: Any) -> None:
    session_id, epoch = _identity(node)
    if (ctx.session_id is not None and ctx.session_id != session_id) or (
        ctx.session_epoch is not None and ctx.session_epoch != epoch
    ):
        raise ToolError("The current session changed; retry from its current conversation")


def _encode(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _pack_history(key: str, page: Any) -> str:
    """Keep the newest messages and fit the entire serialized UTF-8 response."""
    entries = list(page.entries)
    messages: list[dict[str, Any]] = []
    omitted = False
    truncated = False

    def response(rows: list[dict[str, Any]], cut: bool, more: bool) -> dict[str, Any]:
        return {
            "session_key": key,
            "recent": True,
            "order": "oldest_first",
            "scope": "persisted_visible_history",
            "live_output_included": False,
            "tool_details_included": False,
            "message_count": len(rows),
            "messages": rows,
            "has_more": bool(page.has_more or more),
            "truncated": cut,
            # This describes history coverage, never whether the agent finished.
            "complete": bool(
                page.canonical_complete and not page.has_more and not more and not cut
            ),
        }

    for entry in reversed(entries):
        text = entry.content or ""
        row = {
            "role": entry.role,
            "content": text,
            "created_at": entry.created_at,
            "truncated": bool(getattr(entry, "content_truncated", False)),
        }
        candidate = [row, *messages]
        cut = truncated or row["truncated"]
        if (
            len(_encode(response(candidate, cut, omitted)).encode("utf-8"))
            > RECENT_HISTORY_MAX_BYTES
        ):
            # A small prefix is still useful for a long latest answer. Search
            # serialized bytes, including JSON escaping and all response metadata.
            row["truncated"] = True
            low, high = 0, len(text)
            while low < high:
                middle = (low + high + 1) // 2
                row["content"] = text[:middle]
                if (
                    len(_encode(response(candidate, True, False)).encode("utf-8"))
                    <= RECENT_HISTORY_MAX_BYTES
                ):
                    low = middle
                else:
                    high = middle - 1
            row["content"] = text[:low]
            if (
                low
                and len(_encode(response(candidate, True, False)).encode("utf-8"))
                <= RECENT_HISTORY_MAX_BYTES
            ):
                messages = candidate
            omitted = len(messages) < len(entries)
            truncated = True
            break
        messages = candidate
        truncated = cut
    return _encode(response(messages, truncated, omitted))


async def recent_session_history(
    manager: Any, session_key: str, limit: int, ctx: ToolContext | None
) -> str:
    """Owner reads are cross-session; restricted callers may only read themselves."""
    key = canonicalize_session_key(session_key)
    if ctx is None or not key or len(key) > 512:
        raise ToolError("Recent history requires a trusted session context and a valid session key")
    current_key = canonicalize_session_key(ctx.session_key)
    if not ctx.is_owner and (not current_key or key != current_key):
        raise ToolError("Recent history is unavailable for another session in this context")

    caller = None
    if not ctx.is_owner or is_sidechat_key(current_key):
        caller = await manager.get_session(current_key)
        if caller is None:
            raise ToolError("The current session no longer exists")
        _require_admitted_owner(ctx, caller)

    target = await manager.get_session(key)
    if target is None:
        raise ToolError(f"Session not found: {key}")
    target_identity = _identity(target)
    parent_identity = None
    if is_sidechat_key(current_key):
        side = sidechat_metadata(caller)
        if not side or side.get("state") != "ready":
            raise ToolError("The side conversation is no longer active")
        if key == side.get("parentKey"):
            parent_identity = (side.get("parentSessionId"), side.get("parentEpoch"))
            if target_identity != parent_identity:
                raise ToolError(
                    "The parent session changed since this side conversation was created"
                )

    page = await manager.get_canonical_transcript_page(
        key, limit=limit, content_mode="tool_preview"
    )
    current_target = await manager.get_session(key)
    if current_target is None or _identity(current_target) != target_identity:
        raise ToolError("The target session changed during the history read; retry")
    if caller is not None:
        current_caller = await manager.get_session(current_key)
        if current_caller is None or _identity(current_caller) != _identity(caller):
            raise ToolError("The current session changed during the history read; retry")
        if is_sidechat_key(current_key):
            side = sidechat_metadata(current_caller)
            if not side or side.get("state") != "ready":
                raise ToolError("The side conversation closed during the history read")
            if parent_identity is not None and (
                side.get("parentKey") != key
                or (side.get("parentSessionId"), side.get("parentEpoch")) != parent_identity
            ):
                raise ToolError("The side conversation parent changed during the history read")
    return _pack_history(key, page)
