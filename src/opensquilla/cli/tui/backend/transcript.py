"""Renderer-neutral transcript storage and viewport projection."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace

from opensquilla.cli.tui.backend.render_summary import sanitize_terminal_text

type TranscriptRole = str
type ToolStatus = str


@dataclass(frozen=True)
class MessageItem:
    role: TranscriptRole
    text: str
    run_id: str | None
    timestamp_ms: int
    item_id: str = ""


@dataclass(frozen=True)
class ToolItem:
    tool_id: str
    name: str
    status: ToolStatus
    args_preview: str
    output_preview: str
    expanded: bool
    timestamp_ms: int
    item_id: str = ""
    detail_line_count: int = 1
    is_error: bool = False


@dataclass(frozen=True)
class RouterDecisionItem:
    tier: str
    model: str
    baseline_model: str | None
    confidence: float | None
    rollout_phase: str | None
    timestamp_ms: int
    item_id: str = ""


@dataclass(frozen=True)
class StatusItem:
    message: str
    style: str
    timestamp_ms: int
    item_id: str = ""


@dataclass(frozen=True)
class UsageItem:
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    timestamp_ms: int
    item_id: str = ""


type TranscriptItem = (
    MessageItem | ToolItem | RouterDecisionItem | StatusItem | UsageItem
)


@dataclass(frozen=True)
class ToolPreviewPolicy:
    max_arg_chars: int = 240
    max_output_lines: int = 12
    max_output_chars: int = 2_000


@dataclass(frozen=True)
class ToolPreview:
    text: str
    truncated: bool
    line_count: int


@dataclass(frozen=True)
class ViewportRequest:
    scroll_offset: int
    viewport_height: int
    overscan: int = 2


@dataclass(frozen=True)
class VisibleTranscriptItem:
    item: TranscriptItem
    row_start: int
    row_count: int


@dataclass(frozen=True)
class ViewportProjection:
    items: tuple[VisibleTranscriptItem, ...]
    total_items: int
    total_rows: int
    visible_rows: int


class TranscriptStore:
    def __init__(self) -> None:
        self._items: list[TranscriptItem] = []
        self._counters: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self._items)

    def append(self, item: TranscriptItem) -> TranscriptItem:
        stored_item = self._assign_item_id(item)
        self._items.append(stored_item)
        return stored_item

    def clear(self) -> None:
        self._items.clear()
        self._counters.clear()

    def snapshot(self) -> tuple[TranscriptItem, ...]:
        return tuple(self._items)

    def _assign_item_id(self, item: TranscriptItem) -> TranscriptItem:
        if item.item_id:
            return item
        if isinstance(item, ToolItem) and item.tool_id:
            # Repeated appends for one tool_id (running -> done transitions,
            # or providers reusing tool_use ids across turns) must still get
            # unique item_ids so renderers can key rows by them.
            base = f"tool-{item.tool_id}"
            seen = self._counters.get(base, 0) + 1
            self._counters[base] = seen
            if seen == 1:
                return replace(item, item_id=base)
            return replace(item, item_id=f"{base}-{seen}")
        prefix = _item_prefix(item)
        next_id = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = next_id
        return replace(item, item_id=f"{prefix}-{next_id}")


def build_args_preview(args: object, policy: ToolPreviewPolicy) -> ToolPreview:
    text = sanitize_terminal_text(_stringify_json(args))
    text, truncated = _truncate_chars(text, policy.max_arg_chars, marker="...")
    return ToolPreview(text=text, truncated=truncated, line_count=_line_count(text))


def build_output_preview(
    output: object,
    policy: ToolPreviewPolicy,
    *,
    is_error: bool = False,
) -> ToolPreview:
    text = sanitize_terminal_text(_stringify_output(output))
    if is_error:
        text = f"error: {text}"
    text, truncated_by_lines = _truncate_lines(text, policy.max_output_lines)
    text, truncated_by_chars = _truncate_chars(
        text,
        policy.max_output_chars,
        marker="... truncated",
    )
    return ToolPreview(
        text=text,
        truncated=truncated_by_lines or truncated_by_chars,
        line_count=_line_count(text),
    )


def project_viewport(
    snapshot: Sequence[TranscriptItem],
    request: ViewportRequest,
) -> ViewportProjection:
    rows = tuple(_row_count(item) for item in snapshot)
    total_rows = sum(rows)
    viewport_height = max(0, request.viewport_height)
    overscan = max(0, request.overscan)
    start_row = max(0, request.scroll_offset - overscan)
    end_row = max(start_row, request.scroll_offset + viewport_height + overscan)
    visible: list[VisibleTranscriptItem] = []
    row = 0

    for item, row_count in zip(snapshot, rows, strict=True):
        next_row = row + row_count
        if next_row > start_row and row < end_row:
            visible.append(
                VisibleTranscriptItem(
                    item=item,
                    row_start=row,
                    row_count=row_count,
                )
            )
        row = next_row
        if row >= end_row:
            break

    return ViewportProjection(
        items=tuple(visible),
        total_items=len(snapshot),
        total_rows=total_rows,
        visible_rows=max(0, min(total_rows, end_row) - start_row),
    )


def _item_prefix(item: TranscriptItem) -> str:
    if isinstance(item, MessageItem):
        return "message"
    if isinstance(item, ToolItem):
        return "tool"
    if isinstance(item, RouterDecisionItem):
        return "router"
    if isinstance(item, StatusItem):
        return "status"
    if isinstance(item, UsageItem):
        return "usage"
    raise TypeError(f"Unsupported transcript item: {type(item)!r}")


def _line_count(text: str) -> int:
    if not text:
        return 1
    return text.count("\n") + 1


def _truncate_chars(text: str, max_chars: int, *, marker: str) -> tuple[str, bool]:
    if max_chars < 0 or len(text) <= max_chars:
        return text, False
    return f"{text[:max_chars]}{marker}", True


def _truncate_lines(text: str, max_lines: int) -> tuple[str, bool]:
    if max_lines < 1:
        return "... truncated", bool(text)
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text, False
    return "\n".join([*lines[:max_lines], "... truncated"]), True


def _stringify_output(output: object) -> str:
    if isinstance(output, dict) and output.get("type") == "image":
        mime = output.get("mime", "image")
        width = output.get("width", "?")
        height = output.get("height", "?")
        return f"[image {mime} {width}x{height}]"
    if isinstance(output, str):
        return output
    return _stringify_json(output)


def _stringify_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        # Tool args/output are typed `object`: bytes, Paths, or circular
        # structures must degrade to a readable preview, not raise.
        return str(value)


def _row_count(item: TranscriptItem) -> int:
    if isinstance(item, ToolItem) and item.expanded:
        return max(1, item.detail_line_count)
    return 1
