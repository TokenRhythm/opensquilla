"""History turn limiting, orphan tool-pairing repair, and transcript reload."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
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
from opensquilla.provider.types import ContentBlockDocument, ContentBlockImage
from opensquilla.silent_reply import sanitize_historical_silent_reply

_SYNTHETIC_USER_PREFIXES = (
    "[Available skills for this turn]",
    "[Context summary]",
    "[Request context for this turn]",
    "[Runtime context for this turn]",
)


@dataclass(frozen=True)
class HistoryReplayEntryProjection:
    """One transcript row projected into provider-visible replay material.

    ``content`` is deliberately hidden from ``repr`` because it may contain
    user text or an in-memory typed media block.  Callers can also represent
    non-message transcript records without teaching this module about session
    persistence: legacy summaries and terminal notices are accumulated on the
    returned :class:`HistoryReplayProjection` instead.
    """

    role: str | None = None
    content: Any = field(default="", repr=False)
    tool_calls: list[dict[str, Any]] | None = field(default=None, repr=False)
    reasoning_content: str | None = field(default=None, repr=False)
    turn_context: Mapping[str, Any] | None = field(default=None, repr=False)
    legacy_summary_marker: str | None = field(default=None, repr=False)
    terminal_notice: str | None = field(default=None, repr=False)
    estimate_complete: bool = True
    persisted_token_count: int = 0
    raw_token_floor_applies: bool = True
    # ``None`` preserves the prior state, matching ignored/system rows.  A
    # terminal notice uses ``False`` so positional current-user trimming does
    # not remove a prompt that is no longer the transcript tail.
    last_entry_was_user: bool | None = None


@dataclass(frozen=True)
class HistoryReplayMessageProvenance:
    """Private transcript-row provenance for one reconstructed message."""

    entry_index: int | None = field(default=None, repr=False)
    estimate_complete: bool = field(default=True, repr=False)
    persisted_token_count: int = field(default=0, repr=False)
    raw_token_floor_applies: bool = field(default=True, repr=False)


@dataclass(frozen=True)
class HistoryReplayProjection:
    """Side-effect-free reconstruction of a transcript replay slice."""

    messages: tuple[Message, ...] = field(default=(), repr=False)
    message_provenance: tuple[HistoryReplayMessageProvenance, ...] = field(
        default=(),
        repr=False,
    )
    legacy_summary_markers: tuple[str, ...] = field(default=(), repr=False)
    terminal_notices: tuple[str, ...] = field(default=(), repr=False)
    estimate_complete: bool = True


@dataclass(frozen=True)
class HistoryReplayCapacityProjection:
    """Media-aware capacity projection for a reconstructed history slice."""

    messages: tuple[Message, ...] = field(default=(), repr=False)
    estimated_tokens: int = 0
    message_count: int = 0
    media_block_count: int = 0
    media_reserve_tokens: int = 0
    estimate_complete: bool = True


def project_history_replay(
    entries: Sequence[Any],
    *,
    excluded_entry_indexes: Collection[int] = (),
    trim_last_user: bool = True,
    bound_slice_applied: bool = False,
    entry_projector: Callable[[Any, int], HistoryReplayEntryProjection],
) -> HistoryReplayProjection:
    """Reconstruct the exact pre-current transcript message sequence.

    The caller owns persistence-specific decoding through ``entry_projector``;
    this function owns the replay invariants shared by history loading and
    router admission: bound/queued exclusions, persisted tool reconstruction,
    positional current-user trimming, and terminal-notice placement.  Inputs
    are never mutated.
    """

    excluded = set(excluded_entry_indexes)
    messages: list[Message] = []
    message_provenance: list[HistoryReplayMessageProvenance] = []
    legacy_summary_markers: list[str] = []
    terminal_notices: dict[str, HistoryReplayMessageProvenance] = {}
    estimate_complete = True
    last_entry_was_user = False

    for entry_index, entry in enumerate(entries):
        if entry_index in excluded:
            # Mirrors the queued-send loader: a skipped bound/future user is
            # not the replay tail and therefore disables positional trimming.
            last_entry_was_user = False
            continue

        projected = entry_projector(entry, entry_index)
        estimate_complete = estimate_complete and projected.estimate_complete
        if projected.legacy_summary_marker is not None:
            legacy_summary_markers.append(projected.legacy_summary_marker)
        if projected.terminal_notice is not None:
            terminal_notices.setdefault(
                projected.terminal_notice,
                HistoryReplayMessageProvenance(
                    entry_index=entry_index,
                    estimate_complete=projected.estimate_complete,
                    persisted_token_count=max(0, projected.persisted_token_count),
                    raw_token_floor_applies=projected.raw_token_floor_applies,
                ),
            )
        if projected.role in {"user", "assistant"}:
            reconstructed = reconstruct_messages_from_entry(
                projected.role,
                projected.content,
                projected.tool_calls,
                projected.reasoning_content,
                turn_context=projected.turn_context,
            )
            messages.extend(reconstructed)
            message_provenance.extend(
                HistoryReplayMessageProvenance(
                    entry_index=entry_index,
                    estimate_complete=projected.estimate_complete,
                    persisted_token_count=max(0, projected.persisted_token_count),
                    raw_token_floor_applies=projected.raw_token_floor_applies,
                )
                for _message in reconstructed
            )
        if projected.last_entry_was_user is not None:
            last_entry_was_user = projected.last_entry_was_user

    if (
        not bound_slice_applied
        and trim_last_user
        and last_entry_was_user
        and messages
        and messages[-1].role == "user"
    ):
        messages.pop()
        message_provenance.pop()

    unique_notices = tuple(terminal_notices)
    messages.extend(Message(role="assistant", content=notice) for notice in unique_notices)
    message_provenance.extend(terminal_notices[notice] for notice in unique_notices)
    return HistoryReplayProjection(
        messages=tuple(messages),
        message_provenance=tuple(message_provenance),
        legacy_summary_markers=tuple(legacy_summary_markers),
        terminal_notices=unique_notices,
        estimate_complete=estimate_complete,
    )


def project_history_replay_capacity(
    projection: HistoryReplayProjection,
    *,
    max_history_turns: int = 0,
) -> HistoryReplayCapacityProjection:
    """Estimate replay tokens after the route's real history-tail policy.

    Typed media is replaced by a bounded placeholder before tokenization and
    pays the same decoded-byte reserve used by provider request proof.  Plain
    dictionaries (including arbitrary JSON/data URLs in tool arguments) are
    intentionally *not* recognized as media and remain fully tokenized.
    Invalid or unsupported typed media keeps its raw conservative projection
    and marks the result incomplete so admission fails closed.
    """

    from opensquilla.contracts.attachments import IMAGE_ATTACHMENT_MIMES
    from opensquilla.provider.request_proof import estimate_provider_media_tokens
    from opensquilla.session.tokenizer import estimate_tokens

    messages = list(projection.messages)
    provenance = list(projection.message_provenance)
    if messages and not provenance:
        # Compatibility for internal callers that construct a projection by
        # hand. Runtime-built projections always carry row provenance.
        provenance = [
            HistoryReplayMessageProvenance(
                estimate_complete=projection.estimate_complete,
            )
            for _ in range(len(messages))
        ]
    provenance_shape_complete = len(provenance) == len(messages)
    if not provenance_shape_complete:
        provenance = [
            HistoryReplayMessageProvenance(estimate_complete=False)
            for _ in range(len(messages))
        ]
    if max_history_turns > 0:
        retained_indexes = _limit_turn_retained_indexes(messages, max_history_turns)
        messages = [messages[index] for index in retained_indexes]
        provenance = [provenance[index] for index in retained_indexes]
    messages, retained_indexes = _repair_tool_pairing_with_retained_indexes(messages)
    provenance = [provenance[index] for index in retained_indexes]

    media_block_count = 0
    media_reserve_tokens = 0
    estimate_complete = provenance_shape_complete and all(
        item.estimate_complete for item in provenance
    )

    def _project_value(value: Any) -> Any:
        nonlocal media_block_count, media_reserve_tokens, estimate_complete

        media_kind: str | None = None
        if isinstance(value, ContentBlockImage):
            if (
                value.source_type != "base64"
                or value.media_type not in IMAGE_ATTACHMENT_MIMES
            ):
                estimate_complete = False
            else:
                media_kind = "image"
        elif isinstance(value, ContentBlockDocument):
            if value.source_type != "base64" or value.media_type != "application/pdf":
                estimate_complete = False
            else:
                media_kind = "pdf"

        if media_kind is not None:
            try:
                decoded_bytes = len(base64.b64decode(value.data, validate=True))
            except (binascii.Error, ValueError):
                estimate_complete = False
            else:
                reserve = estimate_provider_media_tokens(media_kind, decoded_bytes)
                media_block_count += 1
                media_reserve_tokens += reserve
                dumped = value.model_dump(mode="json", exclude_none=True)
                dumped["data"] = (
                    f"[history_{media_kind}_omitted: {len(value.data)} chars]"
                )
                return dumped

        if isinstance(value, list | tuple):
            return [_project_value(item) for item in value]
        if isinstance(value, dict):
            # Do not infer media from untyped user/tool JSON.
            return {str(key): _project_value(item) for key, item in value.items()}
        model_fields = getattr(type(value), "model_fields", None)
        if isinstance(model_fields, dict):
            projected_model: dict[str, Any] = {}
            for name in model_fields:
                item = getattr(value, name, None)
                if item is not None:
                    projected_model[str(name)] = _project_value(item)
            return projected_model
        return value

    payload: list[Any] = []
    entry_estimates: dict[tuple[str, int], int] = {}
    entry_media_reserves: dict[tuple[str, int], int] = {}
    entry_token_floors: dict[tuple[str, int], int] = {}
    for message_index, (message, source) in enumerate(zip(messages, provenance, strict=True)):
        media_before = media_reserve_tokens
        projected_message = _project_value(message)
        payload.append(projected_message)
        serialized_message = json.dumps(
            projected_message,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        message_tokens = estimate_tokens(serialized_message)
        message_media_reserve = media_reserve_tokens - media_before
        source_key = (
            ("entry", source.entry_index)
            if source.entry_index is not None
            else ("message", message_index)
        )
        entry_estimates[source_key] = entry_estimates.get(source_key, 0) + message_tokens
        entry_media_reserves[source_key] = (
            entry_media_reserves.get(source_key, 0) + message_media_reserve
        )
        if source.raw_token_floor_applies:
            entry_token_floors[source_key] = max(
                entry_token_floors.get(source_key, 0),
                max(0, source.persisted_token_count),
            )

    adjusted_entry_tokens = sum(
        max(tokens, entry_token_floors.get(source_key, 0))
        + entry_media_reserves.get(source_key, 0)
        for source_key, tokens in entry_estimates.items()
    )
    serialized_tokens = 0
    if payload:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        serialized_tokens = estimate_tokens(serialized) + media_reserve_tokens
    estimated_tokens = max(serialized_tokens, adjusted_entry_tokens)
    return HistoryReplayCapacityProjection(
        messages=tuple(messages),
        estimated_tokens=max(0, estimated_tokens),
        message_count=len(messages),
        media_block_count=media_block_count,
        media_reserve_tokens=max(0, media_reserve_tokens),
        estimate_complete=estimate_complete,
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


def _limit_turn_retained_indexes(
    messages: list[Message],
    max_turns: int,
) -> tuple[int, ...]:
    """Return source indexes for the suffix retained by ``limit_turns``."""

    limited = limit_turns(messages, max_turns)
    if limited is messages:
        return tuple(range(len(messages)))
    start = len(messages) - len(limited)
    return tuple(range(start, len(messages)))


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


def _repair_tool_pairing_with_retained_indexes(
    messages: list[Message],
) -> tuple[list[Message], tuple[int, ...]]:
    """Repair tool pairing and return indexes retained from ``messages``.

    OpenAI-compatible providers require an assistant message with tool calls to
    be followed immediately by tool result messages for every requested
    ``tool_call_id``. A matching ID elsewhere in the transcript is not enough:
    ordinary user/context messages between the call and result still make the
    provider request invalid.

    Returns the original list reference if no repairs are needed. The private
    index sidecar lets capacity accounting discard provenance for messages that
    this repair removes without changing the public repair API.
    """
    if not messages:
        return messages, ()

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
    retained_indexes: list[int] = []
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
        retained_indexes.append(index)

    if not changed:
        return messages, tuple(range(len(messages)))
    return repaired, tuple(retained_indexes)


def repair_tool_pairing(messages: list[Message]) -> list[Message]:
    """Remove messages with malformed tool_use/tool_result adjacency."""

    repaired, _retained_indexes = _repair_tool_pairing_with_retained_indexes(messages)
    return repaired


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
