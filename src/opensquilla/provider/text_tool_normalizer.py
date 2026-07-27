"""Trusted normalization of provider text that may encode tool calls.

The normalizer owns the boundary between literal model text and executable
tool events.  It is deliberately independent from UI/display filtering:
unconfirmed, malformed, unauthorized, oversized, or truncated candidates are
returned byte-for-byte as literal text.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from .compat_policy import (
    TEXT_TOOL_DIALECT_MINIMAX_XML,
    TEXT_TOOL_DIALECT_PLAIN_JSON,
    TEXT_TOOL_DIALECT_QWEN_TAG,
    TextToolDialect,
)
from .types import ToolDefinition

log = structlog.get_logger(__name__)

MAX_TEXT_TOOL_CANDIDATE_CHARS = 256_000
PLAIN_TOOL_INTERSTITIAL_WHITESPACE_MAX = 8

_TOOL_NAME_CHARS = r"A-Za-z0-9_.:-"
_PLAIN_JSON_TOOL_PREFIX_RE = re.compile(
    rf"([A-Za-z_][A-Za-z0-9_.:-]*)[ \t]{{0,{PLAIN_TOOL_INTERSTITIAL_WHITESPACE_MAX}}}(?=\{{)",
)
_QWEN_TOOL_CALL_OPEN_RE = re.compile(r"<tool_call>", re.IGNORECASE)
_QWEN_TOOL_CALL_CLOSE_RE = re.compile(r"</tool_call>", re.IGNORECASE)
_QWEN_FUNCTION_OPEN_RE = re.compile(r"<function=([^>]+)>", re.IGNORECASE)
_QWEN_FUNCTION_CLOSE_RE = re.compile(r"</function>", re.IGNORECASE)
_QWEN_PARAMETER_OPEN_RE = re.compile(r"<parameter=([^>]+)>", re.IGNORECASE)
_QWEN_PARAMETER_CLOSE_RE = re.compile(r"</parameter>", re.IGNORECASE)
_MINIMAX_TOOL_CALL_OPEN_RE = re.compile(
    r"<minimax:tool_call>",
    re.IGNORECASE,
)
_MINIMAX_TOOL_CALL_CLOSE_RE = re.compile(r"</minimax:tool_call>", re.IGNORECASE)
_MINIMAX_INVOKE_OPEN_RE = re.compile(
    r'<invoke\s+name\s*=\s*"(?P<name>[^"]+)"\s*>',
    re.IGNORECASE,
)
_MINIMAX_INVOKE_CLOSE_RE = re.compile(r"</invoke>", re.IGNORECASE)
_MINIMAX_PARAMETER_OPEN_RE = re.compile(
    r'<parameter\s+name\s*=\s*"(?P<name>[^"]+)"\s*>',
    re.IGNORECASE,
)
_MINIMAX_PARAMETER_CLOSE_RE = re.compile(r"</parameter>", re.IGNORECASE)
_STRUCTURED_TOOL_SCAFFOLD_RE = re.compile(
    r"<details><summary>View areas around line\b[\s\S]*?</details>\s*$",
    re.IGNORECASE,
)
_STRUCTURED_TOOL_SCAFFOLD_OPEN_RE = re.compile(
    r"<details><summary>View areas around line\b",
    re.IGNORECASE,
)
_CANONICAL_QWEN_PREFIX = "<tool_call>"
_CANONICAL_MINIMAX_PREFIX = "<minimax:tool_call>"
_CANONICAL_SCAFFOLD_PREFIX = "<details><summary>view areas around line"
_MARKDOWN_FENCE_LINE_RE = re.compile(r" {0,3}(?P<fence>`{3,}|~{3,})")
_RAW_HTML_TAGS = ("pre", "code", "script", "style", "textarea")
_RAW_HTML_COMMENT_OPEN = "<!--"
_RAW_HTML_COMMENT_CLOSE = "-->"
_RAW_HTML_TOKEN_MAX_CHARS = 4_096
_RAW_HTML_MAX_DEPTH = 32


def _reject_nonstandard_json_constant(value: str) -> Any:
    raise ValueError(f"non-standard JSON constant: {value}")


def _strict_json_loads(value: str) -> Any:
    return json.loads(value, parse_constant=_reject_nonstandard_json_constant)


def _is_strict_json_value(value: Any) -> bool:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return False
    return True


@dataclass(frozen=True)
class _MarkdownCodeRange:
    start: int
    end: int
    closed: bool


@dataclass(frozen=True)
class _RawHtmlState:
    tags: tuple[str, ...] = ()
    in_comment: bool = False
    opaque: bool = False
    trailing_token: str = ""


@dataclass(frozen=True)
class _HtmlTagScan:
    end: int
    name: str
    closing: bool
    valid_name_boundary: bool
    complete: bool
    oversized: bool
    self_closing: bool = False
    exact_close: bool = False


@dataclass(frozen=True)
class SyntheticTextToolCall:
    tool_name: str
    arguments: dict[str, Any]
    dialect: TextToolDialect
    parse_format: str


@dataclass(frozen=True)
class LiteralTextSegment:
    text: str


@dataclass(frozen=True)
class SyntheticToolSegment:
    calls: tuple[SyntheticTextToolCall, ...]
    source_text: str


type TextToolSegment = LiteralTextSegment | SyntheticToolSegment


@dataclass(frozen=True)
class _PendingStart:
    start: int
    kind: str  # partial | literal_partial | protocol


@dataclass(frozen=True)
class _RawTextToolCall:
    tool_name: str
    arguments: dict[str, Any]
    parse_format: str


@dataclass(frozen=True)
class _ProtocolSpan:
    start: int
    end: int
    calls: tuple[_RawTextToolCall, ...]


def _mask_candidate_spans(
    text: str,
    spans: list[tuple[int, int]],
) -> str:
    """Hide candidate bodies while preserving offsets and line boundaries.

    A non-whitespace sentinel is intentional: replacing a call at column zero
    with spaces would manufacture an indented-code signal that was not present
    outside the candidate.
    """

    masked = list(text)
    for start, end in spans:
        for index in range(start, end):
            if masked[index] not in {"\n", "\r"}:
                masked[index] = "\0"
    return "".join(masked)


def _markdown_line_spans(text: str) -> Iterator[tuple[int, int, int]]:
    """Return ``(start, content_end, line_end)`` for CR/LF-delimited lines."""

    line_start = 0
    while line_start < len(text):
        cr = text.find("\r", line_start)
        lf = text.find("\n", line_start)
        endings = [position for position in (cr, lf) if position >= 0]
        if not endings:
            yield line_start, len(text), len(text)
            break
        content_end = min(endings)
        line_end = content_end + (
            2 if text[content_end : content_end + 2] == "\r\n" else 1
        )
        yield line_start, content_end, line_end
        line_start = line_end


def _fenced_code_ranges(text: str) -> list[_MarkdownCodeRange]:
    """Find Markdown fences using the same line-level rules as the stream state."""

    ranges: list[_MarkdownCodeRange] = []
    active_marker = ""
    active_start = 0
    for line_start, content_end, line_end in _markdown_line_spans(text):
        line = text[line_start:content_end]
        match = _MARKDOWN_FENCE_LINE_RE.match(line)
        marker = match.group("fence") if match is not None else ""
        if active_marker:
            remainder = line[match.end("fence") :] if match is not None else ""
            if (
                marker
                and marker[0] == active_marker[0]
                and len(marker) >= len(active_marker)
                and not remainder.strip()
            ):
                ranges.append(_MarkdownCodeRange(active_start, line_end, True))
                active_marker = ""
            continue
        if marker:
            assert match is not None
            active_marker = marker
            active_start = line_start + match.start("fence")
    if active_marker:
        ranges.append(_MarkdownCodeRange(active_start, len(text), False))
    return ranges


def _fenced_or_inline_code_ranges(text: str) -> list[_MarkdownCodeRange]:
    """Find fenced blocks first, then conservative inline backtick spans.

    A fenced close must be a same-character run of at least the opening length,
    preceded by at most three spaces and followed only by horizontal whitespace.
    Treating an arbitrary same-length run inside the body as a close can promote a
    protocol example that is still inside an unclosed code block into a tool call.
    """

    fenced = _fenced_code_ranges(text)
    ranges = list(fenced)
    index = 0
    fence_index = 0
    while index < len(text):
        while fence_index < len(fenced) and fenced[fence_index].end <= index:
            fence_index += 1
        if (
            fence_index < len(fenced)
            and fenced[fence_index].start <= index < fenced[fence_index].end
        ):
            index = fenced[fence_index].end
            continue
        if text[index] != "`":
            index += 1
            continue

        run_end = index
        while run_end < len(text) and text[run_end] == "`":
            run_end += 1
        delimiter_len = run_end - index
        search = run_end
        search_fence_index = fence_index
        close_end = -1
        while search < len(text):
            while (
                search_fence_index < len(fenced)
                and fenced[search_fence_index].end <= search
            ):
                search_fence_index += 1
            if (
                search_fence_index < len(fenced)
                and fenced[search_fence_index].start
                <= search
                < fenced[search_fence_index].end
            ):
                search = fenced[search_fence_index].end
                continue
            close = text.find("`", search)
            if close < 0:
                break
            while (
                search_fence_index < len(fenced)
                and fenced[search_fence_index].end <= close
            ):
                search_fence_index += 1
            if (
                search_fence_index < len(fenced)
                and fenced[search_fence_index].start
                <= close
                < fenced[search_fence_index].end
            ):
                search = fenced[search_fence_index].end
                continue
            close_run_end = close
            while close_run_end < len(text) and text[close_run_end] == "`":
                close_run_end += 1
            if close_run_end - close == delimiter_len:
                close_end = close_run_end
                break
            search = close_run_end
        if close_end < 0:
            ranges.append(_MarkdownCodeRange(index, len(text), False))
            break
        ranges.append(_MarkdownCodeRange(index, close_end, True))
        index = close_end
    ranges.sort(key=lambda item: item.start)
    return ranges


def _comment_close_partial_suffix(text: str) -> str:
    for size in range(min(len(text), len(_RAW_HTML_COMMENT_CLOSE) - 1), 0, -1):
        suffix = text[-size:]
        if _RAW_HTML_COMMENT_CLOSE.startswith(suffix):
            return suffix
    return ""


def _scan_html_tag_at(text: str, start: int) -> _HtmlTagScan:
    """Scan one bounded HTML-like tag, respecting quotes around attributes."""

    cursor = start + 1
    closing = cursor < len(text) and text[cursor] == "/"
    if closing:
        cursor += 1
    name_start = cursor
    while cursor < len(text) and (
        text[cursor].isalnum() or text[cursor] in {"-", "_", ":"}
    ):
        cursor += 1
    name = text[name_start:cursor].lower()
    valid_name_boundary = cursor >= len(text) or text[cursor].isspace() or text[cursor] in {
        "/",
        ">",
    }
    name_end = cursor
    quote = ""
    while cursor < len(text):
        if cursor - start >= _RAW_HTML_TOKEN_MAX_CHARS:
            return _HtmlTagScan(
                end=cursor,
                name=name,
                closing=closing,
                valid_name_boundary=valid_name_boundary,
                complete=False,
                oversized=True,
            )
        char = text[cursor]
        if quote:
            if char == quote:
                quote = ""
        elif char in {'"', "'"}:
            quote = char
        elif char == ">":
            remainder = text[name_end:cursor]
            return _HtmlTagScan(
                end=cursor + 1,
                name=name,
                closing=closing,
                valid_name_boundary=valid_name_boundary,
                complete=True,
                oversized=False,
                self_closing=not closing and remainder.rstrip().endswith("/"),
                exact_close=closing and valid_name_boundary and not remainder.strip(),
            )
        cursor += 1
    return _HtmlTagScan(
        end=len(text),
        name=name,
        closing=closing,
        valid_name_boundary=valid_name_boundary,
        complete=False,
        oversized=False,
    )


def _possible_raw_open_partial(candidate: str, scan: _HtmlTagScan) -> bool:
    if scan.closing:
        return False
    lowered = candidate.lower()
    if _RAW_HTML_COMMENT_OPEN.startswith(lowered):
        return True
    if scan.name:
        return any(tag.startswith(scan.name) for tag in _RAW_HTML_TAGS)
    return any(f"<{tag}".startswith(lowered) for tag in _RAW_HTML_TAGS)


def _confirmed_raw_open(scan: _HtmlTagScan) -> bool:
    return (
        not scan.closing
        and scan.valid_name_boundary
        and scan.name in _RAW_HTML_TAGS
    )


def _state_without_trailing(state: _RawHtmlState) -> _RawHtmlState:
    return _RawHtmlState(
        tags=state.tags,
        in_comment=state.in_comment,
        opaque=state.opaque,
    )


def _scan_raw_html_code_ranges(
    text: str,
    *,
    initial_state: _RawHtmlState | None = None,
) -> tuple[list[_MarkdownCodeRange], _RawHtmlState]:
    """Single-pass raw-HTML scanner shared by batch and streaming paths.

    The continuation state is deliberately conservative. An oversized unfinished
    token permanently becomes opaque for the response rather than forgetting quote
    state and later accepting a fake close tag.
    """

    state = _state_without_trailing(initial_state or _RawHtmlState())
    if state.opaque:
        opaque_ranges = [_MarkdownCodeRange(0, len(text), False)] if text else []
        return opaque_ranges, state

    ranges: list[_MarkdownCodeRange] = []
    tags = list(state.tags)
    in_comment = state.in_comment
    context_start = 0 if tags or in_comment else -1
    cursor = 0
    while cursor < len(text):
        if in_comment:
            close = text.find(_RAW_HTML_COMMENT_CLOSE, cursor)
            if close < 0:
                if context_start >= 0:
                    ranges.append(_MarkdownCodeRange(context_start, len(text), False))
                return ranges, _RawHtmlState(
                    tags=tuple(tags),
                    in_comment=True,
                    trailing_token=_comment_close_partial_suffix(text),
                )
            cursor = close + len(_RAW_HTML_COMMENT_CLOSE)
            in_comment = False
            if not tags:
                ranges.append(_MarkdownCodeRange(context_start, cursor, True))
                context_start = -1
            continue

        token_start = text.find("<", cursor)
        if token_start < 0:
            break
        if text.startswith(_RAW_HTML_COMMENT_OPEN, token_start):
            if context_start < 0:
                context_start = token_start
            in_comment = True
            cursor = token_start + len(_RAW_HTML_COMMENT_OPEN)
            continue

        scan = _scan_html_tag_at(text, token_start)
        if not scan.complete:
            candidate = text[token_start:]
            if scan.oversized:
                if tags or _confirmed_raw_open(scan):
                    if context_start < 0:
                        context_start = token_start
                    ranges.append(_MarkdownCodeRange(context_start, len(text), False))
                    poison_tags = tuple(tags) or ((scan.name,) if scan.name else ())
                    return ranges, _RawHtmlState(tags=poison_tags, opaque=True)
                break

            trailing_token = candidate
            if tags:
                ranges.append(_MarkdownCodeRange(context_start, len(text), False))
                return ranges, _RawHtmlState(
                    tags=tuple(tags),
                    trailing_token=trailing_token,
                )
            if _confirmed_raw_open(scan):
                ranges.append(_MarkdownCodeRange(token_start, len(text), False))
                return ranges, _RawHtmlState(trailing_token=trailing_token)
            if _possible_raw_open_partial(candidate, scan):
                return ranges, _RawHtmlState(trailing_token=trailing_token)
            break

        if tags:
            if scan.exact_close and scan.name == tags[-1]:
                tags.pop()
                cursor = scan.end
                if not tags:
                    ranges.append(_MarkdownCodeRange(context_start, cursor, True))
                    context_start = -1
                continue
            if (
                _confirmed_raw_open(scan)
                and not scan.self_closing
            ):
                if len(tags) >= _RAW_HTML_MAX_DEPTH:
                    ranges.append(_MarkdownCodeRange(context_start, len(text), False))
                    return ranges, _RawHtmlState(tags=tuple(tags), opaque=True)
                tags.append(scan.name)
            cursor = scan.end
            continue

        if _confirmed_raw_open(scan) and not scan.self_closing:
            tags.append(scan.name)
            context_start = token_start
        cursor = scan.end

    if context_start >= 0:
        ranges.append(_MarkdownCodeRange(context_start, len(text), False))
    return ranges, _RawHtmlState(tags=tuple(tags), in_comment=in_comment)


def _raw_html_code_ranges(text: str) -> list[_MarkdownCodeRange]:
    ranges, _state = _scan_raw_html_code_ranges(text)
    return ranges


def _project_code_ranges(
    ranges: Sequence[_MarkdownCodeRange],
    *,
    offset: int,
    text_length: int,
) -> list[_MarkdownCodeRange]:
    projected: list[_MarkdownCodeRange] = []
    for code_range in ranges:
        if code_range.end <= offset or code_range.start >= offset + text_length:
            continue
        projected.append(
            _MarkdownCodeRange(
                max(0, code_range.start - offset),
                min(text_length, code_range.end - offset),
                code_range.closed,
            )
        )
    return projected


def _raw_html_ranges_from_state(
    text: str,
    state: _RawHtmlState,
    *,
    trailing_already_included: str = "",
) -> list[_MarkdownCodeRange]:
    trailing = state.trailing_token
    if trailing_already_included:
        if trailing_already_included.endswith(trailing):
            trailing = ""
        elif trailing.endswith(trailing_already_included):
            trailing = trailing[: -len(trailing_already_included)]
    offset = len(trailing)
    ranges, _state = _scan_raw_html_code_ranges(
        trailing + text,
        initial_state=_state_without_trailing(state),
    )
    return _project_code_ranges(ranges, offset=offset, text_length=len(text))


def _markdown_syntax_code_ranges(
    text: str,
    *,
    initial_line_spaces: int = 0,
    initial_line_indented: bool = False,
) -> list[_MarkdownCodeRange]:
    ranges = _fenced_or_inline_code_ranges(text)

    line_start = 0
    first_line = True
    while line_start < len(text):
        cr = text.find("\r", line_start)
        lf = text.find("\n", line_start)
        newline_positions = [position for position in (cr, lf) if position >= 0]
        if newline_positions:
            newline = min(newline_positions)
            line_end = newline + (
                2 if text[newline : newline + 2] == "\r\n" else 1
            )
        else:
            newline = -1
            line_end = len(text)
        line = text[line_start:line_end]
        leading_spaces = len(line) - len(line.lstrip(" "))
        indented = line.startswith("\t") or leading_spaces >= 4
        if first_line:
            indented = (
                initial_line_indented
                or line.startswith("\t")
                or initial_line_spaces + leading_spaces >= 4
            )
        if indented:
            ranges.append(_MarkdownCodeRange(line_start, line_end, True))
        if newline < 0:
            break
        line_start = line_end
        first_line = False
    ranges.sort(key=lambda item: item.start)
    return ranges


def _markdown_code_ranges(
    text: str,
    *,
    initial_line_spaces: int = 0,
    initial_line_indented: bool = False,
) -> list[_MarkdownCodeRange]:
    syntax_ranges = _markdown_syntax_code_ranges(
        text,
        initial_line_spaces=initial_line_spaces,
        initial_line_indented=initial_line_indented,
    )
    raw_ranges = _raw_html_code_ranges(
        _mask_candidate_spans(
            text,
            [(item.start, item.end) for item in syntax_ranges],
        )
    )
    syntax_ranges = _markdown_syntax_code_ranges(
        _mask_candidate_spans(
            text,
            [(item.start, item.end) for item in raw_ranges],
        ),
        initial_line_spaces=initial_line_spaces,
        initial_line_indented=initial_line_indented,
    )
    raw_ranges = _raw_html_code_ranges(
        _mask_candidate_spans(
            text,
            [(item.start, item.end) for item in syntax_ranges],
        )
    )
    ranges = syntax_ranges + raw_ranges
    ranges.sort(key=lambda item: item.start)
    return ranges


def _contextual_markdown_code_ranges(
    text: str,
    *,
    context_prefix: str,
    initial_line_spaces: int,
    initial_line_indented: bool,
    initial_raw_html_state: _RawHtmlState | None = None,
    raw_trailing_already_included: str = "",
) -> list[_MarkdownCodeRange]:
    """Project Markdown and raw-HTML ranges from bounded streaming state."""

    state = initial_raw_html_state or _RawHtmlState()

    def syntax_ranges(value: str) -> list[_MarkdownCodeRange]:
        offset = len(context_prefix)
        if context_prefix:
            ranges = _markdown_syntax_code_ranges(context_prefix + value)
        else:
            ranges = _markdown_syntax_code_ranges(
                value,
                initial_line_spaces=initial_line_spaces,
                initial_line_indented=initial_line_indented,
            )
        return _project_code_ranges(ranges, offset=offset, text_length=len(value))

    syntax = syntax_ranges(text)
    raw = _raw_html_ranges_from_state(
        _mask_candidate_spans(text, [(item.start, item.end) for item in syntax]),
        state,
        trailing_already_included=raw_trailing_already_included,
    )
    syntax = syntax_ranges(
        _mask_candidate_spans(text, [(item.start, item.end) for item in raw])
    )
    raw = _raw_html_ranges_from_state(
        _mask_candidate_spans(text, [(item.start, item.end) for item in syntax]),
        state,
        trailing_already_included=raw_trailing_already_included,
    )
    ranges = syntax + raw
    ranges.sort(key=lambda item: item.start)
    return ranges


def _markdown_fence_marker(line_prefix: str) -> str:
    match = _MARKDOWN_FENCE_LINE_RE.match(line_prefix)
    return match.group("fence") if match is not None else ""


def _position_is_in_code(position: int, ranges: list[_MarkdownCodeRange]) -> bool:
    return any(code_range.start <= position < code_range.end for code_range in ranges)


def _line_start(text: str, position: int) -> int:
    """Return the start of the CR/LF-delimited line containing ``position``."""

    return max(text.rfind("\r", 0, position), text.rfind("\n", 0, position)) + 1


def _line_end(text: str, position: int) -> int:
    """Return the end of the CR/LF-delimited line containing ``position``."""

    cr = text.find("\r", position)
    lf = text.find("\n", position)
    endings = [item for item in (cr, lf) if item >= 0]
    return min(endings) if endings else len(text)


def _candidate_starts_on_standalone_line(
    text: str,
    position: int,
    *,
    initial_line_spaces: int = 0,
    initial_line_indented: bool = False,
    initial_line_has_non_space: bool = False,
) -> bool:
    """Require a candidate to begin on its own non-Markdown-code line.

    Streaming classification may receive only the current chunk, so the
    initial line state describes raw text received before ``text``.  Up to
    three leading spaces are accepted; a tab or four spaces are Markdown code
    and must remain literal.
    """

    line_start = _line_start(text, position)
    prefix = text[line_start:position]
    if any(char != " " for char in prefix):
        return False
    if line_start:
        return len(prefix) <= 3
    if initial_line_indented or initial_line_has_non_space:
        return False
    return initial_line_spaces + len(prefix) <= 3


def _candidate_ends_on_standalone_line(text: str, position: int) -> bool:
    """Require only horizontal spacing after a candidate on its closing line."""

    return all(char in {" ", "\t"} for char in text[position : _line_end(text, position)])


def _standalone_candidate_layout(
    text: str,
    spans: Sequence[tuple[int, int]],
) -> bool:
    """Validate a terminal sequence of one or more standalone protocol blocks.

    Normal assistant prose may precede the first block, but the first opening
    must start on a clean line.  Once tool protocol begins, blocks may only be
    separated or followed by whitespace.  This prevents prose examples such
    as ``before:<tool_call>...</tool_call>:after`` from gaining execution
    authority while retaining the provider pattern of explanation followed by
    one or more terminal tool-call blocks.
    """

    if not spans:
        return False
    ordered = sorted(spans)
    if not _candidate_starts_on_standalone_line(text, ordered[0][0]):
        return False
    if not _candidate_ends_on_standalone_line(text, ordered[-1][1]):
        return False
    previous_end = ordered[0][1]
    for start, end in ordered[1:]:
        if start < previous_end or text[previous_end:start].strip():
            return False
        previous_end = end
    return not text[previous_end:].strip()


def _skip_whitespace(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _remove_one_framing_eol(value: str) -> str:
    """Remove at most one logical EOL at each XML framing boundary."""

    if value.startswith("\r\n"):
        value = value[2:]
    elif value.startswith(("\r", "\n")):
        value = value[1:]
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith(("\r", "\n")):
        value = value[:-1]
    return value


def _raw_qwen_json_call(value: object) -> _RawTextToolCall | None:
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    arguments = value.get("arguments", {})
    if not isinstance(name, str) or not name.strip():
        return None
    if isinstance(arguments, str):
        try:
            arguments = _strict_json_loads(arguments)
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
            return None
    if not isinstance(arguments, dict) or not _is_strict_json_value(arguments):
        return None
    return _RawTextToolCall(name.strip(), arguments, "json")


def _raw_qwen_xml_call_at(
    text: str,
    index: int,
) -> tuple[_RawTextToolCall, int] | None:
    function_open = _QWEN_FUNCTION_OPEN_RE.match(text, index)
    if function_open is None:
        return None
    tool_name = function_open.group(1).strip()
    if not tool_name:
        return None
    cursor = function_open.end()
    arguments: dict[str, Any] = {}
    while True:
        cursor = _skip_whitespace(text, cursor)
        function_close = _QWEN_FUNCTION_CLOSE_RE.match(text, cursor)
        if function_close is not None:
            return _RawTextToolCall(tool_name, arguments, "xml"), function_close.end()
        parameter_open = _QWEN_PARAMETER_OPEN_RE.match(text, cursor)
        if parameter_open is None:
            return None
        parameter_name = parameter_open.group(1).strip()
        if not parameter_name or parameter_name in arguments:
            return None
        parameter_close = _QWEN_PARAMETER_CLOSE_RE.search(text, parameter_open.end())
        if parameter_close is None:
            return None
        arguments[parameter_name] = _remove_one_framing_eol(
            text[parameter_open.end() : parameter_close.start()]
        )
        cursor = parameter_close.end()


def _qwen_protocol_span_at(text: str, start: int) -> _ProtocolSpan | None:
    wrapper_open = _QWEN_TOOL_CALL_OPEN_RE.match(text, start)
    if wrapper_open is None:
        return None
    cursor = _skip_whitespace(text, wrapper_open.end())
    raw_call: _RawTextToolCall | None = None
    body_end = cursor
    if cursor < len(text) and text[cursor] == "{":
        try:
            value, body_end = json.JSONDecoder(
                parse_constant=_reject_nonstandard_json_constant,
            ).raw_decode(text, cursor)
        except (json.JSONDecodeError, ValueError, RecursionError):
            return None
        raw_call = _raw_qwen_json_call(value)
    else:
        xml_call = _raw_qwen_xml_call_at(text, cursor)
        if xml_call is None:
            return None
        raw_call, body_end = xml_call
    if raw_call is None:
        return None
    wrapper_close = _QWEN_TOOL_CALL_CLOSE_RE.match(
        text,
        _skip_whitespace(text, body_end),
    )
    if wrapper_close is None:
        return None
    return _ProtocolSpan(start, wrapper_close.end(), (raw_call,))


def _minimax_protocol_span_at(text: str, start: int) -> _ProtocolSpan | None:
    wrapper_open = _MINIMAX_TOOL_CALL_OPEN_RE.match(text, start)
    if wrapper_open is None:
        return None
    cursor = wrapper_open.end()
    calls: list[_RawTextToolCall] = []
    while True:
        cursor = _skip_whitespace(text, cursor)
        wrapper_close = _MINIMAX_TOOL_CALL_CLOSE_RE.match(text, cursor)
        if wrapper_close is not None:
            if not calls:
                return None
            return _ProtocolSpan(start, wrapper_close.end(), tuple(calls))
        invoke_open = _MINIMAX_INVOKE_OPEN_RE.match(text, cursor)
        if invoke_open is None:
            return None
        tool_name = invoke_open.group("name").strip()
        if not tool_name:
            return None
        cursor = invoke_open.end()
        arguments: dict[str, Any] = {}
        while True:
            cursor = _skip_whitespace(text, cursor)
            invoke_close = _MINIMAX_INVOKE_CLOSE_RE.match(text, cursor)
            if invoke_close is not None:
                calls.append(_RawTextToolCall(tool_name, arguments, "xml"))
                cursor = invoke_close.end()
                break
            parameter_open = _MINIMAX_PARAMETER_OPEN_RE.match(text, cursor)
            if parameter_open is None:
                return None
            parameter_name = parameter_open.group("name").strip()
            if not parameter_name or parameter_name in arguments:
                return None
            parameter_close = _MINIMAX_PARAMETER_CLOSE_RE.search(
                text,
                parameter_open.end(),
            )
            if parameter_close is None:
                return None
            value = text[parameter_open.end() : parameter_close.start()]
            arguments[parameter_name] = _remove_one_framing_eol(value)
            cursor = parameter_close.end()


def _scan_protocol_spans(
    text: str,
    *,
    opening: re.Pattern[str],
    parse_at: Callable[[str, int], _ProtocolSpan | None],
) -> list[_ProtocolSpan]:
    """Return complete spans without letting earlier malformed prose poison the tail.

    A model may describe or abandon one malformed wrapper before emitting a valid
    standalone terminal call.  Keep the malformed bytes literal and continue at
    the next opening; complete-but-invalid spans are still rejected atomically by
    the schema/identity validation performed by the caller.
    """

    spans: list[_ProtocolSpan] = []
    cursor = 0
    while opening_match := opening.search(text, cursor):
        span = parse_at(text, opening_match.start())
        if span is None:
            cursor = opening_match.end()
            continue
        spans.append(span)
        cursor = span.end
    return spans


_JSON_XML_VALUE_TYPES = frozenset(
    {"integer", "number", "boolean", "array", "object", "null"}
)


def _schema_declared_types(schema: Mapping[str, Any]) -> frozenset[str]:
    """Return explicitly declared JSON types from common schema union shapes."""

    declared: set[str] = set()

    def add_type(value: object) -> None:
        if isinstance(value, str):
            declared.add(value)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            declared.update(item for item in value if isinstance(item, str))

    add_type(schema.get("type"))
    for union_key in ("anyOf", "oneOf"):
        branches = schema.get(union_key)
        if not isinstance(branches, Sequence) or isinstance(branches, (str, bytes)):
            continue
        for branch in branches:
            if isinstance(branch, Mapping):
                add_type(branch.get("type"))
    return frozenset(declared)


def _json_value_matches_type(value: object, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value))
        )
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "null":
        return value is None
    return False


def _decode_xml_arguments_for_schema(
    tool: ToolDefinition,
    arguments: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Decode XML parameter text only when the target schema is unambiguous.

    XML tool dialects do not carry JSON scalar types.  String-capable
    properties therefore keep their exact parsed text.  Non-string values are
    decoded only from complete JSON and only when exactly one declared target
    type matches, preventing coercions such as ``true`` -> ``1`` or an
    integer/number union from silently choosing a branch.
    """

    properties = tool.input_schema.properties or {}
    decoded = dict(arguments)
    errors: list[str] = []
    for name, raw_value in arguments.items():
        if not isinstance(raw_value, str):
            continue
        property_schema = properties.get(name)
        if not isinstance(property_schema, Mapping):
            continue
        declared_types = _schema_declared_types(property_schema)
        if not declared_types or "string" in declared_types:
            continue
        json_types = declared_types & _JSON_XML_VALUE_TYPES
        if not json_types:
            continue
        try:
            value = _strict_json_loads(raw_value)
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
            expected = "|".join(sorted(json_types))
            errors.append(f"{name} is not valid JSON for declared type {expected}")
            continue
        if not _is_strict_json_value(value):
            expected = "|".join(sorted(json_types))
            errors.append(f"{name} is not finite JSON for declared type {expected}")
            continue
        matching_types = [
            expected
            for expected in json_types
            if _json_value_matches_type(value, expected)
        ]
        if len(matching_types) != 1:
            expected = "|".join(sorted(json_types))
            errors.append(f"{name} is ambiguous or invalid for declared type {expected}")
            continue
        decoded[name] = value
    return decoded, errors


def _schema_validation_errors(
    tool: ToolDefinition,
    arguments: dict[str, Any],
) -> list[str]:
    # Lazy import is required: importing opensquilla.tools while provider
    # modules initialize can freeze the built-in registry in a partial state.
    from opensquilla.tools.schema_validation import validate_tool_arguments

    schema = tool.input_schema
    errors = validate_tool_arguments(
        arguments,
        properties=schema.properties or {},
        required=schema.required or [],
        additional_properties=schema.additional_properties,
    )
    if errors:
        return errors
    properties = set((schema.properties or {}).keys())
    if properties and arguments and not (set(arguments) & properties):
        return ["arguments did not include any known tool properties"]
    return []


def _validated_call(
    *,
    tool_name: str,
    raw_arguments: dict[str, Any],
    dialect: TextToolDialect,
    parse_format: str,
    tools_by_name: dict[str, ToolDefinition],
    provider_kind: str,
    model: str,
) -> SyntheticTextToolCall | None:
    # Keep provider import order side-effect free; these modules transitively
    # import the built-in tool registry.
    from opensquilla.tools.argument_normalization import (
        canonicalize_tool_arguments,
        format_alias_conflicts,
    )

    if tool_name not in tools_by_name:
        event_name = (
            "provider.qwen_text_tool_call_rejected_unknown_tool"
            if dialect == TEXT_TOOL_DIALECT_QWEN_TAG
            else "provider.text_tool_call_rejected_unknown_tool"
        )
        log.warning(
            event_name,
            provider=provider_kind,
            model=model,
            tool=tool_name,
            dialect=dialect,
        )
        return None

    normalization = canonicalize_tool_arguments(tool_name, raw_arguments)
    arguments = normalization.arguments
    if normalization.conflicts:
        conflicts = format_alias_conflicts(normalization.conflicts)
        log.warning(
            "provider.tool_arguments_alias_conflict",
            provider=provider_kind,
            model=model,
            tool=tool_name,
            conflicts=conflicts[:5],
        )
        errors = conflicts[:5]
    else:
        decode_errors: list[str] = []
        if parse_format == "xml":
            arguments, decode_errors = _decode_xml_arguments_for_schema(
                tools_by_name[tool_name],
                arguments,
            )
        errors = decode_errors or _schema_validation_errors(
            tools_by_name[tool_name],
            arguments,
        )
    if errors:
        schema_event = (
            "provider.qwen_text_tool_call_rejected_schema"
            if dialect == TEXT_TOOL_DIALECT_QWEN_TAG
            else "provider.text_tool_call_rejected_schema"
        )
        log.warning(
            schema_event,
            provider=provider_kind,
            model=model,
            tool=tool_name,
            dialect=dialect,
            parse_format=parse_format,
            errors=errors,
        )
        return None
    if normalization.aliases_applied:
        log.warning(
            "provider.tool_arguments_aliases_applied",
            provider=provider_kind,
            model=model,
            tool=tool_name,
            aliases=normalization.aliases_applied,
        )
    return SyntheticTextToolCall(
        tool_name=tool_name,
        arguments=arguments,
        dialect=dialect,
        parse_format=parse_format,
    )


def _plain_text_tool_match(
    text: str,
    *,
    code_ranges: list[_MarkdownCodeRange] | None = None,
) -> tuple[int, int, str, dict[str, Any]] | None:
    decoder = json.JSONDecoder(parse_constant=_reject_nonstandard_json_constant)
    for match in reversed(list(_PLAIN_JSON_TOOL_PREFIX_RE.finditer(text))):
        if code_ranges and _position_is_in_code(match.start(), code_ranges):
            continue
        if match.start() and re.match(f"[{_TOOL_NAME_CHARS}]", text[match.start() - 1]):
            continue
        try:
            arguments, end = decoder.raw_decode(text, match.end())
        except (json.JSONDecodeError, ValueError, RecursionError):
            continue
        if (
            text[end:].strip()
            or not isinstance(arguments, dict)
            or not _is_strict_json_value(arguments)
        ):
            continue
        return match.start(), end, match.group(1), arguments
    return None


def _outside_code_openings(
    pattern: re.Pattern[str],
    text: str,
    code_ranges: list[_MarkdownCodeRange],
) -> list[int]:
    return [
        match.start()
        for match in pattern.finditer(text)
        if not _position_is_in_code(match.start(), code_ranges)
    ]


def _all_openings_are_covered(openings: list[int], spans: list[tuple[int, int]]) -> bool:
    return all(any(start <= opening < end for start, end in spans) for opening in openings)


def classify_text_tool_segments(
    full_text: str,
    tools: list[ToolDefinition] | None,
    *,
    dialects: frozenset[TextToolDialect],
    provider_kind: str,
    model: str,
) -> list[TextToolSegment]:
    """Atomically classify complete text, preserving every rejected byte."""

    if not full_text:
        return []
    if not tools or not dialects:
        return [LiteralTextSegment(full_text)]
    tools_by_name = {tool.name: tool for tool in tools}
    matches: list[tuple[int, int, tuple[SyntheticTextToolCall, ...]]] = []

    if TEXT_TOOL_DIALECT_QWEN_TAG in dialects:
        protocol_spans = _scan_protocol_spans(
            full_text,
            opening=_QWEN_TOOL_CALL_OPEN_RE,
            parse_at=_qwen_protocol_span_at,
        )
        for protocol_span in protocol_spans:
            raw_call = protocol_span.calls[0]
            call = _validated_call(
                tool_name=raw_call.tool_name,
                raw_arguments=raw_call.arguments,
                dialect=TEXT_TOOL_DIALECT_QWEN_TAG,
                parse_format=raw_call.parse_format,
                tools_by_name=tools_by_name,
                provider_kind=provider_kind,
                model=model,
            )
            if call is None:
                return [LiteralTextSegment(full_text)]
            span = (protocol_span.start, protocol_span.end)
            matches.append((*span, (call,)))

    if TEXT_TOOL_DIALECT_MINIMAX_XML in dialects:
        protocol_spans = _scan_protocol_spans(
            full_text,
            opening=_MINIMAX_TOOL_CALL_OPEN_RE,
            parse_at=_minimax_protocol_span_at,
        )
        for protocol_span in protocol_spans:
            calls: list[SyntheticTextToolCall] = []
            for raw_call in protocol_span.calls:
                call = _validated_call(
                    tool_name=raw_call.tool_name,
                    raw_arguments=raw_call.arguments,
                    dialect=TEXT_TOOL_DIALECT_MINIMAX_XML,
                    parse_format=raw_call.parse_format,
                    tools_by_name=tools_by_name,
                    provider_kind=provider_kind,
                    model=model,
                )
                if call is None:
                    return [LiteralTextSegment(full_text)]
                calls.append(call)
            span = (protocol_span.start, protocol_span.end)
            matches.append((*span, tuple(calls)))

    if TEXT_TOOL_DIALECT_PLAIN_JSON in dialects:
        plain_match = _plain_text_tool_match(full_text)
        plain_spans: list[tuple[int, int]] = []
        if plain_match is not None:
            start, end, tool_name, raw_arguments = plain_match
            call = _validated_call(
                tool_name=tool_name,
                raw_arguments=raw_arguments,
                dialect=TEXT_TOOL_DIALECT_PLAIN_JSON,
                parse_format="json",
                tools_by_name=tools_by_name,
                provider_kind=provider_kind,
                model=model,
            )
            if call is None:
                return [LiteralTextSegment(full_text)]
            plain_spans.append((start, end))
            matches.append((start, end, (call,)))
        for tool_name in tools_by_name:
            pattern = re.compile(
                rf"(?<![{_TOOL_NAME_CHARS}]){re.escape(tool_name)}"
                rf"[ \t]{{0,{PLAIN_TOOL_INTERSTITIAL_WHITESPACE_MAX}}}\{{"
            )
            openings = _outside_code_openings(pattern, full_text, [])
            if not _all_openings_are_covered(openings, plain_spans):
                return [LiteralTextSegment(full_text)]

    matches.sort(key=lambda item: (item[0], item[1]))
    cursor = 0
    for start, end, _calls in matches:
        if start < cursor:
            return [LiteralTextSegment(full_text)]
        cursor = end

    all_candidate_spans = [(start, end) for start, end, _calls in matches]
    outside_text = _mask_candidate_spans(full_text, all_candidate_spans)
    outside_code_ranges = _markdown_code_ranges(outside_text)
    if not matches:
        return [LiteralTextSegment(full_text)]

    last_index = len(matches) - 1
    last_start, last_end, _last_calls = matches[last_index]
    if (
        _position_is_in_code(last_start, outside_code_ranges)
        or not _candidate_ends_on_standalone_line(full_text, last_end)
        or full_text[last_end:].strip()
    ):
        return [LiteralTextSegment(full_text)]

    first_index = last_index
    while first_index:
        previous_start, previous_end, _previous_calls = matches[first_index - 1]
        current_start = matches[first_index][0]
        if (
            _position_is_in_code(previous_start, outside_code_ranges)
            or full_text[previous_end:current_start].strip()
        ):
            break
        first_index -= 1
    while first_index <= last_index and not _candidate_starts_on_standalone_line(
        full_text,
        matches[first_index][0],
    ):
        first_index += 1
    if first_index > last_index:
        return [LiteralTextSegment(full_text)]

    matches = matches[first_index:]
    candidate_spans = [(start, end) for start, end, _calls in matches]
    if not _standalone_candidate_layout(full_text, candidate_spans):
        return [LiteralTextSegment(full_text)]

    partial = _find_partial_suffix(
        full_text,
        dialects=dialects,
        allowed_tool_names=frozenset(tools_by_name),
        include_structured_scaffold=False,
        code_ranges=outside_code_ranges,
    )
    if partial is not None and not any(
        start <= partial.start < end for start, end, _calls in matches
    ):
        return [LiteralTextSegment(full_text)]

    if not matches:
        return [LiteralTextSegment(full_text)]

    cursor = 0
    segments: list[TextToolSegment] = []
    for start, end, segment_calls in matches:
        if start < cursor:
            return [LiteralTextSegment(full_text)]
        if start > cursor:
            segments.append(LiteralTextSegment(full_text[cursor:start]))
        segments.append(SyntheticToolSegment(segment_calls, full_text[start:end]))
        cursor = end
    if cursor < len(full_text):
        segments.append(LiteralTextSegment(full_text[cursor:]))
    return segments


def warn_for_unauthorized_plain_candidate(
    full_text: str,
    tools: list[ToolDefinition] | None,
    *,
    dialects: frozenset[TextToolDialect],
    provider_kind: str,
    model: str,
) -> None:
    """Make a narrowed legacy plain candidate diagnosable without logging data."""

    if (
        not full_text
        or not tools
        or TEXT_TOOL_DIALECT_PLAIN_JSON in dialects
        or provider_kind not in {"dashscope", "minimax", "openrouter", "tokenrhythm"}
    ):
        return
    match = _plain_text_tool_match(
        full_text,
        code_ranges=_markdown_code_ranges(full_text),
    )
    if match is None:
        return
    start, end, tool_name, _arguments = match
    if not _standalone_candidate_layout(full_text, [(start, end)]):
        return
    if tool_name not in {tool.name for tool in tools}:
        return
    log.warning(
        "provider.text_tool_candidate_not_authorized",
        provider=provider_kind,
        model=model,
        dialect=TEXT_TOOL_DIALECT_PLAIN_JSON,
        reason="dialect_not_enabled_for_model",
        recommendation="use native tools or a trusted provider text-tool profile",
    )


def _complete_scaffold_tail_start(text: str) -> int | None:
    """Return the start of a complete standalone scaffold tail."""

    match = _STRUCTURED_TOOL_SCAFFOLD_RE.search(text)
    if match is None:
        return None
    span = (match.start(), match.end())
    return match.start() if _standalone_candidate_layout(text, [span]) else None


def _static_candidate_prefixes(
    dialects: frozenset[TextToolDialect],
    *,
    include_structured_scaffold: bool,
) -> tuple[str, ...]:
    prefixes: list[str] = []
    if TEXT_TOOL_DIALECT_QWEN_TAG in dialects:
        prefixes.append(_CANONICAL_QWEN_PREFIX)
    if TEXT_TOOL_DIALECT_MINIMAX_XML in dialects:
        prefixes.append(_CANONICAL_MINIMAX_PREFIX)
    if include_structured_scaffold:
        prefixes.append(_CANONICAL_SCAFFOLD_PREFIX)
    return tuple(prefixes)


def _find_partial_suffix(
    text: str,
    *,
    dialects: frozenset[TextToolDialect],
    allowed_tool_names: frozenset[str],
    include_structured_scaffold: bool,
    code_ranges: list[_MarkdownCodeRange],
    previous_char: str = "",
    initial_line_spaces: int = 0,
    initial_line_indented: bool = False,
    initial_line_has_non_space: bool = False,
    prevalidated_prefix_chars: int = 0,
    prevalidated_standalone: bool = True,
) -> _PendingStart | None:
    prefixes = _static_candidate_prefixes(
        dialects,
        include_structured_scaffold=include_structured_scaffold,
    )
    max_static = max((len(prefix) for prefix in prefixes), default=0)
    max_tool = max((len(name) for name in allowed_tool_names), default=0)
    start = max(
        0,
        len(text)
        - max(max_static, max_tool + PLAIN_TOOL_INTERSTITIAL_WHITESPACE_MAX),
    )
    lower_text = text.lower()
    for index in range(start, len(text)):
        if _position_is_in_code(index, code_ranges):
            continue
        standalone = (
            prevalidated_standalone
            if index < prevalidated_prefix_chars
            else _candidate_starts_on_standalone_line(
                text,
                index,
                initial_line_spaces=initial_line_spaces,
                initial_line_indented=initial_line_indented,
                initial_line_has_non_space=initial_line_has_non_space,
            )
        )
        partial_kind = "partial" if standalone else "literal_partial"
        suffix = text[index:]
        lower_suffix = lower_text[index:]
        if any(prefix.startswith(lower_suffix) for prefix in prefixes):
            return _PendingStart(index, partial_kind)
        if TEXT_TOOL_DIALECT_PLAIN_JSON not in dialects:
            continue
        preceding = text[index - 1] if index else previous_char
        if preceding and re.match(f"[{_TOOL_NAME_CHARS}]", preceding):
            continue
        for tool_name in allowed_tool_names:
            if tool_name.startswith(suffix):
                return _PendingStart(index, partial_kind)
            if suffix.startswith(tool_name):
                interstitial = suffix[len(tool_name) :]
                if (
                    len(interstitial) <= PLAIN_TOOL_INTERSTITIAL_WHITESPACE_MAX
                    and all(char in {" ", "\t"} for char in interstitial)
                ):
                    return _PendingStart(index, partial_kind)
    return None


def _find_pending_start(
    text: str,
    *,
    dialects: frozenset[TextToolDialect],
    allowed_tool_names: frozenset[str],
    include_structured_scaffold: bool,
    initial_line_spaces: int,
    initial_line_indented: bool,
    initial_line_has_non_space: bool,
    previous_char: str,
    markdown_context_prefix: str = "",
    raw_html_state: _RawHtmlState | None = None,
    raw_trailing_already_included: str = "",
    prevalidated_prefix_chars: int = 0,
    prevalidated_standalone: bool = True,
) -> _PendingStart | None:
    if not text:
        return None
    code_ranges = _contextual_markdown_code_ranges(
        text,
        context_prefix=markdown_context_prefix,
        initial_line_spaces=initial_line_spaces,
        initial_line_indented=initial_line_indented,
        initial_raw_html_state=raw_html_state,
        raw_trailing_already_included=raw_trailing_already_included,
    )
    candidates: list[_PendingStart] = []

    def protocol_kind(position: int) -> str:
        standalone = (
            prevalidated_standalone
            if position < prevalidated_prefix_chars
            else _candidate_starts_on_standalone_line(
                text,
                position,
                initial_line_spaces=initial_line_spaces,
                initial_line_indented=initial_line_indented,
                initial_line_has_non_space=initial_line_has_non_space,
            )
        )
        return "protocol" if standalone else "literal_protocol"

    patterns: list[re.Pattern[str]] = []
    if TEXT_TOOL_DIALECT_QWEN_TAG in dialects:
        patterns.append(_QWEN_TOOL_CALL_OPEN_RE)
    if TEXT_TOOL_DIALECT_MINIMAX_XML in dialects:
        patterns.append(_MINIMAX_TOOL_CALL_OPEN_RE)
    if include_structured_scaffold:
        patterns.append(_STRUCTURED_TOOL_SCAFFOLD_OPEN_RE)
    for pattern in patterns:
        for match in pattern.finditer(text):
            if not _position_is_in_code(match.start(), code_ranges):
                kind = protocol_kind(match.start())
                # Embedded protocol-looking text is ordinary visible prose
                # and must not disable a later standalone terminal block.
                if kind == "literal_protocol":
                    continue
                candidates.append(_PendingStart(match.start(), kind))
                break

    if TEXT_TOOL_DIALECT_PLAIN_JSON in dialects:
        for tool_name in allowed_tool_names:
            pattern = re.compile(
                rf"(?<![{_TOOL_NAME_CHARS}]){re.escape(tool_name)}"
                rf"[ \t]{{0,{PLAIN_TOOL_INTERSTITIAL_WHITESPACE_MAX}}}\{{"
            )
            for match in pattern.finditer(text):
                if (
                    match.start() == 0
                    and previous_char
                    and re.match(f"[{_TOOL_NAME_CHARS}]", previous_char)
                ):
                    continue
                if not _position_is_in_code(match.start(), code_ranges):
                    kind = protocol_kind(match.start())
                    if kind != "literal_protocol":
                        candidates.append(_PendingStart(match.start(), kind))
                        break

    partial = _find_partial_suffix(
        text,
        dialects=dialects,
        allowed_tool_names=allowed_tool_names,
        include_structured_scaffold=include_structured_scaffold,
        code_ranges=code_ranges,
        previous_char=previous_char,
        initial_line_spaces=initial_line_spaces,
        initial_line_indented=initial_line_indented,
        initial_line_has_non_space=initial_line_has_non_space,
        prevalidated_prefix_chars=prevalidated_prefix_chars,
        prevalidated_standalone=prevalidated_standalone,
    )
    if partial is not None:
        candidates.append(partial)
    if not candidates:
        return None
    return min(candidates, key=lambda item: item.start)


def _literalize_segments(segments: list[TextToolSegment]) -> str:
    return "".join(
        segment.text
        if isinstance(segment, LiteralTextSegment)
        else segment.source_text
        for segment in segments
    )


def _native_filtered_segments(
    segments: list[TextToolSegment],
    native_calls: list[tuple[str, dict[str, Any]]],
) -> list[TextToolSegment]:
    def signature(name: str, arguments: dict[str, Any]) -> tuple[str, str] | None:
        try:
            canonical_arguments = json.dumps(
                arguments,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError, OverflowError, RecursionError):
            return None
        return name, canonical_arguments

    remaining_native_signatures = Counter(
        call_signature
        for name, arguments in native_calls
        if (call_signature := signature(name, arguments)) is not None
    )
    output: list[TextToolSegment] = []
    for segment in segments:
        if isinstance(segment, LiteralTextSegment):
            output.append(segment)
            continue
        signatures = [
            signature(call.tool_name, call.arguments)
            for call in segment.calls
        ]
        if any(call_signature is None for call_signature in signatures):
            output.append(LiteralTextSegment(segment.source_text))
            continue
        required = Counter(call_signature for call_signature in signatures if call_signature)
        if all(
            remaining_native_signatures[call_signature] >= count
            for call_signature, count in required.items()
        ):
            remaining_native_signatures.subtract(required)
            continue
        output.append(LiteralTextSegment(segment.source_text))
    return output


class TextToolStreamNormalizer:
    """Incremental, bounded text/tool state machine for one provider call."""

    def __init__(
        self,
        *,
        tools: list[ToolDefinition] | None,
        dialects: frozenset[TextToolDialect],
        provider_kind: str,
        model: str,
        max_candidate_chars: int = MAX_TEXT_TOOL_CANDIDATE_CHARS,
    ) -> None:
        self._tools = tools
        self._dialects = dialects if tools else frozenset()
        self._provider_kind = provider_kind
        self._model = model
        self._allowed_tool_names = frozenset(tool.name for tool in tools or [])
        self._max_candidate_chars = max(1, max_candidate_chars)
        self._partial = ""
        self._partial_previous_char = ""
        self._partial_standalone = True
        self._locked_parts: list[str] = []
        self._locked_chars = 0
        self._locked_kind = ""
        self._native_tool_seen = False
        self._native_lifecycle_deferred = False
        self._native_candidate_text = ""
        self._owned_scaffold = ""
        self._synthesis_disabled = False
        self._line_leading_spaces = 0
        self._line_indented = False
        self._line_has_non_space = False
        self._markdown_fence = ""
        self._markdown_line_prefix = ""
        self._markdown_line_truncated = False
        self._raw_html_state = _RawHtmlState()
        self._last_raw_char = ""

    def _update_line_context(self, text: str) -> None:
        for char in text:
            if char in {"\r", "\n"}:
                self._line_leading_spaces = 0
                self._line_indented = False
                self._line_has_non_space = False
                continue
            if self._line_has_non_space:
                continue
            if char == "\t":
                self._line_indented = True
                self._line_has_non_space = True
            elif char == " ":
                self._line_leading_spaces += 1
                if self._line_leading_spaces >= 4:
                    self._line_indented = True
            else:
                self._line_has_non_space = True

    def _finish_markdown_line(self) -> None:
        marker = _markdown_fence_marker(self._markdown_line_prefix)
        if self._markdown_fence:
            remainder = self._markdown_line_prefix[
                self._markdown_line_prefix.find(marker) + len(marker) :
            ]
            if (
                marker
                and marker[0] == self._markdown_fence[0]
                and len(marker) >= len(self._markdown_fence)
                and not self._markdown_line_truncated
                and not remainder.strip()
            ):
                self._markdown_fence = ""
        elif marker:
            self._markdown_fence = marker
        self._markdown_line_prefix = ""
        self._markdown_line_truncated = False

    def _update_markdown_context(self, text: str) -> None:
        line_prefix = self._markdown_line_prefix
        syntax_prefix = (
            f"{self._markdown_fence}\n{line_prefix}"
            if self._markdown_fence
            else line_prefix
        )
        syntax_ranges = _project_code_ranges(
            _markdown_syntax_code_ranges(syntax_prefix + text),
            offset=len(syntax_prefix),
            text_length=len(text),
        )
        previous_state = self._raw_html_state
        trailing = previous_state.trailing_token

        def scan_raw(
            value: str,
        ) -> tuple[list[_MarkdownCodeRange], _RawHtmlState]:
            raw_ranges, state = _scan_raw_html_code_ranges(
                trailing + value,
                initial_state=_state_without_trailing(previous_state),
            )
            return (
                _project_code_ranges(
                    raw_ranges,
                    offset=len(trailing),
                    text_length=len(text),
                ),
                state,
            )

        projected_raw_ranges, _provisional_state = scan_raw(
            _mask_candidate_spans(
                text,
                [(item.start, item.end) for item in syntax_ranges],
            )
        )
        syntax_ranges = _project_code_ranges(
            _markdown_syntax_code_ranges(
                syntax_prefix
                + _mask_candidate_spans(
                    text,
                    [(item.start, item.end) for item in projected_raw_ranges],
                )
            ),
            offset=len(syntax_prefix),
            text_length=len(text),
        )
        projected_raw_ranges, self._raw_html_state = scan_raw(
            _mask_candidate_spans(
                text,
                [(item.start, item.end) for item in syntax_ranges],
            )
        )
        markdown_text = _mask_candidate_spans(
            text,
            [(item.start, item.end) for item in projected_raw_ranges],
        )

        for char in markdown_text:
            if char in {"\r", "\n"}:
                self._finish_markdown_line()
                continue
            if len(self._markdown_line_prefix) < 256:
                self._markdown_line_prefix += char
            else:
                self._markdown_line_truncated = True

    def _markdown_context_prefix(self, partial: str) -> str:
        line_prefix = self._markdown_line_prefix
        if partial and line_prefix.endswith(partial):
            line_prefix = line_prefix[: -len(partial)]
        if self._markdown_fence:
            return f"{self._markdown_fence}\n{line_prefix}"
        return line_prefix

    @property
    def native_lifecycle_deferred(self) -> bool:
        """Whether native lifecycle events must wait for duplicate resolution."""

        return self._native_lifecycle_deferred

    @property
    def held_chars(self) -> int:
        """Characters currently withheld from the public text stream."""

        return (
            self._locked_chars
            + len(self._partial)
            + len(self._native_candidate_text)
            + len(self._owned_scaffold)
        )

    @property
    def held_event_count(self) -> int:
        """One logical holdback entry when any candidate text is pending."""

        return int(self.held_chars > 0)

    def _unlock_literal(self) -> str:
        text = "".join(self._locked_parts)
        self._locked_parts = []
        self._locked_chars = 0
        self._locked_kind = ""
        return text

    def _append_locked(self, text: str) -> list[str]:
        self._locked_parts.append(text)
        self._locked_chars += len(text)
        if self._locked_chars > self._max_candidate_chars:
            literal = self._unlock_literal()
            self._synthesis_disabled = True
            log.warning(
                "provider.text_tool_candidate_oversized",
                provider=self._provider_kind,
                model=self._model,
                reason="candidate_char_limit_exceeded",
                max_candidate_chars=self._max_candidate_chars,
            )
            return [literal]
        return []

    def _lock(self, text: str, pending: _PendingStart) -> None:
        self._locked_parts = [text]
        self._locked_chars = len(text)
        self._locked_kind = pending.kind

    def _push_untracked(self, text: str) -> list[str]:
        if not text:
            return []
        if not self._tools or self._synthesis_disabled:
            return [text]
        if self._locked_kind:
            return self._append_locked(text)

        partial_text = self._partial
        partial_chars = len(partial_text)
        partial_standalone = self._partial_standalone
        combined = partial_text + text
        previous_char = (
            self._partial_previous_char if self._partial else self._last_raw_char
        )
        self._partial = ""
        self._partial_previous_char = ""
        self._partial_standalone = True
        active_dialects = frozenset() if self._native_tool_seen else self._dialects
        pending = _find_pending_start(
            combined,
            dialects=active_dialects,
            allowed_tool_names=self._allowed_tool_names,
            include_structured_scaffold=not self._native_tool_seen,
            initial_line_spaces=self._line_leading_spaces,
            initial_line_indented=self._line_indented,
            initial_line_has_non_space=self._line_has_non_space,
            previous_char=previous_char,
            markdown_context_prefix=self._markdown_context_prefix(partial_text),
            raw_html_state=self._raw_html_state,
            raw_trailing_already_included=partial_text,
            prevalidated_prefix_chars=partial_chars,
            prevalidated_standalone=partial_standalone,
        )
        if pending is None:
            return [combined] if combined else []
        visible = combined[: pending.start]
        tail = combined[pending.start :]
        output = [visible] if visible else []
        if pending.kind in {"partial", "literal_partial"}:
            self._partial = tail
            self._partial_previous_char = (
                combined[pending.start - 1] if pending.start else previous_char
            )
            self._partial_standalone = pending.kind == "partial"
        else:
            self._lock(tail, _PendingStart(0, pending.kind))
            if self._locked_chars > self._max_candidate_chars:
                output.extend(self._append_locked(""))
        return output

    def push(self, text: str) -> list[str]:
        initial_spaces = self._line_leading_spaces
        initial_indented = self._line_indented
        output = self._push_untracked(text)
        # _push_untracked needs the prior line state.  Restore that state for
        # the single raw update even when it recursively processed a suffix.
        self._line_leading_spaces = initial_spaces
        self._line_indented = initial_indented
        self._update_line_context(text)
        self._update_markdown_context(text)
        if text:
            self._last_raw_char = text[-1]
        return output

    def observe_native_tool_start(self, _tool_name: str) -> list[TextToolSegment]:
        self._native_tool_seen = True
        pending = self._unlock_literal() if self._locked_kind else self._partial
        self._partial = ""
        self._partial_previous_char = ""
        self._partial_standalone = True
        scaffold_start = _complete_scaffold_tail_start(pending)
        if scaffold_start is not None:
            code_ranges = _markdown_code_ranges(pending)
            if _position_is_in_code(scaffold_start, code_ranges):
                scaffold_start = None
        if scaffold_start is not None:
            self._owned_scaffold = pending[scaffold_start:]
            pending = pending[:scaffold_start]
            self._native_lifecycle_deferred = True
        if not pending:
            return []
        segments = classify_text_tool_segments(
            pending,
            self._tools,
            dialects=self._dialects,
            provider_kind=self._provider_kind,
            model=self._model,
        )
        calls = [
            call
            for segment in segments
            if isinstance(segment, SyntheticToolSegment)
            for call in segment.calls
        ]
        if calls:
            self._native_candidate_text = pending
            self._native_lifecycle_deferred = True
            return []
        return [LiteralTextSegment(_literalize_segments(segments))]

    def abandon_native_lifecycle_defer(self) -> list[TextToolSegment]:
        """Replay ambiguous text before releasing a bounded native queue."""

        pending = self._unlock_literal() if self._locked_kind else self._partial
        self._partial = ""
        self._partial_previous_char = ""
        self._partial_standalone = True
        pending = self._native_candidate_text + self._owned_scaffold + pending
        self._native_candidate_text = ""
        self._owned_scaffold = ""
        self._native_lifecycle_deferred = False
        self._synthesis_disabled = True
        return [LiteralTextSegment(pending)] if pending else []

    def finish(
        self,
        *,
        successful_text_tool_terminal: bool,
        native_calls: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> list[TextToolSegment]:
        pending = self._unlock_literal() if self._locked_kind else self._partial
        self._partial = ""
        self._partial_previous_char = ""
        self._partial_standalone = True
        native_candidate = self._native_candidate_text
        self._native_candidate_text = ""
        owned_scaffold = self._owned_scaffold
        self._owned_scaffold = ""
        if self._native_tool_seen and not successful_text_tool_terminal:
            pending = native_candidate + owned_scaffold + pending
        if not pending:
            if not native_candidate:
                return []
        if self._native_tool_seen:
            candidate_text = native_candidate if successful_text_tool_terminal else pending
            if not candidate_text:
                return [LiteralTextSegment(pending)] if pending else []
            segments = classify_text_tool_segments(
                candidate_text,
                self._tools,
                dialects=self._dialects,
                provider_kind=self._provider_kind,
                model=self._model,
            )
            filtered = _native_filtered_segments(segments, native_calls or [])
            if successful_text_tool_terminal and pending:
                filtered.append(LiteralTextSegment(pending))
            return filtered
        if not successful_text_tool_terminal or self._synthesis_disabled:
            return [LiteralTextSegment(pending)]
        return classify_text_tool_segments(
            pending,
            self._tools,
            dialects=self._dialects,
            provider_kind=self._provider_kind,
            model=self._model,
        )
