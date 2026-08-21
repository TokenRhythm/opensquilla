"""History turn limiting, orphan tool-pairing repair, and transcript reload."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from opensquilla.execution_status import (
    normalize_execution_status,
    normalize_legacy_execution_status,
)
from opensquilla.provider import (
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
)
from opensquilla.silent_reply import sanitize_historical_silent_reply

_SYNTHETIC_USER_PREFIXES = (
    "[Available skills for this turn]",
    "[Context summary]",
    "[Request context for this turn]",
    "[Runtime context for this turn]",
)


@dataclass(frozen=True)
class RestrictedHistoryProjectionResult:
    """Counts from stripping historical tool protocol at a restricted turn."""

    tool_uses_removed: int = 0
    tool_results_removed: int = 0
    empty_messages_removed: int = 0
    synthetic_messages_removed: int = 0


def strip_historical_tool_pairs(
    messages: list[Message],
) -> tuple[list[Message], RestrictedHistoryProjectionResult]:
    """Remove historical tool protocol from a provider-only request view.

    PromptAnnotation turns must not inherit paths, commands, or tool payloads
    from earlier unrestricted turns. All historical tool-use and tool-result
    blocks are removed together, while ordinary text and media are preserved.
    Persisted transcript rows are never mutated.
    """

    projected: list[Message] = []
    uses_removed = 0
    results_removed = 0
    empty_removed = 0
    synthetic_removed = 0
    for message in messages:
        if (
            isinstance(message.content, str)
            and message.content.startswith(_SYNTHETIC_USER_PREFIXES)
        ):
            synthetic_removed += 1
            continue
        if not isinstance(message.content, list):
            projected.append(message)
            continue
        content: list[Any] = []
        changed = False
        for block in message.content:
            if isinstance(block, ContentBlockToolUse):
                uses_removed += 1
                changed = True
                continue
            if isinstance(block, ContentBlockToolResult):
                results_removed += 1
                changed = True
                continue
            content.append(block)
        if not changed:
            projected.append(message)
            continue
        if not content:
            empty_removed += 1
            continue
        projected.append(
            Message(
                role=message.role,
                content=content,
                # Reasoning attached to a historical tool call may itself
                # describe paths or command arguments, so it is not retained.
                reasoning_content=None,
            )
        )
    return projected, RestrictedHistoryProjectionResult(
        tool_uses_removed=uses_removed,
        tool_results_removed=results_removed,
        empty_messages_removed=empty_removed,
        synthetic_messages_removed=synthetic_removed,
    )


def _is_real_user_turn(message: Message) -> bool:
    if message.role != "user":
        return False
    content = message.content
    if isinstance(content, str):
        return not content.startswith(_SYNTHETIC_USER_PREFIXES)
    if isinstance(content, list):
        return not all(isinstance(block, ContentBlockToolResult) for block in content)
    return True


def limit_turns(messages: list[Message], max_turns: int) -> list[Message]:
    """Keep the most recent max_turns user/assistant turn pairs.

    A 'turn' is counted by user messages. Returns the original list
    reference if no truncation needed (caller can use identity check).
    """
    if max_turns <= 0 or not messages:
        return messages

    # Count real user messages from the end; synthetic context messages should
    # not evict conversation turns from the provider prefix.
    user_count = 0
    cut_index = 0
    for i in range(len(messages) - 1, -1, -1):
        if _is_real_user_turn(messages[i]):
            user_count += 1
            if user_count > max_turns:
                # i is the user msg we want to exclude; next msg after i is the cut point
                # but we want to start at the *next* user msg (i+2 skips the assistant at i+1)
                # Actually: cut at the user msg itself, i.e. cut_index = i + 1 would include
                # the assistant reply to this user msg. We want to exclude msg[i] and prior,
                # so cut at the first non-excluded index, which is i+1 only if i+1 is a user msg.
                # Simpler: scan forward from i+1 to find the next user message.
                cut_index = i + 1
                while cut_index < len(messages) and not _is_real_user_turn(
                    messages[cut_index]
                ):
                    cut_index += 1
                break

    if cut_index == 0:
        return messages  # within budget

    return messages[cut_index:]


def _extract_tool_use_ids(content: Any) -> set[str]:
    """Extract tool_use IDs from message content."""
    ids: set[str] = set()
    if isinstance(content, list):
        for block in content:
            # ContentBlockToolUse has 'id' field
            if hasattr(block, "id") and hasattr(block, "name") and hasattr(block, "input"):
                ids.add(block.id)
    return ids


def _extract_tool_result_ids(content: Any) -> set[str]:
    """Extract tool_use_ids from tool result blocks."""
    ids: set[str] = set()
    if isinstance(content, list):
        for block in content:
            if hasattr(block, "tool_use_id") and hasattr(block, "is_error"):
                ids.add(block.tool_use_id)
    return ids


def _is_tool_result_block(block: Any) -> bool:
    return hasattr(block, "tool_use_id") and hasattr(block, "is_error")


def _dedupe_tool_result_blocks(content: Any) -> tuple[Any, bool]:
    if not isinstance(content, list):
        return content, False

    last_index_by_id: dict[str, int] = {}
    result_count = 0
    for index, block in enumerate(content):
        if not _is_tool_result_block(block):
            continue
        result_count += 1
        last_index_by_id[block.tool_use_id] = index

    if result_count == len(last_index_by_id):
        return content, False

    next_content: list[Any] = []
    changed = False
    for index, block in enumerate(content):
        if _is_tool_result_block(block) and last_index_by_id.get(block.tool_use_id) != index:
            changed = True
            continue
        next_content.append(block)
    return next_content, changed


def repair_tool_pairing(messages: list[Message]) -> list[Message]:
    """Remove messages with malformed tool_use/tool_result adjacency.

    OpenAI-compatible providers require an assistant message with tool calls to
    be followed immediately by tool result messages for every requested
    ``tool_call_id``. A matching ID elsewhere in the transcript is not enough:
    ordinary user/context messages between the call and result still make the
    provider request invalid.

    Returns original list reference if no repairs needed.
    """
    if not messages:
        return messages

    valid_tool_call_indices: set[int] = set()
    valid_tool_result_indices: set[int] = set()

    for index, message in enumerate(messages[:-1]):
        use_ids = _extract_tool_use_ids(message.content)
        if not use_ids:
            continue
        if message.role != "assistant":
            continue

        result_indices: set[int] = set()
        result_ids: set[str] = set()
        for result_index in range(index + 1, len(messages)):
            next_result_ids = _extract_tool_result_ids(messages[result_index].content)
            if not next_result_ids:
                break
            result_ids.update(next_result_ids)
            if not result_ids.issubset(use_ids):
                break
            result_indices.add(result_index)
            if result_ids == use_ids:
                break

        if result_ids == use_ids:
            valid_tool_call_indices.add(index)
            valid_tool_result_indices.update(result_indices)

    repaired: list[Message] = []
    changed = False
    for index, message in enumerate(messages):
        use_ids = _extract_tool_use_ids(message.content)
        result_ids = _extract_tool_result_ids(message.content)

        if use_ids and index not in valid_tool_call_indices:
            changed = True
            continue
        if result_ids and index not in valid_tool_result_indices:
            changed = True
            continue

        if result_ids:
            content, deduped = _dedupe_tool_result_blocks(message.content)
            if deduped:
                message = Message(
                    role=message.role,
                    content=content,
                    reasoning_content=message.reasoning_content,
                )
                changed = True

        repaired.append(message)

    return messages if not changed else repaired


def _coerce_tool_input(raw: Any) -> dict[str, Any]:
    """Coerce a persisted tool_use.input back into a dict.

    Persistence may store input as dict, JSON string, or empty string (mid-stream
    partial). Anthropic's tool_use.input must conform to the tool's input_schema;
    fabricating a fallback key like ``{"_raw": ...}`` produces a shape no real
    schema declares, so on any non-dict payload we emit ``{}`` — a faithful
    "input missing" marker. Matching tool_result blocks still pair via
    tool_use_id, and ``repair_tool_pairing`` prunes any remaining orphan.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def reconstruct_messages_from_entry(
    role: str,
    content: Any,
    tool_calls: list[dict[str, Any]] | None,
    reasoning_content: str | None = None,
    *,
    turn_context: Mapping[str, Any] | None = None,
) -> list[Message]:
    """Rebuild provider Messages from one persisted transcript entry.

    An assistant turn is persisted as a single row whose ``tool_calls`` JSON
    column flattens every iteration's segments (text / tool_use / tool_result)
    into one ordered list. The in-memory agent loop instead appends a separate
    Message per iteration (see ``agent.run_turn``): each iteration produces an
    assistant message (text + tool_use blocks) followed, once the tools run,
    by a user message carrying tool_result blocks. A multi-iteration turn
    reloaded from disk must restore that per-iteration shape.

    Segmentation rule: a ``tool_result`` segment closes the current iteration.
    Whatever arrives after (text or tool_use) starts the next iteration — so we
    flush the accumulated assistant + user(tool_result) pair first.

    Returns ``[]`` for entries that contribute nothing. Orphan tool_use blocks
    without a matching tool_result are preserved here; ``repair_tool_pairing``
    prunes them later if they stay orphan across the whole conversation.
    """
    if role not in ("user", "assistant"):
        return []

    silent_reply = sanitize_historical_silent_reply(
        content,
        tool_calls,
        role=role,
        turn_context=turn_context,
    )
    content = silent_reply.content
    tool_calls = silent_reply.segments

    if role == "user":
        if content:
            return [Message(role="user", content=content)]
        return []

    if not tool_calls:
        if content:
            return [
                Message(
                    role="assistant",
                    content=content,
                    reasoning_content=reasoning_content,
                )
            ]
        return []

    messages: list[Message] = []
    pending_assistant: list[Any] = []
    pending_results: list[Any] = []

    def _flush() -> None:
        if pending_assistant:
            messages.append(Message(role="assistant", content=list(pending_assistant)))
            pending_assistant.clear()
        if pending_results:
            messages.append(Message(role="user", content=list(pending_results)))
            pending_results.clear()

    for seg in tool_calls:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type")
        if seg_type == "text":
            text = seg.get("text") or ""
            if not text:
                continue
            # text after a tool_result begins the next iteration → flush prior pair
            if pending_results:
                _flush()
            pending_assistant.append(ContentBlockText(text=text))
        elif seg_type == "tool_use":
            tool_use_id = seg.get("tool_use_id") or seg.get("id")
            name = seg.get("name") or ""
            if not tool_use_id or not name:
                continue
            if pending_results:
                _flush()
            pending_assistant.append(
                ContentBlockToolUse(
                    id=tool_use_id,
                    name=name,
                    input=_coerce_tool_input(seg.get("input")),
                )
            )
        elif seg_type == "tool_result":
            tool_use_id = seg.get("tool_use_id")
            if not tool_use_id:
                continue
            raw_result = seg.get("result", "")
            if isinstance(raw_result, (str, list)):
                result_content: str | list[Any] = raw_result
            else:
                result_content = str(raw_result)
            pending_results.append(
                ContentBlockToolResult(
                    tool_use_id=tool_use_id,
                    content=result_content,
                    is_error=bool(seg.get("is_error")),
                    execution_status=(
                        normalize_execution_status(seg.get("execution_status"))
                        if "execution_status" in seg
                        else normalize_legacy_execution_status(is_error=bool(seg.get("is_error")))
                    ),
                )
            )

    _flush()

    # If the segment list carried no text at all but the entry.content still
    # holds the concatenated turn text, prepend it to the first assistant
    # message as a best-effort preserve. (The per-iteration assignment is
    # ambiguous in this degenerate case, but never happens in practice — the
    # runtime flushes current_text_parts into a "text" segment before any
    # tool_use or at end of stream.)
    if (
        isinstance(content, str)
        and content.strip()
        and not any(
            isinstance(m.content, list) and any(isinstance(b, ContentBlockText) for b in m.content)
            for m in messages
            if m.role == "assistant"
        )
    ):
        first_assistant = next((m for m in messages if m.role == "assistant"), None)
        if first_assistant is not None and isinstance(first_assistant.content, list):
            first_assistant.content.insert(0, ContentBlockText(text=content))
        elif not messages:
            messages.append(Message(role="assistant", content=content))

    if isinstance(content, str) and "[generated artifact omitted:" in content:
        markers = [
            line.strip()
            for line in content.splitlines()
            if line.strip().startswith("[generated artifact omitted:")
        ]
        if markers:
            marker_text = "\n".join(markers)
            assistant = next(
                (
                    m
                    for m in reversed(messages)
                    if m.role == "assistant" and isinstance(m.content, list)
                ),
                None,
            )
            if assistant is not None:
                content_blocks = assistant.content
                if not isinstance(content_blocks, list):
                    return messages
                existing = "\n".join(
                    block.text for block in content_blocks if isinstance(block, ContentBlockText)
                )
                if marker_text not in existing:
                    content_blocks.append(ContentBlockText(text=marker_text))
            elif not messages:
                messages.append(Message(role="assistant", content=marker_text))

    if reasoning_content:
        first_assistant = next((m for m in messages if m.role == "assistant"), None)
        if first_assistant is not None:
            first_assistant.reasoning_content = reasoning_content

    return messages
