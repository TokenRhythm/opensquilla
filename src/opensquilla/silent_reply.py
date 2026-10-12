"""Shared normalization for model-authored silent-reply control tokens."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Literal

type SilentReplyDelivery = Literal["visible", "suppressed"]
type SilentReplySuppressionReason = Literal["no_reply", "heartbeat_ack"]

NO_REPLY_TOKEN = "NO_REPLY"
HEARTBEAT_ACK_TOKEN = "HEARTBEAT_OK"
SILENT_REPLY_SENTINELS = frozenset({NO_REPLY_TOKEN, HEARTBEAT_ACK_TOKEN})

_HEARTBEAT_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_HEARTBEAT_UNCLOSED_THINK_RE = re.compile(r"<think>.*\Z", re.DOTALL)
_HEARTBEAT_FINAL_TAG_RE = re.compile(r"</?final>")
_INTERNAL_MIXED_SENTINEL_RUN_KINDS = frozenset({"goal", "heartbeat"})
_MARKDOWN_SENTINEL_WRAPPERS = (
    ("**", "**"),
    ("__", "__"),
    ("~~", "~~"),
    ("*", "*"),
    ("_", "_"),
    ("`", "`"),
)
_MARKDOWN_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,}).*$")


@dataclass(frozen=True, slots=True)
class SilentReplyNormalization:
    """Canonical text and delivery decision for one assistant payload."""

    text: str
    changed: bool
    suppressed: bool
    sentinel: str | None
    delivery: SilentReplyDelivery
    suppression_reason: SilentReplySuppressionReason | None


@dataclass(frozen=True, slots=True)
class SilentReplySegmentsNormalization:
    """Copy-on-write normalization result for persisted turn segments."""

    segments: list[dict[str, Any]]
    changed: bool
    suppressed: bool
    sentinels: tuple[str, ...]
    delivery: SilentReplyDelivery
    suppression_reason: SilentReplySuppressionReason | None


@dataclass(frozen=True, slots=True)
class HistoricalSilentReplySanitization:
    """Sanitized assistant content and segments for read-time projections."""

    content: Any
    segments: list[dict[str, Any]] | None
    changed: bool
    suppressed: bool
    delivery: SilentReplyDelivery
    suppression_reason: SilentReplySuppressionReason | None


def _suppression_reason(sentinel: str | None) -> SilentReplySuppressionReason | None:
    if sentinel == NO_REPLY_TOKEN:
        return "no_reply"
    if sentinel == HEARTBEAT_ACK_TOKEN:
        return "heartbeat_ack"
    return None


def _result(
    original: str,
    text: str,
    *,
    sentinel: str | None = None,
    suppressed: bool = False,
) -> SilentReplyNormalization:
    return SilentReplyNormalization(
        text=text,
        changed=text != original,
        suppressed=suppressed,
        sentinel=sentinel,
        delivery="suppressed" if suppressed else "visible",
        suppression_reason=_suppression_reason(sentinel) if suppressed else None,
    )


def _sentinel_from_line(line: str) -> str | None:
    # In Markdown, a tab or four leading spaces makes an indented code block.
    # Up to three spaces remain ordinary presentation whitespace around a
    # control line.
    indentation_columns = 0
    for character in line:
        if character == " ":
            indentation_columns += 1
        elif character == "\t":
            indentation_columns += 4 - (indentation_columns % 4)
        else:
            break
        if indentation_columns >= 4:
            return None
    candidate = line.strip()
    if candidate in SILENT_REPLY_SENTINELS:
        return candidate
    # Triple backticks are fenced-code delimiters, not presentation around a
    # control token. A quote prefix likewise remains unmatched by construction.
    if candidate.startswith("```") or candidate.endswith("```"):
        return None
    while candidate:
        unwrapped = candidate
        for prefix, suffix in _MARKDOWN_SENTINEL_WRAPPERS:
            if (
                candidate.startswith(prefix)
                and candidate.endswith(suffix)
                and len(candidate) > len(prefix) + len(suffix)
            ):
                unwrapped = candidate[len(prefix) : -len(suffix)].strip()
                break
        if unwrapped == candidate:
            return None
        candidate = unwrapped
        if candidate in SILENT_REPLY_SENTINELS:
            return candidate
    return None


def _markdown_fence_mask(lines: Sequence[str]) -> list[bool]:
    """Mark lines that are fenced Markdown code, including an unclosed tail."""

    protected: list[bool] = []
    fence_character: str | None = None
    fence_length = 0
    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if fence_character is None:
            opening = _MARKDOWN_FENCE_OPEN_RE.match(line)
            if opening is None:
                protected.append(False)
                continue
            marker = opening.group(1)
            fence_character = marker[0]
            fence_length = len(marker)
            protected.append(True)
            continue

        protected.append(True)
        closing = re.fullmatch(
            rf" {{0,3}}{re.escape(fence_character)}{{{fence_length},}}[ \t]*",
            line,
        )
        if closing is not None:
            fence_character = None
            fence_length = 0
    return protected


def _edge_sentinel_lines(text: str) -> tuple[str, tuple[str, ...]]:
    """Remove only whole sentinel lines at the visible payload edges."""

    lines = text.splitlines(keepends=True)
    if not lines:
        return text, ()
    fenced_code = _markdown_fence_mask(lines)

    leading_tokens: list[str] = []
    left_probe = 0
    while left_probe < len(lines):
        while left_probe < len(lines) and not lines[left_probe].strip():
            left_probe += 1
        if left_probe >= len(lines):
            break
        if fenced_code[left_probe]:
            break
        candidate = _sentinel_from_line(lines[left_probe])
        if candidate is None:
            break
        leading_tokens.append(candidate)
        left_probe += 1
    left = left_probe if leading_tokens else 0

    trailing_tokens: list[str] = []
    right_probe = len(lines) - 1
    while right_probe >= left:
        while right_probe >= left and not lines[right_probe].strip():
            right_probe -= 1
        if right_probe < left:
            break
        if fenced_code[right_probe]:
            break
        candidate = _sentinel_from_line(lines[right_probe])
        if candidate is None:
            break
        trailing_tokens.append(candidate)
        right_probe -= 1
    right = right_probe + 1 if trailing_tokens else len(lines)

    sentinels = (*leading_tokens, *reversed(trailing_tokens))
    if not sentinels:
        return text, ()
    projected = "".join(lines[left:right])
    if trailing_tokens:
        # Remove only the line break that separated substantive text from a
        # removed trailing marker. Preserve indentation and ordinary spaces in
        # the remaining body.
        projected = projected.rstrip("\r\n")
    return projected, sentinels


def normalize_silent_reply(
    text: str,
    *,
    run_kind: str,
    input_mode: str | None = None,
    heartbeat_ack_max_chars: int = 300,
) -> SilentReplyNormalization:
    """Normalize the silent-reply protocol without deleting ordinary prose.

    Exact sentinel-only output remains suppressed for every run kind for
    compatibility. Mixed output is interpreted only for an internal system
    event, Goal continuation, or heartbeat, and only when the token occupies a
    complete leading or trailing logical line. Tokens embedded in prose or code
    remain ordinary model output.
    """

    stripped = text.strip()
    if stripped in SILENT_REPLY_SENTINELS:
        return _result(text, "", sentinel=stripped, suppressed=True)

    normalized = text
    if run_kind == "heartbeat":
        normalized = _HEARTBEAT_THINK_BLOCK_RE.sub("", normalized)
        normalized = _HEARTBEAT_UNCLOSED_THINK_RE.sub("", normalized)
        normalized = _HEARTBEAT_FINAL_TAG_RE.sub("", normalized)
        if normalized != text:
            normalized = normalized.strip()

        heartbeat_stripped = normalized.strip()
        if heartbeat_stripped in SILENT_REPLY_SENTINELS:
            return _result(
                text,
                "",
                sentinel=heartbeat_stripped,
                suppressed=True,
            )

        def _short_ack(payload: str) -> bool:
            return len(payload.strip()) <= heartbeat_ack_max_chars

        if heartbeat_stripped.startswith(HEARTBEAT_ACK_TOKEN):
            remainder = heartbeat_stripped[len(HEARTBEAT_ACK_TOKEN) :].strip()
            if _short_ack(remainder):
                return _result(
                    text,
                    "",
                    sentinel=HEARTBEAT_ACK_TOKEN,
                    suppressed=True,
                )
        if heartbeat_stripped.endswith(HEARTBEAT_ACK_TOKEN):
            remainder = heartbeat_stripped[: -len(HEARTBEAT_ACK_TOKEN)].strip()
            if _short_ack(remainder):
                return _result(
                    text,
                    "",
                    sentinel=HEARTBEAT_ACK_TOKEN,
                    suppressed=True,
                )

    mixed_sentinels_allowed = (
        input_mode == "system_event" or run_kind in _INTERNAL_MIXED_SENTINEL_RUN_KINDS
    )
    if mixed_sentinels_allowed:
        normalized, sentinels = _edge_sentinel_lines(normalized)
        if sentinels:
            sentinel = sentinels[0]
            if not normalized:
                return _result(text, "", sentinel=sentinel, suppressed=True)
            return _result(text, normalized, sentinel=sentinel)

    return _result(text, normalized)


def is_silent_reply_prefix(text: str) -> bool:
    """Return whether *text* is an exact or distinctive partial sentinel.

    This is intended for cancellation cleanup of a fully buffered internal
    stream. It deliberately rejects a one-letter ``N`` and natural-language
    ``HEART...`` prefixes to avoid discarding ordinary partial replies.
    """

    candidate = text.strip()
    if candidate in SILENT_REPLY_SENTINELS:
        return True
    if candidate != candidate.upper():
        return False
    if len(candidate) >= 2 and NO_REPLY_TOKEN.startswith(candidate):
        return candidate == "NO" or "_" in candidate
    return candidate.startswith("HEARTBEAT_") and HEARTBEAT_ACK_TOKEN.startswith(candidate)


def sanitize_silent_reply_segments(
    segments: Sequence[Mapping[str, Any]] | None,
    *,
    run_kind: str,
    input_mode: str | None = None,
    heartbeat_ack_max_chars: int = 300,
) -> SilentReplySegmentsNormalization:
    """Normalize text segments without mutating the caller's objects."""

    normalized_segments = [dict(segment) for segment in segments or ()]
    text_segments = [
        segment
        for segment in normalized_segments
        if segment.get("type") == "text" and isinstance(segment.get("text"), str)
    ]
    saw_text = bool(text_segments)
    raw_text = "".join(str(segment["text"]) for segment in text_segments)
    aggregate = normalize_silent_reply(
        raw_text,
        run_kind=run_kind,
        input_mode=input_mode,
        heartbeat_ack_max_chars=heartbeat_ack_max_chars,
    )
    changed = aggregate.changed
    sentinels: list[str] = [aggregate.sentinel] if aggregate.sentinel else []

    if aggregate.changed:
        projected = _project_canonical_text_to_segments(
            normalized_segments,
            canonical_text=aggregate.text,
        )
        if projected is not None:
            normalized_segments = projected

    # A tool boundary is also a presentation boundary. If an outer text
    # carrier consists solely of a marker, remove that carrier without
    # treating an identical middle carrier as a reply boundary.
    if input_mode == "system_event" or run_kind in _INTERNAL_MIXED_SENTINEL_RUN_KINDS:
        while True:
            index = next(
                (
                    index
                    for index, segment in enumerate(normalized_segments)
                    if segment.get("type") == "text"
                    and isinstance(segment.get("text"), str)
                    and bool(segment["text"].strip())
                ),
                None,
            )
            if index is None:
                break
            result = normalize_silent_reply(
                normalized_segments[index]["text"],
                run_kind=run_kind,
                input_mode=input_mode,
                heartbeat_ack_max_chars=heartbeat_ack_max_chars,
            )
            if not result.suppressed:
                break
            if result.sentinel is not None:
                sentinels.append(result.sentinel)
            normalized_segments.pop(index)
            changed = True

        while True:
            index = next(
                (
                    index
                    for index in range(len(normalized_segments) - 1, -1, -1)
                    if normalized_segments[index].get("type") == "text"
                    and isinstance(normalized_segments[index].get("text"), str)
                    and bool(normalized_segments[index]["text"].strip())
                ),
                None,
            )
            if index is None:
                break
            result = normalize_silent_reply(
                normalized_segments[index]["text"],
                run_kind=run_kind,
                input_mode=input_mode,
                heartbeat_ack_max_chars=heartbeat_ack_max_chars,
            )
            if not result.suppressed:
                break
            if result.sentinel is not None:
                sentinels.append(result.sentinel)
            normalized_segments.pop(index)
            changed = True

    visible_text = any(
        segment.get("type") == "text"
        and isinstance(segment.get("text"), str)
        and bool(segment["text"].strip())
        for segment in normalized_segments
    )
    saw_sentinel = bool(sentinels)
    suppressed = saw_text and saw_sentinel and not visible_text
    if suppressed:
        # Do not let an otherwise empty text carrier make a silent assistant
        # row look persistable. Tool/artifact lifecycle records remain intact.
        normalized_segments = [
            segment
            for segment in normalized_segments
            if segment.get("type") != "text"
            or not isinstance(segment.get("text"), str)
            or bool(segment["text"].strip())
        ]
    sentinel = sentinels[0] if sentinels else None
    return SilentReplySegmentsNormalization(
        segments=normalized_segments,
        changed=changed,
        suppressed=suppressed,
        sentinels=tuple(sentinels),
        delivery="suppressed" if suppressed else "visible",
        suppression_reason=_suppression_reason(sentinel) if suppressed else None,
    )


def _project_canonical_text_to_segments(
    segments: list[dict[str, Any]],
    *,
    canonical_text: str,
) -> list[dict[str, Any]] | None:
    """Project a deletion-only normalization back onto ordered text carriers."""

    text_values = [
        str(segment["text"])
        for segment in segments
        if segment.get("type") == "text" and isinstance(segment.get("text"), str)
    ]
    raw_text = "".join(text_values)
    if raw_text == canonical_text:
        return [dict(segment) for segment in segments]
    if not canonical_text:
        return [
            dict(segment)
            for segment in segments
            if segment.get("type") != "text" or not isinstance(segment.get("text"), str)
        ]

    matcher = SequenceMatcher(None, raw_text, canonical_text, autojunk=False)
    retained_ranges: list[tuple[int, int]] = []
    for tag, raw_start, raw_end, canonical_start, canonical_end in matcher.get_opcodes():
        if tag == "equal":
            retained_ranges.append((raw_start, raw_end))
            continue
        if tag == "delete":
            continue
        # Silent-reply normalization is deletion-only. Refuse an ambiguous
        # projection rather than moving text across a tool boundary.
        return None

    projected: list[dict[str, Any]] = []
    cursor = 0
    for raw_segment in segments:
        segment = dict(raw_segment)
        value = segment.get("text")
        if segment.get("type") != "text" or not isinstance(value, str):
            projected.append(segment)
            continue
        start = cursor
        end = cursor + len(value)
        cursor = end
        kept = "".join(
            value[max(raw_start, start) - start : min(raw_end, end) - start]
            for raw_start, raw_end in retained_ranges
            if raw_start < end and raw_end > start
        )
        if kept:
            segment["text"] = kept
            projected.append(segment)

    projected_text = "".join(
        str(segment["text"])
        for segment in projected
        if segment.get("type") == "text" and isinstance(segment.get("text"), str)
    )
    return projected if projected_text == canonical_text else None


def _history_context(turn_context: Mapping[str, Any] | None) -> tuple[str, str | None]:
    context = turn_context or {}
    run_kind = str(context.get("run_kind") or "default")
    input_mode_raw = context.get("input_mode")
    input_mode = str(input_mode_raw) if input_mode_raw else None
    if str(context.get("intent") or "") == "goal_continuation":
        return "goal", "system_event"
    return run_kind, input_mode


def _sanitize_history_content(
    content: Any,
    *,
    run_kind: str,
    input_mode: str | None,
    heartbeat_ack_max_chars: int,
) -> tuple[Any, bool, bool, SilentReplySuppressionReason | None]:
    parsed_from_json = False
    parsed = content
    if isinstance(content, str) and content.lstrip().startswith("{"):
        try:
            candidate = json.loads(content)
        except (TypeError, ValueError):
            candidate = None
        if isinstance(candidate, dict) and (
            isinstance(candidate.get("text"), str)
            or isinstance(candidate.get("display_text"), str)
        ):
            parsed = candidate
            parsed_from_json = True

    if isinstance(parsed, Mapping):
        payload = dict(parsed)
        changed = False
        saw_sentinel = False
        visible_text = False
        reason: SilentReplySuppressionReason | None = None
        for key in ("display_text", "text"):
            value = payload.get(key)
            if not isinstance(value, str):
                continue
            result = normalize_silent_reply(
                value,
                run_kind=run_kind,
                input_mode=input_mode,
                heartbeat_ack_max_chars=heartbeat_ack_max_chars,
            )
            if result.changed:
                payload[key] = result.text
                changed = True
            saw_sentinel = saw_sentinel or result.sentinel is not None
            visible_text = visible_text or bool(result.text.strip())
            reason = reason or result.suppression_reason
        suppressed = saw_sentinel and not visible_text
        if not changed:
            return content, False, suppressed, reason if suppressed else None
        if parsed_from_json:
            return (
                json.dumps(payload, ensure_ascii=False),
                True,
                suppressed,
                reason if suppressed else None,
            )
        return payload, True, suppressed, reason if suppressed else None

    if isinstance(content, str):
        result = normalize_silent_reply(
            content,
            run_kind=run_kind,
            input_mode=input_mode,
            heartbeat_ack_max_chars=heartbeat_ack_max_chars,
        )
        return result.text, result.changed, result.suppressed, result.suppression_reason
    return content, False, False, None


def sanitize_historical_silent_reply(
    content: Any,
    segments: Sequence[Mapping[str, Any]] | None,
    *,
    role: str,
    turn_context: Mapping[str, Any] | None,
    heartbeat_ack_max_chars: int = 300,
) -> HistoricalSilentReplySanitization:
    """Sanitize one historical projection while preserving its stored source."""

    copied_segments = [dict(segment) for segment in segments] if segments is not None else None
    if role != "assistant":
        return HistoricalSilentReplySanitization(
            content=content,
            segments=copied_segments,
            changed=False,
            suppressed=False,
            delivery="visible",
            suppression_reason=None,
        )

    run_kind, input_mode = _history_context(turn_context)
    content_out, content_changed, content_suppressed, content_reason = (
        _sanitize_history_content(
            content,
            run_kind=run_kind,
            input_mode=input_mode,
            heartbeat_ack_max_chars=heartbeat_ack_max_chars,
        )
    )
    segment_out = sanitize_silent_reply_segments(
        segments,
        run_kind=run_kind,
        input_mode=input_mode,
        heartbeat_ack_max_chars=heartbeat_ack_max_chars,
    )
    has_visible_segment_text = any(
        segment.get("type") == "text"
        and isinstance(segment.get("text"), str)
        and bool(segment["text"].strip())
        for segment in segment_out.segments
    )
    has_visible_content = False
    if isinstance(content_out, str):
        if content_out.lstrip().startswith("{"):
            try:
                content_payload = json.loads(content_out)
            except (TypeError, ValueError):
                content_payload = None
            if isinstance(content_payload, dict):
                has_visible_content = any(
                    isinstance(content_payload.get(key), str)
                    and bool(content_payload[key].strip())
                    for key in ("display_text", "text")
                )
            else:
                has_visible_content = bool(content_out.strip())
        else:
            has_visible_content = bool(content_out.strip())
    elif isinstance(content_out, Mapping):
        has_visible_content = any(
            isinstance(content_out.get(key), str) and bool(content_out[key].strip())
            for key in ("display_text", "text")
        )

    saw_suppression = content_suppressed or segment_out.suppressed
    suppressed = saw_suppression and not has_visible_content and not has_visible_segment_text
    reason = content_reason or segment_out.suppression_reason
    return HistoricalSilentReplySanitization(
        content=content_out,
        segments=segment_out.segments if segments is not None else None,
        changed=content_changed or segment_out.changed,
        suppressed=suppressed,
        delivery="suppressed" if suppressed else "visible",
        suppression_reason=reason if suppressed else None,
    )


__all__ = [
    "HEARTBEAT_ACK_TOKEN",
    "HistoricalSilentReplySanitization",
    "NO_REPLY_TOKEN",
    "SILENT_REPLY_SENTINELS",
    "SilentReplyDelivery",
    "SilentReplyNormalization",
    "SilentReplySegmentsNormalization",
    "SilentReplySuppressionReason",
    "is_silent_reply_prefix",
    "normalize_silent_reply",
    "sanitize_historical_silent_reply",
    "sanitize_silent_reply_segments",
]
