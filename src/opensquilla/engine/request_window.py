"""Local, request-only history windows; these never produce checkpoints."""

from __future__ import annotations

from collections.abc import Collection, Iterator, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from opensquilla.engine.history import repair_tool_pairing
from opensquilla.provider import ContentBlockToolResult, Message
from opensquilla.provider.request_proof import (
    _compact_tool_payload_once,
    protected_tool_result_indexes,
)


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

    limit = max(0, min(protected_start, len(roles) - 1))
    if protected_indexes:
        limit = min(limit, max(0, min(protected_indexes)))
    for cut in range(1, limit + 1):
        if roles[cut] == "user" and roles[cut - 1] == "assistant":
            yield cut


def request_window_notice(omitted_count: int) -> str:
    """Describe omitted raw history without presenting an invented summary."""

    return (
        "[Temporary history window]\n"
        f"{max(0, omitted_count)} earlier messages are omitted from this request. "
        "They remain stored, but their contents are not available in this window. "
        "Continue from the retained context; do not infer missing details."
    )


@dataclass(frozen=True)
class RequestWindowCandidate:
    messages: list[Message]
    kept_indices: tuple[int, ...]
    omitted_count: int

    def map_index(self, original: int | None) -> int | None:
        if original is None:
            return None
        return 1 + sum(index < original for index in self.kept_indices)


def iter_request_window_candidates(
    messages: list[Message],
    *,
    protected_start_index: int,
    protected_indexes: Collection[int] = (),
    retained_indexes: Collection[int] = (),
    live_boundary: tuple[int, int] | None = None,
) -> Iterator[RequestWindowCandidate]:
    """Keep original objects and emit only protocol-balanced smaller windows.

    ``live_boundary`` comes from the existing active-user/recent-round guard.
    It permits omitting completed rounds inside a still-active user turn while
    retaining its original request prefix and every protected recent round.
    Exact provider admission remains the caller's responsibility.
    """

    protected = frozenset(protected_indexes)
    retained = frozenset(retained_indexes)
    seen: set[tuple[int, ...]] = set()
    def _index_sequences() -> Iterator[tuple[int, ...]]:
        for cut in iter_window_prefix_cuts(
            [message.role for message in messages],
            protected_start=protected_start_index,
            protected_indexes=protected - retained,
        ):
            if (
                isinstance(messages[cut].content, list)
                and any(
                    isinstance(block, ContentBlockToolResult) for block in messages[cut].content
                )
            ):
                continue
            if repair_tool_pairing(messages[:cut]) != messages[:cut]:
                continue
            yield tuple(sorted(retained | set(range(cut, len(messages)))))
        if live_boundary is not None:
            active_user, keep_start = live_boundary
            if protected_start_index <= active_user < keep_start <= len(messages):
                yield tuple(sorted(
                    retained
                    | set(range(protected_start_index, active_user + 1))
                    | set(range(keep_start, len(messages)))
                ))

    for kept_indices in _index_sequences():
        if kept_indices in seen or not protected.issubset(kept_indices):
            continue
        seen.add(kept_indices)
        omitted_count = len(messages) - len(kept_indices)
        if omitted_count <= 0:
            continue
        kept = [messages[index] for index in kept_indices]
        if repair_tool_pairing(kept) != kept:
            continue
        candidate = [Message(role="user", content=request_window_notice(omitted_count)), *kept]
        if repair_tool_pairing(candidate) != candidate:
            continue
        yield RequestWindowCandidate(candidate, kept_indices, omitted_count)
