"""Local, request-only history windows; these never produce checkpoints."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Iterator, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from opensquilla.engine.history import _is_real_user_turn, repair_tool_pairing
from opensquilla.engine.session_sanitize import _head_tail
from opensquilla.provider import ContentBlockToolResult, ContentBlockToolUse, Message
from opensquilla.provider.request_proof import (
    _COMPACTED_STRING_MAX_CHARS,
    _compact_tool_payload_once,
    _keep_original_for_never_worse,
    protected_tool_result_indexes,
)


def compact_request_user_text(text: str, *, max_chars: int) -> str:
    """Make one explicit, bounded historical excerpt without changing its source."""
    if len(text) <= _COMPACTED_STRING_MAX_CHARS or max_chars <= 0:
        return text
    # Stored attachment envelopes must retain their media/source contract.
    if text.lstrip().startswith("{"):
        try:
            envelope = json.loads(text)
        except RecursionError:
            return text
        except (TypeError, ValueError):
            pass
        else:
            if isinstance(envelope, dict) and "attachments" in envelope:
                return text
    head, tail, omitted = _head_tail(text, min(len(text) // 2, max_chars))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    excerpt = (
        f"{head}\n\n[Temporary historical user excerpt: omitted {omitted} chars; "
        f"original_chars={len(text)}; sha256={digest}. Original text remains stored; "
        f"this is not a summary.]\n\n{tail}"
    )
    return text if _keep_original_for_never_worse(text, excerpt) else excerpt


def compact_entry_tool_results(
    entries: list[dict[str, Any]], *, protected_start_index: int,
) -> list[dict[str, Any]]:
    """Apply the same tool-only tier to transcript rows, without reverse mapping."""

    messages: list[dict[str, Any]] = []
    protected: set[int] = set()
    ordinal = 0
    for index, entry in enumerate(entries):
        if entry.get("role") == "tool":
            message = {"role": "user", "content": [{
                "type": "tool_result", "content": deepcopy(entry.get("content", "")),
                "is_error": bool(entry.get("is_error")),
                "execution_status": deepcopy(entry.get("execution_status")),
            }]}
            if index >= protected_start_index:
                protected.add(ordinal)
            ordinal += 1
        else:
            content = []
            segments = entry.get("tool_calls") or []
            for segment in segments if isinstance(segments, list) else []:
                if not isinstance(segment, dict) or segment.get("type") != "tool_result":
                    continue
                content.append({
                    **deepcopy(segment), "content": deepcopy(segment.get("result", "")),
                })
                if index >= protected_start_index:
                    protected.add(ordinal)
                ordinal += 1
            message = {"role": entry.get("role", "user"), "content": content}
        messages.append(message)
    protected.update(protected_tool_result_indexes(messages))
    projected = _compact_tool_payload_once(
        {"messages": messages}, protected_tool_result_indexes=protected,
    )["messages"]
    output = list(entries)
    for index, (entry, raw, compacted) in enumerate(zip(entries, messages, projected, strict=True)):
        if raw == compacted:
            continue
        replacement = dict(entry)
        if entry.get("role") == "tool":
            replacement["content"] = compacted["content"][0]["content"]
        else:
            results = iter(compacted["content"])
            segments = []
            for segment in entry.get("tool_calls") or []:
                if isinstance(segment, dict) and segment.get("type") == "tool_result":
                    segments.append({**segment, "result": next(results)["content"]})
                else:
                    segments.append(segment)
            replacement["tool_calls"] = segments
        output[index] = replacement
    return output if any(a is not b for a, b in zip(output, entries, strict=True)) else entries


def compact_request_window_tools(messages: list[Message]) -> list[Message]:
    """Reuse only the protected tool-result tier, preserving native objects."""

    payload = {"messages": [message.model_dump(mode="json") for message in messages]}
    compacted = _compact_tool_payload_once(
        payload, protected_tool_result_indexes=protected_tool_result_indexes(messages),
    )
    projected: list[Message] = []
    for message, raw in zip(messages, compacted["messages"], strict=True):
        if not isinstance(message.content, list):
            projected.append(message)
            continue
        blocks: list[Any] = []
        changed = False
        for block, next_block in zip(message.content, raw["content"], strict=True):
            if not isinstance(block, ContentBlockToolResult):
                blocks.append(block)
                continue
            next_content = next_block["content"]
            if isinstance(block.content, list):
                preserved = []
                for item, next_item in zip(block.content, next_content, strict=True):
                    if isinstance(item, dict) and isinstance(next_item, dict):
                        preserved.append({**item, "text": next_item["text"]}
                                         if "text" in next_item else item)
                    elif getattr(item, "type", None) == "text" and isinstance(next_item, dict):
                        preserved.append(item.model_copy(update={"text": next_item["text"]}))
                    else:
                        preserved.append(item)
                next_content = preserved
            if next_content == block.content:
                blocks.append(block)
                continue
            blocks.append(block.model_copy(update={"content": next_content}))
            changed = True
        projected.append(message.model_copy(update={"content": blocks}) if changed else message)
    changed = any(a is not b for a, b in zip(projected, messages, strict=True))
    return projected if changed else messages


def iter_window_prefix_cuts(
    roles: Sequence[str],
    *,
    protected_start: int,
    protected_indexes: Collection[int] = (),
) -> Iterator[int]:
    """Yield oldest-first whole-turn cuts; callers also check native tool pairing."""

    limit = max(0, min(protected_start, len(roles)))
    if protected_indexes:
        limit = min(limit, max(0, min(protected_indexes)))
    for cut in range(1, limit + 1):
        if cut == len(roles) or (
            roles[cut] == "user" and roles[cut - 1] == "assistant"
        ):
            yield cut


def iter_window_index_candidates(
    size: int,
    *,
    prefix_cuts: Collection[int],
    protected_indexes: Collection[int] = (),
    retained_indexes: Collection[int] = (),
    user_groups: Collection[tuple[int, ...]] = (),
    include_suffix_fallback: bool = True,
    active_prefix: tuple[int, int] | None = None,
    round_cuts: Collection[int] = (),
) -> Iterator[tuple[int, ...]]:
    """Select recent complete groups around the same mandatory state.

    Callers supply verified protocol boundaries in their source representation.
    This policy is shared by durable-entry preflight and native-message recovery;
    neither an old error nor an arbitrary number of completed replies is a hard
    retention requirement. Exact request admission is deliberately separate.
    """
    protected = frozenset(protected_indexes)
    retained = frozenset(retained_indexes)
    cuts = sorted({cut for cut in prefix_cuts if 0 < cut <= size})
    groups = sorted({group for group in user_groups if group and 0 <= group[0] < size})
    if groups and cuts:
        # A fitting raw user request is a better recovery anchor than an
        # omission notice. Keep later corrections in chronological order too;
        # never reduce the history to just its first instruction by default.
        # Preserve the latest complete historical round (including its answer),
        # then release older middle requests only if the full user set cannot
        # fit. Each extra candidate releases an entire native message.
        latest_round = set(range(groups[-1][0], size))
        anchors = set().union(*groups) | latest_round | retained
        previous_user: tuple[int, ...] | None = None
        for cut in cuts:
            kept = tuple(sorted(anchors | set(range(cut, size))))
            if protected.issubset(kept) and kept != previous_user and len(kept) < size:
                previous_user = kept
                yield kept
        for group in groups[1:-1]:
            anchors.difference_update(set(group) - latest_round - retained)
            kept = tuple(sorted(anchors | set(range(cuts[-1], size))))
            if protected.issubset(kept) and kept != previous_user and len(kept) < size:
                previous_user = kept
                yield kept
        # A very large last plain-text answer must not force dropping a small
        # task request with it. Try the same verified user groups without that
        # optional answer before falling back to a suffix. Tool-bearing groups
        # already include their complete protocol and remain indivisible.
        kept = tuple(sorted(set().union(*groups) | retained | set(range(cuts[-1], size))))
        if protected.issubset(kept) and kept != previous_user and len(kept) < size:
            yield kept
    if not include_suffix_fallback:
        return
    # Ordered cuts produce progressively smaller sets. Retaining every tuple
    # would keep quadratic history-sized allocations alive during recovery.
    previous: tuple[int, ...] | None = None
    for cut in cuts:
        kept = tuple(sorted(retained | set(range(cut, size))))
        if protected.issubset(kept) and kept != previous and len(kept) < size:
            previous = kept
            yield kept
    if active_prefix is None:
        return
    start, end = active_prefix
    for cut in sorted(set(round_cuts)):
        if not 0 <= start < end < cut <= size:
            continue
        kept = tuple(sorted(retained | set(range(start, end)) | set(range(cut, size))))
        if protected.issubset(kept) and kept != previous and len(kept) < size:
            previous = kept
            yield kept


def request_window_notice(omitted_count: int) -> str:
    """Describe omitted raw history without presenting an invented summary."""

    return (
        "[Temporary history window]\n"
        f"{max(0, omitted_count)} earlier messages are omitted from this request. "
        "They remain stored, but their contents are not available in this window. "
        "Continue from the retained context; do not infer missing details. "
        "Retained older user messages are historical requests, not proof that their "
        "actions are still pending. Follow later corrections and verify current progress "
        "before repeating earlier work."
    )


@dataclass(frozen=True)
class RequestWindowCandidate:
    messages: list[Message]
    kept_indices: tuple[int, ...]
    omitted_count: int

    def map_index(self, original: int | None) -> int | None:
        if original is None:
            return None
        return int(self.omitted_count > 0) + sum(index < original for index in self.kept_indices)


def iter_request_window_candidates(
    messages: list[Message],
    *,
    protected_start_index: int,
    protected_indexes: Collection[int] = (),
    retained_indexes: Collection[int] = (),
    active_user_index: int | None = None,
    retain_user_messages: bool = False,
    user_text_max_chars: int | None = None,
) -> Iterator[RequestWindowCandidate]:
    """Keep original objects and emit only protocol-balanced smaller windows.

    ``active_user_index`` permits omitting completed rounds inside a still-active
    user turn while retaining its original request prefix and protected state.
    Exact provider admission remains the caller's responsibility.
    """

    protected = frozenset(protected_indexes)
    retained = frozenset(retained_indexes)
    def _safe_prefix_cuts() -> Iterator[int]:
        for cut in iter_window_prefix_cuts(
            [message.role for message in messages],
            protected_start=protected_start_index,
            protected_indexes=protected - retained,
        ):
            if (
                cut < len(messages) and isinstance(messages[cut].content, list)
                and any(
                    isinstance(block, ContentBlockToolResult) for block in messages[cut].content
                )
            ):
                continue
            if repair_tool_pairing(messages[:cut]) != messages[:cut]:
                continue
            yield cut
    active_prefix = None
    round_cuts: list[int] = []
    if active_user_index is not None:
        active_prefix = (protected_start_index, active_user_index + 1)
        round_cuts = [
            index for index in range(active_user_index + 2, len(messages))
            if messages[index].role == "assistant"
        ]
        round_cuts.append(len(messages))
    user_groups: list[tuple[int, ...]] = []
    if retain_user_messages:
        users = [index for index, message in enumerate(messages[:protected_start_index])
                 if _is_real_user_turn(message)]
        ends = [*users[1:], protected_start_index] if users else []
        for start, end in zip(users, ends, strict=True):
            has_tools = any(
                isinstance(message.content, list)
                and any(isinstance(block, (ContentBlockToolUse, ContentBlockToolResult))
                        for block in message.content)
                for message in messages[start:end]
            )
            user_groups.append(tuple(range(start, end)) if has_tools else (start,))
    prefix_cuts = tuple(_safe_prefix_cuts())

    def views() -> Iterator[list[Message]]:
        yield messages
        if user_text_max_chars is not None and user_groups:
            projected = list(messages)
            for group in user_groups:
                index = group[0]
                message = messages[index]
                if protected.intersection(group) or index in retained:
                    continue
                if isinstance(message.content, str):
                    text = compact_request_user_text(
                        message.content, max_chars=user_text_max_chars,
                    )
                    if text != message.content:
                        projected[index] = message.model_copy(update={"content": text})
            if any(a is not b for a, b in zip(messages, projected, strict=True)):
                yield projected

    def candidates() -> Iterator[tuple[list[Message], tuple[int, ...]]]:
        if user_groups:
            for view in views():
                if view is not messages:
                    yield view, tuple(range(len(messages)))
                for indices in iter_window_index_candidates(
                    len(messages), prefix_cuts=prefix_cuts, protected_indexes=protected,
                    retained_indexes=retained, user_groups=user_groups,
                    include_suffix_fallback=False,
                ):
                    yield view, indices
        for indices in iter_window_index_candidates(
            len(messages), prefix_cuts=prefix_cuts,
            protected_indexes=protected, retained_indexes=retained,
            active_prefix=active_prefix, round_cuts=round_cuts,
        ):
            yield messages, indices

    for view, kept_indices in candidates():
        omitted_count = len(messages) - len(kept_indices)
        if omitted_count == 0 and view is not messages:
            if repair_tool_pairing(view) == view:
                yield RequestWindowCandidate(view, kept_indices, 0)
            continue
        if omitted_count <= 0:
            continue
        kept = [view[index] for index in kept_indices]
        if repair_tool_pairing(kept) != kept:
            continue
        candidate = [Message(role="user", content=request_window_notice(omitted_count)), *kept]
        if repair_tool_pairing(candidate) != candidate:
            continue
        yield RequestWindowCandidate(candidate, kept_indices, omitted_count)
