"""Read completed conversation counts without materializing transcript bodies."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _value(entry: Any, key: str, default: Any = None) -> Any:
    return entry.get(key, default) if isinstance(entry, Mapping) else getattr(entry, key, default)


_TOOL_CALL_TYPES = frozenset(
    {"tool_use", "tool_call", "function_call", "custom_tool_call", "function"}
)


def _has_tool_calls(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    blocks = value if isinstance(value, list) else [value]
    return any(
        isinstance(block, Mapping)
        and (
            (isinstance(block.get("type"), str) and block.get("type") in _TOOL_CALL_TYPES)
            or isinstance(block.get("function"), Mapping)
        )
        for block in blocks
    )


def _completed_reply(value: Any) -> bool:
    if value is None or value == []:
        return True
    blocks = value if isinstance(value, list) else [value]
    text_blocks = [
        block
        for block in blocks
        if isinstance(block, Mapping)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and block["text"].strip()
    ]
    if any(block.get("presentation") == "answer" for block in text_blocks):
        return True
    if _has_tool_calls(value) or any(
        block.get("presentation") in {"intermediate", "commentary"} for block in text_blocks
    ):
        return False
    return bool(text_blocks)


def count_completed_conversation_turns(
    entries: Sequence[Any],
    *,
    bound_user_message_id: str | None = None,
    exclude_last_user: bool = False,
) -> int:
    """Count completed user/reply pairs, with causal IDs preferred over position."""
    rows: list[Any] = []
    seen: set[tuple[Any, Any]] = set()
    for entry in entries:
        context = _value(entry, "turn_context") or {}
        if isinstance(context, Mapping) and context.get("visibility") == "model-only":
            continue
        identity = (_value(entry, "id"), _value(entry, "message_id"))
        if identity != (None, None):
            if identity in seen:
                continue
            seen.add(identity)
        rows.append(entry)
    boundary = len(rows)
    if bound_user_message_id is not None:
        boundary = next(
            (
                index
                for index, entry in enumerate(rows)
                if _value(entry, "role") == "user"
                and _value(entry, "message_id") == bound_user_message_id
            ),
            -1,
        )
        if boundary < 0:
            return 0
    elif exclude_last_user and rows and _value(rows[-1], "role") == "user":
        boundary -= 1

    def turn_id(entry: Any) -> str | None:
        context = _value(entry, "turn_context")
        value = context.get("turn_id") if isinstance(context, Mapping) else None
        return value if isinstance(value, str) and value else None

    admitted = {
        turn_id(entry)
        for index, entry in enumerate(rows)
        if index < boundary and _value(entry, "role") == "user" and turn_id(entry)
    }
    completed: set[str] = set()
    legacy_pending = False
    legacy_completed = 0
    for index, entry in enumerate(rows):
        role = _value(entry, "role")
        turn_identity = turn_id(entry)
        if role == "user":
            if index < boundary:
                legacy_pending = turn_identity is None
            continue
        if role != "assistant":
            continue
        complete = _value(entry, "completed_reply")
        if complete is None:
            complete = _completed_reply(_value(entry, "tool_calls"))
        if not complete:
            continue
        if turn_identity is not None:
            if turn_identity in admitted:
                completed.add(turn_identity)
        elif legacy_pending:
            legacy_completed += 1
            legacy_pending = False
    return len(completed) + legacy_completed


async def read_completed_conversation_turns(
    storage: Any,
    session_id: str,
    *,
    bound_user_message_id: str | None = None,
    exclude_last_user: bool = False,
) -> int:
    """Use one canonical metadata snapshot, including rows retained by compaction."""
    rows = await storage._read_history_query(
        """WITH canonical AS (
          SELECT id, message_id, role, turn_context, tool_calls, created_at
          FROM transcript_entries WHERE session_id = ?
          UNION ALL
          SELECT original_entry_id AS id, message_id, role, turn_context, tool_calls, created_at
          FROM compacted_transcript_entries AS archived WHERE session_id = ?
            AND NOT EXISTS (SELECT 1 FROM transcript_entries AS active
              WHERE active.session_id = archived.session_id
                AND active.id = archived.original_entry_id)
        ) SELECT id, message_id, role, created_at,
          CASE WHEN json_valid(turn_context)
            THEN json_extract(turn_context, '$.turn_id') END AS turn_id,
          CASE WHEN json_valid(turn_context)
            THEN json_extract(turn_context, '$.visibility') END AS visibility,
          CASE WHEN tool_calls IS NULL OR tool_calls = '' THEN 1
            WHEN json_valid(tool_calls) THEN
              CASE WHEN json_type(tool_calls) = 'null' OR tool_calls = '[]' THEN 1
              WHEN json_type(tool_calls) IN ('array', 'object') THEN (
                SELECT CASE
                  WHEN MAX(CASE WHEN block.type = 'object' THEN
                    json_extract(block.value, '$.type') = 'text'
                    AND json_extract(block.value, '$.presentation') = 'answer'
                    AND json_type(block.value, '$.text') = 'text'
                    AND length(trim(json_extract(block.value, '$.text'))) > 0 ELSE 0 END) THEN 1
                  WHEN MAX(CASE WHEN block.type = 'object' THEN
                    json_extract(block.value, '$.type') IN
                      ('tool_use', 'tool_call', 'function_call', 'custom_tool_call', 'function')
                    OR json_type(block.value, '$.function') = 'object'
                    OR (json_extract(block.value, '$.type') = 'text'
                      AND json_extract(block.value, '$.presentation')
                        IN ('intermediate', 'commentary'))
                    ELSE 0 END) THEN 0
                  ELSE COALESCE(MAX(CASE WHEN block.type = 'object' THEN
                    json_extract(block.value, '$.type') = 'text'
                    AND json_type(block.value, '$.text') = 'text'
                    AND length(trim(json_extract(block.value, '$.text'))) > 0 ELSE 0 END), 0) END
                FROM json_each(CASE WHEN json_type(tool_calls) = 'object'
                  THEN json_array(json(tool_calls)) ELSE tool_calls END) AS block
              ) ELSE 0 END
            ELSE 0 END AS completed_reply
          FROM canonical WHERE role IN ('user', 'assistant') ORDER BY created_at, id""",
        (session_id, session_id),
        operation="completed_conversation_turns",
    )
    metadata = [
        {
            "id": row["id"],
            "message_id": row["message_id"],
            "role": row["role"],
            "turn_context": {"turn_id": row["turn_id"], "visibility": row["visibility"]},
            "completed_reply": bool(row["completed_reply"]),
        }
        for row in rows
    ]
    return count_completed_conversation_turns(
        metadata,
        bound_user_message_id=bound_user_message_id,
        exclude_last_user=exclude_last_user,
    )
